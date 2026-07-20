"""证据溯源 auto_cite + 配置项单测 + 可核验引用体系（Task 6-10）单测。

- auto_cite：句级引用回填（点1）。
- citation_index.build_index：服务端受控编号 [1..N] → chunk_id（第二层）。
- judge._verify_claims：NLI 三分类（support/contradict/neutral）（第四层）。
- citation_verifier.verify：三层校验引擎（位置/相似度/NLI）（Task 9）。
- qa_service._apply_citation_verification：串联答案+校验+CRAG 联动（Task 10）。
"""
import asyncio

from app.config import settings
from app.rag import citation
from app.rag.citation_index import build_index, chunk_id_of


def _run(coro):
    return asyncio.run(coro)


def test_citation_settings_defaults():
    # 用 _env_file=None 测纯默认值，避免运行时 .env 覆盖污染断言
    from app.config import Settings
    s = Settings(_env_file=None)
    assert s.CITATION_AUTO_ENABLE is True
    assert s.CITATION_SIM_THRESHOLD == 0.6
    # 新开关默认全 opt-in（关闭=现状）
    assert s.CITATION_VERIFIER_ENABLE is False
    assert s.CITATION_NLI_ENABLE is False
    assert s.CITATION_NLI_TIMEOUT == 5
    assert s.CITATION_STRUCTURED_OUTPUT is False
    assert s.CITATION_REWRITE_ON_FAIL is True
    assert s.CITATION_VERIFY_SIM_THRESHOLD == 0.4  # 校验2专用阈值(独立于 auto_cite 补标的 0.6)


def test_auto_cite_all_already_cited(monkeypatch):
    """答案每句已有角标 → 不再补，trace 全支撑。"""
    async def fake_embed(texts, chunk_ids=None):
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    answer = "主变油温应≤85℃[1]。应申请停运[1]。"
    contexts = [{"chunk": "主变油温限值85", "docName": "A"}]
    annotated, trace = _run(citation.auto_cite(answer, contexts, threshold=0.6))
    assert "[1]" in annotated
    assert trace["totalSupported"] == 2
    assert trace["supportRatio"] == 1.0


def test_auto_cite_bare_sentence_matched(monkeypatch):
    """无角标句子 → 补到最相似 chunk。"""
    calls = []

    async def fake_embed(texts, chunk_ids=None):
        calls.append(texts)
        if len(calls) == 1:           # 第一次：chunks
            return [[1.0, 0.0], [0.0, 1.0]]
        return [[0.9, 0.1]]           # bare 句偏向 chunk0

    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    answer = "油温过高需停运。"        # 无角标
    contexts = [{"chunk": "油温限值", "docName": "A"}, {"chunk": "停运流程", "docName": "B"}]
    annotated, trace = _run(citation.auto_cite(answer, contexts, threshold=0.6))
    assert "[1]" in annotated                   # 补到 chunk0 → [1]（标点前）
    assert trace["supportRatio"] == 1.0


def test_auto_cite_below_threshold_not_annotated(monkeypatch):
    """句子与所有 chunk 相似度都低于阈值 → 不补，保留无引用。"""
    calls = []

    async def fake_embed(texts, chunk_ids=None):
        calls.append(texts)
        if len(calls) == 1:
            return [[1.0, 0.0], [0.9, 0.4359]]   # 两 chunk 近乎同向
        return [[0.0, 1.0]]                       # 句子与两 chunk 都低相关

    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    answer = "完全无关的一句话。"
    contexts = [{"chunk": "x", "docName": "A"}, {"chunk": "y", "docName": "B"}]
    annotated, trace = _run(citation.auto_cite(answer, contexts, threshold=0.6))
    assert "[" not in annotated                   # 没补任何角标
    assert trace["totalSupported"] == 0


def test_auto_cite_embed_failure_degrades(monkeypatch):
    """embed 异常 → 降级，返回原答案 + 仅原有角标 trace，不抛。"""
    async def boom(texts):
        raise RuntimeError("embed down")
    monkeypatch.setattr("app.services.embedding_service.embed_texts", boom)
    answer = "有据[1]。无据句。"
    contexts = [{"chunk": "x", "docName": "A"}]
    annotated, trace = _run(citation.auto_cite(answer, contexts, threshold=0.6))
    assert "[1]" in annotated
    assert trace["totalSupported"] == 1           # 只有原本带角标那句
    assert trace["totalSentences"] == 2


