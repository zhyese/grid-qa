"""数据飞轮·A5 cv 治理版本段 + semantic_cache 查治理 单测。

① citation_cache_version() 含 G 段（qa:gov_gen Redis 计数器，治理事件 bump）；
② semantic_cache_get 命中 blocked doc 的答案 → 降级 miss。
开关 SEMANTIC_CACHE_GOV_FILTER_ENABLE 默认关（关=现状不过滤）。
"""
import asyncio


def _run(coro):
    return asyncio.run(coro)


def test_a5_cv_has_g_segment(monkeypatch):
    """citation_cache_version() 输出形如 cvXXX{G}（G 段=进程内存镜像）。"""
    import app.config as cfg
    cfg._gov_generation = 0  # 测试隔离：先清零（防其他用例 bump 累积）
    # 模拟 governance_propagate_service bump 7 次
    for _ in range(7):
        cfg.bump_gov_generation_inproc()
    try:
        cv = cfg.citation_cache_version()
        # 末段含 G=7（前 3 段是 V/S/N 各 0/1）
        assert cv.startswith("cv")
        assert cv.endswith("7")  # G=7
        # "cv" + 3 开关位 + 至少 1 G 位
        assert len(cv) >= 5
    finally:
        # 测试隔离：重置进程内存
        cfg._gov_generation = 0


def test_a5_cv_g_default_zero():
    """进程内存计数默认 0（无 bump 时）。"""
    import app.config as cfg
    cfg._gov_generation = 0
    cv = cfg.citation_cache_version()
    assert cv.endswith("0")


def test_a5_semantic_get_filters_blocked_doc(monkeypatch):
    """命中候选的 retrievalSource 含 blocked doc → 降级 miss。"""
    import app.rag.semantic_cache as sc
    import app.config as cfg
    import app.services.knowledge_governance_service as gov

    monkeypatch.setattr(cfg.settings, "SEMANTIC_CACHE_ENABLE", True)
    monkeypatch.setattr(cfg.settings, "SEMANTIC_CACHE_GOV_FILTER_ENABLE", True)

    # 嵌入/索引前置：让 semantic_cache_get 直接进入命中分支
    async def fake_embed(q):
        return [0.1, 0.2]
    monkeypatch.setattr(sc.embedding_service, "embed_query", fake_embed)

    cached_data = {
        "answer": "旧答案",
        "retrievalSource": [{"docId": "d-blocked", "docName": "旧.pdf"}],
    }
    index = [{"query": "主变油温", "model_type": "default", "emb": "fake",
              "cache_key": "qa:default:default:主变油温:cv000", "ts": 0}]

    call_count = {"i": 0}

    async def fake_get_json(key):
        call_count["i"] += 1
        # 第1次：exact key 命中（return cached_data）；第2次：semantic 索引；第3次：semantic 命中 candidate
        if call_count["i"] == 1:
            return None  # exact miss
        if call_count["i"] == 2:
            return index  # 返回索引
        return cached_data  # 候选 cache_key 命中
    monkeypatch.setattr(sc.redis_client, "cache_get_json", fake_get_json)

    # mock _b64_to_emb 返回合理向量 + 让余弦 > MEDIUM
    import numpy as np
    monkeypatch.setattr(sc, "_b64_to_emb",
                        lambda s: np.array([0.1, 0.2], dtype=np.float32))

    # blocked_document_ids 返回 {d-blocked}
    async def fake_blocked(db, ids, *, tenant_id=None, now=None):
        return {"d-blocked"}
    monkeypatch.setattr(gov, "blocked_document_ids", fake_blocked)

    data, hit, sim = _run(sc.semantic_cache_get(None, "主变油温", tenant_id="default"))
    assert data is None
    assert hit == "miss"


def test_a5_semantic_get_passes_when_filter_disabled(monkeypatch):
    """SEMANTIC_CACHE_GOV_FILTER_ENABLE=False（默认）→ 不过滤，命中候选即返回。"""
    import app.rag.semantic_cache as sc
    import app.config as cfg

    monkeypatch.setattr(cfg.settings, "SEMANTIC_CACHE_ENABLE", True)
    monkeypatch.setattr(cfg.settings, "SEMANTIC_CACHE_GOV_FILTER_ENABLE", False)

    cached_data = {"answer": "答案", "retrievalSource": [{"docId": "d-x"}]}

    async def fake_embed(q):
        return [0.1, 0.2]
    monkeypatch.setattr(sc.embedding_service, "embed_query", fake_embed)

    call_count = {"i": 0}

    async def fake_get_json(key):
        call_count["i"] += 1
        if call_count["i"] == 1:
            return None
        if call_count["i"] == 2:
            return [{"query": "q", "model_type": "default", "emb": "x",
                     "cache_key": "k1", "ts": 0}]
        return cached_data
    monkeypatch.setattr(sc.redis_client, "cache_get_json", fake_get_json)

    import numpy as np
    monkeypatch.setattr(sc, "_b64_to_emb",
                        lambda s: np.array([0.1, 0.2], dtype=np.float32))

    data, hit, sim = _run(sc.semantic_cache_get(None, "主变油温", tenant_id="default"))
    assert data is not None  # 未过滤，命中
    assert hit in ("semantic_high", "semantic_medium")
