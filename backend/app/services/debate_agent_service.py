"""Multi-Agent 辩论式故障诊断。

3 个角色 agent 从不同视角独立诊断 → Judge agent 汇总 → 有冲突时辩论轮 → 终裁。

角色分工：
- Regulation Agent：查规程/标准/手册，严格按条文推断
- Graph Agent：查知识图谱因果链，追踪设备→故障→处置路径
- Case Agent：查历史相似故障案例，基于经验推断

辩论流程（≤2 轮）：
1) 并行唤醒 3 agent → 各自输出带证据链的诊断
2) Judge 对比三方 → 如有重大分歧（原因不同/可能性矛盾）→ 辩论轮
3) 辩论轮：各 agent 看到对方证据后修正 → Judge 终裁
"""
import asyncio
import json
import time
from dataclasses import dataclass, field

from app.core.obs import degraded
from app.providers.factory import get_llm_provider
from app.services import domain_service, kg_service, retrieval_service

_TOPK = 5
_MAX_DEBATE_ROUNDS = 2


# ---------- Agent prompt 模板 ----------

_REGULATION_SYSTEM = """你是电网运维规程专家。严格基于检索到的规程/标准/手册条文进行诊断推理。
你对每个可能原因都必须在规程条文中找到明确依据，无依据则不列入。
输出严格 JSON (仅 JSON 对象，不要其他文字)：
{"causes":[{"name":"可能原因","likelihood":"高/中/低","evidence":"规程条文依据(直接引用检索原文)","handling":"标准的处置措施(引用规程原文)"}],"summary":"基于规程的总体判断","risks":["风险点(基于规程列明的危险点)"]}
原因按可能性从高到低排序。"""

_GRAPH_SYSTEM = """你是电网知识图谱因果推理专家。基于知识图谱中的设备-故障-处置因果链进行诊断推理。
你擅长从多跳关联中发现故障传播路径（如 A设备故障→B参数异常→C保护动作）。
输出严格 JSON (仅 JSON 对象，不要其他文字)：
{"causes":[{"name":"可能原因","likelihood":"高/中/低","evidence":"图谱因果链(直接引用图谱中的实体关系和路径)","handling":"处置措施(基于图谱中的处置节点)"}],"summary":"基于图谱的因果推理判断","risks":["风险点(基于故障传播路径列明的连锁风险)"]}
原因按可能性从高到低排序。注意：图谱中无直接关联时，如实说明"图谱中无直接因果链"。"""

_CASE_SYSTEM = """你是电网运维历史案例专家。基于检索到的历史相似故障案例及其处置经验进行诊断推理。
你对每个可能原因都要引用历史上类似案例的处理方式和效果。
输出严格 JSON (仅 JSON 对象，不要其他文字)：
{"causes":[{"name":"可能原因","likelihood":"高/中/低","evidence":"历史案例依据(直接引用案例内容)","handling":"历史处置措施(引用案例中的实际处置)"}],"summary":"基于历史案例的经验判断","risks":["风险点(基于历史案例中出现的风险)"]}
原因按可能性从高到低排序。无相似案例时如实说明。"""

_JUDGE_SYSTEM = """你是电网运维首席诊断专家。你收到三位领域专家的独立诊断意见（规程专家、图谱专家、案例专家）。
请综合三方证据做最终诊断：
1) 三方一致的结论直接纳入
2) 有分歧的方向→判断哪方的证据更充分、更可靠
3) 需要补充验证的问题→标为待确认

输出严格 JSON (仅 JSON 对象，不要其他文字)：
{"causes":[{"name":"最终可能原因","likelihood":"高/中/低","evidence":"综合证据(注明来源:规程/图谱/案例)","handling":"处置措施","sourceConsensus":"三方一致/规程主导/图谱主导/案例主导/待确认"}],"summary":"综合诊断结论","risks":["最终风险点"],"disagreements":[{"issue":"分歧点","regulationView":"规程方意见","graphView":"图谱方意见","caseView":"案例方意见","resolution":"如何裁决"}]}
原因按可能性从高到低排序。"""


# ---------- 数据类 ----------

@dataclass
class AgentOpinion:
    agent_name: str
    symptom: str
    raw_causes: list[dict]  # 原始 LLM 输出
    summary: str
    risks: list[str]
    raw_response: str  # LLM 原始响应（调试用）
    latency_ms: int = 0