def test_auto_cite_empty_contexts():
    """无 contexts → 原样返回，不调 embed。"""
    annotated, trace = _run(citation.auto_cite("某句。", [], threshold=0.6))
    assert "[" not in annotated
    assert trace["totalSupported"] == 0


# ---------- Task 6: citation_index 服务端受控编号 ----------


def test_build_index_maps_position_to_chunk_id():
    """build_index：位置编号 [1..N] → chunk_id，与 prompt [i+1] 对齐。"""
    contexts = [
        {"chunkId": "c1", "chunk": "油温限值", "docName": "A"},
        {"chunkId": "c2", "chunk": "停运流程", "docName": "B"},
    ]
    idx = build_index(contexts)
    assert idx == {1: "c1", 2: "c2"}
    assert idx[1] == "c1"


def test_build_index_empty():
    assert build_index([]) == {}


def test_chunk_id_of_returns_empty_for_out_of_range():
    """越界编号返回空串（供校验1 判非法引用）。"""
    idx = {1: "c1", 2: "c2"}
    assert chunk_id_of(1, idx) == "c1"
    assert chunk_id_of(3, idx) == ""
    assert chunk_id_of(0, idx) == ""


# ---------- Task 7: 结构化 schema + 降级解析 ----------


def test_citation_answer_schema_parse():
    """结构化 JSON 输出 → CitationAnswer。"""
    from app.schemas.citation import CitationAnswer
    raw = {
        "answer_text": "主变油温应≤85℃[1]。",
        "citation_map": [{"sentence": "主变油温应≤85℃", "ref_id": 1, "chunk_id": "c1",
                          "metadata": {"doc_title": "A", "section_path": "3.1", "page_num": 3}}],
        "unverified_claim": [],
    }
    ans = CitationAnswer(**raw)
    assert ans.answer_text.endswith("[1]。")
    assert ans.citation_map[0].ref_id == 1
    assert ans.unverified_claim == []


def test_parse_citation_answer_degrades_on_plain_text():
    """LLM 纯文本输出（无 JSON）→ 降级：answer_text=原文，citation_map 走 evidence_trace 反查。"""
    from app.schemas.citation import parse_citation_answer
    ans = parse_citation_answer("主变油温应≤85℃[1]。", index={1: "c1"})
    assert ans.answer_text == "主变油温应≤85℃[1]。"
    # 降级路径：[1] 反查到 c1
    assert any(c.ref_id == 1 and c.chunk_id == "c1" for c in ans.citation_map)


# ---------- Task 8: judge._verify_claims NLI 三分类 ----------


def test_verify_claims_three_way(monkeypatch):
    """_verify_claims 三分类：support / contradict / neutral。"""
    async def fake_chat(messages, temperature=0, max_tokens=800):
        # 模拟 LLM 返回逐条判定
        return '{"claims":[{"text":"油温限值85度","label":"support"},{"text":"核辐射免责","label":"contradict"},{"text":"背景介绍","label":"neutral"}]}'
    monkeypatch.setattr("app.providers.factory.get_llm_provider",
                        lambda mt: type("P", (), {"chat": staticmethod(fake_chat)})())
    from app.rag import judge
    res = _run(judge._verify_claims(["油温限值85度", "核辐射免责", "背景介绍"], ["资料A"], "deepseek"))
    labels = [r["label"] for r in res]
    assert labels == ["support", "contradict", "neutral"]


def test_judge_hallucination_still_works_after_extract(monkeypatch):
    """_verify_claims 新增后，judge_hallucination 仍可调用且返回结构完整（零回归守护）。

    mock 格式必须匹配 judge_hallucination 内部期望（supported + counts），非 _verify_claims 的 label 格式。
    """
    async def fake_chat(messages, temperature=0, max_tokens=800):
        return '{"claims":[{"text":"油温限值85","supported":true}],"supported_count":1,"total_count":1}'
    monkeypatch.setattr("app.providers.factory.get_llm_provider",
                        lambda mt: type("P", (), {"chat": staticmethod(fake_chat)})())
    from app.rag import judge
    res = _run(judge.judge_hallucination("油温限值85", ["资料A"], "deepseek"))
    assert res["supported_ratio"] == 1.0  # 1/1 support
    assert "hallucination" in res


