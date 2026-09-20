"""RAG 问答编排：热点缓存 / 多轮指代消解 / 检索 / CRAG自纠错 / prompt / LLM / 后处理 / 相关问题推荐。"""
import json
import re
import time
import asyncio

_bg_tasks: set = set()  # 持有后台 task 引用，防 GC


def _fire_and_forget(coro):
    """后台 task 持引用防 GC + 完成后自动从集合移除（防已完成 Task 长期累积）。

    替代裸 `_bg_tasks.add(asyncio.create_task(...))` 范式：后者缺 add_done_callback，
    已完成 Task 引用会长期驻留 _bg_tasks 集合造成泄漏。本 helper 统一收口。
    """
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.clients import redis_client
from app.config import settings
from app.core import safety
from app.core.obs import degraded
from app.core.qa_trace import get_collector as _get_trace, span as _trace_span
from app.providers.factory import get_llm_provider
from app.rag import citation, prompt_templates
from app.models.document import Document
from app.services import config_service, conversation_service, kg_service, retrieval_service, term_service

_HISTORY_LIMIT = 6  # 拼接最近 3 轮（6 条消息）


def _cache_tenant(tenant: str | None) -> str:
    return (tenant or "default").strip() or "default"


def _llm_degradation_fields(llm_prov, model_type: str | None) -> dict:
    """从 FallbackLLMProvider 读取真实命中的 provider + 是否发生过降级切换。

    modelType 用实际命中的 provider（而非请求参数/默认配置），修复前端
    "🤖模型"徽章失真——旧代码 modelType 恒等于 model_type or settings.LLM_PROVIDER，
    fallback 切备后前端完全看不出来。
    """
    actual = getattr(llm_prov, "last_used_name", model_type or settings.LLM_PROVIDER)
    return {
        "modelType": actual,
        "llmDegraded": bool(getattr(llm_prov, "degraded", False)),
        "llmDegradedReason": getattr(llm_prov, "degrade_reason", ""),
    }


def _cap_confidence_for_local_model(confidence: str, actual_provider: str) -> str:
    """本地应急模型(ollama)作答质量明显低于云端，confidence 封顶 medium，
    避免"高置信"徽章与"本地应急模型"警告语义打架（顺带效果：ollama 作答不会被当作
    高置信答案写入长期缓存/highbase，符合"应急答案不该被当权威沉淀"的预期）。
    """
    if actual_provider == "ollama" and confidence == "high":
        return "medium"
    return confidence


def _retrieval_degradation_fields() -> dict:
    """读取本次请求的 trace mark，判断云端向量检索是否降级（bge 独扛，见 _dense_dual）。"""
    tc = _get_trace()
    is_degraded = bool(tc and tc.marks.get("dense_cloud_failed"))
    return {
        "retrievalDegraded": is_degraded,
        "retrievalDegradedReason": ("云端向量检索不可用，已降级为本地embedding+关键词检索"
                                     if is_degraded else ""),
    }


def _llm_all_down_response(nq: str, contexts: list[dict], t0: float,
                            conversation_id: str | None) -> dict:
    """LLM fallback 链（含本地 ollama 兜底）全部耗尽时的结构化拒答，替代裸抛异常导致的
    非流式裸 500 / 流式 SSE 硬断（I2）。仍保留已检索到的证据，不让用户白等一场。"""
    return {
        "answer": "抱歉，当前所有 AI 模型（含本地应急模型）暂时不可用，请稍后重试。",
        "retrievalSource": [{
            "docId": c.get("docId", ""), "docName": c.get("docName", ""),
            "docType": c.get("docType", ""), "chunkIdx": c.get("chunkIdx"),
            "chunk": c.get("chunk", ""), "score": c.get("score", 0.0),
            "sources": c.get("sources", []),
        } for c in contexts],
        "responseTime": round(time.time() - t0, 3), "hallucinationRate": 0.0,
        "cached": False, "confidence": "refused", "cragAction": "llm_all_down",
        "conversationId": conversation_id or "",
    }


def _cache_key(model_type: str | None, query: str, tenant: str | None = "default") -> str:
    from app.config import citation_cache_version
    return f"qa:{_cache_tenant(tenant)}:{model_type or 'default'}:{query}:{citation_cache_version()}"


def _multi_cache_key(model_type: str | None, search_q: str, tenant: str | None,
                     conversation_id: str | None) -> str:
    """C4 多轮缓存键：standalone query(search_q 含指代消解) + conversation_id 隔离对话。

    同对话同语义追问 → 同 key 命中；跨对话隔离；文档撤回/过期由 _cache_knowledge_valid 复核兜底。
    """
    from app.config import citation_cache_version
    cid = (conversation_id or "")[:8]
    return f"qa:{_cache_tenant(tenant)}:{model_type or 'default'}:{search_q}:conv{cid}:{citation_cache_version()}"


async def _is_blacklisted(nq: str) -> bool:
    """缓存黑名单检查（高频坏答案禁缓存命中，由反馈驱动 auto_tune_cache_ttl 写入 Redis set）。"""
    try:
        from app.services.feedback_optimizer_service import is_query_blacklisted
        return await is_query_blacklisted(nq)
    except Exception:
        return False


async def _hit_hotqa(nq: str, conversation_id: str, t0: float) -> dict | None:
    """hotqa 高频问答对命中（永久缓存，like 写入）。命中返回完整 answer dict，未命中返回 None。

    key=hotqa:{nq}（normalize 后的 query），value=JSON{query,answer,sources,count,lastLikedAt,tenant}。
    永不过期（TTL=-1）—— 用户认可的高频答案直接复用，跳过检索/CRAG/生成链路。
    sources 是逗号分隔字符串，这里重建为 retrievalSource 列表（docName 占位）。
    任何异常吞掉降级（degraded 日志），返回 None → 走正常检索链路。
    """
    if not getattr(settings, "HOTQA_ENABLE", True):
        return None
    if not nq:
        return None
    try:
        from app.clients import redis_client
        hot = await redis_client.get_redis().get(f"hotqa:{nq}")
        if not hot:
            return None
        # 黑名单优先：该 query 被多人 dislike 进黑名单后，hotqa 不应复用
        if await _is_blacklisted(nq):
            return None
        d = json.loads(hot)
        sources_str = d.get("sources") or ""
        sources_list = [
            {
                "docId": "", "docName": s.strip(), "docType": "",
                "chunkIdx": None, "chunk": "", "score": 0.0, "sources": [],
            }
            for s in sources_str.split(",")
            if s.strip()
        ]
        return {
            "answer": d.get("answer", ""),
            "retrievalSource": sources_list,
            "responseTime": round(time.time() - t0, 3),
            "hallucinationRate": 0.0,
            "cached": True,
            "cacheLayer": "hotqa",
            "confidence": "high",
            "conversationId": conversation_id or "",
            "hotqaCount": d.get("count", 1),
        }
    except Exception as e:
        degraded("hotqa_hit", e)
        return None


async def _cache_knowledge_valid(
    db: AsyncSession, cached: dict | None, tenant: str | None,
) -> bool:
    """缓存命中前复核其证据文档时效，防止已撤回/过期知识继续回答。"""
    if not cached:
        return False
    doc_ids = {
        str(item.get("docId") or item.get("doc_id") or "")
        for item in (cached.get("retrievalSource") or [])
        if isinstance(item, dict)
    }
    doc_ids.discard("")
    if not doc_ids:
        return not bool(tenant)
    try:
        if tenant:
            owned = (
                await db.execute(
                    select(Document.id).where(
                        Document.id.in_(doc_ids),
                        Document.tenant_id == tenant,
                    )
                )
            ).scalars().all()
            if set(owned) != doc_ids:
                return False
        from app.services import knowledge_governance_service

        blocked = await knowledge_governance_service.blocked_document_ids(
            db, doc_ids, tenant_id=tenant,
        )
        return not blocked
    except Exception as exc:
        degraded("knowledge_governance_cache_gate", exc)
        # fail-closed（红队缺口 #7）：治理后端不可用时无法核验文档时效，
        # 宁可缓存 miss 重走检索/LLM，也不冒"已撤回/过期知识继续回答"的风险。
        # 内部调用方（answer/stream_answer）恒带 tenant（默认 default）；
        # 显式 tenant=None 的离线/兼容调用同样 fail-closed。
        return False