@dataclass
class DebateResult:
    final_diagnosis: dict  # Judge 终裁
    opinions: list[AgentOpinion] = field(default_factory=list)
    rounds: int = 0
    total_latency_ms: int = 0
    degraded: bool = False
    degrade_reason: str = ""


# ---------- 工具函数 ----------

def _extract_json(text: str) -> dict | None:
    """从 LLM 回复中提取第一个 JSON 对象。"""
    import re
    m = re.search(r"(\{.*\})", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


async def _call_agent(
    db, system_prompt: str, symptom: str, model_type: str | None,
    additional_context: str = "",
) -> tuple[dict, str]:
    """调用一个 agent，返回 (解析后的 dict, 原始响应)。"""
    provider = get_llm_provider(model_type)
    context = f"故障症状：{symptom}"
    if additional_context:
        context += f"\n\n【参考其他专家意见】\n{additional_context}"
    content = await provider.chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": context},
        ],
        temperature=0.3,
        max_tokens=1500,
    )
    parsed = _extract_json(content) or {}
    return parsed, content


async def _regulation_agent(db, symptom: str, model_type: str | None, extra: str = "") -> tuple[dict, str, float]:
    """规程 agent：检索规程后诊断。"""
    t0 = time.perf_counter()
    # 先检索规程
    ctx = await retrieval_service.mixed_search(db, symptom, _TOPK, doc_type="运维手册", model_type=model_type)
    refs = "\n\n".join(f"[{i+1}] {(c.get('docName') or '')}: {(c.get('chunk') or '')[:300]}"
                       for i, c in enumerate(ctx[:_TOPK]))
    prompt = f"故障症状：{symptom}\n\n【检索到的规程资料】\n{refs or '无相关规程'}"
    if extra:
        prompt += f"\n\n【其他专家意见参考】\n{extra}"
    parsed, raw = await _call_agent(db, _REGULATION_SYSTEM + "\n\n请基于以下资料诊断：\n" + prompt,
                                    symptom, model_type)
    return parsed, raw, (time.perf_counter() - t0) * 1000


async def _graph_agent(db, symptom: str, model_type: str | None, extra: str = "") -> tuple[dict, str, float]:
    """图谱 agent：查图谱后诊断。"""
    t0 = time.perf_counter()
    rows = await kg_service.graph_context(symptom, 12)
    graph_str = "\n".join(rows) if rows else "图谱中无直接关联"
    prompt = f"故障症状：{symptom}\n\n【知识图谱因果链】\n{graph_str}"
    if extra:
        prompt += f"\n\n【其他专家意见参考】\n{extra}"
    parsed, raw = await _call_agent(db, _GRAPH_SYSTEM + "\n\n请基于以下图谱信息诊断：\n" + prompt,
                                    symptom, model_type)
    return parsed, raw, (time.perf_counter() - t0) * 1000


async def _case_agent(db, symptom: str, model_type: str | None, extra: str = "") -> tuple[dict, str, float]:
    """案例 agent：查历史案例后诊断。"""
    t0 = time.perf_counter()
    res = await domain_service.similar_case(db, symptom, model_type, _TOPK)
    cases = res.get("cases", [])
    case_str = "\n\n".join(f"[{i+1}] {(c.get('docName') or '')}: {(c.get('text') or '')[:300]}"
                           for i, c in enumerate(cases[:_TOPK])) if cases else "无相似历史案例"
    prompt = f"故障症状：{symptom}\n\n【历史相似案例】\n{case_str}"
    if extra:
        prompt += f"\n\n【其他专家意见参考】\n{extra}"
    parsed, raw = await _call_agent(db, _CASE_SYSTEM + "\n\n请基于以下历史案例诊断：\n" + prompt,
                                    symptom, model_type)
    return parsed, raw, (time.perf_counter() - t0) * 1000