# ---------- Task 9: citation_verifier 三层校验引擎 ----------


def test_verify_check1_drops_out_of_range_ref(monkeypatch):
    """校验1：ref_id 越界（不在 index）→ drop。"""
    async def fake_embed(texts, chunk_ids=None):  # 校验2 放行（同向量 → cosine=1）
        return [[1.0] for _ in texts]
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    from app.rag.citation_verifier import verify
    from app.schemas.citation import CitationItem
    cmap = [CitationItem(sentence="s1", ref_id=1, chunk_id="c1"),
            CitationItem(sentence="s2", ref_id=99, chunk_id="")]  # 99 越界
    res = _run(verify("s1[1] s2[99]", cmap, {1: "c1"}, [{"chunkId": "c1", "chunk": "x"}], "deepseek",
                      nli_enable=False))
    assert 99 in res.dropped_refs
    keep = [i for i in res.items if i.action == "keep"]
    assert any(i.ref_id == 1 for i in keep)


def test_verify_check2_drops_low_similarity(monkeypatch):
    """校验2：句 vs chunk cosine < 0.6 → drop。"""
    # 造差异化：句 "完全无关句" → [0,1]，chunk "x" → [1,0]（正交，cosine=0）
    async def fake_embed2(texts, chunk_ids=None):
        return [[0.0, 1.0] if "无关" in t else [1.0, 0.0] for t in texts]
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed2)
    from app.rag.citation_verifier import verify
    from app.schemas.citation import CitationItem
    cmap = [CitationItem(sentence="完全无关句", ref_id=1, chunk_id="c1")]
    res = _run(verify("完全无关句[1]", cmap, {1: "c1"}, [{"chunkId": "c1", "chunk": "x"}], "deepseek",
                      nli_enable=False))
    assert res.items[0].action == "drop"


def test_verify_nli_contradict_drops(monkeypatch):
    """校验3：NLI 判 contradict → drop。"""
    async def fake_verify(claims, sources, model_type=None):
        return [{"text": c, "label": "contradict"} for c in claims]
    monkeypatch.setattr("app.rag.judge._verify_claims", fake_verify)
    async def fake_embed(texts, chunk_ids=None):  # 校验2 放行
        return [[1.0] for _ in texts]
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    from app.rag.citation_verifier import verify
    from app.schemas.citation import CitationItem
    cmap = [CitationItem(sentence="核辐射免责", ref_id=1, chunk_id="c1")]
    res = _run(verify("核辐射免责[1]", cmap, {1: "c1"}, [{"chunkId": "c1", "chunk": "核辐射不在保障范围"}], "deepseek",
                      nli_enable=True))
    assert res.items[0].nli_label == "contradict"
    assert res.items[0].action == "drop"
    assert 1 in res.dropped_refs


# ---------- Task 10: qa_service._apply_citation_verification 串联 ----------


def test_apply_citation_verification_disabled_returns_empty(monkeypatch):
    """CITATION_VERIFIER_ENABLE=False → (ans, {})，零字段、零破坏。"""
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "CITATION_VERIFIER_ENABLE", False)
    from app.services.qa_service import _apply_citation_verification
    ans, extras = _run(_apply_citation_verification(
        "主变油温应≤85℃[1]。", [{"chunkId": "c1", "chunk": "油温85", "docName": "A"}], "deepseek"))
    assert ans == "主变油温应≤85℃[1]。"
    assert extras == {}