async def _crag_correct(
    db: AsyncSession, nq: str, contexts: list[dict],
    model_type: str | None, topk: int, tenant: str = "default",
) -> tuple[list[dict], str, str, str, dict]:
    """CRAG 分级 + 纠错闭环。返回 (contexts, confidence, action, grade, extras)。

    分级：CRAG v2（LLM 逐条评估证据）优先，未启用/失败回退 v1（rerank top1 分数）。
    incorrect → query 改写重检索 → 仍 incorrect → refused 保守拒答。
    contexts 可能被纠错重检索替换。
    extras: CRAG_V3_ENABLE 开时含 cragDetail/cragReason（捡回 v2 逐条 detail，断点 A），
            关={} 现状（前端零改动）。缓存字段同步见 T9 cv R 段。
    """
    confidence, action, grade = "high", "normal", ""
    extras: dict = {}
    if not settings.CRAG_ENABLE:
        return contexts, confidence, action, grade, extras
    from app.rag import crag

    rerank_ok = settings.RERANK_ENABLE
    # 分级：v2 优先，失败回退 v1
    grade = ""
    _v2_detail: dict = {}
    if settings.CRAG_PERDOC_ENABLE:
        from app.rag import crag_v2
        try:
            grade, _v2_detail = await crag_v2.grade_with_llm(nq, contexts, model_type)
        except Exception as e:
            degraded("crag_v2", e)
    _v3 = getattr(settings, "CRAG_V3_ENABLE", False)

    def _es_of(ctxs: list[dict], detail: dict | None) -> float | None:
        """当前 contexts/detail 的证据强度（v2 detail 优先，否则 v1 top1）。"""
        if detail:
            return crag.evidence_strength(detail=detail, rerank_ok=rerank_ok)
        if ctxs:
            return crag.evidence_strength(top1=float(ctxs[0].get("score", 0.0)), rerank_ok=rerank_ok)
        return None

    _es: float | None = None
    # T4（断点 C）：V3 开时 grade 统一由 es 分桶（消除 v1 绝对阈值/v2 相对计数口径漂移）
    if _v3:
        _es = _es_of(contexts, _v2_detail)
        grade, _ = crag.grade(0.0, len(contexts), rerank_ok, es=_es)
    elif not grade:
        top1 = float(contexts[0].get("score", 0.0)) if contexts else 0.0
        grade, _ = crag.grade(top1, len(contexts), rerank_ok)

    _rewrite_delta: float | None = None
    if grade == crag.GRADE_INCORRECT:
        _es_before = _es
        try:
            from app.services.query_rewrite import rewrite_query
            new_q = await rewrite_query(nq, model_type, force=True)
            if new_q and new_q != nq:
                new_ctx = await retrieval_service.mixed_search(db, new_q, topk, tenant=tenant)
                if new_ctx:
                    contexts = new_ctx
                    if _v3:
                        _es = _es_of(contexts, None)  # 改写后无 v2 detail
                        grade, _ = crag.grade(0.0, len(contexts), rerank_ok, es=_es)
                    else:
                        top1 = float(contexts[0].get("score", 0.0))
                        grade, _ = crag.grade(top1, len(contexts), rerank_ok)
                    _rewrite_delta = round(
                        (_es if _es is not None else 0.0)
                        - (_es_before if _es_before is not None else 0.0), 3)
                    # T5（断点 E）：action×grade 决策矩阵（V3）；关 V3 老逻辑 rewritten
                    if _v3:
                        if grade == crag.GRADE_CORRECT:
                            action = "rewritten_recovered"
                        elif grade == crag.GRADE_AMBIGUOUS:
                            action = "rewritten_partial"
                        else:
                            action = "rewritten_failed"
                    else:
                        action = "rewritten"
        except Exception as e:
            degraded("crag_rewrite", e)

    # ambiguous 档邻域扩展：证据有限时捞相邻 chunk 补全上下文（默认关，开关 CRAG_NEIGHBOR_EXPAND_ENABLE）
    if grade == crag.GRADE_AMBIGUOUS and getattr(settings, "CRAG_NEIGHBOR_EXPAND_ENABLE", False):
        try:
            contexts = await _expand_neighbors(db, contexts, getattr(settings, "CRAG_NEIGHBOR_WINDOW", 1))
        except Exception as e:
            degraded("crag_neighbor_expand", e)

    _rewrote = action.startswith("rewritten")
    confidence = crag.confidence_of(grade, _rewrote)
    # 关 V3 老逻辑：incorrect+rewritten → refused（V3 路径 action 已是 rewritten_failed，confidence_score 判 refused）
    if not _v3 and grade == crag.GRADE_INCORRECT and action == "rewritten":
        action = "refused"

    # T2+T3+T5：CRAG_V3_ENABLE 开时连续置信度 + 归因 + 矩阵
    if _v3:
        # _es 已在 grade 时算（含改写重判后）；兜底重算（如 contexts 被邻域扩展改写）
        if _es is None:
            _es = _es_of(contexts, _v2_detail)
        _score, _label = crag.confidence_score(_es, action, degraded=not rerank_ok)
        confidence = crag.label_to_confidence(_label)  # 老字段映射（前端零改动）
        extras["confidenceScore"] = _score
        extras["confidenceLabel"] = _label
        extras["evidenceStrength"] = _es
        extras["evaluatorDegraded"] = not rerank_ok                  # T5（断点 D）：评估器降级标记
        if _rewrite_delta is not None:
            extras["rewriteDelta"] = _rewrite_delta                  # T5（断点 E）：改写纠错增益
        _rf = _refused_reason(action, len(contexts), grade)          # T5（断点 E）：refused 归因
        if _rf:
            extras["refusedReason"] = _rf
        if _v2_detail:                                                # T2: 逐条 detail 归因（仅 v2 路径）
            extras["cragDetail"] = _v2_detail
            extras["cragReason"] = _format_crag_reason(_v2_detail, grade)

    try:
        from app.core import metrics
        metrics.CRAG_GRADE.labels(grade).inc()
        metrics.CRAG_ACTION.labels(action).inc()
        metrics.CRAG_CONFIDENCE.labels(confidence).inc()
        # T7（可观测）：V3 细化度量（证据强度/5档标签/refused归因/改写增益）
        if _v3:
            if _es is not None:
                metrics.CRAG_EVIDENCE_STRENGTH.observe(_es)
            metrics.CRAG_CONFIDENCE_LABEL.labels(_label).inc()
            if _rf:
                metrics.CRAG_REFUSED_REASON.labels(_rf).inc()
            if _rewrite_delta is not None:
                metrics.CRAG_REWRITE_DELTA.observe(_rewrite_delta)
    except Exception:
        pass
    # sufficiency gating：judge.answerability 判证据够不够答（opt-in，默认关=现状）
    # judge 判不可答 → 置信降一档 + missing_info 入 extras；与 DEBATE 开关闭环（降档→更易触发辩论）
    if getattr(settings, "SUFFICIENCY_GATE_ENABLE", False) and contexts:
        try:
            from app.rag.judge import judge_answerability
            _chk = [c.get("chunk", c.get("text", "")) or "" for c in contexts[:5]]
            _abl = await judge_answerability(nq, _chk, model_type)
            extras["answerability"] = _abl
            if not _abl.get("answerable", False):
                extras["insufficient"] = _abl.get("missing_info", "")
                _down = {"high": "medium_high", "medium_high": "medium_low",
                         "medium_low": "low", "low": "low", "refused": "refused"}
                confidence = _down.get(confidence, confidence)
        except Exception as e:
            degraded("sufficiency_gate", e)
    return contexts, confidence, action, grade, extras


async def _write_highbase(nq: str, answer: str, tenant: str = "default") -> None:
    """B6: high 答案写入独立 highbase key（不依赖问答缓存存活）。

    dislike 时 check_overconfident 扫 `qa:highbase:{nq}` 检出冲突——
    原实现扫 `qa:*:{nq}` 问答缓存，TTL 过期就丢，导致历史 high 失忆。
    highbase TTL=OVERCONFIDENT_BASELINE_TTL_DAYS（默认 30 天），独立于 QA_CACHE_TTL。
    Redis 异常 → degraded 吞掉（安全侧：不检出）。
    """
    if not getattr(settings, "CONFIDENCE_OVERCONFIDENT_ENABLE", False):
        return
    try:
        import time
        ttl_days = int(getattr(settings, "OVERCONFIDENT_BASELINE_TTL_DAYS", 30) or 30)
        payload = {
            "confidence": "high",
            "answer": (answer or "")[:500],
            "ts": time.time(),
            "tenant": tenant or "default",
        }
        await redis_client.get_redis().set(
            f"qa:highbase:{nq}", json.dumps(payload, ensure_ascii=False),
            ex=ttl_days * 86400,
        )
    except Exception as e:
        degraded("overconfident_baseline_write", e)


async def _maybe_debate_augment(
    db, nq: str, ans: str, confidence: str, crag_extras: dict, model_type: str | None,
) -> tuple[str, str]:
    """低置信 → debate_agent 三专家裁决，共识前置注入答案 + 升级置信（opt-in）。

    接通旁支 debate_diagnose（原仅 /domain/diagnose-debate，不反哺 QA 主链）。
    DEBATE_ON_LOW_CONFIDENCE_ENABLE 默认关=现状。debate 是诊断型（规程/图谱/案例三视角），
    对故障/处置类低置信问题价值最大；与 SUFFICIENCY_GATE 形成闭环——证据不足→降档→触发辩论。
    异常 degraded 不阻塞主链。返回 (ans, confidence) 供 caller 赋值。
    """
    if not getattr(settings, "DEBATE_ON_LOW_CONFIDENCE_ENABLE", False):
        return ans, confidence
    if confidence not in ("medium_low", "low", "refused"):
        return ans, confidence
    try:
        from app.services.debate_agent_service import debate_diagnose
        deb = await debate_diagnose(db, nq, model_type)
        diag = deb.get("diagnosis") or {}
        summary = (diag.get("summary") or "").strip()
        causes = diag.get("causes") or []
        disagreements = (deb.get("debate") or {}).get("disagreements") or []
        if summary:
            head = "【多专家联合裁决（规程 / 图谱 / 案例 三视角交叉验证）】\n" + summary
            if causes:
                head += "\n可能原因：" + "；".join(
                    c.get("name", "") for c in causes[:5] if isinstance(c, dict) and c.get("name"))
            if disagreements:
                head += "\n⚠ 专家分歧点（请人工核对）：" + "；".join(str(d)[:80] for d in disagreements[:3])
            ans = head + "\n\n" + (ans or "")
            confidence = "medium_high"  # 多专家交叉验证后升级（不到 high，保留人审空间）
            crag_extras["debateTriggered"] = True
            crag_extras["debateConsensus"] = summary[:500]
            crag_extras["debateDisagreements"] = len(disagreements)
    except Exception as e:
        degraded("debate_on_low_confidence", e)
    return ans, confidence


