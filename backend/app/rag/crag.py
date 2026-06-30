"""Corrective RAG (CRAG)：检索结果相关性分级 + 纠错闭环（实时护栏，降幻觉）。

检索（rerank 已给相关性分）后分级：
  correct    top1 分 >= HIGH  → 证据充分，正常生成
  ambiguous  中间            → 证据有限，答案标注不确定性
  incorrect  top1 分 < LOW 或空 → 触发纠错（query 改写重检索）；仍低分 → refused（拒答/保守）

信号源复用 rerank 的 relevance_score（不额外调评估 LLM，省钱 + 低延迟）。
rerank 未启用/失败时 score 语义不可靠 → 降级 ambiguous（保守，不误触发纠错）。

对应 2026 RAG 趋势的 Self-RAG / CRAG / Adaptive RAG：自纠错闭环。
与 rag/judge.py 互补：judge 是离线事后验尸，CRAG 是在线实时前置护栏。
"""
from app.config import settings

GRADE_CORRECT = "correct"
GRADE_AMBIGUOUS = "ambiguous"
GRADE_INCORRECT = "incorrect"


def grade(top1_score: float, n_contexts: int, rerank_ok: bool = True) -> tuple[str, str]:
    """检索结果分级。返回 (grade, reason)。

    rerank_ok=False（rerank 关闭/失败，score 不可靠）→ 降级 ambiguous，不误触发纠错。
    """
    if not rerank_ok:
        return GRADE_AMBIGUOUS, "rerank 未启用，无法可靠分级（保守降级）"
    if n_contexts == 0:
        return GRADE_INCORRECT, "检索无结果"
    if top1_score >= settings.CRAG_HIGH:
        return GRADE_CORRECT, f"top1 相关性 {top1_score:.2f} ≥ {settings.CRAG_HIGH}"
    if top1_score < settings.CRAG_LOW:
        return GRADE_INCORRECT, f"top1 相关性 {top1_score:.2f} < {settings.CRAG_LOW}"
    return GRADE_AMBIGUOUS, f"top1 相关性 {top1_score:.2f} 介于阈值间"


def confidence_of(grade: str, rewritten: bool) -> str:
    """把分级 + 是否改写重检索映射为对外置信度 high/medium/low/refused。

    refused = 改写重检索后仍 incorrect（强相关证据缺失）→ 建议拒答/保守作答。
    """
    if grade == GRADE_CORRECT and not rewritten:
        return "high"
    if grade == GRADE_INCORRECT and rewritten:
        return "refused"
    return "medium"
