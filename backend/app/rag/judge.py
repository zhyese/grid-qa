"""LLM-as-judge：判定答案是否被资料支撑，算真实幻觉率（替代启发式 estimate）。

用于离线评测（scripts/eval_qa.py），不每次问答调用（避免额外 LLM 延迟）。
"""
import json
import re
from typing import Optional


async def judge_hallucination(
    answer: str, sources: list[str], model_type: Optional[str] = None
) -> dict:
    """返回 {supported_ratio, hallucination, reason}。"""
    from app.providers.factory import get_llm_provider

    refs = "\n".join(f"[{i + 1}] {s}" for i, s in enumerate(sources))
    prompt = (
        "你是严格的电网问答审核员。判断下方【答案】是否被【参考资料】直接支撑。\n"
        "- supported_ratio：答案内容中被资料直接支撑的比例(0~1)\n"
        "- hallucination：1 - supported_ratio（编造/无支撑内容占比）\n"
        "只输出一行 JSON：{\"supported_ratio\": 0.0~1.0, \"hallucination\": 0.0~1.0, \"reason\": \"简短说明\"}\n\n"
        f"【参考资料】\n{refs}\n\n【答案】\n{answer}"
    )
    out = await get_llm_provider(model_type).chat(
        [{"role": "user", "content": prompt}], temperature=0, max_tokens=200
    )
    m = re.search(r"\{.*\}", out, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            return {
                "supported_ratio": float(d.get("supported_ratio", 0.0)),
                "hallucination": float(d.get("hallucination", 1.0)),
                "reason": str(d.get("reason", "")),
            }
        except Exception:
            pass
    return {"supported_ratio": 0.0, "hallucination": 1.0, "reason": "judge 输出解析失败"}