async def _maybe_collect_refused(
    *, nq: str, answer: str, confidence: str,
    grade: str, action: str, tenant: str = "default",
) -> None:
    """B4/B7：refused / 无结果 自动入证据补全队列（fire-and-forget 由 caller 决定）。

    触发条件：confidence=='refused' 或 action in {rewritten_failed, refused}（含 stream 无结果路径）。
    collect 本身去重（同 query pending 跳过），多次调用安全。
    开关 CRAG_REFUSED_TO_GAP_ENABLE 关时整体跳过。
    source：action 空（stream 无结果路径）→ auto_no_recall；否则 auto_crag。
    """
    if not getattr(settings, "CRAG_REFUSED_TO_GAP_ENABLE", True):
        return
    is_refused = confidence == "refused" or action in ("rewritten_failed", "refused")
    if not is_refused:
        return
    source = "auto_no_recall" if (grade == "incorrect" and not action) else "auto_crag"
    try:
        from app.services import evidence_gap_service
        await evidence_gap_service.collect(
            nq, answer or "", confidence or "refused",
            grade or "", action or "", source, tenant or "default")
    except Exception as e:
        degraded("crag_refused_to_gap", e)


def _format_crag_reason(detail: dict | None, grade: str) -> str:
    """v2 逐条 detail → 人话归因（confidence refinement T2）。

    detail: crag_v2.labels_to_grade 产物 {relevant, partial, irrelevant, n}。
    grade: correct/ambiguous/incorrect。空 detail 返回 ""。
    """
    if not detail:
        return ""
    rel = int(detail.get("relevant", 0))
    partial = int(detail.get("partial", 0))
    irr = int(detail.get("irrelevant", 0))
    n = int(detail.get("n", rel + partial + irr))
    grade_cn = {"correct": "证据充分", "ambiguous": "证据有限",
                "incorrect": "证据不足"}.get(grade, "")
    return f"{grade_cn}：{rel} relevant + {partial} partial + {irr} irrelevant / {n} 条（v2 per-doc）"


def _refused_reason(action: str, n_contexts: int, grade: str) -> str:
    """refused 归因（confidence refinement T5）。非 refused 场景返回 ''。

    - no_recall: 检索无结果（contexts 空）
    - rewrite_exhausted: 改写重检索后仍 incorrect
    - out_of_domain: Self-RAG 判定非运维（在 stream_answer 标 self_rag_skip，此处不产）
    - evidence_contradict: 兜底（v2 无 relevant 且证据矛盾，T5 简化）
    """
    if n_contexts == 0:
        return "no_recall"
    if action == "rewritten_failed":
        return "rewrite_exhausted"
    if grade == "incorrect":
        return "evidence_contradict"
    return ""


async def _expand_neighbors(db: AsyncSession, contexts: list[dict], window: int = 1, max_add: int = 4) -> list[dict]:
    """ambiguous 档邻域扩展：对 top 命中 chunk，捞同文档相邻 chunk_idx±window 补进 contexts。

    去重（不重复已命中），数量限 max_add。补全证据完整性（关键信息被切到邻块时捞回）。
    与检索层 small-to-big(_expand_parents) 互补：本层在 CRAG 证据不足时触发，开关独立。
    """
    from sqlalchemy import and_, or_, select
    from app.models.chunk import Chunk
    from app.models.document import Document

    seeds = [(c.get("docId"), c.get("chunkIdx")) for c in contexts[:3]
             if c.get("docId") and c.get("chunkIdx") is not None]
    if not seeds:
        return contexts
    conds = []
    for doc_id, cidx in seeds:
        for off in range(-int(window), int(window) + 1):
            if off == 0:
                continue
            conds.append((doc_id, int(cidx) + off))
    if not conds:
        return contexts
    existing = {(c.get("docId"), c.get("chunkIdx")) for c in contexts}
    rows = (await db.execute(
        select(Chunk, Document).join(Document, Chunk.doc_id == Document.id).where(
            or_(*[and_(Chunk.doc_id == d, Chunk.chunk_idx == ci) for d, ci in conds])
        )
    )).all()
    added = []
    for c, d in rows:
        if (c.doc_id, c.chunk_idx) in existing:
            continue
        added.append({
            "chunk": c.content or "", "score": 0.0, "docId": c.doc_id,
            "docName": d.doc_name or "", "docType": d.doc_type or "",
            "chunkIdx": c.chunk_idx, "sources": ["neighbor"],
        })
        existing.add((c.doc_id, c.chunk_idx))
    return contexts + added[:max_add]


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


async def _enrich_citation_metadata(db: AsyncSession, citation_map: list) -> None:
    """跨 task 依赖补查（Task 7 reviewer 遗留 gap）：contexts 是 retrieval_service._to_item
    产物，**不含** chunk 元数据（section_path/page_num/bbox/table_header）。

    按 citation_map 的 chunk_id 批量查 Chunk 表（一次 select...in_()），把元数据 merge
    进每个 CitationItem.metadata，使引用卡片能展示章节/页码/高亮定位。
    失败静默降级（不阻塞主链路）。
    """
    if not citation_map:
        return
    try:
        from app.models.chunk import Chunk
        chunk_ids = {c.chunk_id for c in citation_map if c.chunk_id}
        if not chunk_ids:
            return
        rows = (await db.execute(
            select(Chunk.id, Chunk.section_path, Chunk.page_num, Chunk.bbox, Chunk.table_header)
            .where(Chunk.id.in_(chunk_ids))
        )).all()
        meta_by_id = {
            r[0]: {
                "section_path": r[1] or "",
                "page_num": r[2],
                "bbox": r[3],
                "table_header": r[4] or "",
            }
            for r in rows
        }
        for c in citation_map:
            if not c.chunk_id:
                continue
            m = meta_by_id.get(c.chunk_id)
            if m:
                # merge：parse_citation_answer 已填 doc_title/original_text，这里只补定位字段
                c.metadata.update(m)
    except Exception as e:
        degraded("citation_metadata_enrich", e)


async def _apply_citation_verification(
    ans: str, contexts: list[dict], model_type: str | None,
    *, db: AsyncSession | None = None, cmap_override: list | None = None,
    query: str | None = None, tenant: str = "default",
) -> tuple[str, dict]:
    """可核验引用后处理：结构化解析 → 元数据补查 → 三层校验 → 返回 (最终答案, 附加字段)。

    CITATION_VERIFIER_ENABLE=False 时直接返回 (ans, {})，零破坏（前端无新字段、主链路零影响）。
    开启时：build_index(contexts) → parse_citation_answer(ans,...) → (可选)Chunk 元数据补查
    → verify(...) → 把 dropped 编号替换为警示说明；extras 含 citationVerified/citationIndex/
    citationMap/unverifiedClaims。校验 rewrite_needed=True 透传到 extras，由 answer 决定
    是否触发 CRAG 二次检索（最多 1 次，防死循环）。

    db：可选 AsyncSession，由 answer 传入以回填 Chunk 元数据；单测不传则跳过补查。
    """
    if not getattr(settings, "CITATION_VERIFIER_ENABLE", False):
        return ans, {}
    from app.rag.citation_index import build_index
    from app.rag.citation_verifier import verify

    index = build_index(contexts)
    if cmap_override is not None:
        # STRUCTURED_OUTPUT：ans 已是 answer_text，cmap 已结构化（每 ref_id 一项，不重复），不再 parse
        ans_text = ans
        cmap = cmap_override
        unverified: list = []
    else:
        from app.schemas.citation import parse_citation_answer
        parsed = parse_citation_answer(ans, index, contexts)
        ans_text = parsed.answer_text
        cmap = parsed.citation_map
        unverified = parsed.unverified_claim

    # 跨 task 依赖：按 chunk_id 批量回填 section_path/page_num/bbox/table_header
    if db is not None:
        await _enrich_citation_metadata(db, cmap)

    # C1：NLI 异步后置（CITATION_NLI_ASYNC_ENABLE）——同步只跑校验1+2（不阻塞首答/done），
    # 校验3 NLI 由 _schedule_nli_backfill 后台跑完回写缓存（复用 _bg_tasks 持引用防 GC）。
    # 关=现状（verify 内同步跑 NLI，或 CITATION_NLI_ENABLE 关则不跑）。
    _nli_async = getattr(settings, "CITATION_NLI_ASYNC_ENABLE", False)
    _nli_on = getattr(settings, "CITATION_NLI_ENABLE", False)
    verdict = await verify(
        ans_text, cmap, index, contexts, model_type,
        nli_enable=(False if _nli_async else _nli_on),
    )

    # 把 drop 的编号从答案里剔除（替换为警示说明）
    final_ans = ans_text
    for ref in verdict.dropped_refs:
        final_ans = final_ans.replace(f"[{ref}]", "（该引用经核验无可靠证据支撑）")
    extras = {
        "citationVerified": verdict.model_dump(),
        "citationIndex": index,
        "citationMap": [c.model_dump() for c in cmap if c.ref_id not in verdict.dropped_refs],
        "unverifiedClaims": unverified + verdict.unverified_additions,
    }
    # C1：NLI 异步后置——同步阶段(NLI 未跑)派发后台 task，结果回写缓存(尽力)+degraded 日志
    if _nli_async and _nli_on:
        _schedule_nli_backfill(model_type, cmap, contexts, query, tenant, verdict)
    return final_ans, extras


