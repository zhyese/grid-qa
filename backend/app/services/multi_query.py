"""多查询分解：复杂问题拆成多个子问题并行检索，提升覆盖（A2）。

如"对比主变和配变的温度处置" → ["主变压器温度异常处置", "配电变压器温度异常处置"]。
每个子问题独立检索后候选合并（跨查询 RRF），再统一 rerank。
关闭/失败返回空列表，调用方按单 query 处理。
"""
import json
import re

from app.config import settings
from app.core.obs import degraded
from app.providers.factory import get_llm_provider


async def decompose(query: str, model_type: str | None = None, n: int = 3) -> list[str]:
    """拆解复杂问题为 n 个子问题。关闭/失败返回 []。"""
    if not getattr(settings, "MULTI_QUERY_ENABLE", False) or not query.strip():
        return []
    prompt = (
        f"你是电网运维检索专家。把下面的复杂问题拆成 {n} 个更具体的子问题（用于并行检索），"
        "覆盖问题不同侧面，每个子问题应能独立检索到相关运维资料。"
        "只输出 JSON 字符串数组，如 [\"子问题1\",\"子问题2\"]，不要解释：\n"
        f"问题：{query}"
    )
    try:
        ans = await get_llm_provider(model_type).chat(
            [{"role": "user", "content": prompt}], temperature=0.3, max_tokens=300
        )
    except Exception as e:
        degraded("multi_query", e)
        return []
    m = re.search(r"\[.*\]", ans or "", re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    return [str(x).strip() for x in arr if str(x).strip()][:n]
