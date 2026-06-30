"""多轮指代消解：把追问改写成带上下文的独立查询（S7）。

"它的处置步骤呢" → "主变压器温度异常的处置步骤"。仅多轮(有历史)触发，消解代词/指代，
让检索拿到完整语义——原 history 拼接只给 LLM 上下文，检索用的仍是残缺 query。
失败返回原 query。
"""
from app.config import settings
from app.core.obs import degraded
from app.providers.factory import get_llm_provider


async def rewrite_standalone(
    query: str, history: list[dict], model_type: str | None = None
) -> str:
    """把追问改写为独立可检索查询。关闭/无历史/失败返回原 query。"""
    if not getattr(settings, "STANDALONE_REWRITE_ENABLE", False):
        return query
    if not history or not query.strip():
        return query
    dialog = "\n".join(
        f"{'用户' if h.get('role') == 'user' else '助手'}：{(h.get('content') or '')[:120]}"
        for h in history[-6:]
    )
    prompt = (
        "根据多轮对话上下文，把最后一句追问改写成一个信息完整、可独立检索的查询"
        "（消解代词和指代，保留关键设备/故障/操作术语）。只输出改写后的查询，不要解释、不要引号：\n"
        f"【对话历史】\n{dialog}\n【追问】{query}\n【独立查询】"
    )
    try:
        return (await get_llm_provider(model_type).chat(
            [{"role": "user", "content": prompt}], temperature=0, max_tokens=120
        )).strip() or query
    except Exception as e:
        degraded("standalone_rewrite", e)
        return query