def _schedule_nli_backfill(model_type, citation_map, contexts, query, tenant, sync_verdict):
    """C1：NLI 异步后置调度——同步校验1+2 返回后，后台跑校验3 NLI（不阻塞首答/done）。

    结果回写缓存 citationVerified.nli（下次命中即完整）+ degraded 日志；任何异常吞掉不影响主链路。
    复用 _bg_tasks 持 task 引用防 GC，done 回调清理集合。
    """
    try:
        t = asyncio.create_task(_nli_backfill(model_type, citation_map, contexts, query, tenant, sync_verdict))
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)
    except Exception as e:
        degraded("citation_nli_async_schedule", e)


async def _nli_backfill(model_type, citation_map, contexts, query, tenant, sync_verdict):
    """C1：NLI 后台核验——对同步阶段通过校验1+2 的项跑 NLI，结果回写缓存。

    用 ref_id 对齐 sync_verdict.items(valid=True) 与 citation_map(sentence)，重现 verify 校验3 输入。
    容错：NLI 超时/异常、缓存 miss/异常均降级跳过（degraded 日志），永不阻塞主链路。
    """
    from app.rag import judge
    # 同步路径 NLI 未跑 → valid=True 即校验1+2 通过；按 ref_id 取 sentence 作 NLI claims
    valid_refs = {it.ref_id for it in sync_verdict.items if it.valid}
    valid = [c for c in citation_map if c.ref_id in valid_refs and c.sentence]
    if not valid:
        return
    ctx_by_id = {c.get("chunkId"): c for c in contexts}
    claims = [c.sentence for c in valid]
    sources = [ctx_by_id.get(c.chunk_id, {}).get("chunk", "") for c in valid]
    try:
        verdicts = await asyncio.wait_for(
            judge._verify_claims(claims, sources, model_type),
            timeout=settings.CITATION_NLI_TIMEOUT,
        )
    except Exception as e:
        degraded("citation_nli_async", e)
        return
    # 回写缓存（尽力而为：query 缺/缓存 miss/异常均跳过）
    if not query:
        return
    try:
        ck = _cache_key(model_type, query, tenant)
        cached = await redis_client.cache_get_json(ck)
    except Exception as e:
        degraded("citation_nli_async_cache_read", e)
        return
    if not cached or not cached.get("citationVerified"):
        return
    try:
        cv = cached["citationVerified"]
        label_by_ref = {c.ref_id: v.get("label", "neutral") for c, v in zip(valid, verdicts)}
        for it in cv.get("items", []):
            if it.get("ref_id") in label_by_ref:
                it["nli_label"] = label_by_ref[it["ref_id"]]
        cv["nli_async_done"] = True
        cached["citationVerified"] = cv
        await redis_client.cache_set_json(ck, cached, settings.QA_CACHE_TTL)
    except Exception as e:
        degraded("citation_nli_async_cache_write", e)


def _injection_blocked(query: str) -> str | None:
    """高危注入拦截文案（红队缺口 #2）。返回拒答文本=拦截；None=放行。

    INJECTION_GUARD_STRICT_ENABLE 开启时，命中"指令覆盖/伪装 system"类硬注入
    → 结构化拒答；关=现状只告警不阻断（保守模式防误杀电网技术问题）。
    """
    if not getattr(settings, "INJECTION_GUARD_STRICT_ENABLE", False):
        return None
    hit, frag = safety.detect_injection_critical(query)
    if not hit:
        return None
    try:
        from app.core import metrics
        metrics.SAFETY_BLOCK.labels("injection_blocked").inc()
    except Exception:
        pass
    try:
        from loguru import logger
        logger.warning(f"[安全:injection_blocked] 高危注入已拦截 hit={frag} | query={(query or '')[:60]}")
    except Exception:
        pass
    return ("您的请求包含疑似指令注入内容（试图覆盖系统指令），已被安全策略拦截。"
            "请用自然语言描述电网运维问题（设备、故障现象、处置需求）。")


