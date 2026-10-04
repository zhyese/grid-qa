"""H1 检索底座：向量空间一致性守卫单测（不连真实 Redis / 不打云 API）。"""
import pytest

from app.clients import redis_client
from app.config import settings
from app.providers.embedding.bge_embedding import BgeEmbedding
from app.providers.embedding.qwen_embedding import QwenEmbedding
from app.services import embedding_service


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    embedding_service.reset_model_signature_cache()
    yield fake
    embedding_service.reset_model_signature_cache()


def test_qwen_signature_includes_model_and_dim(monkeypatch):
    monkeypatch.setattr(settings, "QWEN_EMB_MODEL", "text-embedding-v4", raising=False)
    monkeypatch.setattr(settings, "EMBEDDING_DIM", 1024, raising=False)
    assert QwenEmbedding().model_signature == "qwen:text-embedding-v4:1024"


def test_bge_signature_includes_model_and_dim(monkeypatch):
    monkeypatch.setattr(settings, "BGE_MODEL", "BAAI/bge-small-zh-v1.5", raising=False)
    monkeypatch.setattr(settings, "BGE_DIM", 512, raising=False)
    assert BgeEmbedding().model_signature == "bge:BAAI/bge-small-zh-v1.5:512"


@pytest.mark.asyncio
async def test_first_call_initializes_signature(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "EMBED_MODEL_GUARD_ENABLE", True, raising=False)
    monkeypatch.setattr(settings, "EMB_PROVIDER", "qwen", raising=False)
    monkeypatch.setattr(settings, "QWEN_EMB_MODEL", "text-embedding-v4", raising=False)

    res = await embedding_service.ensure_model_consistency("qwen")
    assert res["status"] == "initialized"
    assert fake_redis.store["embed:model_sig:qwen"] == res["signature"]


@pytest.mark.asyncio
async def test_same_model_is_ok(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "EMBED_MODEL_GUARD_ENABLE", True, raising=False)
    monkeypatch.setattr(settings, "QWEN_EMB_MODEL", "text-embedding-v4", raising=False)
    await embedding_service.ensure_model_consistency("qwen")

    embedding_service.reset_model_signature_cache()   # 模拟进程重启
    res = await embedding_service.ensure_model_consistency("qwen")
    assert res["status"] == "ok"


@pytest.mark.asyncio
async def test_model_change_reports_drift(fake_redis, monkeypatch):
    """换模型（未重建 Milvus）→ 必须报 drift，而不是静默继续。"""
    monkeypatch.setattr(settings, "EMBED_MODEL_GUARD_ENABLE", True, raising=False)
    monkeypatch.setattr(settings, "QWEN_EMB_MODEL", "text-embedding-v3", raising=False)
    await embedding_service.ensure_model_consistency("qwen")

    # 切到 v4（等价于改 .env 后重启）
    monkeypatch.setattr(settings, "QWEN_EMB_MODEL", "text-embedding-v4", raising=False)
    embedding_service.reset_model_signature_cache()
    res = await embedding_service.ensure_model_consistency("qwen")

    assert res["status"] == "drift"
    assert res["previous"] == "qwen:text-embedding-v3:1024"
    assert "rebuild_embeddings" in res["message"]


@pytest.mark.asyncio
async def test_mark_rebuilt_clears_drift(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "EMBED_MODEL_GUARD_ENABLE", True, raising=False)
    monkeypatch.setattr(settings, "QWEN_EMB_MODEL", "text-embedding-v3", raising=False)
    await embedding_service.ensure_model_consistency("qwen")

    monkeypatch.setattr(settings, "QWEN_EMB_MODEL", "text-embedding-v4", raising=False)
    embedding_service.reset_model_signature_cache()
    assert (await embedding_service.ensure_model_consistency("qwen"))["status"] == "drift"

    # 重建向量后
    await embedding_service.mark_vectors_rebuilt("qwen")
    assert (await embedding_service.ensure_model_consistency("qwen"))["status"] == "ok"


@pytest.mark.asyncio
async def test_guard_disabled(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "EMBED_MODEL_GUARD_ENABLE", False, raising=False)
    res = await embedding_service.ensure_model_consistency("qwen")
    assert res["status"] == "disabled"
    assert fake_redis.store == {}


