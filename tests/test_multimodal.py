"""多模态 VLM 服务单测（关闭态纯逻辑）。"""
import asyncio

from app.services import multimodal_service


def test_describe_image_disabled_returns_empty():
    """VLM_ENABLE 默认关 → 直接返回空串（调用方回退纯 OCR，不影响主流程）。"""
    assert asyncio.run(multimodal_service.describe_image(b"\x89PNG fake")) == ""


def test_describe_image_empty_returns_empty():
    assert asyncio.run(multimodal_service.describe_image(b"")) == ""
    assert asyncio.run(multimodal_service.describe_image(None)) == ""