async def answer(
    db: AsyncSession, query: str, model_type: str | None = None,
    topk: int = 5, conversation_id: str | None = None, username: str = "",
    tenant: str = "default", user_dept: str | None = None, user_role: str | None = None,
) -> dict:
    t0 = time.time()
    with _trace_span("normalize"):
        nq = term_service.normalize(query)
        safety.guard_query(query)  # 入站 prompt injection 告警（D4）
    _blk = _injection_blocked(query)
    if _blk:
        return {
            "answer": _blk, "retrievalSource": [],
            "responseTime": round(time.time() - t0, 3), "hallucinationRate": 0.0,
            "cached": False, "confidence": "refused", "cragAction": "injection_blocked",
            "conversationId": conversation_id or "",
        }
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

    # hotqa：用户 like 写入的高频问答对（永久缓存）——最高优先级，命中直接返回，跳检索/CRAG/生成。
    # 多轮也命中（用户认可的高频答案与上下文无关）；HOTQA_ENABLE opt-out；异常吞掉走正常链路。
    with _trace_span("hotqa"):
        hot = await _hit_hotqa(nq, conversation_id or "", t0)
    _tc = _get_trace()
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        _tc.attach("hotqa", hit=bool(hot))
    if hot:
        try:
            from app.core import metrics
            metrics.cache_hit_inc("hotqa")
            metrics.QA_TOTAL.labels(model_type or settings.LLM_PROVIDER, "true").inc()
        except Exception:
            pass
        return hot

    # 多轮不走缓存（上下文变化）；单轮走三级缓存：Redis(L1) → 语义缓存(L1.5) → MySQL(L2) → LLM(L3)
    if is_single and not await _is_blacklisted(nq):
        # L1: Redis 热点缓存（精确 key 匹配）
        try:
            cached = await redis_client.cache_get_json(_cache_key(model_type, nq, tenant))
        except Exception as e:
            degraded("qa_cache_get", e)
            cached = None
        if cached and await _cache_knowledge_valid(db, cached, tenant):
            cached["cached"] = True
            cached["cacheLayer"] = "redis"
            cached["responseTime"] = round(time.time() - t0, 3)
            try:
                from app.core import metrics
                metrics.QA_TOTAL.labels(model_type or settings.LLM_PROVIDER, "true").inc()
                metrics.cache_hit_inc("redis")
            except Exception:
                pass
            # M6: 缓存命中路径也写 highbase（供 dislike 时 overconfident 检出）
            if cached.get("confidence") == "high":
                _fire_and_forget(_write_highbase(nq, cached.get("answer", ""), tenant))
            return cached

        # L2: MySQL 二级缓存（精确持久，Redis 过期/evict 时兜底；优先于模糊语义匹配）
        if settings.CACHE_PERSIST_ENABLE:
            try:
                from app.services.cache_persist import cache_get_mysql
                mysql_cached = await cache_get_mysql(db, model_type, nq, tenant_id=tenant)
                if mysql_cached and await _cache_knowledge_valid(db, mysql_cached, tenant):
                    mysql_cached["cached"] = True
                    mysql_cached["cacheLayer"] = "mysql"
                    mysql_cached["responseTime"] = round(time.time() - t0, 3)
                    try:
                        from app.core import metrics
                        metrics.QA_TOTAL.labels(model_type or settings.LLM_PROVIDER, "true").inc()
                        metrics.cache_hit_inc("mysql")
                    except Exception:
                        pass
                    if mysql_cached.get("confidence") == "high":
                        _fire_and_forget(_write_highbase(nq, mysql_cached.get("answer", ""), tenant))
                    return mysql_cached
            except Exception as e:
                degraded("qa_cache_mysql", e)

        # L1.5: 语义缓存（模糊相似，精确持久 miss 后兜底）
        if getattr(settings, "SEMANTIC_CACHE_ENABLE", False):
            try:
                from app.rag.semantic_cache import semantic_cache_get
                sc_data, sc_type, sc_sim = await semantic_cache_get(model_type, nq, tenant_id=tenant)
                if (
                    sc_data
                    and sc_type in ("semantic_high", "semantic_medium")
                    and await _cache_knowledge_valid(db, sc_data, tenant)
                ):
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
                    if sc_data.get("confidence") == "high":
                        _fire_and_forget(_write_highbase(nq, sc_data.get("answer", ""), tenant))
                    return sc_data
            except Exception as e:
                degraded("semantic_cache_get", e)

    # 多轮历史（提前获取：供指代消解 + 拼 LLM 上下文，避免重复查）
    history: list[dict] = []
    if conversation_id:
        history = await conversation_service.get_messages(db, conversation_id, _HISTORY_LIMIT)
        # P4-⑭ 多轮摘要（CONV_SUMMARY_ENABLE 默认关=原样；开=超阈值换「摘要+最近K条」）
        from app.services.conversation_summary import summarized_history_for_prompt
        history = await summarized_history_for_prompt(db, conversation_id, history)
    # 多轮指代消解：检索用改写后的独立查询
    with _trace_span("standalone_rewrite"):
        search_q = await _search_query_for_retrieve(db, query, nq, conversation_id, history, model_type)
    _tc = _get_trace()
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        _tc.attach("standalone_rewrite", rewritten=search_q[:160], changed=search_q != nq)

    # 多轮且 query 完整(standalone 未改写 search_q==nq) → 也查 Redis 热点缓存
    # 场景：用户点推荐问题(完整 query)接续对话，答案不依赖上下文，可安全命中
    if conversation_id and search_q == nq and not await _is_blacklisted(nq):
        try:
            cached = await redis_client.cache_get_json(_cache_key(model_type, nq, tenant))
            if cached and await _cache_knowledge_valid(db, cached, tenant):
                cached["cached"] = True
                cached["cacheLayer"] = "redis"
                cached["responseTime"] = round(time.time() - t0, 3)
                try:
                    from app.core import metrics
                    metrics.QA_TOTAL.labels(model_type or settings.LLM_PROVIDER, "true").inc()
                    metrics.cache_hit_inc("redis")
                except Exception:
                    pass
                if cached.get("confidence") == "high":
                    _fire_and_forget(_write_highbase(nq, cached.get("answer", ""), tenant))
                return cached
        except Exception as e:
            degraded("qa_cache_get_multi", e)

    # C4: 多轮 standalone(search_q!=nq) 缓存扩面——同对话同语义追问命中（MULTI_TURN_CACHE_ENABLE）
    if (conversation_id and search_q != nq
            and getattr(settings, "MULTI_TURN_CACHE_ENABLE", False)
            and not await _is_blacklisted(search_q)):
        try:
            cached = await redis_client.cache_get_json(
                _multi_cache_key(model_type, search_q, tenant, conversation_id))
            if cached and await _cache_knowledge_valid(db, cached, tenant):
                cached["cached"] = True
                cached["cacheLayer"] = "redis_multi"
                cached["responseTime"] = round(time.time() - t0, 3)
                try:
                    from app.core import metrics
                    metrics.QA_TOTAL.labels(model_type or settings.LLM_PROVIDER, "true").inc()
                    metrics.cache_hit_inc("redis_multi")
                except Exception:
                    pass
                if cached.get("confidence") == "high":
                    _fire_and_forget(_write_highbase(nq, cached.get("answer", ""), tenant))
                return cached
        except Exception as e:
            degraded("qa_cache_get_multi_standalone", e)

    # 智能路由：根据查询特征选择最优检索路径（Phase A）
    with _trace_span("routing"):
        routing = None
        if settings.ROUTING_ENABLE:
            try:
                from app.routing.routing_service import route_query
                routing = route_query(search_q)
            except Exception as e:
                degraded("routing_dispatch", e)
    _tc = _get_trace()
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False) and routing:
        _feat = getattr(routing, "features", None)
        _tc.attach("routing", route=routing.route, confidence=routing.confidence,
                   reason=(routing.reason or "")[:200],
                   queryType=(getattr(_feat, "query_type", "") or ""))

    with _trace_span("retrieval"):
        contexts = await retrieval_service.mixed_search(
            db, search_q, topk, tenant=tenant, routing_decision=routing,
            user_dept=user_dept, user_role=user_role,
        )
    _tc = _get_trace()
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        _tc.attach("retrieval", hits=len(contexts),
                   route=(routing.route if routing else "hybrid"),
                   top1=round(float(contexts[0].get("score") or 0), 4) if contexts else None)
    if not contexts:
        # 无结果兜底：记录为知识缺口（喂证据补全闭环）+ 友好引导，而非生硬拒答
        try:
            from app.services.evidence_gap_service import collect
            await collect(nq, "", "refused", "", "", "auto", tenant or "default")
        except Exception:
            pass
        return {
            "answer": (f"未在知识库检索到与「{nq[:40]}」直接相关的内容。建议：\n"
                       f"① 换用更具体的设备/故障术语重新提问（如设备型号、故障现象）；\n"
                       f"② 确认相关文档已上传并完成「解析 + 向量化」；\n"
                       f"③ 该问题已自动记录为知识缺口，补充资料后将纳入检索。"),
            "retrievalSource": [], "responseTime": round(time.time() - t0, 3),
            "hallucinationRate": 0.0, "cached": False, "confidence": "refused",
            "conversationId": conversation_id or "",
        }

    # Corrective RAG：分级 + 纠错闭环
    with _trace_span("crag"):
        contexts, confidence, crag_action, crag_grade, crag_extras = await _crag_correct(
            db, nq, contexts, model_type, topk, tenant
        )
    _tc = _get_trace()
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        _tc.attach("crag", grade=crag_grade, action=crag_action, confidence=confidence,
                   **({"extras": crag_extras} if crag_extras else {}))
    # 注：CRAG refused 的证据补全收集已挪到答案生成后（与 medium 共用既有 collect 点），
    # 避免 answer="" 被先写入导致真实答案被去重吞掉（I1 修复）。

    # GraphRAG：融合知识图谱结构化上下文（KG_RAG_ENABLE 默认开）
    with _trace_span("graphrag"):
        graph: list[str] = []
        if settings.KG_RAG_ENABLE:
            try:
                graph = await kg_service.graph_context(nq, db=db, tenant=tenant)
            except Exception as e:
                degraded("kg_graph_context", e)
                graph = []
    _tc = _get_trace()
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        _tc.attach("graphrag", lines=len(graph),
                   enabled=getattr(settings, "KG_RAG_ENABLE", False))
    _structured = (getattr(settings, "CITATION_STRUCTURED_OUTPUT", False)
                   and getattr(settings, "CITATION_VERIFIER_ENABLE", False))
    messages = prompt_templates.build_messages_with_history(
        nq, contexts, history, graph, confidence, structured=_structured,
    )
    try:
        from app.providers.llm_router import classify_llm
        _tier, _tier_reason = classify_llm(nq)
    except Exception:
        _tier, _tier_reason = "plus", "skip"
    _tc0 = _get_trace()
    if _tc0:
        _tc0.mark("llm_tier", _tier)
        _tc0.mark("llm_route_reason", _tier_reason)
    _llm0 = time.time()
    _llm_prov = get_llm_provider(model_type, tier=_tier)
    # B4：真实 token usage（opt-in，默认关 → 走原 chat str 路径，估算 token）
    _llm_usage: dict | None = None
    try:
        _temperature = config_service.rt_temperature()
        if getattr(settings, "LLM_USAGE_TRACK_ENABLE", False):
            raw, _llm_usage = await _llm_prov.chat_with_usage(
                messages, temperature=_temperature, max_tokens=settings.LLM_MAX_TOKENS,
            )
        else:
            raw = await _llm_prov.chat(
                messages, temperature=_temperature, max_tokens=settings.LLM_MAX_TOKENS)
    except Exception as e:
        # 全部 provider（含本地 ollama 兜底）耗尽 → 优雅拒答，不裸抛 500（I2）
        degraded("llm_all_exhausted", e)
        try:
            from app.services.evidence_gap_service import collect
            await collect(nq, "", "refused", "", "", "auto_llm_down", tenant or "default")
        except Exception:
            pass
        return _llm_all_down_response(nq, contexts, t0, conversation_id)
    _llm_fields = _llm_degradation_fields(_llm_prov, model_type)
    _tc = _get_trace()
    if _tc:
        _tc.record("llm", time.time() - _llm0)
        _tc.mark("provider_used", _llm_fields["modelType"])
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        from app.core.qa_trace import llm_attrs
        _tc.attach("llm", **llm_attrs(messages, temperature=_temperature,
                                      max_tokens=settings.LLM_MAX_TOKENS,
                                      usage=_llm_usage, model=_llm_fields["modelType"],
                                      output=raw))
    raw = safety.safe_answer(raw)  # 答案脱敏（PII_MASK_ENABLE 开启时，D4）
    # STRUCTURED_OUTPUT：LLM 输出 JSON → parse 取 answer_text + 结构化 citation_map(每 ref 一项不重复)
    # 跳过 auto_cite(结构化已有 cmap)；否则走 auto_cite 补标(现状)。
    _cmap_override = None
    if _structured:
        from app.rag.citation_index import build_index as _bi
        from app.schemas.citation import parse_citation_answer as _pca
        _parsed0 = _pca(raw, _bi(contexts), contexts)
        ans = _parsed0.answer_text or raw
        _trace = citation.evidence_trace(ans)
        if _parsed0.structured:           # LLM 真输出 JSON → 用结构化 cmap(每 ref_id 一项)
            _cmap_override = _parsed0.citation_map
    elif getattr(settings, "CITATION_AUTO_ENABLE", True):
        ans, _trace = await citation.auto_cite(raw, contexts)
    else:
        ans = raw
        _trace = citation.evidence_trace(ans)
    # ===== Task 10: 可核验引用三层校验 + 校验-CRAG 联动 =====
    # attach 必须在 citation span 关闭后（span 打开前 attach 找不到同名 span 会静默 no-op）
    with _trace_span("citation"):
        final_ans, citation_extras = await _apply_citation_verification(
            ans, contexts, model_type, db=db, cmap_override=_cmap_override,
        )
    _tc = _get_trace()
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        _tc.attach("citation", refs=len(contexts), annotated=len(_trace or []))
    # 校验要求 rewrite 且开关开 → 复用 rewrite_query + mixed_search 重检索重生成再 verify（最多 1 次，防死循环）
    # C2: CRAG 已做过改写轮（rewritten* 任一终态）或已 refused 时不再二次 rewrite
    # （citation 仍 needed→用现 contexts，省二次 LLM，防级联重检索；
    #   rewritten_failed 二次全链路只是重复注定失败的改写，refused 二次改写违背拒答语义）
    if (citation_extras.get("citationVerified", {}).get("rewrite_needed")
            and getattr(settings, "CITATION_REWRITE_ON_FAIL", True)
            and crag_action not in ("rewritten", "rewritten_recovered",
                                    "rewritten_partial", "rewritten_failed", "refused")):
        try:
            from app.services.query_rewrite import rewrite_query
            new_q = await rewrite_query(nq, model_type, force=True)
            if new_q and new_q != nq:
                contexts2 = await retrieval_service.mixed_search(
                    db, new_q, topk, tenant=tenant,
                    user_dept=user_dept, user_role=user_role,
                )
                if contexts2:
                    messages2 = prompt_templates.build_messages_with_history(
                        new_q, contexts2, history, graph, confidence, structured=_structured,
                    )
                    ans2 = await get_llm_provider(model_type).chat(
                        messages2, temperature=config_service.rt_temperature(),
                    )
                    ans2 = safety.safe_answer(ans2)
                    _cmap_override2 = None
                    if _structured:
                        from app.rag.citation_index import build_index as _bi
                        from app.schemas.citation import parse_citation_answer as _pca
                        _parsed2 = _pca(ans2, _bi(contexts2), contexts2)
                        ans2 = _parsed2.answer_text or ans2
                        if _parsed2.structured:
                            _cmap_override2 = _parsed2.citation_map
                    elif getattr(settings, "CITATION_AUTO_ENABLE", True):
                        ans2, _trace2 = await citation.auto_cite(ans2, contexts2)
                    final_ans, citation_extras = await _apply_citation_verification(
                        ans2, contexts2, model_type, db=db, cmap_override=_cmap_override2,
                    )
                    contexts = contexts2
                    _trace = citation.evidence_trace(final_ans)
        except Exception as e:
            degraded("citation_rewrite联动", e)
    ans = final_ans   # 校验/联动可能改写答案（drop→警示替换；rewrite→二次生成答案）
    # debate 低置信触发（opt-in）：共识前置注入答案 + 升级置信（接通旁支 debate_agent）
    ans, confidence = await _maybe_debate_augment(db, nq, ans, confidence, crag_extras, model_type)
    confidence = _cap_confidence_for_local_model(confidence, _llm_fields["modelType"])
    # ===== /Task 10 =====
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
        metrics.UNGROUNDED_RATIO.observe(_halluc)   # 启发式未引用率(廉价代理)；HALLUC 留给 LLM-judge 真值
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
        **_llm_fields,
        **_retrieval_degradation_fields(),
        **citation_extras,   # Task 10: 空 dict（开关关）时不新增字段，零破坏
        **crag_extras,       # T2: CRAG_V3 归因字段（关={}零破坏；随 result 进缓存自动同步）
    }

    # 单轮 或 多轮 且 高置信(confidence==high) 才写；黑名单/证据有限/不足不写
    # 注：多轮指代问题(search_q!=nq)会写但读时按 search_q==nq 过滤(避免跨对话脏命中)
    if (is_single or conversation_id) and confidence == "high" and not await _is_blacklisted(nq):
        # L2: MySQL 持久化（先写，保证数据不丢）
        if settings.CACHE_PERSIST_ENABLE:
            try:
                from app.services.cache_persist import cache_set_mysql
                await cache_set_mysql(db, model_type, nq, query, result, tenant_id=tenant)
            except Exception as e:
                degraded("qa_cache_mysql_set", e)
        # L1: Redis 热点（后写，MySQL 已成功）
        try:
            await redis_client.cache_set_json(_cache_key(model_type, nq, tenant), result, settings.QA_CACHE_TTL)
        except Exception as e:
            degraded("qa_cache_set", e)
        # C4: 多轮 standalone 额外写 multi key（search_q!=nq + 开关开；读配对见上方 C4 块）
        if (conversation_id and search_q != nq
                and getattr(settings, "MULTI_TURN_CACHE_ENABLE", False)):
            try:
                await redis_client.cache_set_json(
                    _multi_cache_key(model_type, search_q, tenant, conversation_id),
                    result, settings.QA_CACHE_TTL)
            except Exception as e:
                degraded("qa_cache_set_multi_standalone", e)
        # L1.5: 语义缓存索引（异步，不阻塞）
        if getattr(settings, "SEMANTIC_CACHE_ENABLE", False):
            try:
                from app.rag.semantic_cache import semantic_cache_set
                await semantic_cache_set(model_type, nq, _cache_key(model_type, nq, tenant), tenant_id=tenant)
            except Exception as e:
                degraded("semantic_cache_set", e)
    # B6: high 答案写独立 highbase key（TTL 30 天，不依赖问答缓存存活）——供 dislike 时 overconfident 检出
    if confidence == "high":
        _fire_and_forget(_write_highbase(nq, ans, tenant))
    try:
        from app.core import metrics
        metrics.QA_TOTAL.labels(model_type or settings.LLM_PROVIDER, "false").inc()
        metrics.cache_hit_inc("llm")
    except Exception:
        pass
    # 成本追踪（记录 token 用量 → 成本报告数据来源）
    # B4：LLM_USAGE_TRACK_ENABLE 开 → 用 r.usage 真实 token；关 → 沿用 len//2 估算
    try:
        import asyncio
        from app.services.cost_tracker_service import record_token_usage
        if _llm_usage:
            _in_tok, _out_tok = _llm_usage.get("input", 0), _llm_usage.get("output", 0)
        else:
            _in_tok, _out_tok = len(str(messages)) // 2, len(ans) // 2
        asyncio.ensure_future(record_token_usage(db, username, tenant, model_type or settings.LLM_PROVIDER, _in_tok, _out_tok))
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
    # I1：refused 路径 source=auto_crag（覆盖原 B4 非流式独立路径，answer=真实 ans 保留）
    if settings.EVIDENCE_GAP_AUTO_COLLECT and confidence in ("medium", "refused"):
        try:
            from app.services import evidence_gap_service
            _src = "auto_crag" if (confidence == "refused" or crag_action in ("rewritten_failed", "refused")) else "auto"
            _fire_and_forget(evidence_gap_service.collect(
                nq, ans, confidence, crag_grade, crag_action, _src, tenant,
            ))
        except Exception:
            pass
    return result