@pytest.mark.asyncio
async def test_guard_degrades_when_redis_down(monkeypatch):
    """Redis 异常不能让守卫抛异常（向量化主链路必须继续）。"""
    def boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", boom)
    monkeypatch.setattr(settings, "EMBED_MODEL_GUARD_ENABLE", True, raising=False)
    embedding_service.reset_model_signature_cache()
    res = await embedding_service.ensure_model_consistency("qwen")
    assert res["status"] == "error"
    embedding_service.reset_model_signature_cache()


@pytest.mark.asyncio
async def test_first_baseline_warns_when_collection_not_empty(fake_redis, monkeypatch):
    """本次升级（v3→v4）场景：库里已有存量向量但无基线 → 必须提示先重建并置待重建标记。"""
    monkeypatch.setattr(settings, "EMBED_MODEL_GUARD_ENABLE", True, raising=False)
    monkeypatch.setattr(settings, "QWEN_EMB_MODEL", "text-embedding-v4", raising=False)
    monkeypatch.setattr(embedding_service, "_collection_has_vectors", lambda p: True)

    res = await embedding_service.ensure_model_consistency("qwen")
    assert res["status"] == "initialized"
    assert res["rebuildPending"] is True
    assert "rebuild_embeddings" in res["message"]
    assert "embed:rebuild_pending:qwen" in fake_redis.store


@pytest.mark.asyncio
async def test_pending_marker_survives_restart(fake_redis, monkeypatch):
    """核心防遗忘：基线已写入后签名比对会报 ok，但待重建标记必须持续提醒。

    这正是真实踩过的坑——冒烟重建（--limit）把 v4 基线写进去后，守卫就再也不喊了，
    而库里 3213 条云向量其实还是 v3。
    """
    monkeypatch.setattr(settings, "EMBED_MODEL_GUARD_ENABLE", True, raising=False)
    monkeypatch.setattr(settings, "QWEN_EMB_MODEL", "text-embedding-v4", raising=False)
    monkeypatch.setattr(embedding_service, "_collection_has_vectors", lambda p: True)
    await embedding_service.ensure_model_consistency("qwen")

    embedding_service.reset_model_signature_cache()   # 模拟进程重启
    res = await embedding_service.ensure_model_consistency("qwen")
    assert res["status"] == "ok"                      # 签名一致
    assert res["rebuildPending"] is True              # 但依然在喊
    assert "rebuild_embeddings" in res["message"]

    pend = await embedding_service.rebuild_pending("qwen")
    assert pend["pending"] is True
    assert pend["since"]                              # 置位时间可查，便于判断拖了多久


@pytest.mark.asyncio
async def test_no_pending_when_collection_empty(fake_redis, monkeypatch):
    """空库首次建基线不该报待重建（没有旧向量可漂移）。"""
    monkeypatch.setattr(settings, "EMBED_MODEL_GUARD_ENABLE", True, raising=False)
    monkeypatch.setattr(settings, "QWEN_EMB_MODEL", "text-embedding-v4", raising=False)
    monkeypatch.setattr(embedding_service, "_collection_has_vectors", lambda p: False)

    res = await embedding_service.ensure_model_consistency("qwen")
    assert res["status"] == "initialized"
    assert res["rebuildPending"] is False


@pytest.mark.asyncio
async def test_mark_rebuilt_clears_pending(fake_redis, monkeypatch):
    """全量重建成功后，待重建标记必须清除（否则告警永不消失=狼来了）。"""
    monkeypatch.setattr(settings, "EMBED_MODEL_GUARD_ENABLE", True, raising=False)
    monkeypatch.setattr(settings, "QWEN_EMB_MODEL", "text-embedding-v4", raising=False)
    monkeypatch.setattr(embedding_service, "_collection_has_vectors", lambda p: True)
    await embedding_service.ensure_model_consistency("qwen")
    assert (await embedding_service.rebuild_pending("qwen"))["pending"] is True

    await embedding_service.mark_vectors_rebuilt("qwen")

    assert (await embedding_service.rebuild_pending("qwen"))["pending"] is False
    assert (await embedding_service.ensure_model_consistency("qwen"))["rebuildPending"] is False


