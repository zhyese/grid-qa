"""领域增强服务：故障诊断推理(D1) / 相似案例检索(D2) / 两票辅助生成(D3)。

电网运维核心价值功能，复用检索/图谱基建，不重复造轮子：
- diagnose：症状 → 多查询分解 → 并行检索 + 图谱因果 → 可能原因排序 + 处置 + 风险
- similar_case：故障案例库(docType=故障案例)相似检索，"历史上类似故障怎么处理的"
- generate_ticket：操作任务 → 检索规程 → 结构化操作票(步骤/安全措施/风险点)
"""
import json
import re

from app.config import settings
from app.core.obs import degraded
from app.core.safety import detect_injection
from app.providers.factory import get_llm_provider
from app.services import kg_service, retrieval_service

_DIAG_DIM_PROMPT = """你是电网运维诊断专家。根据故障症状，列出 3-5 个需要排查的可能原因方向（每个方向是一句可独立检索的短语，聚焦设备/部件/故障类型）。
只输出 JSON 字符串数组，如 ["方向1","方向2"]，不要解释：
症状：{symptom}"""

_DIAG_PROMPT = """你是电网运维故障诊断专家。基于检索资料和知识图谱因果链，对故障症状给出诊断结论。
输出严格 JSON：
{{"causes":[{{"name":"可能原因","likelihood":"高/中/低","evidence":"资料依据","handling":"处置措施"}}],"summary":"总体判断","risks":["风险点1"]}
要求：原因按可能性从高到低排序；只基于资料，资料不足如实说明；涉及停电/接地/倒闸等高风险处置须在 risks 标注。

【症状】{symptom}
【检索资料】
{refs}
【知识图谱(因果链)】
{graph}"""

_TICKET_PROMPT = """你是电网运维操作票专家。根据操作任务和规程资料，生成结构化操作票。
输出严格 JSON：
{{"device":"涉及设备","steps":["操作步骤1（严格按规程顺序）","操作步骤2"],"safety":["安全措施1"],"risks":["风险点1"],"notes":"备注"}}
要求：步骤严格按规程顺序，不得遗漏关键步骤；涉及停电/接地/倒闸必须列安全措施；只基于规程资料，无依据如实说明。

【操作任务】{task}
【规程资料】
{refs}"""


