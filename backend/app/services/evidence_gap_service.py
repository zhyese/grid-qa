"""证据补全：收集(medium/refused 去重) + AI续写 + 人工确认 + 同步入库回流。

bg task 用独立 AsyncSessionLocal（防 session 并发 500）。
"""
from datetime import datetime

from sqlalchemy import desc, func, select

from app.core.obs import degraded
from app.db.session import AsyncSessionLocal
from app.models.evidence_gap import EvidenceGap


async def collect(query: str, answer: str, confidence: str, grade: str, action: str,
                  source: str = "auto", tenant: str = "default") -> int:
    """去重写：同 query 已有 pending 则跳过。返回新 id（0=去重跳过/异常）。bg task 用独立 session。"""
    if not query:
        return 0
    try:
        async with AsyncSessionLocal() as db:
            existing = (await db.execute(
                select(EvidenceGap).where(EvidenceGap.query == query, EvidenceGap.status == "pending")
            )).scalar_one_or_none()
            if existing:
                return 0
            row = EvidenceGap(query=query, original_answer=(answer or "")[:2000],
                              confidence=confidence, grade=grade, crag_action=action,
                              source=source, tenant=tenant)
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return row.id
    except Exception as e:
        degraded("evidence_gap_collect", e)
        return 0


async def list_gaps(status: str | None = None, page: int = 1, size: int = 20) -> dict:
    try:
        async with AsyncSessionLocal() as db:
            q = select(EvidenceGap).order_by(desc(EvidenceGap.ts))
            cq = select(func.count()).select_from(EvidenceGap)
            if status:
                q = q.where(EvidenceGap.status == status)
                cq = cq.where(EvidenceGap.status == status)
            total = (await db.execute(cq)).scalar() or 0
            rows = (await db.execute(q.offset((page - 1) * size).limit(size))).scalars().all()
            return {"total": total, "list": [{
                "id": r.id, "ts": r.ts.strftime("%Y-%m-%d %H:%M") if r.ts else "",
                "query": r.query, "originalAnswer": (r.original_answer or "")[:200],
                "confidence": r.confidence, "status": r.status, "source": r.source,
                "aiDraft": r.ai_draft, "finalAnswer": r.final_answer, "syncedDocId": r.synced_doc_id,
            } for r in rows]}
    except Exception as e:
        degraded("evidence_gap_list", e)
        return {"total": 0, "list": []}


async def get_gap(gap_id: int) -> dict | None:
    try:
        async with AsyncSessionLocal() as db:
            r = (await db.execute(select(EvidenceGap).where(EvidenceGap.id == gap_id))).scalar_one_or_none()
            if not r:
                return None
            return {"id": r.id, "query": r.query, "confidence": r.confidence, "status": r.status,
                    "aiDraft": r.ai_draft, "finalAnswer": r.final_answer, "originalAnswer": r.original_answer}
    except Exception as e:
        degraded("evidence_gap_get", e)
        return None


async def ai_draft(gap_id: int, model_type: str | None = None) -> str:
    """AI 续写草稿：放宽检索(topk×倍) 再 LLM 生成。失败返回空串。状态 pending→ai_drafted。"""
    from app.config import settings
    from app.services import retrieval_service
    from app.providers.factory import get_llm_provider
    from app.rag import prompt_templates
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(EvidenceGap).where(EvidenceGap.id == gap_id))).scalar_one_or_none()
            if not row:
                return ""
            topk = 5 * settings.EVIDENCE_GAP_DRAFT_TOPK_MULT
            contexts = await retrieval_service.mixed_search(db, row.query, topk, tenant=row.tenant)
            messages = prompt_templates.build_messages_with_history(row.query, contexts, [], [], "medium")
            draft = (await get_llm_provider(model_type).chat(messages, temperature=0.3)).strip()
            row.ai_draft = draft
            row.status = "ai_drafted"
            await db.commit()
            return draft
    except Exception as e:
        degraded("evidence_gap_ai_draft", e)
        return ""


