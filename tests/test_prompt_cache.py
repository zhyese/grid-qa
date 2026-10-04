"""F7 Prompt Caching 单测：显式标记、降级重试、usage 归一化、成本核算。

不依赖任何外部服务（不连 Redis/MySQL/云 API）。
"""
import types

import pytest

from app.config import settings
from app.providers import prompt_cache


@pytest.fixture
def cache_on(monkeypatch):
    monkeypatch.setattr(settings, "PROMPT_CACHE_ENABLE", True, raising=False)
    monkeypatch.setattr(settings, "PROMPT_CACHE_MIN_CHARS", 10, raising=False)
    yield
    # monkeypatch 自动还原


def _msgs(text="x" * 100):
    return [
        {"role": "system", "content": text},
        {"role": "user", "content": "1号主变油温高怎么办"},
    ]


def test_disabled_returns_same_object():
    """默认关：原样返回同一对象（调用方靠 `is` 判断是否加了标记）。"""
    msgs = _msgs()
    assert prompt_cache.apply_explicit_cache(msgs, "qwen") is msgs


def test_enabled_marks_last_system_message(cache_on):
    msgs = _msgs()
    out = prompt_cache.apply_explicit_cache(msgs, "qwen")
    assert out is not msgs                      # 新列表，不污染调用方
    assert msgs[0]["content"] == "x" * 100      # 原入参未被修改
    blocks = out[0]["content"]
    assert isinstance(blocks, list)
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[0]["text"] == "x" * 100


def test_enabled_but_provider_not_supported(cache_on):
    """DeepSeek 自动缓存、方舟/本地不传参 → 不做显式标记。"""
    msgs = _msgs()
    for prov in ("deepseek", "doubao", "ollama"):
        assert prompt_cache.apply_explicit_cache(msgs, prov) is msgs


def test_short_prefix_not_marked(cache_on):
    """前缀短于最小长度（百炼要求 ≥1024 token）→ 不标记。"""
    msgs = _msgs(text="短前缀")
    assert prompt_cache.apply_explicit_cache(msgs, "qwen") is msgs


def test_no_system_message_not_marked(cache_on):
    msgs = [{"role": "user", "content": "y" * 100}]
    assert prompt_cache.apply_explicit_cache(msgs, "qwen") is msgs


@pytest.mark.asyncio
async def test_call_with_cache_fallback_retries_without_mark(cache_on):
    """网关不认 cache_control → 去掉标记重试一次，主链路不挂。"""
    calls = []

    async def create(messages):
        calls.append(messages)
        if isinstance(messages[0]["content"], list):
            raise ValueError("Invalid parameter: cache_control is not supported by this model")
        return "ok"

    res = await prompt_cache.call_with_cache_fallback(create, _msgs(), "qwen")
    assert res == "ok"
    assert len(calls) == 2
    assert isinstance(calls[0][0]["content"], list)   # 第一次带标记
    assert calls[1][0]["content"] == "x" * 100        # 第二次去标记


@pytest.mark.asyncio
async def test_call_with_cache_fallback_no_extra_call_when_unmarked():
    """未开启时只调用一次（零额外开销）。"""
    calls = []

    async def create(messages):
        calls.append(messages)
        return "ok"

    await prompt_cache.call_with_cache_fallback(create, _msgs(), "qwen")
    assert len(calls) == 1


def test_cached_tokens_from_usage_openai_shape():
    usage = {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 640}}
    assert prompt_cache.cached_tokens_from_usage(usage) == 640


def test_cached_tokens_from_usage_deepseek_shape():
    usage = {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 512}
    assert prompt_cache.cached_tokens_from_usage(usage) == 512


def test_cached_tokens_from_usage_object_shape():
    details = types.SimpleNamespace(cached_tokens=128)
    usage = types.SimpleNamespace(prompt_tokens_details=details)
    assert prompt_cache.cached_tokens_from_usage(usage) == 128


def test_cached_tokens_from_usage_missing_is_zero():
    assert prompt_cache.cached_tokens_from_usage(None) == 0
    assert prompt_cache.cached_tokens_from_usage({}) == 0
    assert prompt_cache.cached_tokens_from_usage({"prompt_tokens_details": {}}) == 0


def test_usage_dict_none_returns_none():
    assert prompt_cache.usage_dict(None) is None


def test_usage_dict_normalizes_all_fields():
    usage = {"prompt_tokens": 900, "completion_tokens": 120,
             "prompt_tokens_details": {"cached_tokens": 600,
                                       "cache_creation_input_tokens": 200}}
    assert prompt_cache.usage_dict(usage) == {
        "input": 900, "output": 120, "cached": 600, "cache_creation": 200}


def test_compute_cost_savings_on_cache_hit():
    """命中 800/1000 输入 token：按 10% 计价，省下的钱 = 800×输入单价×90%。"""
    from app.services.cost_tracker_service import compute_cost
    price_in = 0.001 / 1000
    cost, saved = compute_cost("qwen", 1000, 0, cached_tokens=800)
    assert cost == pytest.approx(200 * price_in + 800 * price_in * 0.1)
    assert saved == pytest.approx(800 * price_in * 0.9)


def test_compute_cost_clamps_cached_to_input():
    """cached 是 input 子集：误传超过 input 不产生负费用/超额节省。"""
    from app.services.cost_tracker_service import compute_cost
    cost, saved = compute_cost("qwen", 100, 0, cached_tokens=9999)
    assert cost >= 0
    assert saved == pytest.approx(100 * (0.001 / 1000) * 0.9)


def test_compute_cost_no_cache_is_baseline():
    from app.services.cost_tracker_service import compute_cost
    cost, saved = compute_cost("qwen", 500, 200)
    assert saved == 0.0
    assert cost == pytest.approx(500 * (0.001 / 1000) + 200 * (0.002 / 1000))