def test_apply_citation_verification_enabled_includes_fields(monkeypatch):
    """CITATION_VERIFIER_ENABLE=True → extras 含 citationVerified/citationIndex/citationMap。"""
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "CITATION_VERIFIER_ENABLE", True)
    monkeypatch.setattr(cfg.settings, "CITATION_NLI_ENABLE", False)
    # mock verify：返回 keep（校验放行，不打真实 NLI/embed）
    async def fake_verify(answer_text, cmap, index, contexts, mt, *, nli_enable=None):
        from app.schemas.citation import VerifyItem, VerifyResult
        return VerifyResult(items=[VerifyItem(ref_id=1, chunk_id="c1", valid=True,
                                              nli_label="unknown", action="keep")])
    monkeypatch.setattr("app.rag.citation_verifier.verify", fake_verify)
    from app.services.qa_service import _apply_citation_verification
    ans, extras = _run(_apply_citation_verification(
        "主变油温应≤85℃[1]。", [{"chunkId": "c1", "chunk": "主变油温不超过85度", "docName": "A"}], "deepseek"))
    assert "citationVerified" in extras
    assert "citationIndex" in extras
    assert extras["citationIndex"] == {1: "c1"}            # build_index 位置→chunk_id
    assert ans == "主变油温应≤85℃[1]。"                       # 无 drop，答案不变


# ---------- C1: NLI 异步后置（CITATION_NLI_ASYNC_ENABLE）----------


def test_apply_citation_nli_async_skips_sync_and_schedules_backfill(monkeypatch):
    """C1：ASYNC 开+NLI 开 → verify 同步路径 nli_enable=False（NLI 不阻塞首答），并派发后台回填。"""
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "CITATION_VERIFIER_ENABLE", True)
    monkeypatch.setattr(cfg.settings, "CITATION_NLI_ENABLE", True)
    monkeypatch.setattr(cfg.settings, "CITATION_NLI_ASYNC_ENABLE", True)
    seen = {}

    async def fake_verify(answer_text, cmap, index, contexts, mt, *, nli_enable=None):
        seen["nli_enable"] = nli_enable
        from app.schemas.citation import VerifyItem, VerifyResult
        return VerifyResult(items=[VerifyItem(ref_id=1, chunk_id="c1", valid=True,
                                              nli_label="unknown", action="keep")])
    monkeypatch.setattr("app.rag.citation_verifier.verify", fake_verify)
    scheduled = []
    monkeypatch.setattr("app.services.qa_service._schedule_nli_backfill",
                        lambda *a, **kw: scheduled.append(a))
    from app.services.qa_service import _apply_citation_verification
    _run(_apply_citation_verification(
        "油温限值[1]。", [{"chunkId": "c1", "chunk": "油温不超过85", "docName": "A"}], "deepseek",
        query="油温限值", tenant="t1"))
    assert seen["nli_enable"] is False          # 同步路径不跑 NLI（不阻塞首答）
    assert len(scheduled) == 1                  # 派发后台 NLI 回填
    assert scheduled[0][3] == "油温限值"          # query 透传（回写缓存 key 用）


def test_apply_citation_nli_sync_when_async_disabled(monkeypatch):
    """C1：ASYNC 关+NLI 开 → verify 同步跑 NLI（=现状），不派发后台 task。"""
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "CITATION_VERIFIER_ENABLE", True)
    monkeypatch.setattr(cfg.settings, "CITATION_NLI_ENABLE", True)
    monkeypatch.setattr(cfg.settings, "CITATION_NLI_ASYNC_ENABLE", False)
    seen = {}

    async def fake_verify(answer_text, cmap, index, contexts, mt, *, nli_enable=None):
        seen["nli_enable"] = nli_enable
        from app.schemas.citation import VerifyItem, VerifyResult
        return VerifyResult(items=[VerifyItem(ref_id=1, chunk_id="c1", valid=True, action="keep")])
    monkeypatch.setattr("app.rag.citation_verifier.verify", fake_verify)
    scheduled = []
    monkeypatch.setattr("app.services.qa_service._schedule_nli_backfill",
                        lambda *a, **kw: scheduled.append(a))
    from app.services.qa_service import _apply_citation_verification
    _run(_apply_citation_verification(
        "油温[1]。", [{"chunkId": "c1", "chunk": "油温", "docName": "A"}], "deepseek"))
    assert seen["nli_enable"] is True           # 同步现状
    assert scheduled == []                      # 不派发后台