async def deep_draft(gap_id: int, model_type: str | None = None) -> str:
    """深度AI补全：Agent 引擎(QA persona)多轮调工具交叉验证，比 ai_draft(single-pass)更深。
    状态 pending→ai_drafted，结果写 ai_draft。复用 S1 agent_runtime + S5 get_persona。"""
    from app.services.agent_runtime import run_agent
    from app.services.persona_store import get_persona
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(EvidenceGap).where(EvidenceGap.id == gap_id))).scalar_one_or_none()
            if not row:
                return ""
            persona = await get_persona("qa")
            result = await run_agent(db, persona, row.query, model_type)
            draft = result.answer if isinstance(result.answer, str) else str(result.answer)
            row.ai_draft = draft
            row.status = "ai_drafted"
            await db.commit()
            return draft
    except Exception as e:
        degraded("evidence_gap_deep_draft", e)
        return ""


async def deep_draft_stream(gap_id: int, model_type: str | None = None):
    """SSE 流式深度补全：meta→tool_step×N→token→done。done 时更新 gap.ai_draft。
    复用 agent_runtime on_step + asyncio.Queue 桥接（同 qa _stream_agent 模式）。"""
    import asyncio
    from app.services.agent_runtime import run_agent
    from app.services.persona_store import get_persona
    query = ""
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(EvidenceGap).where(EvidenceGap.id == gap_id))).scalar_one_or_none()
            query = row.query if row else ""
    except Exception as e:
        degraded("evidence_gap_deep_stream_get", e)
    if not query:
        yield {"type": "done", "error": "记录不存在或 query 为空"}
        return
    yield {"type": "meta", "query": query}
    persona = await get_persona("qa")
    queue: asyncio.Queue = asyncio.Queue()

    async def _run():
        try:
            async with AsyncSessionLocal() as db:
                res = await run_agent(db, persona, query, model_type,
                                      on_step=lambda s: queue.put_nowait({"type": "tool_step", "step": s}))
            await queue.put({"type": "_result", "result": res})
        except Exception as e:
            degraded("evidence_gap_deep_stream", e)
            await queue.put({"type": "_error", "error": f"{type(e).__name__}: {e}"})

    task = asyncio.create_task(_run())
    draft = ""
    try:
        while True:
            item = await queue.get()
            t = item["type"]
            if t == "tool_step":
                yield item
            elif t == "_result":
                res = item["result"]
                draft = res.answer if isinstance(res.answer, str) else str(res.answer)
                yield {"type": "token", "content": draft}
                try:
                    async with AsyncSessionLocal() as db:
                        r = (await db.execute(select(EvidenceGap).where(EvidenceGap.id == gap_id))).scalar_one_or_none()
                        if r:
                            r.ai_draft = draft
                            r.status = "ai_drafted"
                            await db.commit()
                except Exception as e:
                    degraded("evidence_gap_deep_save", e)
                yield {"type": "done", "iterations": res.iterations, "degraded": res.degraded}
                break
            else:
                yield {"type": "done", "error": item.get("error", "未知错误")}
                break
    finally:
        await task


async def update_ai_draft(gap_id: int, ai_draft: str) -> dict:
    """就地编辑保存 AI 草稿（更新 ai_draft，status 保持 ai_drafted）。"""
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(EvidenceGap).where(EvidenceGap.id == gap_id))).scalar_one_or_none()
            if not row:
                return {"ok": False, "msg": "记录不存在"}
            row.ai_draft = ai_draft or ""
            row.status = "ai_drafted"
            await db.commit()
            return {"ok": True}
    except Exception as e:
        degraded("evidence_gap_update_ai_draft", e)
        return {"ok": False, "msg": str(e)}


async def update_confidence(gap_id: int, confidence: str) -> dict:
    """后台标注/修改 confidence（如 refused→充足 sufficient）。"""
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(EvidenceGap).where(EvidenceGap.id == gap_id))).scalar_one_or_none()
            if not row:
                return {"ok": False, "msg": "记录不存在"}
            row.confidence = confidence or ""
            await db.commit()
            return {"ok": True}
    except Exception as e:
        degraded("evidence_gap_update_confidence", e)
        return {"ok": False, "msg": str(e)}


