"""多模态 RAG：VLM 理解图片(图纸/设备外观/故障现象/曲线图)→描述文本。

OCR 只能提取图片里的文字，丢失图纸结构、设备外观、曲线趋势等空间语义——而这些正是
电网故障定位的关键。VLM(Qwen-VL，百炼 OpenAI 兼容)生成结构化描述，与 OCR 文字合并入知识库。
默认关(VLM_ENABLE)，失败返回空串，调用方回退纯 OCR。
"""
import base64

from app.config import settings
from app.core.obs import degraded

_DESC_PROMPT = (
    "你是电网运维图片分析专家。用 100-200 字描述这张图片的关键信息："
    "设备名称/型号、接线或结构、可见的异常现象(放电/渗漏/变形/污秽)、"
    "图纸上的标注参数或型号、曲线图的趋势。只输出描述，不要寒暄。"
)


async def describe_image(content: bytes) -> str:
    """VLM 描述图片。关闭/空/失败返回空串（调用方回退纯 OCR）。"""
    if not getattr(settings, "VLM_ENABLE", False) or not content:
        return ""
    from openai import AsyncOpenAI

    data_url = f"data:image/jpeg;base64,{base64.b64encode(content).decode()}"
    try:
        client = AsyncOpenAI(api_key=settings.DASHSCOPE_API_KEY, base_url=settings.DASHSCOPE_BASE_URL)
        resp = await client.chat.completions.create(
            model=settings.QWEN_VLM_MODEL,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": _DESC_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}],
            temperature=0.2, max_tokens=400,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        degraded("vlm_describe", e)
        return ""