@pytest.mark.asyncio
async def test_health_reports_rebuild_pending(fake_redis, monkeypatch):
    from app.providers.factory import check_embedding_health

    monkeypatch.setattr(settings, "EMBED_MODEL_GUARD_ENABLE", True, raising=False)
    monkeypatch.setattr(settings, "EMB_PROVIDER", "qwen", raising=False)
    monkeypatch.setattr(settings, "QWEN_EMB_MODEL", "text-embedding-v4", raising=False)
    monkeypatch.setattr(embedding_service, "_collection_has_vectors", lambda p: True)

    class _FakeEmb:
        dim = 1024
        model = "text-embedding-v4"
        model_signature = "qwen:text-embedding-v4:1024"

        async def embed(self, texts):
            return [[0.0] * 1024 for _ in texts]

    monkeypatch.setattr("app.providers.factory.get_embedding_provider", lambda p=None: _FakeEmb())
    out = await check_embedding_health("qwen")
    assert out["rebuildPending"] is True
    assert out["rebuildPendingSince"]
    assert "rebuild_embeddings" in out["driftMessage"]


@pytest.mark.asyncio
async def test_health_reports_guard_even_when_provider_down(fake_redis, monkeypatch):
    """provider 欠费/宕机时，健康检查仍须上报「待重建」。

    否则运维只看到 "embedding 挂了"，不知道重建这件事还欠着——真实场景（百炼欠费）
    恰好就是这样：探测失败 + 待重建，两条信息必须同时可见。
    """
    from app.providers.factory import check_embedding_health

    monkeypatch.setattr(settings, "EMBED_MODEL_GUARD_ENABLE", True, raising=False)
    monkeypatch.setattr(settings, "EMB_PROVIDER", "qwen", raising=False)
    monkeypatch.setattr(settings, "QWEN_EMB_MODEL", "text-embedding-v4", raising=False)
    monkeypatch.setattr(embedding_service, "_collection_has_vectors", lambda p: True)

    class _BrokenEmb:
        dim = 1024
        model = "text-embedding-v4"
        model_signature = "qwen:text-embedding-v4:1024"

        async def embed(self, texts):
            raise RuntimeError("Arrearage: account is not in good standing")

    monkeypatch.setattr("app.providers.factory.get_embedding_provider", lambda p=None: _BrokenEmb())
    out = await check_embedding_health("qwen")
    assert out["status"] == "error"                 # 探测失败照实报
    assert "Arrearage" in out["error"]
    assert out["signature"] == "qwen:text-embedding-v4:1024"
    assert out["rebuildPending"] is True            # 待重建信息仍然可见
    assert "rebuild_embeddings" in out["driftMessage"]


@pytest.mark.asyncio
async def test_health_reports_vector_space_status(fake_redis, monkeypatch):
    """/health/providers 的 embedding 段要带上漂移状态，便于运维一眼看到。"""
    from app.providers.factory import check_embedding_health

    monkeypatch.setattr(settings, "EMBED_MODEL_GUARD_ENABLE", True, raising=False)
    monkeypatch.setattr(settings, "EMB_PROVIDER", "qwen", raising=False)
    monkeypatch.setattr(settings, "QWEN_EMB_MODEL", "text-embedding-v4", raising=False)
    fake_redis.store["embed:model_sig:qwen"] = "qwen:text-embedding-v3:1024"

    class _FakeEmb:
        dim = 1024
        model = "text-embedding-v4"
        model_signature = "qwen:text-embedding-v4:1024"

        async def embed(self, texts):
            return [[0.0] * 1024 for _ in texts]

    monkeypatch.setattr("app.providers.factory.get_embedding_provider", lambda p=None: _FakeEmb())
    out = await check_embedding_health("qwen")
    assert out["status"] == "ok"
    assert out["actualDim"] == 1024
    assert out["vectorSpaceStatus"] == "drift"
    assert "rebuild_embeddings" in out["driftMessage"]
