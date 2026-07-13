"""LLM-as-judge：判定答案是否被资料支撑，算真实幻觉率（替代启发式 estimate）。

用于离线评测（scripts/eval_qa.py），不每次问答调用（避免额外 LLM 延迟）。
"""
import json
import re
from typing import Optional


async def judge_hallucination(
    answer: str, sources: list[str], model_type: Optional[str] = None
) -> dict:
    """基于声明拆解的忠实度核查（RAGAS faithfulness 思路，核心策略重写）。

    旧策略一次问"给个 supported_ratio"→ 模型对总结式答案易误判 + 解析失败假报 1.0。
    新策略：① 拆原子声明 → ② 逐条核验是否被资料支撑（含忠实转述/归纳，不要求原文复述）
            → ③ hallucination = 1 - supported/total（推导，不信模型自报）。
    解析失败返回 hallucination=None（前端不显示），永不假报 100%。
    """
    from app.providers.factory import get_llm_provider

    srcs = [s for s in (sources or []) if s and str(s).strip()]
    if not answer or not srcs:
        return {"supported_ratio": None, "hallucination": None, "reason": "答案或资料为空，跳过核查"}

    refs = "\n".join(f"[{i + 1}] {s}" for i, s in enumerate(srcs))
    prompt = (
        "你是电网问答忠实度核查员。按步骤判断【答案】是否被【参考资料】支撑"
        "（含忠实转述/归纳总结，不要求逐字复述；只要资料能推出该意思即算支撑）：\n"
        "1) 从答案提取全部原子声明（可验证的事实陈述；忽略纯格式/过渡/引用标注句）。\n"
        "2) 逐条判断每条声明能否从资料中找到支撑。\n"
        "严格输出一行 JSON："
        "{\"claims\":[{\"text\":\"声明\",\"supported\":true}],\"supported_count\":N,\"total_count\":M}\n"
        "（supported_count 为 supported=true 的声明数；total_count 为声明总数）\n\n"
        f"【参考资料】\n{refs}\n\n【答案】\n{answer}"
    )
    try:
        out = await get_llm_provider(model_type).chat(
            [{"role": "user", "content": prompt}], temperature=0, max_tokens=800
        )
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            return {"supported_ratio": None, "hallucination": None, "reason": "核查输出无 JSON（已忽略）"}
        d = json.loads(m.group(0))
        claims = d.get("claims") or []
        total = int(d.get("total_count") or len(claims) or 0)
        sup = int(d.get("supported_count") if d.get("supported_count") is not None
                   else sum(1 for c in claims if c.get("supported")))
        if total <= 0:
            return {"supported_ratio": None, "hallucination": None, "reason": "未提取到声明（已忽略）"}
        supported_ratio = round(min(sup, total) / total, 3)
        return {
            "supported_ratio": supported_ratio,
            "hallucination": round(1.0 - supported_ratio, 3),   # 推导，不信模型自报
            "reason": f"{sup}/{total} 条声明被资料支撑",
        }
    except Exception as e:
        return {"supported_ratio": None, "hallucination": None, "reason": f"核查解析失败（已忽略）: {type(e).__name__}"}


async def judge_context_relevance(
    query: str, chunks: list[str], model_type: str | None = None
) -> dict:
    """评估检索上下文与查询的相关性（RAGAS Context Relevance / TruLens Triad C|Q 维度）。

    对每个 chunk 判断是否与 query 相关，汇总为整体相关性评分。
    返回 {relevance_score, relevant_count, irrelevant_count, irrelevant_indices, reason}。
    """
    from app.providers.factory import get_llm_provider

    if not chunks:
        return {
            "relevance_score": 0.0, "relevant_count": 0, "irrelevant_count": 0,
            "irrelevant_indices": [], "reason": "无检索结果",
        }

    refs = "\n".join(f"[{i}] {c[:300]}" for i, c in enumerate(chunks))
    prompt = (
        "你是电网运维检索质量审核员。判断以下【检索到的文档分块】是否与【用户问题】相关。\n"
        "对每个分块，标记：relevant（直接相关/可回答问题）、partial（部分相关/侧面提及）、irrelevant（完全无关）。\n\n"
        f"【用户问题】\n{query}\n\n"
        f"【检索分块】\n{refs}\n\n"
        "输出 JSON：{\n"
        '  "relevance_score": 0.0~1.0（整体相关性，relevant为1, partial为0.5, irrelevant为0, 取均值）,\n'
        '  "labels": {"0": "relevant/partial/irrelevant", "1": ..., ...},\n'
        '  "reason": "简短说明（哪个分块最有用/最没用，为什么）"\n'
        "}"
    )
    try:
        out = await get_llm_provider(model_type).chat(
            [{"role": "user", "content": prompt}], temperature=0, max_tokens=300
        )
        m = re.search(r"\{.*\}", out, re.S)
        if m:
            d = json.loads(m.group(0))
            labels = d.get("labels", {})
            relevant_count = sum(1 for v in labels.values() if v == "relevant")
            partial_count = sum(1 for v in labels.values() if v == "partial")
            irrelevant_count = sum(1 for v in labels.values() if v == "irrelevant")
            irrelevant_indices = [int(k) for k, v in labels.items() if v == "irrelevant"]
            return {
                "relevance_score": float(d.get("relevance_score", 0.0)),
                "relevant_count": relevant_count,
                "partial_count": partial_count,
                "irrelevant_count": irrelevant_count,
                "irrelevant_indices": sorted(irrelevant_indices),
                "reason": str(d.get("reason", "")),
            }
    except Exception:
        pass
    return {
        "relevance_score": 0.5, "relevant_count": 0, "partial_count": 0,
        "irrelevant_count": 0, "irrelevant_indices": [], "reason": "judge 调用失败",
    }


async def judge_answerability(
    query: str, chunks: list[str], model_type: str | None = None
) -> dict:
    """评估给定检索上下文，问题是否可回答（Q|C 维度：给定这些 chunks，能否回答这个问题）。

    用于判断检索质量是否足以支撑回答，还是应该拒答/触发改写重检索。
    返回 {answerable (bool), confidence (0-1), missing_info (str)}。
    """
    from app.providers.factory import get_llm_provider

    if not chunks:
        return {"answerable": False, "confidence": 1.0, "missing_info": "检索结果为空"}

    refs = "\n".join(f"[{i}] {c[:300]}" for i, c in enumerate(chunks[:5]))
    prompt = (
        "你是电网运维知识审核员。判断仅凭以下【检索到的参考资料】，能否完整回答【用户问题】。\n\n"
        f"【用户问题】\n{query}\n\n"
        f"【参考资料】\n{refs}\n\n"
        "输出 JSON：{\n"
        '  "answerable": true/false,\n'
        '  "confidence": 0.0~1.0,\n'
        '  "missing_info": "如果不能回答，缺失哪些关键信息（可回答时为空）"\n'
        "}"
    )
    try:
        out = await get_llm_provider(model_type).chat(
            [{"role": "user", "content": prompt}], temperature=0, max_tokens=200
        )
        m = re.search(r"\{.*\}", out, re.S)
        if m:
            d = json.loads(m.group(0))
            return {
                "answerable": bool(d.get("answerable", False)),
                "confidence": float(d.get("confidence", 0.0)),
                "missing_info": str(d.get("missing_info", "")),
            }
    except Exception:
        pass
    return {"answerable": False, "confidence": 0.0, "missing_info": "judge 调用失败"}
