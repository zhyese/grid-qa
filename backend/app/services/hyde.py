"""HyDE（Hypothetical Document Embeddings）：LLM 先生成假设答案，用其向量检索（A3）。

短/口语化问题直接 embedding 召回差（query 与文档表述 gap 大）。先让 LLM 生成一段
"假设性解答"，表述更接近运维文档语言，dense 召回更准。仅用于 dense（BM25 仍用原 query）。
失败/关闭返回空串，调用方回退原 query。
"""
from app.config import settings
from app.core.obs import degraded
from app.providers.factory import get_llm_provider


async def generate_hypothetical(query: str, model_type: str | None = None) -> str:
    """生成假设性文档。关闭/失败返回空串。"""
    if not getattr(settings, "HYDE_ENABLE", False) or not query.strip():
        return ""
    prompt = (
        "你是电网运维专家。针对下面的问题，写一段 80-150 字的假设性技术解答，"
        "用规程/手册的专业表述（仿佛出自运维文档），不要寒暄、不要分点编号。\n"
        f"问题：{query}\n假设性解答："
    )
    try:
        return (await get_llm_provider(model_type).chat(
            [{"role": "user", "content": prompt}], temperature=0.2, max_tokens=300
        )).strip()
    except Exception as e:
        degraded("hyde", e)
        return ""