async def _stream_agent(db, query, model_type, conversation_id, username, tenant, t0,
                        user_role: str | None = None,
                        memory_read: bool = False, memory_write: bool = False,
                        memory_scope: str = "user", trace_id: str = ""):
    """S2: Agent 流式（meta→tool_step×N→token→done）。run_agent on_step→asyncio.Queue 桥接。
    单轮缓存（Redis L1 + MySQL qa_cache L2，复用三级缓存）：命中跳过 agent；done 后写。
    记忆治理：memory_read/memory_write 任一开启=个性化运行，绕开共享缓存（防记忆答案污染缓存池）。"""
    import asyncio
    from app.services.agent_runtime import run_agent
    from app.services.persona_store import get_persona

    is_single = not conversation_id
    personalized = bool(memory_read or memory_write)
    nq = term_service.normalize(query)
    key = _cache_key(model_type, nq, tenant)
    _p = model_type or settings.LLM_PROVIDER

    # 单轮查缓存（L1 Redis → L2 MySQL），命中→流式返缓存答案（不跑 agent，秒级）
    if is_single and not personalized:
        cached = None
        try:
            cached = await redis_client.cache_get_json(key)
        except Exception as e:
            degraded("agent_cache_get", e)
        if not cached and getattr(settings, "CACHE_PERSIST_ENABLE", False):
            try:
                from app.services.cache_persist import cache_get_mysql
                cached = await cache_get_mysql(db, model_type, nq, tenant_id=tenant)
            except Exception as e:
                degraded("agent_cache_mysql", e)
        if (
            cached
            and cached.get("answer")
            and await _cache_knowledge_valid(db, cached, tenant)
        ):
            conv = await conversation_service.create_conversation(db, username, query)
            cid = conv.id
            try:
                await conversation_service.save_message(db, cid, "user", query)
                await conversation_service.save_message(db, cid, "assistant", cached["answer"])
            except Exception as e:
                degraded("agent_cache_conv", e)
            yield {"type": "meta", "sources": cached.get("retrievalSource", []),
                   "conversationId": cid, "agentMode": True, "cached": True,
                   "cacheLayer": cached.get("cacheLayer", "redis")}
            yield {"type": "token", "content": cached["answer"]}
            try:
                from app.core import metrics
                metrics.QA_TOTAL.labels(_p, "true").inc()
                metrics.cache_hit_inc(cached.get("cacheLayer", "redis") or "redis")
            except Exception:
                pass
            if cached.get("confidence") == "high":
                _fire_and_forget(_write_highbase(nq, cached.get("answer", ""), tenant))
            yield {"type": "done", "responseTime": round(time.time() - t0, 3),
                   "conversationId": cid, "agentMode": True, "cached": True,
                   "cacheLayer": cached.get("cacheLayer", "redis")}
            return

    # miss → 跑 agent
    if not conversation_id:
        conv = await conversation_service.create_conversation(db, username, query)
        conversation_id = conv.id
    yield {"type": "meta", "sources": [], "conversationId": conversation_id, "agentMode": True}

    queue: asyncio.Queue = asyncio.Queue()

    async def _run():
        try:
            qa_persona = await get_persona("qa")
            res = await run_agent(
                db, qa_persona, query, model_type,
                ctx={"username": username, "tenant": tenant, "role": user_role or "",
                     "memoryRead": memory_read, "memoryWrite": memory_write,
                     "memoryScope": memory_scope, "trace_id": trace_id},
                on_step=lambda s: queue.put_nowait({"type": "tool_step", "step": s}),
            )
            await queue.put({"type": "_result", "result": res})
        except Exception as e:
            degraded("qa_agent_stream", e)
            await queue.put({"type": "_error", "error": f"{type(e).__name__}: {e}"})

    task = asyncio.create_task(_run())
    try:
        while True:
            item = await queue.get()
            t = item["type"]
            if t == "tool_step":
                yield item
            elif t == "_result":
                res = item["result"]
                ans = res.answer if isinstance(res.answer, str) else str(res.answer)
                # 单轮 + 非降级 + 非个性化 → 写缓存（agent 多轮验证质量高，默认 high；与普通问答共享 key）
                if is_single and not res.degraded and not personalized:
                    cache_result = {
                        "answer": ans, "retrievalSource": [], "confidence": "high",
                        "responseTime": round(time.time() - t0, 3), "hallucinationRate": 0.0,
                        "cached": True, "cacheLayer": "redis", "agentMode": True,
                    }
                    if getattr(settings, "CACHE_PERSIST_ENABLE", False):
                        try:
                            from app.services.cache_persist import cache_set_mysql
                            await cache_set_mysql(db, model_type, nq, query, cache_result, tenant_id=tenant)
                        except Exception as e:
                            degraded("agent_cache_mysql_set", e)
                    try:
                        await redis_client.cache_set_json(key, cache_result, settings.QA_CACHE_TTL)
                    except Exception as e:
                        degraded("agent_cache_set", e)
                yield {"type": "token", "content": ans}
                try:
                    await conversation_service.save_message(db, conversation_id, "user", query)
                    await conversation_service.save_message(db, conversation_id, "assistant", ans)
                except Exception as e:
                    degraded("agent_conv_save", e)
                yield {"type": "done", "responseTime": round(time.time() - t0, 3),
                       "conversationId": conversation_id, "agentMode": True,
                       "iterations": res.iterations, "degraded": res.degraded,
                       "toolsUsed": res.tools_used, "cached": False}
                break
            else:  # _error
                yield {"type": "done", "responseTime": round(time.time() - t0, 3),
                       "conversationId": conversation_id, "agentMode": True,
                       "degraded": True, "error": item["error"]}
                break
    finally:
        await task


