"""Prompt Caching（上下文缓存）支持：显式缓存标记 + 缓存命中 token 核算。

三家 provider 的缓存能力（2026-10 官方文档核实）：

- **DeepSeek**：上下文硬盘缓存**默认开启、无需传参**（前缀完整匹配即命中）。
  返回 usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens。
- **阿里百炼 Qwen**：分「隐式缓存」（自动，无需配置，命中率不确定）与
  「显式缓存」（需在 messages 的 content 块加 `cache_control: {"type":"ephemeral"}`，
  单请求最多 4 个标记、最小 1024 token、有效期 5 分钟）。两者互斥。
  返回 usage.prompt_tokens_details.cached_tokens / .cache_creation_input_tokens。
- **火山方舟 Doubao / 本地 Ollama**：本项目未使用显式缓存参数，仅做命中核算（若网关返回）。

设计原则（对齐仓库铁律）：
- 默认关（`PROMPT_CACHE_ENABLE=False`）= 现状：不改 messages、不传新参数、零回归。
- 显式标记**只加在 system 消息**（稳定前缀，最符合前缀缓存的命中语义），且只对 qwen 生效。
- 网关若不认该参数（旧模型/不支持），自动**去掉标记重试一次**并 degraded 记录，绝不因缓存把主链路打挂。
- 命中核算对全部 provider 通用：拿不到字段就是 0，不影响原有计费口径。
"""
from __future__ import annotations

import copy

from app.core.obs import degraded

# 显式缓存标记（百炼唯一合法取值）
_CACHE_CONTROL = {"type": "ephemeral"}
# 需要显式标记的 provider（DeepSeek 自动缓存，无需标记）
_EXPLICIT_MARK_PROVIDERS = {"qwen"}


def enabled() -> bool:
    from app.config import settings
    return bool(getattr(settings, "PROMPT_CACHE_ENABLE", False))


def _min_chars() -> int:
    """显式缓存最小内容长度（百炼要求 ≥1024 token，中文约 1 char≈1 token，留余量）。"""
    from app.config import settings
    return int(getattr(settings, "PROMPT_CACHE_MIN_CHARS", 1200))


def apply_explicit_cache(messages: list[dict], provider: str) -> list[dict]:
    """给稳定前缀（最后一条 system 消息）加显式缓存标记。

    返回**新列表**（不改调用方入参）；不满足条件时原样返回入参对象（同一引用，
    调用方可用 `is` 判断是否真的加了标记，用于决定失败后是否重试）。
    """
    if not enabled() or provider not in _EXPLICIT_MARK_PROVIDERS:
        return messages
    if not isinstance(messages, list) or not messages:
        return messages
    idx = -1
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("role") == "system" and isinstance(m.get("content"), str):
            idx = i
    if idx < 0:
        return messages
    text = messages[idx]["content"]
    if len(text) < _min_chars():
        return messages
    out = list(messages)
    new_msg = dict(out[idx])
    new_msg["content"] = [{"type": "text", "text": text, "cache_control": _CACHE_CONTROL}]
    out[idx] = new_msg
    return out


def is_cache_param_error(exc: BaseException) -> bool:
    """判断异常是否由 cache_control 参数不被支持引起（用于降级重试）。"""
    msg = str(exc).lower()
    return "cache_control" in msg or "cache control" in msg


async def call_with_cache_fallback(create_fn, messages: list[dict], provider: str):
    """调 create_fn(messages)；显式标记被拒时去掉标记重试一次。

    create_fn: 接收 messages 返回 SDK 响应的协程函数。
    标记未生效（未开启/不适用/前缀过短）时只调用一次，零额外开销。
    """
    marked = apply_explicit_cache(messages, provider)
    if marked is messages:
        return await create_fn(messages)
    try:
        return await create_fn(marked)
    except Exception as exc:
        if is_cache_param_error(exc):
            degraded("prompt_cache_param_unsupported", exc, provider)
            return await create_fn(messages)
        raise


def _get(obj, name: str):
    """兼容 SDK 对象与 dict 两种形态取字段。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def cached_tokens_from_usage(usage) -> int:
    """从 SDK usage 提取缓存命中 token 数（多 provider 兼容）。

    优先级：OpenAI 兼容 prompt_tokens_details.cached_tokens（百炼/方舟）
            → DeepSeek prompt_cache_hit_tokens → 0。
    """
    details = _get(usage, "prompt_tokens_details")
    cached = _get(details, "cached_tokens")
    if cached is None:
        cached = _get(usage, "prompt_cache_hit_tokens")
    try:
        return max(0, int(cached or 0))
    except (TypeError, ValueError):
        return 0


def cache_creation_tokens_from_usage(usage) -> int:
    """从 SDK usage 提取「创建缓存」token 数（百炼显式缓存首轮写入量）。"""
    details = _get(usage, "prompt_tokens_details")
    created = _get(details, "cache_creation_input_tokens")
    try:
        return max(0, int(created or 0))
    except (TypeError, ValueError):
        return 0


def usage_dict(usage) -> dict | None:
    """把 SDK usage 归一化为本项目 usage dict（含缓存字段）。

    无 usage 时返回 None（沿用调用方估算口径）。
    """
    if usage is None:
        return None
    try:
        return {
            "input": int(_get(usage, "prompt_tokens") or 0),
            "output": int(_get(usage, "completion_tokens") or 0),
            "cached": cached_tokens_from_usage(usage),
            "cache_creation": cache_creation_tokens_from_usage(usage),
        }
    except Exception:
        return None


def clone_messages(messages: list[dict]) -> list[dict]:
    """深拷贝 messages（缓存标记实验用，避免污染调用方）。"""
    return copy.deepcopy(messages)
