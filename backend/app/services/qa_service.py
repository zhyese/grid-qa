"""RAG 问答编排：热点缓存 / 多轮指代消解 / 检索 / CRAG自纠错 / prompt / LLM / 后处理 / 相关问题推荐。"""
import json
import re
import time
import asyncio

_bg_tasks: set = set()  # 持有后台 task 引用，防 GC

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import redis_client
from app.config import settings
from app.core import safety
from app.core.obs import degraded
from app.providers.factory import get_llm_provider
from app.rag import citation, prompt_templates
from app.services import config_service, conversation_service, kg_service, retrieval_service, term_service

_HISTORY_LIMIT = 6  # 拼接最近 3 轮（6 条消息）


def _cache_key(model_type: str | None, query: str) -> str:
    return f"qa:{model_type or 'default'}:{query}"


async def _is_blacklisted(nq: str) -> bool:
    """缓存黑名单检查（高频坏答案禁缓存命中，由反馈驱动 auto_tune_cache_ttl 写入 Redis set）。"""
    try:
        from app.services.feedback_optimizer_service import is_query_blacklisted
        return await is_query_blacklisted(nq)
    except Exception:
        return False


async def _crag_correct(
    db: AsyncSession, nq: str, contexts: list[dict],
    model_type: str | None, topk: int, tenant: str = "default",
) -> tuple[list[dict], str, str, str]:
    """CRAG 分级 + 纠错闭环。返回 (contexts, confidence, action, grade)。

    分级：CRAG v2（LLM 逐条评估证据）优先，未启用/失败回退 v1（rerank top1 分数）。
    incorrect → query 改写重检索 → 仍 incorrect → refused 保守拒答。
    contexts 可能被纠错重检索替换。
    """
    confidence, action, grade = "high", "normal", ""
    if not settings.CRAG_ENABLE:
        return contexts, confidence, action, grade
    from app.rag import crag

    rerank_ok = settings.RERANK_ENABLE
    # 分级：v2 优先，失败回退 v1
    grade = ""
    if settings.CRAG_PERDOC_ENABLE:
        from app.rag import crag_v2
        try:
            grade, _ = await crag_v2.grade_with_llm(nq, contexts, model_type)
        except Exception as e:
            degraded("crag_v2", e)
    if not grade:
        top1 = float(contexts[0].get("score", 0.0)) if contexts else 0.0
        grade, _ = crag.grade(top1, len(contexts), rerank_ok)

    if grade == crag.GRADE_INCORRECT:
        try:
            from app.services.query_rewrite import rewrite_query
            new_q = await rewrite_query(nq, model_type, force=True)
            if new_q and new_q != nq:
                new_ctx = await retrieval_service.mixed_search(db, new_q, topk, tenant=tenant)
                if new_ctx:
                    contexts = new_ctx
                    top1 = float(contexts[0].get("score", 0.0))
                    grade, _ = crag.grade(top1, len(contexts), rerank_ok)
                    action = "rewritten"
        except Exception as e:
            degraded("crag_rewrite", e)

    confidence = crag.confidence_of(grade, action == "rewritten")
    if grade == crag.GRADE_INCORRECT and action == "rewritten":
        action = "refused"
    try:
        from app.core import metrics
        metrics.CRAG_GRADE.labels(grade).inc()
        metrics.CRAG_ACTION.labels(action).inc()
    except Exception:
        pass
    return contexts, confidence, action, grade


async def _search_query_for_retrieve(
    db: AsyncSession, query: str, nq: str, conversation_id: str | None,
    history: list[dict], model_type: str | None,
) -> str:
    """多轮指代消解：把追问改写成带上下文的独立查询用于检索（S7）。

    单轮/关闭/失败返回 nq（原归一化 query）。改写仅影响检索，不影响给 LLM 的原问题。
    """
    if not conversation_id or not history:
        return nq
    if not getattr(settings, "STANDALONE_REWRITE_ENABLE", False):
        return nq
    from app.services import standalone_query
    try:
        rewritten = await standalone_query.rewrite_standalone(query, history, model_type)
        return term_service.normalize(rewritten) if rewritten else nq
    except Exception as e:
        degraded("standalone_dispatch", e)
        return nq


