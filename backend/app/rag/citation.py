"""答案后处理：引用统计与幻觉率启发式估算。"""
import re


def count_citations(answer: str) -> int:
    """统计答案中出现的引用编号 [n] 数量（去重）。"""
    return len(set(re.findall(r"\[(\d+)\]", answer)))


def estimate_hallucination(answer: str, ref_count: int) -> float:
    """启发式幻觉率：未被引用的参考资料占比（0~1）。
    说明：MVP 占位指标，S10 接 LLM-as-judge 正式评估。
    """
    if ref_count <= 0:
        return 1.0
    cited = count_citations(answer)
    covered = min(cited, ref_count)
    return round(max(0.0, 1.0 - covered / ref_count), 3)
