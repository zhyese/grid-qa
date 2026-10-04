"""OllamaLLM 单测：本地应急模型 chat/chat_with_usage/stream，mock AsyncOpenAI，
不依赖真实 Ollama 服务（照 test_provider_tools.py 的 mock 范式）。"""
import asyncio
from types import SimpleNamespace

from app.providers.llm.ollama_llm import OllamaLLM


def _make_resp(content, usage=None):
    msg = SimpleNamespace(content=content)
    u = SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]) if usage else None
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=u)


def test_chat_returns_content(monkeypatch):
    p = OllamaLLM()

    async def fake_create(**kw):
        assert kw["model"] == p.model
        return _make_resp("本地应急回答")

    monkeypatch.setattr(p.client.chat.completions, "create", fake_create)
    r = asyncio.run(p.chat([{"role": "user", "content": "主变异常怎么处置"}]))
    assert r == "本地应急回答"


def test_chat_with_usage_returns_token_counts(monkeypatch):
    p = OllamaLLM()

    async def fake_create(**kw):
        return _make_resp("答案", usage=(10, 20))

    monkeypatch.setattr(p.client.chat.completions, "create", fake_create)
    content, usage = asyncio.run(p.chat_with_usage([{"role": "user", "content": "x"}]))
    assert content == "答案"
    # F7 起 usage 契约扩展出缓存字段（本地 Ollama 无缓存，恒为 0）
    assert usage == {"input": 10, "output": 20, "cached": 0, "cache_creation": 0}


def test_stream_yields_tokens(monkeypatch):
    p = OllamaLLM()

    async def fake_stream(**kw):
        async def gen():
            for tok in ["本地", "应急", "回答"]:
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=tok))])
        return gen()

    monkeypatch.setattr(p.client.chat.completions, "create", fake_stream)

    async def collect():
        out = []
        async for tok in p.stream([{"role": "user", "content": "x"}]):
            out.append(tok)
        return out

    assert asyncio.run(collect()) == ["本地", "应急", "回答"]


def test_uses_ollama_base_url_and_local_timeout():
    """base_url 走 OLLAMA_BASE_URL + /v1；timeout 用独立的 LLM_LOCAL_TIMEOUT（CPU 推理更慢）。"""
    from app.config import settings
    p = OllamaLLM()
    assert str(p.client.base_url).rstrip("/") == f"{settings.OLLAMA_BASE_URL}/v1"
    assert p.client.timeout == settings.LLM_LOCAL_TIMEOUT