async def _judge_agent(
    db, symptom: str, opinions: list[tuple[str, dict, str]], model_type: str | None,
) -> tuple[dict, str, float]:
    """Judge agent：综合三方意见做终裁。"""
    t0 = time.perf_counter()
    parts = [f"故障症状：{symptom}\n"]
    for name, parsed, raw in opinions:
        summary = parsed.get("summary", "无") if isinstance(parsed, dict) else "解析失败"
        causes = parsed.get("causes", []) if isinstance(parsed, dict) else []
        causes_str = "\n".join(
            f"  - {c.get('name','')} (可能性:{c.get('likelihood','')}) 证据:{c.get('evidence','')[:200]}"
            for c in causes
        ) if causes else "  无"
        parts.append(f"\n==== {name} 诊断 ====\n总体判断：{summary}\n可能原因：\n{causes_str}")
    summary_prompt = "\n".join(parts)
    parsed, raw = await _call_agent(db, _JUDGE_SYSTEM + "\n\n请综合以下三份专家意见：\n" + summary_prompt,
                                    symptom, model_type)
    return parsed, raw, (time.perf_counter() - t0) * 1000


def _consensus_needs_debate(opinions: list[tuple[str, dict, str]]) -> bool:
    """判断是否需要辩论轮：三方对同一故障的原因分歧明显。"""
    all_causes = []
    for name, parsed, _raw in opinions:
        causes = parsed.get("causes", []) if isinstance(parsed, dict) else []
        all_causes.append({name: [c.get("name", "") for c in causes]})
    # 简单启发：如果三方都给出了完全不同且数量>0的原因，需要辩论
    cause_sets = []
    for name, parsed, _raw in opinions:
        causes = parsed.get("causes", []) if isinstance(parsed, dict) else []
        cause_sets.append({c.get("name", "") for c in causes})
    if len(cause_sets) >= 2:
        # 计算重叠度
        overlap = cause_sets[0] & cause_sets[1] if len(cause_sets) > 1 else set()
        if len(cause_sets) > 2:
            overlap = overlap & cause_sets[2]
        # 如果三方共同原因少于1个且每方都有原因 → 需要辩论
        if len(overlap) < 1 and all(len(s) > 0 for s in cause_sets):
            return True
    return False


def _format_opinion_for_context(name: str, parsed: dict, raw: str) -> str:
    """将一个 agent 的意见格式化为其他 agent 可读的上下文。"""
    causes = parsed.get("causes", []) if isinstance(parsed, dict) else []
    summary = parsed.get("summary", "") if isinstance(parsed, dict) else ""
    lines = [f"【{name} 的诊断意见】", f"总体判断：{summary}"]
    for c in causes:
        lines.append(f"- 原因：{c.get('name','')}（{c.get('likelihood','')}）依据：{c.get('evidence','')[:200]}")
    return "\n".join(lines)


