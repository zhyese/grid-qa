"""CRAG v2：LLM 逐条评估检索证据相关性（per-doc grading），替代仅 top1 分数（S6/A1）。

v1（crag.py）只看 rerank top1 分数三档分级；v2 让 LLM 对每个候选判定
relevant/partial/irrelevant，按 relevant 数量分级，能识破"top1 高分但其余全无关"的伪相关，
也兼具 Self-RAG"证据是否足够"的判断。失败时返回空 grade，调用方回退 v1。
"""
import json
import re

from app.config import settings
from app.core.obs import degraded
from app.providers.factory import get_llm_provider
from app.rag.crag import GRADE_AMBIGUOUS, GRADE_CORRECT, GRADE_INCORRECT


def labels_to_grade(labels: list[str]) -> tuple[str, dict]:
    """per-doc 相关性标签 → 分级（纯逻辑，可单测）。

    ≥2 条 relevant=correct（证据充分）；1 条 relevant/partial=ambiguous（证据有限）；
    全 irrelevant=incorrect（触发纠错）。
    """
    rel = sum(1 for l in labels if l == "relevant")
    partial = sum(1 for l in labels if l == "partial")
    if rel >= 2:
        grade = GRADE_CORRECT
    elif rel + partial >= 1:
        grade = GRADE_AMBIGUOUS
    else:
        grade = GRADE_INCORRECT
    return grade, {"relevant": rel, "partial": partial,
                   "irrelevant": len(labels) - rel - partial}


async def grade_with_llm(
    query: str, contexts: list[dict], model_type: str | None = None
) -> tuple[str, dict]:
    """LLM 逐条评估证据相关性。返回 (grade, detail)。

    grade: correct/ambiguous/incorrect 或 ""（未启用/失败，调用方回退 v1）。
    """
    if not getattr(settings, "CRAG_PERDOC_ENABLE", False) or not contexts:
        return "", {}
    refs = "\n".join(
        f"[{i + 1}] {(c.get('chunk') or '')[:200]}" for i, c in enumerate(contexts[:8])
    )
    prompt = (
        "你是严格的电网问答审核员。判断每条参考资料对回答问题的相关性。\n"
        "label 三档：relevant(直接回答)/partial(部分相关)/irrelevant(无关)。\n"
        '只输出 JSON：{"verdicts":[{"idx":1,"label":"relevant"}]}\n'
        f"问题：{query}\n资料：\n{refs}"
    )
    try:
        ans = await get_llm_provider(model_type).chat(
            [{"role": "user", "content": prompt}], temperature=0, max_tokens=400
        )
    except Exception as e:
        degraded("crag_v2_grade", e)
        return "", {}
    m = re.search(r"\{.*\}", ans or "", re.S)
    if not m:
        return "", {}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return "", {}
    labels = [v.get("label", "irrelevant") for v in data.get("verdicts", [])]
    return labels_to_grade(labels)