def test_nli_backfill_writes_nli_label_to_cache(monkeypatch):
    """C1：_nli_backfill 跑 NLI 后回写缓存 citationVerified.items 的 nli_label。"""
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "CITATION_NLI_TIMEOUT", 5)
    from app.schemas.citation import CitationItem, VerifyItem, VerifyResult
    sync_verdict = VerifyResult(items=[VerifyItem(ref_id=1, chunk_id="c1", valid=True,
                                                  nli_label="unknown", action="keep")])
    cmap = [CitationItem(sentence="油温限值85度", ref_id=1, chunk_id="c1")]
    contexts = [{"chunkId": "c1", "chunk": "油温不超过85度"}]

    async def fake_nli(claims, sources, model_type=None):
        return [{"text": c, "label": "contradict"} for c in claims]
    monkeypatch.setattr("app.rag.judge._verify_claims", fake_nli)

    from app.services.qa_service import _nli_backfill, _cache_key
    ck = _cache_key("deepseek", "油温限值", "t1")
    cache_store = {ck: {"answer": "x", "citationVerified": {
        "items": [{"ref_id": 1, "chunk_id": "c1", "valid": True, "nli_label": "unknown", "action": "keep"}],
        "dropped_refs": [], "unverified_additions": [], "degraded": False, "rewrite_needed": False,
    }}}

    async def fake_get(key): return cache_store.get(key)
    written = {}
    async def fake_set(key, data, ttl): written[key] = data
    monkeypatch.setattr("app.services.qa_service.redis_client.cache_get_json", fake_get)
    monkeypatch.setattr("app.services.qa_service.redis_client.cache_set_json", fake_set)

    _run(_nli_backfill("deepseek", cmap, contexts, "油温限值", "t1", sync_verdict))
    assert ck in written
    items = written[ck]["citationVerified"]["items"]
    assert items[0]["nli_label"] == "contradict"          # NLI 结果回写
    assert written[ck]["citationVerified"]["nli_async_done"] is True


def test_nli_backfill_skips_write_when_cache_miss(monkeypatch):
    """C1：_nli_backfill 缓存 miss → NLI 仍跑（不因 miss 跳过），但不回写（尽力而为）。"""
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "CITATION_NLI_TIMEOUT", 5)
    from app.schemas.citation import CitationItem, VerifyItem, VerifyResult
    sync_verdict = VerifyResult(items=[VerifyItem(ref_id=1, chunk_id="c1", valid=True, action="keep")])
    cmap = [CitationItem(sentence="油温", ref_id=1, chunk_id="c1")]
    called = {"nli": False}

    async def fake_nli(claims, sources, model_type=None):
        called["nli"] = True
        return [{"text": c, "label": "support"} for c in claims]
    monkeypatch.setattr("app.rag.judge._verify_claims", fake_nli)
    async def fake_get(key): return None
    written = {}
    async def fake_set(key, data, ttl): written[key] = data
    monkeypatch.setattr("app.services.qa_service.redis_client.cache_get_json", fake_get)
    monkeypatch.setattr("app.services.qa_service.redis_client.cache_set_json", fake_set)
    from app.services.qa_service import _nli_backfill
    _run(_nli_backfill("deepseek", cmap, [{"chunkId": "c1", "chunk": "油温"}], "油温", "t1", sync_verdict))
    assert called["nli"] is True                  # NLI 仍执行
    assert written == {}                          # 缓存 miss 不回写


# ---------- C3: stream_answer done 接校验（CITATION_VERIFIER_ENABLE）----------


def _patch_stream_externals(monkeypatch):
    """stream_answer LLM 路径外部依赖最小替身（单轮、high 置信走缓存写）。"""
    import app.config as cfg
    for k in ("CACHE_PERSIST_ENABLE", "KG_RAG_ENABLE", "ROUTING_ENABLE",
              "SEMANTIC_CACHE_ENABLE", "SELF_RAG_ENABLE", "EVIDENCE_GAP_AUTO_COLLECT",
              "MULTI_TURN_CACHE_ENABLE", "CITATION_STRUCTURED_OUTPUT"):
        monkeypatch.setattr(cfg.settings, k, False)

    async def fake_sq(db, query, nq, cid, history, mt): return nq
    monkeypatch.setattr("app.services.qa_service._search_query_for_retrieve", fake_sq)
    async def fake_bl(nq): return False
    monkeypatch.setattr("app.services.qa_service._is_blacklisted", fake_bl)
    async def fake_get_cache(key): return None
    monkeypatch.setattr("app.services.qa_service.redis_client.cache_get_json", fake_get_cache)
    async def fake_set_cache(*a, **kw): return None
    monkeypatch.setattr("app.services.qa_service.redis_client.cache_set_json", fake_set_cache)
    async def fake_cc(*a, **kw): return type("Conv", (), {"id": "conv1"})()
    monkeypatch.setattr("app.services.qa_service.conversation_service.create_conversation", fake_cc)
    async def fake_sm(*a, **kw): return None
    monkeypatch.setattr("app.services.qa_service.conversation_service.save_message", fake_sm)