async def debate_diagnose(
    db, symptom: str, model_type: str | None = None,
) -> dict:
    """Multi-Agent 辩论式诊断入口。

    返回格式兼容现有 diagnose_agent 的返回，新增 debate 字段。
    """
    t0 = time.perf_counter()

    # Round 1: 三方并行独立诊断
    try:
        reg_task = _regulation_agent(db, symptom, model_type)
        graph_task = _graph_agent(db, symptom, model_type)
        case_task = _case_agent(db, symptom, model_type)
        results = await asyncio.gather(reg_task, graph_task, case_task, return_exceptions=True)
    except Exception as e:
        degraded("debate_round1_error", e)
        return await _fallback(db, symptom, model_type, f"round1_error:{type(e).__name__}", t0)

    opinions: list[tuple[str, dict, str]] = []
    agent_opinions: list[AgentOpinion] = []
    errors = []
    for agent_name, raw_result in zip(
        ["规程专家", "图谱专家", "案例专家"], results
    ):
        if isinstance(raw_result, Exception):
            errors.append(f"{agent_name}: {raw_result}")
            opinions.append((agent_name, {"causes": [], "summary": f"{agent_name}诊断失败", "risks": []}, ""))
            agent_opinions.append(AgentOpinion(
                agent_name=agent_name, symptom=symptom,
                raw_causes=[], summary=f"{agent_name}诊断失败", risks=[],
                raw_response=f"Error: {raw_result}", latency_ms=0,
            ))
            continue
        parsed, raw, lat = raw_result
        causes = parsed.get("causes", []) if isinstance(parsed, dict) else []
        summary = parsed.get("summary", "") if isinstance(parsed, dict) else ""
        risks = parsed.get("risks", []) if isinstance(parsed, dict) else []
        opinions.append((agent_name, parsed, raw))
        agent_opinions.append(AgentOpinion(
            agent_name=agent_name, symptom=symptom,
            raw_causes=causes, summary=summary, risks=risks,
            raw_response=raw, latency_ms=int(lat),
        ))

    # 判断是否需要辩论
    debate_rounds = 0
    needs_debate = _consensus_needs_debate(opinions) and len(errors) < 2
    if needs_debate:
        debate_rounds = 1  # 现在只做1轮辩论（已在入门级做）
        extra_contexts = {name: _format_opinion_for_context(name, parsed, raw)
                          for name, parsed, raw in opinions}
        try:
            reg_task2 = _regulation_agent(db, symptom, model_type,
                                          extra=extra_contexts.get("图谱专家", "") + "\n" + extra_contexts.get("案例专家", ""))
            graph_task2 = _graph_agent(db, symptom, model_type,
                                       extra=extra_contexts.get("规程专家", "") + "\n" + extra_contexts.get("案例专家", ""))
            case_task2 = _case_agent(db, symptom, model_type,
                                     extra=extra_contexts.get("规程专家", "") + "\n" + extra_contexts.get("图谱专家", ""))
            round2 = await asyncio.gather(reg_task2, graph_task2, case_task2, return_exceptions=True)
            opinions2 = []
            for agent_name, raw_result2 in zip(["规程专家", "图谱专家", "案例专家"], round2):
                if isinstance(raw_result2, Exception):
                    opinions2.append((agent_name, opinions[["规程专家", "图谱专家", "案例专家"].index(agent_name)][1], ""))
                    continue
                parsed2, raw2, _ = raw_result2
                opinions2.append((agent_name, parsed2, raw2))
            opinions = opinions2
        except Exception as e:
            degraded("debate_round2_error", e)

    # Judge 终裁
    try:
        judge_parsed, judge_raw, judge_lat = await _judge_agent(db, symptom, opinions, model_type)
    except Exception as e:
        degraded("debate_judge_error", e)
        judge_parsed = {"causes": [], "summary": "终裁失败，使用规程专家意见", "risks": [], "disagreements": []}
        if opinions:
            judge_parsed = opinions[0][1]  # 回退到规程专家意见

    total = int((time.perf_counter() - t0) * 1000)

    final_causes = judge_parsed.get("causes", []) if isinstance(judge_parsed, dict) else []
    final_summary = judge_parsed.get("summary", "") if isinstance(judge_parsed, dict) else ""
    final_risks = judge_parsed.get("risks", []) if isinstance(judge_parsed, dict) else []
    disagreements = judge_parsed.get("disagreements", []) if isinstance(judge_parsed, dict) else []

    # 埋点
    try:
        from app.core import metrics
        metrics.DOMAIN_CALLS.labels("debate_diagnose").inc()
    except Exception:
        pass

    return {
        "symptom": symptom,
        "diagnosis": {
            "causes": final_causes,
            "summary": final_summary,
            "risks": final_risks,
        },
        "debate": {
            "rounds": debate_rounds + 1,
            "neededDebate": needs_debate,
            "disagreements": disagreements,
            "opinions": [
                {
                    "agent": o.agent_name,
                    "summary": o.summary,
                    "causes": o.raw_causes,
                    "risks": o.risks,
                    "latencyMs": o.latency_ms,
                }
                for o in agent_opinions
            ],
        },
        "iterations": len(agent_opinions),
        "degraded": len(errors) > 0,
        "degradeReason": "; ".join(errors) if errors else None,
        "latencyMs": total,
    }


async def _fallback(db, symptom, model_type, reason, t0):
    """降级到普通诊断。"""
    degraded("debate_diagnose_fallback", Exception(reason))
    try:
        data = await domain_service.diagnose(db, symptom, model_type)
        diag = data.get("diagnosis", {"summary": "", "causes": []})
    except Exception as e:
        degraded("debate_fallback_error", e)
        diag = {"summary": "诊断生成失败", "causes": []}
    return {
        "symptom": symptom,
        "diagnosis": diag,
        "debate": None,
        "iterations": 1,
        "degraded": True,
        "degradeReason": reason,
        "latencyMs": int((time.perf_counter() - t0) * 1000),
    }