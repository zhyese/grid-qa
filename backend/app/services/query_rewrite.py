"""LLM query 改写：口语化/简短提问 → 规范完整检索查询，提升召回。

默认关闭（QUERY_REWRITE_ENABLE），开启会增加一次 LLM 调用延迟。
"""
from app.config import settings
from app.core.obs import degraded
from app.providers.factory import get_llm_provider


async def rewrite_query(query: str, model_type: str | None = None) -> str:
    """改写失败时原样返回，不影响主流程。"""
    if not settings.QUERY_REWRITE_ENABLE or not query.strip():
        return query
    prompt = (
        "你是电网运维检索查询改写助手。将下面的用户提问改写为更规范、信息更完整、"
        "适合向量检索的查询（保留关键设备/故障/操作术语，去掉口语）。"
        "只输出改写后的查询，不要解释、不要引号：\n" + query
    )
    try:
        return (await get_llm_provider(model_type).chat(
            [{"role": "user", "content": prompt}], temperature=0, max_tokens=120
        )).strip() or query
    except Exception as e:
        degraded("query_rewrite", e)
        return query