def test_stream_done_includes_citation_when_enabled(monkeypatch):
    """C3：CITATION_VERIFIER_ENABLE 开 → stream done 含 citationVerified（前端零改动，不读该字段）。"""
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "CITATION_VERIFIER_ENABLE", True)
    monkeypatch.setattr(cfg.settings, "CITATION_AUTO_ENABLE", False)   # 跳过 auto_cite
    _patch_stream_externals(monkeypatch)

    async def fake_apply(ans, contexts, mt, *, db=None, cmap_override=None, query=None, tenant="default"):
        return ans, {"citationVerified": {"items": [{"ref_id": 1, "valid": True}], "dropped_refs": []}}
    monkeypatch.setattr("app.services.qa_service._apply_citation_verification", fake_apply)

    ctx = [{"chunkId": "c1", "chunk": "油温不超过85度", "docName": "A", "score": 0.9}]
    async def fake_mixed(*a, **kw): return ctx
    monkeypatch.setattr("app.services.qa_service.retrieval_service.mixed_search", fake_mixed)
    async def fake_crag(*a, **kw): return (ctx, "high", "none", "good")
    monkeypatch.setattr("app.services.qa_service._crag_correct", fake_crag)

    async def fake_stream(messages, temperature=0.5):
        yield "油温限值[1]。"
    monkeypatch.setattr("app.services.qa_service.get_llm_provider",
                        lambda mt: type("P", (), {"stream": staticmethod(fake_stream)})())

    from app.services.qa_service import stream_answer
    events = []

    async def collect():
        async for ev in stream_answer(None, "油温限值", "deepseek", conversation_id=None,
                                      username="u", tenant="t1"):
            events.append(ev)
    _run(collect())
    done = [e for e in events if e.get("type") == "done"]
    assert done and "citationVerified" in done[0]
    assert done[0]["citationVerified"]["items"][0]["ref_id"] == 1


def test_stream_done_no_citation_when_disabled(monkeypatch):
    """C3：CITATION_VERIFIER_ENABLE 关 → 不调校验，done 不含 citationVerified（=现状）。"""
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "CITATION_VERIFIER_ENABLE", False)
    monkeypatch.setattr(cfg.settings, "CITATION_AUTO_ENABLE", False)
    _patch_stream_externals(monkeypatch)
    called = {"apply": False}

    async def fake_apply(*a, **kw):
        called["apply"] = True
        return a[0], {}
    monkeypatch.setattr("app.services.qa_service._apply_citation_verification", fake_apply)

    ctx = [{"chunkId": "c1", "chunk": "油温", "docName": "A", "score": 0.9}]
    async def fake_mixed(*a, **kw): return ctx
    monkeypatch.setattr("app.services.qa_service.retrieval_service.mixed_search", fake_mixed)
    async def fake_crag(*a, **kw): return (ctx, "high", "none", "good")
    monkeypatch.setattr("app.services.qa_service._crag_correct", fake_crag)
    async def fake_stream(messages, temperature=0.5):
        yield "油温限值。"
    monkeypatch.setattr("app.services.qa_service.get_llm_provider",
                        lambda mt: type("P", (), {"stream": staticmethod(fake_stream)})())

    from app.services.qa_service import stream_answer
    events = []

    async def collect():
        async for ev in stream_answer(None, "油温", "deepseek", conversation_id=None,
                                      username="u", tenant="t1"):
            events.append(ev)
    _run(collect())
    assert called["apply"] is False               # 关时不调校验
    done = [e for e in events if e.get("type") == "done"]
    assert done and "citationVerified" not in done[0]