async def answer(
    db: AsyncSession, query: str, model_type: str | None = None,
    topk: int = 5, conversation_id: str | None = None, username: str = "",
    tenant: str = "default",
) -> dict:
    t0 = time.time()
    nq = term_service.normalize(query)
    safety.guard_query(query)  # 入站 prompt injection 告警（D4）
    is_single = not conversation_id  # 仅单轮查/写缓存（多轮上下文变化不缓存）

    # Self-RAG：非运维问题跳过检索直接拒答（省成本+防污染，SELF_RAG_ENABLE 默认关）
    if settings.SELF_RAG_ENABLE:
        from app.services import self_rag as self_rag_svc
        if not await self_rag_svc.need_retrieve(query, model_type):
            return {
                "answer": self_rag_svc.SKIP_ANSWER, "retrievalSource": [],
                "responseTime": round(time.time() - t0, 3), "hallucinationRate": 0.0,
                "cached": False, "conversationId": conversation_id or "",
                "confidence": "refused", "cragAction": "self_rag_skip",
            }

    # 多轮不走缓存（上下文变化）；单轮走三级缓存：Redis(L1) → 语义缓存(L1.5) → MySQL(L2) → LLM(L3)
    cache_layer = "llm"  # 默认走 LLM
    if is_single and not await _is_blacklisted(nq):
        # L1: Redis 热点缓存（精确 key 匹配）
        try:
            cached = await redis_client.cache_get_json(_cache_key(model_type, nq))
        except Exception as e:
            degraded("qa_cache_get", e)
            cached = None
        if cached:
            cached["cached"] = True
            cached["cacheLayer"] = "redis"
            cached["responseTime"] = round(time.time() - t0, 3)
            try:
                from app.core import metrics
                metrics.QA_TOTAL.labels(model_type or settings.LLM_PROVIDER, "true").inc()
                metrics.cache_hit_inc("redis")
            except Exception:
                pass
            return cached

        # L2: MySQL 二级缓存（精确持久，Redis 过期/evict 时兜底；优先于模糊语义匹配）
        if settings.CACHE_PERSIST_ENABLE:
            try:
                from app.services.cache_persist import cache_get_mysql
                mysql_cached = await cache_get_mysql(db, model_type, nq)
                if mysql_cached:
                    mysql_cached["cached"] = True
                    mysql_cached["cacheLayer"] = "mysql"
                    mysql_cached["responseTime"] = round(time.time() - t0, 3)
                    try:
                        from app.core import metrics
                        metrics.QA_TOTAL.labels(model_type or settings.LLM_PROVIDER, "true").inc()
                        metrics.cache_hit_inc("mysql")
                    except Exception:
                        pass
                    return mysql_cached
            except Exception as e:
                degraded("qa_cache_mysql", e)

        # L1.5: 语义缓存（模糊相似，精确持久 miss 后兜底）
        if getattr(settings, "SEMANTIC_CACHE_ENABLE", False):
            try:
                from app.rag.semantic_cache import semantic_cache_get
                sc_data, sc_type, sc_sim = await semantic_cache_get(model_type, nq)
                if sc_data and sc_type in ("semantic_high", "semantic_medium"):
                    sc_data["cached"] = True
                    sc_data["cacheLayer"] = sc_type
                    sc_data["semanticSimilarity"] = round(sc_sim, 4)
                    sc_data["responseTime"] = round(time.time() - t0, 3)
                    try:
                        from app.core import metrics
                        metrics.QA_TOTAL.labels(model_type or settings.LLM_PROVIDER, "true").inc()
                        metrics.cache_hit_inc("semantic")
                    except Exception:
                        pass
                    return sc_data
            except Exception as e:
                degraded("semantic_cache_get", e)

    # 多轮历史（提前获取：供指代消解 + 拼 LLM 上下文，避免重复查）
    history: list[dict] = []
    if conversation_id:
        history = await conversation_service.get_messages(db, conversation_id, _HISTORY_LIMIT)
    # 多轮指代消解：检索用改写后的独立查询
    search_q = await _search_query_for_retrieve(db, query, nq, conversation_id, history, model_type)

    # 多轮且 query 完整(standalone 未改写 search_q==nq) → 也查 Redis 热点缓存
    # 场景：用户点推荐问题(完整 query)接续对话，答案不依赖上下文，可安全命中
    if conversation_id and search_q == nq and not await _is_blacklisted(nq):
        try:
            cached = await redis_client.cache_get_json(_cache_key(model_type, nq))
            if cached:
                cached["cached"] = True
                cached["cacheLayer"] = "redis"
                cached["responseTime"] = round(time.time() - t0, 3)
                try:
                    from app.core import metrics
                    metrics.QA_TOTAL.labels(model_type or settings.LLM_PROVIDER, "true").inc()
                    metrics.cache_hit_inc("redis")
                except Exception:
                    pass
                return cached
        except Exception as e:
            degraded("qa_cache_get_multi", e)

    # 智能路由：根据查询特征选择最优检索路径（Phase A）
    routing = None
    if settings.ROUTING_ENABLE:
        try:
            from app.routing.routing_service import route_query
            routing = route_query(search_q)
        except Exception as e:
            degraded("routing_dispatch", e)

    contexts = await retrieval_service.mixed_search(
        db, search_q, topk, tenant=tenant, routing_decision=routing,
    )
    if not contexts:
        return {
            "answer": "根据现有资料无法确认该问题，请先上传并解析相关运维文档后重试。",
            "retrievalSource": [], "responseTime": round(time.time() - t0, 3),
            "hallucinationRate": 0.0, "cached": False, "conversationId": conversation_id or "",
        }

    # Corrective RAG：分级 + 纠错闭环
    contexts, confidence, crag_action, crag_grade = await _crag_correct(
        db, nq, contexts, model_type, topk, tenant
    )

    # GraphRAG：融合知识图谱结构化上下文（KG_RAG_ENABLE 默认开）
    graph: list[str] = []
    if settings.KG_RAG_ENABLE:
        try:
            graph = await kg_service.graph_context(nq)
        except Exception as e:
            degraded("kg_graph_context", e)
            graph = []
    messages = prompt_templates.build_messages_with_history(nq, contexts, history, graph, confidence)
    _llm0 = time.time()
    ans = await get_llm_provider(model_type).chat(messages, temperature=config_service.rt_temperature())
    ans = safety.safe_answer(ans)  # 答案脱敏（PII_MASK_ENABLE 开启时，D4）
    # 证据溯源：无角标句子向量相似度自动补标（激活 citation 死代码进主链路）
    if getattr(settings, "CITATION_AUTO_ENABLE", True):
        ans, _trace = await citation.auto_cite(ans, contexts)
    else:
        _trace = citation.evidence_trace(ans)
    try:
        from app.core import metrics
        _p = model_type or settings.LLM_PROVIDER
        metrics.LLM_CALLS.labels(_p).inc()
        metrics.LLM_LATENCY.labels(_p).observe(time.time() - _llm0)
    except Exception:
        pass

    # 持久化对话
    if not conversation_id:
        conv = await conversation_service.create_conversation(db, username, query)
        conversation_id = conv.id
    await conversation_service.save_message(db, conversation_id, "user", query)
    await conversation_service.save_message(db, conversation_id, "assistant", ans)

    _halluc = citation.estimate_hallucination(ans, len(contexts))
    try:
        from app.core import metrics
        metrics.HALLUC.observe(_halluc)
    except Exception:
        pass
    result = {
        "answer": ans,
        "retrievalSource": [{
            "docId": c.get("docId", ""), "docName": c.get("docName", ""),
            "docType": c.get("docType", ""), "chunkIdx": c.get("chunkIdx"),
            "chunk": c.get("chunk", ""), "score": c.get("score", 0.0),
            "sources": c.get("sources", []),
        } for c in contexts],
        "evidenceTrace": _trace,
        "graphCount": len(graph),
        "highRisk": safety.extract_high_risk(ans),
        "confidence": confidence,
        "cragAction": crag_action,
        "cragGrade": crag_grade,
        "responseTime": round(time.time() - t0, 3),
        "hallucinationRate": _halluc,
        "cached": False,
        "cacheLayer": "llm",
        "route": routing.route if routing else "hybrid",
        "routeReason": routing.reason if routing else "",
        "conversationId": conversation_id,
    }

    # 单轮 或 多轮 且 高置信(confidence==high) 才写；黑名单/证据有限/不足不写
    # 注：多轮指代问题(search_q!=nq)会写但读时按 search_q==nq 过滤(避免跨对话脏命中)
    if (is_single or conversation_id) and confidence == "high" and not await _is_blacklisted(nq):
        # L2: MySQL 持久化（先写，保证数据不丢）
        if settings.CACHE_PERSIST_ENABLE:
            try:
                from app.services.cache_persist import cache_set_mysql
                await cache_set_mysql(db, model_type, nq, query, result)
            except Exception as e:
                degraded("qa_cache_mysql_set", e)
        # L1: Redis 热点（后写，MySQL 已成功）
        try:
            await redis_client.cache_set_json(_cache_key(model_type, nq), result, settings.QA_CACHE_TTL)
        except Exception as e:
            degraded("qa_cache_set", e)
        # L1.5: 语义缓存索引（异步，不阻塞）
        if getattr(settings, "SEMANTIC_CACHE_ENABLE", False):
            try:
                from app.rag.semantic_cache import semantic_cache_set
                await semantic_cache_set(model_type, nq, _cache_key(model_type, nq))
            except Exception as e:
                degraded("semantic_cache_set", e)
    try:
        from app.core import metrics
        metrics.QA_TOTAL.labels(model_type or settings.LLM_PROVIDER, "false").inc()
        metrics.cache_hit_inc("llm")
    except Exception:
        pass
    # 成本追踪（记录 token 用量 → 成本报告数据来源；估算 input/output token）
    try:
        import asyncio
        from app.services.cost_tracker_service import record_token_usage
        asyncio.ensure_future(record_token_usage(db, username, tenant, model_type or settings.LLM_PROVIDER, len(str(messages)) // 2, len(ans) // 2))
    except Exception:
        pass
    # 在线质量评测采样（异步跑 LLM Judge，不阻塞响应；评测趋势数据来源）
    try:
        import asyncio
        from app.services.online_eval_service import should_sample, eval_quality
        if should_sample():
            asyncio.ensure_future(eval_quality(db, query, ans, contexts, model_type))
    except Exception:
        pass
    # 证据补全：medium/refused 自动收集（bg task，独立 session，不阻塞响应）
    if settings.EVIDENCE_GAP_AUTO_COLLECT and confidence in ("medium", "refused"):
        try:
            from app.services import evidence_gap_service
            _bg_tasks.add(asyncio.create_task(evidence_gap_service.collect(
                nq, ans, confidence, crag_grade, crag_action, "auto", tenant,
            )))
        except Exception:
            pass
    return result


async def stream_answer(
    db: AsyncSession, query: str, model_type: str | None = None,
    topk: int = 5, conversation_id: str | None = None, username: str = "",
    tenant: str = "default", regen: bool = False,
):
    """流式问答：单轮查热点缓存(命中则快流不调LLM) → 否则 meta/token/done 三段。"""
    t0 = time.time()
    nq = term_service.normalize(query)
    _p = model_type or settings.LLM_PROVIDER
    safety.guard_query(query)  # 入站 prompt injection 告警（D4）
    is_single = not conversation_id  # 仅单轮查/写缓存（多轮上下文变化不缓存）

    # Self-RAG：非运维问题跳过检索直接拒答
    if settings.SELF_RAG_ENABLE:
        from app.services import self_rag as self_rag_svc
        if not await self_rag_svc.need_retrieve(query, model_type):
            yield {"type": "meta", "sources": [], "conversationId": conversation_id or ""}
            yield {"type": "token", "content": self_rag_svc.SKIP_ANSWER}
            yield {"type": "done", "responseTime": round(time.time() - t0, 3),
                   "confidence": "refused", "cragAction": "self_rag_skip",
                   "conversationId": conversation_id or "", "cached": False}
            return

    # 0) 单轮三级缓存：Redis(L1) → MySQL(L2) → LLM(L3)
    #    regen=True（重新生成）跳过缓存读，强制重走 LLM
    cache_layer = "llm"
    if is_single and not regen and not await _is_blacklisted(nq):
        # L1: Redis 热点
        try:
            cached = await redis_client.cache_get_json(_cache_key(model_type, nq))
        except Exception as e:
            degraded("qa_cache_get", e)
            cached = None
        if cached:
            conv = await conversation_service.create_conversation(db, username, query)
            cid = conv.id
            try:
                await conversation_service.save_message(db, cid, "user", query)
                await conversation_service.save_message(db, cid, "assistant", cached.get("answer", ""))
            except Exception as e:
                degraded("conv_save", e)
            yield {"type": "meta", "sources": cached.get("retrievalSource", []), "conversationId": cid}
            yield {"type": "token", "content": cached.get("answer", "")}
            try:
                from app.core import metrics
                metrics.QA_TOTAL.labels(_p, "true").inc()
                metrics.cache_hit_inc("redis")
            except Exception:
                pass
            yield {
                "type": "done", "responseTime": round(time.time() - t0, 3),
                "hallucinationRate": cached.get("hallucinationRate", 0.0),
                "conversationId": cid, "cached": True, "cacheLayer": "redis",
                "route": cached.get("route", "hybrid"),
            }
            return

        # L2: MySQL 二级缓存（精确持久，Redis 过期/evict 时兜底；优先于模糊语义匹配）
        if settings.CACHE_PERSIST_ENABLE:
            try:
                from app.services.cache_persist import cache_get_mysql
                mysql_cached = await cache_get_mysql(db, model_type, nq)
                if mysql_cached:
                    conv = await conversation_service.create_conversation(db, username, query)
                    cid = conv.id
                    try:
                        await conversation_service.save_message(db, cid, "user", query)
                        await conversation_service.save_message(db, cid, "assistant", mysql_cached.get("answer", ""))
                    except Exception as e:
                        degraded("conv_save", e)
                    yield {"type": "meta", "sources": mysql_cached.get("retrievalSource", []), "conversationId": cid}
                    yield {"type": "token", "content": mysql_cached.get("answer", "")}
                    try:
                        from app.core import metrics
                        metrics.QA_TOTAL.labels(_p, "true").inc()
                        metrics.cache_hit_inc("mysql")
                    except Exception:
                        pass
                    yield {
                        "type": "done", "responseTime": round(time.time() - t0, 3),
                        "hallucinationRate": mysql_cached.get("hallucinationRate", 0.0),
                        "conversationId": cid, "cached": True, "cacheLayer": "mysql",
                        "route": mysql_cached.get("route", "hybrid"),
                    }
                    return
            except Exception as e:
                degraded("qa_cache_mysql_stream", e)

        # L1.5: 语义缓存（模糊相似，精确持久 miss 后兜底）
        if getattr(settings, "SEMANTIC_CACHE_ENABLE", False):
            try:
                from app.rag.semantic_cache import semantic_cache_get
                sc_data, sc_type, sc_sim = await semantic_cache_get(model_type, nq)
                if sc_data and sc_type in ("semantic_high", "semantic_medium"):
                    conv = await conversation_service.create_conversation(db, username, query)
                    cid = conv.id
                    try:
                        await conversation_service.save_message(db, cid, "user", query)
                        await conversation_service.save_message(db, cid, "assistant", sc_data.get("answer", ""))
                    except Exception as e:
                        degraded("conv_save", e)
                    yield {"type": "meta", "sources": sc_data.get("retrievalSource", []), "conversationId": cid}
                    yield {"type": "token", "content": sc_data.get("answer", "")}
                    try:
                        from app.core import metrics
                        metrics.QA_TOTAL.labels(_p, "true").inc()
                        metrics.cache_hit_inc("semantic")
                    except Exception:
                        pass
                    yield {
                        "type": "done", "responseTime": round(time.time() - t0, 3),
                        "hallucinationRate": sc_data.get("hallucinationRate", 0.0),
                        "conversationId": cid, "cached": True,
                        "cacheLayer": sc_type,
                        "semanticSimilarity": round(sc_sim, 4),
                        "route": sc_data.get("route", "hybrid"),
                    }
                    return
            except Exception as e:
                degraded("semantic_cache_get_stream", e)

    # 多轮历史 + 指代消解
    history: list[dict] = []
    if conversation_id:
        history = await conversation_service.get_messages(db, conversation_id, _HISTORY_LIMIT)
    search_q = await _search_query_for_retrieve(db, query, nq, conversation_id, history, model_type)

    # 多轮且 query 完整(search_q==nq) → 也查 Redis 热点（流式，存消息接续对话）
    if conversation_id and search_q == nq and not regen and not await _is_blacklisted(nq):
        try:
            cached = await redis_client.cache_get_json(_cache_key(model_type, nq))
            if cached:
                try:
                    await conversation_service.save_message(db, conversation_id, "user", query)
                    await conversation_service.save_message(db, conversation_id, "assistant", cached.get("answer", ""))
                except Exception as e:
                    degraded("conv_save", e)
                yield {"type": "meta", "sources": cached.get("retrievalSource", []), "conversationId": conversation_id}
                yield {"type": "token", "content": cached.get("answer", "")}
                try:
                    from app.core import metrics
                    metrics.QA_TOTAL.labels(_p, "true").inc()
                    metrics.cache_hit_inc("redis")
                except Exception:
                    pass
                yield {"type": "done", "responseTime": round(time.time() - t0, 3),
                       "hallucinationRate": cached.get("hallucinationRate", 0.0),
                       "conversationId": conversation_id, "cached": True, "cacheLayer": "redis",
                       "route": cached.get("route", "hybrid")}
                return
        except Exception as e:
            degraded("qa_cache_get_multi_stream", e)

    # 智能路由：根据查询特征选择最优检索路径（Phase A）
    routing = None
    if settings.ROUTING_ENABLE:
        try:
            from app.routing.routing_service import route_query
            routing = route_query(search_q)
        except Exception as e:
            degraded("routing_dispatch", e)

    contexts = await retrieval_service.mixed_search(
        db, search_q, topk, tenant=tenant, routing_decision=routing,
    )
    if not contexts:
        yield {"type": "done", "content": "根据现有资料无法确认该问题，请先上传并解析相关运维文档后重试。"}
        return

    # Corrective RAG：分级 + 纠错闭环
    contexts, confidence, crag_action, crag_grade = await _crag_correct(
        db, nq, contexts, model_type, topk, tenant
    )

    # GraphRAG
    graph: list[str] = []
    if settings.KG_RAG_ENABLE:
        try:
            graph = await kg_service.graph_context(nq)
        except Exception as e:
            degraded("kg_graph_context", e)
            graph = []
    messages = prompt_templates.build_messages_with_history(nq, contexts, history, graph, confidence)

    # 流式前先建会话，确保 conversationId 可随 meta 下发
    if is_single:
        conv = await conversation_service.create_conversation(db, username, query)
        conversation_id = conv.id

    # 1) meta：引用来源 + 会话 ID
    yield {
        "type": "meta",
        "sources": [{
            "docId": c.get("docId", ""), "docName": c.get("docName", ""),
            "docType": c.get("docType", ""), "chunkIdx": c.get("chunkIdx"),
            "chunk": c.get("chunk", ""), "score": c.get("score", 0.0),
            "sources": c.get("sources", []),
        } for c in contexts],
        "conversationId": conversation_id,
    }

    # 2) 逐 token 流式（打字机）+ LLM 调用埋点
    parts: list[str] = []
    _llm0 = time.time()
    async for token in get_llm_provider(model_type).stream(messages, temperature=config_service.rt_temperature()):
        parts.append(token)
        yield {"type": "token", "content": token}
    try:
        from app.core import metrics
        metrics.LLM_CALLS.labels(_p).inc()
        metrics.LLM_LATENCY.labels(_p).observe(time.time() - _llm0)
    except Exception:
        pass

    # 3) 持久化完整答案
    full = "".join(parts)
    # 证据溯源：补标（done 段下发 annotatedAnswer，前端替换渲染出角标；持久化/缓存均用补标后）
    if getattr(settings, "CITATION_AUTO_ENABLE", True):
        annotated, _trace = await citation.auto_cite(full, contexts)
    else:
        annotated, _trace = full, citation.evidence_trace(full)
    # 成本追踪（记录 token 用量 → 成本报告数据来源；估算 input/output token）
    try:
        import asyncio
        from app.services.cost_tracker_service import record_token_usage
        asyncio.ensure_future(record_token_usage(db, username, tenant, model_type or settings.LLM_PROVIDER, len(str(messages)) // 2, len(annotated) // 2))
    except Exception:
        pass
    # 在线质量评测采样（异步跑 LLM Judge，不阻塞流式；评测趋势数据来源）
    try:
        import asyncio
        from app.services.online_eval_service import should_sample, eval_quality
        if should_sample():
            asyncio.ensure_future(eval_quality(db, query, annotated, contexts, model_type))
    except Exception:
        pass
    try:
        await conversation_service.save_message(db, conversation_id, "user", query)
        await conversation_service.save_message(db, conversation_id, "assistant", annotated)
    except Exception as e:
        degraded("conv_save", e)

    # 4) 单轮 Write-Through 双写缓存（MySQL → Redis）
    halluc = citation.estimate_hallucination(annotated, len(contexts))
    try:
        from app.core import metrics
        metrics.HALLUC.observe(halluc)
    except Exception:
        pass
    cache_data = {
        "answer": annotated,
        "retrievalSource": [{
            "docId": c.get("docId", ""), "docName": c.get("docName", ""),
            "docType": c.get("docType", ""), "chunkIdx": c.get("chunkIdx"),
            "chunk": c.get("chunk", ""), "score": c.get("score", 0.0),
            "sources": c.get("sources", []),
        } for c in contexts],
        "evidenceTrace": _trace,
        "responseTime": round(time.time() - t0, 3),
        "hallucinationRate": halluc,
        "cached": False,
        "cacheLayer": "llm",
        "conversationId": conversation_id,
    }
    # 单轮 或 多轮 且 高置信 才写；黑名单/证据有限/不足不写
    if (is_single or conversation_id) and confidence == "high" and not await _is_blacklisted(nq):
        # L2: MySQL 持久化（先写）
        if settings.CACHE_PERSIST_ENABLE:
            try:
                from app.services.cache_persist import cache_set_mysql
                await cache_set_mysql(db, model_type, nq, query, cache_data)
            except Exception as e:
                degraded("qa_cache_mysql_set_stream", e)
        # L1: Redis 热点（后写）
        try:
            await redis_client.cache_set_json(_cache_key(model_type, nq), cache_data, settings.QA_CACHE_TTL)
        except Exception as e:
            degraded("qa_cache_set", e)
        # L1.5: 语义缓存索引（向量化入库，供后续相似查询命中）
        if getattr(settings, "SEMANTIC_CACHE_ENABLE", False):
            try:
                from app.rag.semantic_cache import semantic_cache_set
                await semantic_cache_set(model_type, nq, _cache_key(model_type, nq))
            except Exception as e:
                degraded("semantic_cache_set_stream", e)
    try:
        from app.core import metrics
        metrics.QA_TOTAL.labels(_p, "false").inc()
        metrics.cache_hit_inc("llm")
    except Exception:
        pass
    # 证据补全：medium/refused 自动收集（bg task，独立 session，不阻塞流式）
    if settings.EVIDENCE_GAP_AUTO_COLLECT and confidence in ("medium", "refused"):
        try:
            from app.services import evidence_gap_service
            _bg_tasks.add(asyncio.create_task(evidence_gap_service.collect(
                nq, annotated, confidence, crag_grade, crag_action, "auto", tenant,
            )))
        except Exception:
            pass
    yield {
        "type": "done",
        "responseTime": round(time.time() - t0, 3),
        "hallucinationRate": halluc,
        "modelType": _p,  # 实际调用的 LLM（前端据此展示 🤖 模型 badge；缓存命中时不带此字段）
        "graphCount": len(graph),
        "highRisk": safety.extract_high_risk(annotated),
        "annotatedAnswer": annotated,        # 补标后全文，前端替换渲染出 [n] 角标上标
        "evidenceTrace": _trace,             # 句级溯源
        "confidence": confidence,
        "cragAction": crag_action,
        "cragGrade": crag_grade,
        "conversationId": conversation_id,
        "cached": False,
        "cacheLayer": "llm",
        "route": routing.route if routing else "hybrid",
        "routeReason": routing.reason if routing else "",
    }


async def generate_related(
    query: str, answer: str = "", model_type: str | None = None
) -> list[str]:
    """基于当前问答，LLM 生成 3 个相关追问问题（引导深挖）。

    独立接口：避免塞进流式 done 拖慢首字延迟，由前端答案渲染后异步拉取。
    """
    provider = get_llm_provider(model_type)
    prompt = (
        "基于以下电网运维问答，生成 3 个用户可能继续追问的相关问题。\n"
        "要求：与原问题相关但换角度或更深一层；简短具体（10-25 字）；聚焦变电/配电/输电运维。\n"
        "只输出 JSON 字符串数组，如 [\"问题1\",\"问题2\",\"问题3\"]，不要任何解释或代码块。\n\n"
        f"【原问题】{query}\n【答案摘要】{(answer or '')[:500]}"
    )
    try:
        ans = await provider.chat(
            [{"role": "user", "content": prompt}], temperature=0.5, max_tokens=400
        )
    except Exception as e:
        degraded("related_gen", e)
        return []
    m = re.search(r"\[.*\]", ans or "", re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    return [str(x).strip()[:60] for x in arr if str(x).strip()][:3]
