"""CRAG v2：LLM 逐条评估检索证据相关性（per-doc grading），替代仅 top1 分数（S6/A1）。

v1（crag.py）只看 rerank top1 分数三档分级；v2 让 LLM 对每个候选判定
relevant/partial/irrelevant，按 relevant 数量分级，能识破"top1 高分但其余全无关"的伪相关，
也兼具 Self-RAG"证据是否足够"的判断。失败时返回空 grade，调用方回退 v1。
"""
import json
import re
import asyncio

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


def _parse_llm_json(ans: str) -> dict | None:
    """从 LLM 返回的文本中清洗并解析 JSON 对象。"""
    if not ans:
        return None
    ans_clean = ans.strip()
    # 移除 markdown 代码块包裹 (如 ```json ... ``` 或 ``` ... ```)
    if ans_clean.startswith("```"):
        first_nl = ans_clean.find("\n")
        if first_nl != -1:
            ans_clean = ans_clean[first_nl:].strip()
        if ans_clean.endswith("```"):
            ans_clean = ans_clean[:-3].strip()
    
    # 查找最外层的 JSON 括号 {}
    m = re.search(r"(\{.*\})", ans_clean, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


async def grade_with_llm(
    query: str, contexts: list[dict], model_type: str | None = None
) -> tuple[str, dict]:
    """LLM 逐条评估证据相关性。返回 (grade, detail)。

    grade: correct/ambiguous/incorrect 或 ""（未启用/失败，调用方回退 v1）。
    """
    if not getattr(settings, "CRAG_PERDOC_ENABLE", False) or not contexts:
        return "", {}
    
    # 构建输入参考文本
    refs = "\n".join(
        f"[{i + 1}] {(c.get('chunk') or '')[:200]}" for i, c in enumerate(contexts[:8])
    )
    
    prompt = (
        "你是严格的电网问答数据审核员。判断每条参考资料对回答用户提问的相关性。\n\n"
        "请对每一条参考资料输出以下三种 label 之一：\n"
        "- relevant: 资料包含能够直接回答问题或部分问题的关键事实与证据。\n"
        "- partial: 资料与问题涉及的设备或场景相关，但无法直接解答该具体问题。\n"
        "- irrelevant: 资料与问题完全无关。\n\n"
        "【约束条件】\n"
        "1. 必须只输出 JSON 对象，不要输出任何其他的解释、前言或后缀文字。\n"
        "2. JSON 输出格式必须完全符合以下 Schema：\n"
        '{"verdicts": [{"idx": 1, "label": "relevant" | "partial" | "irrelevant"}]}\n\n'
        "【示例】\n"
        "问题：10kV电缆头制作需要什么工器具？\n"
        "资料：\n"
        "[1] 制作10kV电缆终端头需要使用剥线钳、热缩枪、压接钳等工器具。\n"
        "[2] 输电线路巡视周期一般为每月一次。\n"
        "输出：\n"
        '{"verdicts": [{"idx": 1, "label": "relevant"}, {"idx": 2, "label": "irrelevant"}]}\n\n'
        f"问题：{query}\n"
        f"资料：\n{refs}\n"
        "输出："
    )
    
    timeout = getattr(settings, "CRAG_TIMEOUT", 5.0)
    try:
        # 使用 asyncio.wait_for 设置超时时间，以防大模型请求卡死
        ans = await asyncio.wait_for(
            get_llm_provider(model_type).chat(
                [{"role": "user", "content": prompt}], temperature=0, max_tokens=400
            ),
            timeout=timeout
        )
    except asyncio.TimeoutError as e:
        degraded("crag_v2_timeout", e)
        return "", {}
    except Exception as e:
        degraded("crag_v2_grade", e)
        return "", {}
        
    data = _parse_llm_json(ans)
    if not data or "verdicts" not in data:
        return "", {}
        
    labels = []
    verdicts_dict = {}
    for v in data.get("verdicts", []):
        if not isinstance(v, dict):
            continue
        try:
            idx = int(v.get("idx"))
            label = str(v.get("label", "irrelevant")).strip().lower()
            verdicts_dict[idx] = label
        except (ValueError, TypeError):
            continue

    n_limit = min(8, len(contexts))
    for i in range(n_limit):
        labels.append(verdicts_dict.get(i + 1, "irrelevant"))
        
    return labels_to_grade(labels)
