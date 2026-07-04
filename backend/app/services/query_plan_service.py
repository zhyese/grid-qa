"""复杂问题分解（Query Planning）：多步推理问题→子查询 DAG→并行检索→合并→生成。

典型用例：
- "变压器油温高和断路器跳闸有什么关系" → ["变压器油温高的原因","断路器跳闸原因","两者关联"]
- 对比类："A 方案和 B 方案哪个更适合 220kV 变电站"
- 条件类："如果主变负载超过 80% 且油温持续上升怎么办"
"""
import asyncio
import json
import re
import time

from app.core.obs import degraded
from app.providers.factory import get_llm_provider
from app.services import retrieval_service, term_service

_DECOMPOSE_PROMPT = """你是电网运维复杂问题分解专家。将用户问题拆解为 2-4 个独立可检索的子问题。
要求：
1) 每个子问题聚焦一个独立维度
2) 子问题应是完整的检索查询，不需要上下文
3) 如果问题包含"对比"、"关系"、"关联"等词，增加一个"综合对比"子问题
4) 如果问题包含条件（"如果…"），拆分条件判断和结论

输出严格 JSON 数组，每个元素含 {"query":"子问题","type":"fact/compare/causal/conditional","dependence":[]}。
dependence 表示该子问题依赖的其他子问题索引（0-based），无依赖留空数组。
只输出 JSON，不要解释。

问题：{question}"""

_SYNTHESIZE_PROMPT = """你是电网运维综合问答专家。基于以下子问题的检索结果，综合回答用户问题。
每个子问题都有对应的检索资料。请综合所有资料给出完整答案，标注各信息出处。

【问题】{question}

【子问题检索结果】
{sub_results}

请给出综合答案（500字以内）。"""


async def decompose_question(question: str, model_type: str | None = None) -> list[dict]:
    """将复杂问题分解为子问题 DAG。"""
    if not question:
        return [{"query": question, "type": "fact", "dependence": []}]
    provider = get_llm_provider(model_type)
    try:
        content = await provider.chat(
            [{"role": "user", "content": _DECOMPOSE_PROMPT.format(question=question)}],
            temperature=0.2, max_tokens=600,
        )
        m = re.search(r"(\[.*\])", content or "", re.S)
        if m:
            subs = json.loads(m.group(0))
            if isinstance(subs, list) and len(subs) >= 1:
                return subs[:4]
    except Exception as e:
        degraded("query_decompose", e)
    return [{"query": question, "type": "fact", "dependence": []}]


async def retrieve_sub_queries(
    db, sub_queries: list[dict], topk: int = 3,
    model_type: str | None = None,
) -> list[dict]:
    """并行检索所有子问题。每个子问题返回 topk 条上下文。"""
    results = []
    tasks = []
    for sq in sub_queries:
        query_text = sq.get("query", "")
        tasks.append(retrieval_service.mixed_search(db, query_text, topk, model_type=model_type))
    contexts = await asyncio.gather(*tasks, return_exceptions=True)
    for sq, ctx in zip(sub_queries, contexts):
        if isinstance(ctx, Exception):
            degraded("sub_query_retrieve", ctx)
            results.append({**sq, "contexts": []})
        else:
            results.append({**sq, "contexts": ctx})
    return results


async def synthesize_answer(
    question: str, sub_results: list[dict],
    model_type: str | None = None,
) -> dict:
    """综合子查询结果生成最终答案。"""
    provider = get_llm_provider(model_type)
    parts = []
    for i, sr in enumerate(sub_results):
        q = sr.get("query", "")
        ctx = sr.get("contexts", [])
        ctx_str = "\n".join(f"  [{j+1}] {(c.get('docName','')+'):' if c.get('docName') else ''}{(c.get('chunk','') or '')[:200]}"
                            for j, c in enumerate(ctx[:3])) if ctx else "  无检索结果"
        q_type = sr.get("type", "fact")
        parts.append(f"子问题{i+1}({q_type}): {q}\n资料:\n{ctx_str}")

    try:
        answer = await provider.chat(
            [{"role": "user", "content": _SYNTHESIZE_PROMPT.format(
                question=question, sub_results="\n\n".join(parts))}],
            temperature=0.3, max_tokens=1000,
        )
    except Exception as e:
        degraded("query_synthesize", e)
        answer = "综合生成失败"

    return {
        "question": question,
        "subQueries": [{"query": sr.get("query", ""), "type": sr.get("type", "fact"),
                        "contextCount": len(sr.get("contexts", []))} for sr in sub_results],
        "answer": answer or "生成失败",
    }


async def plan_and_answer(
    db, question: str, model_type: str | None = None, topk: int = 3,
) -> dict:
    """一站式复杂问题分解+检索+综合。"""
    t0 = time.time()
    subs = await decompose_question(question, model_type)
    sub_results = await retrieve_sub_queries(db, subs, topk, model_type)
    result = await synthesize_answer(question, sub_results, model_type)
    result["latencyMs"] = int((time.time() - t0) * 1000)
    result["subQueryCount"] = len(subs)
    try:
        from app.core import metrics
        metrics.DOMAIN_CALLS.labels("query_plan").inc()
    except Exception:
        pass
    return result