async def edit_answer(gap_id: int, final_answer: str) -> dict:
    """人工编辑保存最终答案，status→confirmed（不同步，待「确认同步」触发入库）。"""
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(EvidenceGap).where(EvidenceGap.id == gap_id))).scalar_one_or_none()
            if not row:
                return {"ok": False, "msg": "记录不存在"}
            row.final_answer = final_answer
            row.status = "confirmed"
            await db.commit()
            return {"ok": True}
    except Exception as e:
        degraded("evidence_gap_edit", e)
        return {"ok": False, "msg": str(e)}


async def confirm_and_sync(gap_id: int, final_answer: str, operator: str,
                           model_type: str | None = None) -> dict:
    """人工确认 final_answer + 同步入库（FAQ文档 vectorize + qa_cache/Redis 双写）。状态→synced。

    FAQ 答案短，直接 1 个 chunk（不切块）；复用 document_service.vectorize_document 向量化。
    """
    import uuid
    from app.config import settings
    from app.models.document import Document
    from app.models.chunk import Chunk
    from app.services import document_service
    from app.services.cache_persist import cache_set_mysql
    from app.clients import redis_client
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(EvidenceGap).where(EvidenceGap.id == gap_id))).scalar_one_or_none()
            if not row:
                return {"ok": False, "msg": "记录不存在"}
            nq = row.query
            if not (final_answer or "").strip():
                return {"ok": False, "msg": "final_answer 为空"}
            # 1) FAQ 文档：答案(markdown)存为 .md 上传 MinIO → 标准文档(预览/下载/版本走统一流程)
            import asyncio as _asyncio
            from app.clients import minio_client
            doc_id = uuid.uuid4().hex
            object_name = f"faq/{doc_id}.md"
            md_bytes = (final_answer or "").encode("utf-8")
            try:
                await _asyncio.to_thread(
                    minio_client.put_object, object_name, md_bytes, len(md_bytes), "text/markdown")
            except Exception as e:
                degraded("evidence_gap_faq_put", e)
            _tags = ""
            try:
                from app.services.document_service import _auto_equipment_tags
                _tags = _auto_equipment_tags(final_answer or "")
            except Exception as e:
                degraded("evidence_gap_faq_equipment_tags", e)
            db.add(Document(
                id=doc_id, doc_name=f"FAQ:{nq[:40]}.md", doc_type=settings.EVIDENCE_GAP_FAQ_DOCTYPE,
                minio_object=object_name, file_size=len(md_bytes),
                upload_user=operator, tenant_id=row.tenant, status="parsed", chunk_count=1,
                equipment_tags=_tags,
            ))
            await db.flush()   # 先把 Document 落库（满足 chunks 外键，避免同 commit flush 顺序致 1452）
            db.add(Chunk(
                doc_id=doc_id, chunk_idx=0, content=final_answer,
                char_count=len(final_answer), chunk_type="text", parent_idx=0, section="",
            ))
            await db.commit()
            # 2) 向量化（复用 vectorize_document，写 Milvus）
            await document_service.vectorize_document(db, doc_id)
            # 3) 缓存双写（qa_cache high + Redis）
            result = {
                "answer": final_answer, "retrievalSource": [], "confidence": "high",
                "hallucinationRate": 0.0, "cached": True, "cacheLayer": "redis",
            }
            await cache_set_mysql(db, model_type, nq, nq, result)
            await redis_client.cache_set_json(
                f"qa:{model_type or 'default'}:{nq}", result, settings.QA_CACHE_TTL,
            )
            # 4) 状态 synced
            row.final_answer = final_answer
            row.synced_doc_id = doc_id
            row.synced_cache = 1
            row.status = "synced"
            row.operator = operator
            row.handled_at = datetime.utcnow()
            await db.commit()
            return {"ok": True, "docId": doc_id}
    except Exception as e:
        degraded("evidence_gap_sync", e)
        return {"ok": False, "msg": str(e)}