def _extract_json(ans: str) -> dict | list | None:
    m = re.search(r"(\{.*\}|\[.*\])", ans or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _guard(query: str) -> None:
    """入站 prompt injection 告警（计数，不阻断）。"""
    if not getattr(settings, "SAFETY_FILTER_ENABLE", False):
        return
    flagged, hit = detect_injection(query)
    if flagged:
        try:
            from app.core import metrics
            metrics.SAFETY_BLOCK.labels("injection").inc()
            metrics.DOMAIN_CALLS.labels("safety_block").inc()
        except Exception:
            pass
        degraded("prompt_injection", ValueError(f"命中注入模式: {hit}"))


async def diagnose(db, symptom: str, model_type: str | None = None, topk: int = 5) -> dict:
    """故障诊断推理：多查询分解 → 并行检索 + 图谱因果 → 可能原因排序。"""
    _guard(symptom)
    provider = get_llm_provider(model_type)
    # 1) 分解排查方向（LLM）
    dims: list[str] = []
    try:
        ans = await provider.chat(
            [{"role": "user", "content": _DIAG_DIM_PROMPT.format(symptom=symptom)}],
            temperature=0.2, max_tokens=300,
        )
        parsed = _extract_json(ans)
        if isinstance(parsed, list):
            dims = [str(x).strip() for x in parsed if str(x).strip()][:5]
    except Exception as e:
        degraded("diag_decompose", e)
    if not dims:
        dims = [symptom]
    # 2) 每个方向检索，合并去重
    contexts: list[dict] = []
    for d in dims:
        try:
            contexts.extend(await retrieval_service.mixed_search(db, d, topk, model_type=model_type))
        except Exception as e:
            degraded("diag_retrieve", e)
    seen, uniq = set(), []
    for c in contexts:
        k = (c.get("chunk", ""))[:80]
        if k and k not in seen:
            seen.add(k)
            uniq.append(c)
    contexts = uniq[:8]
    # 3) 图谱因果链
    graph: list[str] = []
    if settings.KG_RAG_ENABLE:
        try:
            graph = await kg_service.graph_context(symptom)
        except Exception as e:
            degraded("diag_graph", e)
    # 4) LLM 综合诊断
    refs = "\n\n".join(f"[{i + 1}] {(c.get('chunk') or '')[:300]}" for i, c in enumerate(contexts))
    graph_str = "\n".join(f"- {g}" for g in graph) or "无"
    try:
        ans = await provider.chat(
            [{"role": "user", "content": _DIAG_PROMPT.format(
                symptom=symptom, refs=refs or "无相关资料", graph=graph_str)}],
            temperature=0.2, max_tokens=1500,
        )
        diagnosis = _extract_json(ans) or {"summary": (ans or "")[:500], "causes": []}
    except Exception as e:
        degraded("diag_synthesize", e)
        diagnosis = {"summary": "诊断生成失败，请参考检索资料", "causes": []}
    try:
        from app.core import metrics
        metrics.DOMAIN_CALLS.labels("diagnose").inc()
    except Exception:
        pass
    return {
        "symptom": symptom, "dimensions": dims,
        "evidenceCount": len(contexts), "graphCount": len(graph),
        "diagnosis": diagnosis,
        "sources": [{"docName": c.get("docName", ""), "text": (c.get("chunk") or "")[:200]} for c in contexts[:5]],
    }


async def similar_case(db, symptom: str, model_type: str | None = None, topk: int = 5) -> dict:
    """相似历史故障案例检索（D2）：限定故障案例库 docType=故障案例。"""
    _guard(symptom)
    try:
        result = await retrieval_service.mixed_search(
            db, symptom, topk, doc_type="故障案例", model_type=model_type)
    except Exception as e:
        degraded("similar_case", e)
        result = []
    try:
        from app.core import metrics
        metrics.DOMAIN_CALLS.labels("similar_case").inc()
    except Exception:
        pass
    return {
        "symptom": symptom, "caseCount": len(result),
        "cases": [{"docName": c.get("docName", ""), "text": (c.get("chunk") or "")[:300],
                   "score": c.get("score", 0)} for c in result],
    }


async def generate_ticket(db, task: str, model_type: str | None = None, topk: int = 5) -> dict:
    """两票辅助生成（D3）：操作任务 → 检索规程 → 结构化操作票。"""
    _guard(task)
    provider = get_llm_provider(model_type)
    contexts: list[dict] = []
    try:
        contexts = await retrieval_service.mixed_search(db, task, topk, model_type=model_type)
    except Exception as e:
        degraded("ticket_retrieve", e)
    refs = "\n\n".join(f"[{i + 1}] {(c.get('chunk') or '')[:300]}" for i, c in enumerate(contexts))
    try:
        ans = await provider.chat(
            [{"role": "user", "content": _TICKET_PROMPT.format(task=task, refs=refs or "无相关规程")}],
            temperature=0.1, max_tokens=1200,
        )
        ticket = _extract_json(ans) or {"notes": (ans or "")[:500], "steps": []}
    except Exception as e:
        degraded("ticket_gen", e)
        ticket = {"notes": "操作票生成失败，请参考规程资料", "steps": []}
    try:
        from app.core import metrics
        metrics.DOMAIN_CALLS.labels("ticket").inc()
    except Exception:
        pass
    return {
        "task": task, "ticket": ticket,
        "sources": [{"docName": c.get("docName", ""), "text": (c.get("chunk") or "")[:200]} for c in contexts[:5]],
    }