async def stream_answer(
    db: AsyncSession, query: str, model_type: str | None = None,
    topk: int = 5, conversation_id: str | None = None, username: str = "",
    tenant: str = "default", regen: bool = False, agent_mode: bool = False,
    user_dept: str | None = None, user_role: str | None = None,
    memory_read: bool = False, memory_write: bool = False,
    memory_scope: str = "user", trace_id: str = "",
):
    """流式问答：单轮查热点缓存(命中则快流不调LLM) → 否则 meta/token/done 三段。

    agent_mode=True（S2）：走通用 Agent 引擎(QA_PERSONA)，流式推 meta→tool_step×N→token→done。
    memory_read/memory_write：Agent 模式长期记忆 opt-in（个性化运行绕开共享缓存）。
    """
    t0 = time.time()
    nq = term_service.normalize(query)
    _p = model_type or settings.LLM_PROVIDER
    safety.guard_query(query)  # 入站 prompt injection 告警（D4）
    _blk = _injection_blocked(query)
    if _blk:
        yield {"type": "meta", "sources": [], "conversationId": conversation_id or ""}
        yield {"type": "token", "content": _blk}
        yield {"type": "done", "responseTime": round(time.time() - t0, 3),
               "confidence": "refused", "cragAction": "injection_blocked",
               "conversationId": conversation_id or "", "cached": False}
        return
    if agent_mode:
        async for ev in _stream_agent(db, query, model_type, conversation_id, username, tenant, t0,
                                      user_role=user_role,
                                      memory_read=memory_read, memory_write=memory_write,
                                      memory_scope=memory_scope, trace_id=trace_id):
            yield ev
        return
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

    # hotqa：用户 like 写入的高频问答对（永久缓存）——最高优先级，命中直接快流，跳检索/CRAG/生成。
    # regen=True（强制重新生成）跳过 hotqa；HOTQA_ENABLE opt-out；异常吞掉走正常链路。
    if not regen:
        with _trace_span("hotqa"):
            hot = await _hit_hotqa(nq, conversation_id or "", t0)
        _tc = _get_trace()
        if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
            _tc.attach("hotqa", hit=bool(hot))
        if hot:
            # 多轮命中 hotqa 也建独立 conv 落消息（与现有缓存命中段一致），单轮用 conversation_id
            cid = conversation_id
            if not cid:
                try:
                    conv = await conversation_service.create_conversation(db, username, query)
                    cid = conv.id
                except Exception as e:
                    degraded("hotqa_conv_create", e)
                    cid = conversation_id or ""
            else:
                try:
                    await conversation_service.save_message(db, cid, "user", query)
                    await conversation_service.save_message(db, cid, "assistant", hot.get("answer", ""))
                except Exception as e:
                    degraded("hotqa_conv_save", e)
            yield {"type": "meta", "sources": hot.get("retrievalSource", []),
                   "conversationId": cid or "", "cached": True, "cacheLayer": "hotqa"}
            yield {"type": "token", "content": hot.get("answer", "")}
            try:
                from app.core import metrics
                metrics.cache_hit_inc("hotqa")
                metrics.QA_TOTAL.labels(_p, "true").inc()
            except Exception:
                pass
            yield {
                "type": "done", "responseTime": round(time.time() - t0, 3),
                "hallucinationRate": 0.0, "conversationId": cid or "",
                "cached": True, "cacheLayer": "hotqa", "confidence": "high",
                "route": "hotqa",
            }
            return

    # 0) 单轮三级缓存：Redis(L1) → MySQL(L2) → LLM(L3)
    #    regen=True（重新生成）跳过缓存读，强制重走 LLM
    if is_single and not regen and not await _is_blacklisted(nq):
        # L1: Redis 热点
        try:
            cached = await redis_client.cache_get_json(_cache_key(model_type, nq, tenant))
        except Exception as e:
            degraded("qa_cache_get", e)
            cached = None
        if cached and await _cache_knowledge_valid(db, cached, tenant):
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
            if cached.get("confidence") == "high":
                _fire_and_forget(_write_highbase(nq, cached.get("answer", ""), tenant))
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
                mysql_cached = await cache_get_mysql(db, model_type, nq, tenant_id=tenant)
                if mysql_cached and await _cache_knowledge_valid(db, mysql_cached, tenant):
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
                    if mysql_cached.get("confidence") == "high":
                        _fire_and_forget(_write_highbase(nq, mysql_cached.get("answer", ""), tenant))
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
                sc_data, sc_type, sc_sim = await semantic_cache_get(model_type, nq, tenant_id=tenant)
                if (
                    sc_data
                    and sc_type in ("semantic_high", "semantic_medium")
                    and await _cache_knowledge_valid(db, sc_data, tenant)
                ):
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
                    if sc_data.get("confidence") == "high":
                        _fire_and_forget(_write_highbase(nq, sc_data.get("answer", ""), tenant))
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
        # P4-⑭ 多轮摘要（CONV_SUMMARY_ENABLE 默认关=原样；开=超阈值换「摘要+最近K条」）
        from app.services.conversation_summary import summarized_history_for_prompt
        history = await summarized_history_for_prompt(db, conversation_id, history)
    with _trace_span("standalone_rewrite"):
        search_q = await _search_query_for_retrieve(db, query, nq, conversation_id, history, model_type)
    _tc = _get_trace()
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        _tc.attach("standalone_rewrite", rewritten=search_q[:160], changed=search_q != nq)

    # 多轮且 query 完整(search_q==nq) → 也查 Redis 热点（流式，存消息接续对话）
    if conversation_id and search_q == nq and not regen and not await _is_blacklisted(nq):
        try:
            cached = await redis_client.cache_get_json(_cache_key(model_type, nq, tenant))
            if cached and await _cache_knowledge_valid(db, cached, tenant):
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
                if cached.get("confidence") == "high":
                    _fire_and_forget(_write_highbase(nq, cached.get("answer", ""), tenant))
                yield {"type": "done", "responseTime": round(time.time() - t0, 3),
                       "hallucinationRate": cached.get("hallucinationRate", 0.0),
                       "conversationId": conversation_id, "cached": True, "cacheLayer": "redis",
                       "route": cached.get("route", "hybrid")}
                return
        except Exception as e:
            degraded("qa_cache_get_multi_stream", e)

    # 智能路由：根据查询特征选择最优检索路径（Phase A）
    with _trace_span("routing"):
        routing = None
        if settings.ROUTING_ENABLE:
            try:
                from app.routing.routing_service import route_query
                routing = route_query(search_q)
            except Exception as e:
                degraded("routing_dispatch", e)
    _tc = _get_trace()
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False) and routing:
        _feat = getattr(routing, "features", None)
        _tc.attach("routing", route=routing.route, confidence=routing.confidence,
                   reason=(routing.reason or "")[:200],
                   queryType=(getattr(_feat, "query_type", "") or ""))

    with _trace_span("retrieval"):
        contexts = await retrieval_service.mixed_search(
            db, search_q, topk, tenant=tenant, routing_decision=routing,
            user_dept=user_dept, user_role=user_role,
        )
    _tc = _get_trace()
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        _tc.attach("retrieval", hits=len(contexts),
                   route=(routing.route if routing else "hybrid"),
                   top1=round(float(contexts[0].get("score") or 0), 4) if contexts else None)
    if not contexts:
        # B7：stream 无结果 → 自动入证据补全队列（auto_no_recall）
        if getattr(settings, "CRAG_REFUSED_TO_GAP_ENABLE", True):
            _fire_and_forget(_maybe_collect_refused(
                nq=nq, answer="", confidence="refused",
                grade="incorrect", action="", tenant=tenant or "default"))
        yield {"type": "done", "content": "根据现有资料无法确认该问题，请先上传并解析相关运维文档后重试。"}
        return

    # Corrective RAG：分级 + 纠错闭环
    with _trace_span("crag"):
        contexts, confidence, crag_action, crag_grade, crag_extras = await _crag_correct(
            db, nq, contexts, model_type, topk, tenant
        )
    _tc = _get_trace()
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        _tc.attach("crag", grade=crag_grade, action=crag_action, confidence=confidence,
                   **({"extras": crag_extras} if crag_extras else {}))
    # 注：stream CRAG refused 的证据补全收集已挪到答案生成后（与 medium 共用既有 collect 点），
    # 避免 answer="" 被先写入导致真实答案被去重吞掉（I1 修复，与非流式路径对齐）。

    # GraphRAG
    graph: list[str] = []
    if settings.KG_RAG_ENABLE:
        try:
            graph = await kg_service.graph_context(nq, db=db, tenant=tenant)
        except Exception as e:
            degraded("kg_graph_context", e)
            graph = []
    _tc = _get_trace()
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        # 流式路径 graphrag 无既有 span → attach 静默 no-op（与 Task 1 契约一致）
        _tc.attach("graphrag", lines=len(graph),
                   enabled=getattr(settings, "KG_RAG_ENABLE", False))
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
    try:
        from app.providers.llm_router import classify_llm
        _tier, _tier_reason = classify_llm(query)
    except Exception:
        _tier, _tier_reason = "plus", "skip"
    _tc0 = _get_trace()
    if _tc0:
        _tc0.mark("llm_tier", _tier)
        _tc0.mark("llm_route_reason", _tier_reason)
    parts: list[str] = []
    _llm0 = time.time()
    _llm_prov = get_llm_provider(model_type, tier=_tier)
    try:
        _temperature = config_service.rt_temperature()
        async for token in _llm_prov.stream(
            messages, temperature=_temperature, max_tokens=settings.LLM_MAX_TOKENS):
            parts.append(token)
            yield {"type": "token", "content": token}
    except Exception as e:
        # 全部 provider（含本地 ollama 兜底）耗尽 → 优雅收尾，不让 SSE 硬断（I2）
        degraded("llm_all_exhausted_stream", e)
        if not parts:
            yield {
                "type": "error",
                "content": "当前所有 AI 模型（含本地应急模型）暂时不可用，请稍后重试",
                "confidence": "refused", "conversationId": conversation_id or "",
                "responseTime": round(time.time() - t0, 3),
            }
            return
        parts.append("\n（后续生成中断：服务异常）")
    _llm_fields = _llm_degradation_fields(_llm_prov, model_type)
    _p = _llm_fields["modelType"]  # 下游 metrics/modelType 统一用实际命中的 provider（修复失真）
    try:
        from app.core import metrics
        metrics.LLM_CALLS.labels(_p).inc()
        metrics.LLM_LATENCY.labels(_p).observe(time.time() - _llm0)
    except Exception:
        pass
    _tc = _get_trace()
    if _tc:
        _tc.record("llm", time.time() - _llm0)   # 流式 LLM 总耗时(首token→末token)
        _tc.mark("provider_used", _p)
    confidence = _cap_confidence_for_local_model(confidence, _p)

    # 3) 持久化完整答案
    full = "".join(parts)
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        from app.core.qa_trace import llm_attrs
        _tc.attach("llm", **llm_attrs(messages, temperature=_temperature,
                                      max_tokens=settings.LLM_MAX_TOKENS,
                                      usage=None, model=_p, output=full))
    # 证据溯源：补标（done 段下发 annotatedAnswer，前端替换渲染出角标；持久化/缓存均用补标后）
    # 流式路径本无 citation span → 用 record() 补计时+attrs（开关内，关=现状无此行）
    _cit0 = time.time()
    if getattr(settings, "CITATION_AUTO_ENABLE", True):
        annotated, _trace = await citation.auto_cite(full, contexts)
    else:
        annotated, _trace = full, citation.evidence_trace(full)
    _tc = _get_trace()
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        _tc.record("citation", time.time() - _cit0, refs=len(contexts),
                   annotated=len(_trace or []))
    # C3：流式接校验（CITATION_VERIFIER_ENABLE 开时，done 前同步跑校验1+2；NLI 按 C1 异步后置）。
    # annotated 可能被校验 drop 编号→警示替换；citationVerified 随 done 下发（前端零改动，不读该字段）。
    _stream_citation_extras: dict = {}
    if getattr(settings, "CITATION_VERIFIER_ENABLE", False):
        try:
            annotated, _stream_citation_extras = await _apply_citation_verification(
                annotated, contexts, model_type, db=db, query=nq, tenant=tenant,
            )
        except Exception as e:
            degraded("stream_citation_verify", e)
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
        metrics.UNGROUNDED_RATIO.observe(halluc)   # 启发式未引用率(廉价代理)；HALLUC 留给 LLM-judge 真值
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
                await cache_set_mysql(db, model_type, nq, query, cache_data, tenant_id=tenant)
            except Exception as e:
                degraded("qa_cache_mysql_set_stream", e)
        # L1: Redis 热点（后写）
        try:
            await redis_client.cache_set_json(_cache_key(model_type, nq, tenant), cache_data, settings.QA_CACHE_TTL)
        except Exception as e:
            degraded("qa_cache_set", e)
        # L1.5: 语义缓存索引（向量化入库，供后续相似查询命中）
        # debate 低置信触发（opt-in）：annotated 置信低 → 三专家裁决共识注入 + 升级置信
        annotated, confidence = await _maybe_debate_augment(db, nq, annotated, confidence, crag_extras, model_type)
        if getattr(settings, "SEMANTIC_CACHE_ENABLE", False):
            try:
                from app.rag.semantic_cache import semantic_cache_set
                await semantic_cache_set(model_type, nq, _cache_key(model_type, nq, tenant), tenant_id=tenant)
            except Exception as e:
                degraded("semantic_cache_set_stream", e)
    # B6: high 答案写独立 highbase key（TTL 30 天，不依赖问答缓存存活）——供 dislike 时 overconfident 检出
    if confidence == "high":
        _fire_and_forget(_write_highbase(nq, annotated, tenant))
    try:
        from app.core import metrics
        metrics.QA_TOTAL.labels(_p, "false").inc()
        metrics.cache_hit_inc("llm")
    except Exception:
        pass
    # 证据补全：medium/refused 自动收集（bg task，独立 session，不阻塞流式）
    # I1：refused 路径 source=auto_crag（覆盖原 stream B4 独立路径，answer=真实 annotated 保留）
    if settings.EVIDENCE_GAP_AUTO_COLLECT and confidence in ("medium", "refused"):
        try:
            from app.services import evidence_gap_service
            _src = "auto_crag" if (confidence == "refused" or crag_action in ("rewritten_failed", "refused")) else "auto"
            _fire_and_forget(evidence_gap_service.collect(
                nq, annotated, confidence, crag_grade, crag_action, _src, tenant,
            ))
        except Exception:
            pass
    done_ev = {
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
        "llmDegraded": _llm_fields["llmDegraded"],
        "llmDegradedReason": _llm_fields["llmDegradedReason"],
        **_retrieval_degradation_fields(),
    }
    # C3：校验开时随 done 下发 citationVerified（关时不带此字段=现状，前端零改动）
    if _stream_citation_extras.get("citationVerified"):
        done_ev["citationVerified"] = _stream_citation_extras["citationVerified"]
    # T2：CRAG_V3 新字段附加下发（CRAG_V3_ENABLE 关时 crag_extras={}，前端零改动）
    done_ev.update(crag_extras)
    yield done_ev


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
