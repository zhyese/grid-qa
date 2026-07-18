"""文档服务：上传、列表、解析分块、向量化、删除。"""
import asyncio
import uuid
from typing import List

from fastapi import UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import minio_client, milvus_client
from app.config import settings
from app.core.obs import degraded
from app.core.response import BizError
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_version import DocumentVersion
from app.models.kg_triple import KgTriple
from app.services import chunk_service, embedding_service, parse_service

# 允许的扩展名（接口主推 PDF，MVP 放开常见格式含扫描件/图片/Excel台账）
ALLOWED_EXT = {".pdf", ".doc", ".docx", ".txt", ".md", ".png", ".jpg", ".jpeg", ".xlsx"}
MAX_FILES = 5
MAX_SINGLE_SIZE = 100 * 1024 * 1024  # 100MB


def _auto_equipment_tags(text: str) -> str:
    """从全文匹配标准设备术语，作为文档设备标签（逗号分隔去重，限 20 个）。

    复用 term_service 术语表的标准词，实现"文档→设备维度"自动关联（D5 设备台账）。
    """
    if not text:
        return ""
    from app.services.term_service import _load_terms
    try:
        std = set(_load_terms().values())
    except Exception:
        return ""
    tags = sorted({w for w in std if w and w in text}, key=len, reverse=True)
    return ",".join(tags[:20])


def _ext(name: str) -> str:
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _assert_acl(doc: Document, user_dept: str | None, user_role: str | None) -> None:
    """逐文档 ACL 校验，越权抛 403。user 上下文均 None 时跳过（向后兼容；admin 已由 require_perm 放行）。"""
    if user_dept is None and user_role is None:
        return
    if user_role == "admin":
        return
    if doc.dept and user_dept and doc.dept != user_dept:
        raise BizError("无权限访问该部门文档", 403)
    if doc.allowed_roles:
        allowed = [r.strip() for r in doc.allowed_roles.split(",") if r.strip()]
        if allowed and user_role and user_role not in allowed:
            raise BizError("无权限访问该文档（角色未授权）", 403)


async def upload_documents(
    db: AsyncSession, files: List[UploadFile], doc_type: str, username: str,
    tenant_id: str = "default", dept: str = "", allowed_roles: str = "",
) -> dict:
    if len(files) > MAX_FILES:
        raise BizError(f"批量上传不超过 {MAX_FILES} 份", 400)
    success_list, fail_list = [], []
    for f in files:
        name = f.filename or "unnamed"
        try:
            if _ext(name) not in ALLOWED_EXT:
                raise ValueError(f"不支持的格式：{_ext(name)}")
            content = await f.read()
            if len(content) > MAX_SINGLE_SIZE:
                raise ValueError("单文件超过 100MB")
            if not content:
                raise ValueError("文件为空")
            doc_id = uuid.uuid4().hex
            object_name = f"{doc_id}/{name}"
            await asyncio.to_thread(
                minio_client.put_object, object_name, content, len(content),
                f.content_type or "application/octet-stream",
            )
            # 同名归档：同租户同名 → 旧版进 versions，新版覆盖（版本管理 + 回滚）
            existing = (await db.execute(
                select(Document).where(Document.doc_name == name, Document.tenant_id == tenant_id)
            )).scalar_one_or_none()
            if existing:
                max_ver = (await db.execute(
                    select(func.max(DocumentVersion.version)).where(DocumentVersion.doc_id == existing.id)
                )).scalar() or 0
                db.add(DocumentVersion(
                    doc_id=existing.id, version=max_ver + 1,
                    minio_object=existing.minio_object, file_size=existing.file_size,
                    created_by=existing.upload_user,
                ))
                existing.minio_object = object_name
                existing.file_size = len(content)
                existing.status = "pending"
                existing.upload_user = username
                await db.commit()
                success_list.append(f"{name}(已换版→需重新解析)")
                continue
            doc = Document(
                id=doc_id, doc_name=name, doc_type=doc_type,
                minio_object=object_name, file_size=len(content), upload_user=username,
                tenant_id=tenant_id, dept=dept, allowed_roles=allowed_roles,
            )
            db.add(doc)
            await db.commit()
            success_list.append(name)
        except Exception as e:
            fail_list.append(f"{name}({e})")
    return {"successList": success_list, "failList": fail_list}


async def list_documents(db: AsyncSession, keyword: str = "", page: int = 1, size: int = 20,
                         tenant_id: str = "default", user_dept: str | None = None,
                         user_role: str | None = None) -> dict:
    from sqlalchemy import func, or_
    stmt = select(Document).where(Document.tenant_id == tenant_id)
    cnt = select(func.count()).select_from(Document).where(Document.tenant_id == tenant_id)
    # RBAC：admin 看全部；其余只看 dept 空（公开）或同 dept
    if user_role != "admin" and user_dept is not None:
        stmt = stmt.where(or_(Document.dept == "", Document.dept == user_dept))
        cnt = cnt.where(or_(Document.dept == "", Document.dept == user_dept))
    if keyword:
        stmt = stmt.where(Document.doc_name.like(f"%{keyword}%"))
        cnt = cnt.where(Document.doc_name.like(f"%{keyword}%"))
    total = (await db.execute(cnt)).scalar() or 0
    rows = (
        await db.execute(
            stmt.order_by(Document.created_at.desc()).offset((page - 1) * size).limit(size)
        )
    ).scalars().all()
    return {
        "total": total,
        "list": [
            {
                "docId": r.id, "docName": r.doc_name, "docType": r.doc_type,
                "status": r.status, "chunkCount": r.chunk_count, "uploadUser": r.upload_user,
                "equipmentTags": r.equipment_tags or "",
                "dept": r.dept or "",
                "allowedRoles": r.allowed_roles or "",
                "createdAt": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
            }
            for r in rows
        ],
    }


async def get_document(db: AsyncSession, doc_id: str) -> Document:
    doc = (await db.execute(select(Document).where(Document.id == doc_id))).scalar_one_or_none()
    if not doc:
        raise BizError("文档不存在", 404)
    return doc


# 在线预览支持的扩展名 → MIME（docx/xlsx 需转换，暂不支持直预览）
_PREVIEW_MIME = {
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


async def get_preview(db: AsyncSession, doc_id: str,
                      user_dept: str | None = None, user_role: str | None = None) -> tuple[bytes, str]:
    """取原文供在线预览，返回 (content_bytes, mime)。
    优先 MinIO 原文(PDF/图片/文本)；FAQ/无扩展名等文字版(minio_object 空 或 格式不支持)→取已解析 Chunk content(text/plain)。"""
    doc = await get_document(db, doc_id)
    _assert_acl(doc, user_dept, user_role)
    mt = _PREVIEW_MIME.get(_ext(doc.doc_name))
    # 文字版兜底：无 MIME(无扩展名/不支持格式) 或 minio_object 空(FAQ) → 取已解析的 Chunk 纯文本预览
    if mt is None or not doc.minio_object:
        rows = (await db.execute(
            select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_idx)
        )).scalars().all()
        if rows:
            text = "\n\n".join(r.content for r in rows)
            return text.encode("utf-8"), "text/plain; charset=utf-8"
        if mt is None:  # 无 chunk 又不支持格式 → 原错误
            raise BizError("该格式不支持在线预览（支持 PDF/图片/文本）", 400)
    content = await asyncio.to_thread(minio_client.get_object_bytes, doc.minio_object)
    return content, mt


async def list_versions(db: AsyncSession, doc_id: str) -> dict:
    """文档版本历史（换版归档，可回滚）。"""
    doc = await get_document(db, doc_id)
    rows = (await db.execute(
        select(DocumentVersion).where(DocumentVersion.doc_id == doc_id)
        .order_by(DocumentVersion.version.desc())
    )).scalars().all()
    return {
        "docId": doc_id, "docName": doc.doc_name, "currentSize": doc.file_size,
        "versions": [{
            "version": r.version, "fileSize": r.file_size, "createdBy": r.created_by or "",
            "createdAt": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
        } for r in rows],
    }


async def rollback_version(db: AsyncSession, doc_id: str, version: int) -> dict:
    """回滚到指定版本：当前 minio 归档防丢 → 恢复旧版 minio → 清 chunks/向量（待重新解析）。"""
    doc = await get_document(db, doc_id)
    ver = (await db.execute(
        select(DocumentVersion).where(
            DocumentVersion.doc_id == doc_id, DocumentVersion.version == version
        )
    )).scalar_one_or_none()
    if not ver:
        raise BizError("版本不存在", 404)
    # 回滚前把当前状态归档（防丢，可再回滚回来）
    max_ver = (await db.execute(
        select(func.max(DocumentVersion.version)).where(DocumentVersion.doc_id == doc_id)
    )).scalar() or 0
    db.add(DocumentVersion(
        doc_id=doc_id, version=max_ver + 1, minio_object=doc.minio_object,
        file_size=doc.file_size, created_by="rollback",
    ))
    doc.minio_object = ver.minio_object
    doc.file_size = ver.file_size
    doc.status = "pending"
    await db.execute(delete(Chunk).where(Chunk.doc_id == doc_id))  # 清旧，待重新解析向量化
    try:
        milvus_client.delete_by_doc(doc_id)
    except Exception as e:
        degraded("milvus_rollback_delete", e)
    await db.commit()
    return {"docId": doc_id, "rolledTo": version, "status": "pending(需重新解析向量化)"}


async def parse_documents(db: AsyncSession, doc_ids: List[str]) -> list[dict]:
    """从 MinIO 取文件 → 结构化解析(表格/Excel/扫描OCR) → 结构感知分块入 chunks 表。"""
    results = []
    for doc_id in doc_ids:
        doc = await get_document(db, doc_id)
        content = await asyncio.to_thread(minio_client.get_object_bytes, doc.minio_object)
        sections, is_scanned = parse_service.parse_file_structured(doc.doc_name, content)
        if is_scanned or not sections:
            # OCR 文字 + 可选 VLM 图片语义（图纸结构/设备外观，OCR 抓不到的空间信息）
            ocr_sections = await asyncio.to_thread(parse_service.ocr_to_sections, doc.doc_name, content)
            if settings.VLM_ENABLE:
                from app.services import multimodal_service
                vlm_desc = await multimodal_service.describe_image(content)
                if vlm_desc:
                    ocr_text = ocr_sections[0]["content"] if ocr_sections else ""
                    ocr_sections = [{"type": "text", "content": f"【图片语义】{vlm_desc}\n{ocr_text}".strip()}]
            sections = ocr_sections
        structured = chunk_service.split_structured(sections)
        await db.execute(delete(Chunk).where(Chunk.doc_id == doc_id))  # 重新解析先清旧分块
        for i, c in enumerate(structured):
            # 入库拦截：core 字段必填校验（不阻塞主链路，仅记 degraded）
            if not c.get("text"):
                continue
            if not doc_id:
                try:
                    degraded("chunk_intake_missing_doc_id", ValueError("empty doc_id"))
                except Exception:
                    pass
                continue
            page_num = c.get("page_num")
            db.add(Chunk(
                doc_id=doc_id, chunk_idx=i, content=c["text"], char_count=len(c["text"]),
                chunk_type=c["chunk_type"], parent_idx=c["parent_idx"], section=c["section"],
                section_path=c.get("section_path", "") or c.get("section", ""),
                page_num=page_num, bbox=c.get("bbox"),
                table_header=c.get("table_header", ""),
                # 元数据齐全判定：有页码（PDF）或表格表头即视为可精确定位；纯文本无页码→False（前端降级仅文档名）
                metadata_complete=bool(page_num is not None or c.get("table_header")),
            ))
        doc.status = "parsed"
        doc.chunk_count = len(structured)
        # 设备台账自动打标（D5）：全文匹配标准设备术语
        doc.equipment_tags = _auto_equipment_tags("\n".join(s["text"] for s in structured))
        await db.commit()
        results.append({
            "docId": doc_id, "chunkCount": len(structured),
            "tableCount": sum(1 for c in structured if c["chunk_type"] == "table"),
            "chunkList": [c["text"][:80] for c in structured[:5]],
        })
    return results


async def vectorize_document(db: AsyncSession, doc_id: str) -> dict:
    """chunks → 向量 → Milvus。按文档大小路由：大→云, 小→本地 bge（向量空间独立分 collection）。"""
    from app.providers.factory import get_embedding_provider

    doc = await get_document(db, doc_id)
    rows = (
        await db.execute(select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_idx))
    ).scalars().all()
    if not rows:
        raise BizError("文档尚未解析，请先调用解析接口", 400)
    texts = [r.content for r in rows]
    total_chars = sum(len(t) for t in texts)

    # 路由：文档大走云 embedding，小走本地 bge
    if total_chars > settings.DOC_SIZE_THRESHOLD:
        provider, collection, route = settings.EMB_PROVIDER, settings.MILVUS_COLLECTION, "cloud"
    else:
        provider, collection, route = "bge", settings.MILVUS_COLLECTION_BGE, "bge"

    vectors = await get_embedding_provider(provider).embed(texts)
    milvus_client.delete_by_doc(doc_id)  # 先清旧（双 collection）
    await asyncio.to_thread(
        milvus_client.insert_chunks, collection, vectors, texts,
        [doc_id] * len(texts), [doc.doc_name] * len(texts), [r.chunk_idx for r in rows],
    )
    doc.status = "vectorized"
    await db.commit()
    try:
        from app.core import metrics
        metrics.VECTOR_ROUTE.labels(route).inc()
    except Exception:
        pass
    # 数据链路闭环：向量化同时后台触发知识图谱三元组抽取（写 MySQL+Neo4j），不阻塞返回
    # 注：asyncio 用模块级 import（顶部已 import），函数内再 import 会遮蔽致 UnboundLocalError
    try:
        _t = asyncio.create_task(_kg_extract_bg(doc_id))
        _bg_tasks.add(_t)
        _t.add_done_callback(_bg_tasks.discard)
    except Exception as e:
        degraded("kg_extract_dispatch", e)
    # BM25 增量钩子：新文档入向量库后标记脏，下次检索时重建
    try:
        from app.services import bm25_service
        bm25_service.mark_dirty()
    except Exception:
        pass
    return {
        "docId": doc_id, "vectorCount": len(vectors),
        "milvusCollection": collection, "embeddingRoute": route, "docChars": total_chars,
    }


async def vectorize_documents(db: AsyncSession, doc_ids: List[str]) -> dict:
    """批量向量化：串行复用 vectorize_document，单个失败不中断（successList/failList 风格同 upload_documents）。

    未解析/向量生成失败的文档进 failList 并标注原因，其余正常入向量库。
    串行而非并发：embedding provider 有并发/限流约束，且与 parse_documents 串行风格一致。
    """
    success_list: list = []
    fail_list: list = []
    for doc_id in doc_ids:
        try:
            success_list.append(await vectorize_document(db, doc_id))
        except Exception as e:
            fail_list.append(f"{doc_id}({e})")
    return {"successList": success_list, "failList": fail_list}


async def delete_document(db: AsyncSession, doc_id: str,
                          user_dept: str | None = None, user_role: str | None = None) -> None:
    """联动删除 MinIO 文件 + Milvus 向量 + MySQL(chunks+document)。"""
    doc = await get_document(db, doc_id)
    _assert_acl(doc, user_dept, user_role)
    try:
        await asyncio.to_thread(minio_client.remove_object, doc.minio_object)
    except Exception as e:
        degraded("minio_delete", e)
    try:
        milvus_client.delete_by_doc(doc_id)
    except Exception as e:
        degraded("milvus_delete", e)
    # 联动删知识图谱：MySQL 三元组 + Neo4j 边
    await db.execute(delete(KgTriple).where(KgTriple.doc_id == doc_id))
    try:
        from app.clients import neo4j_client
        await neo4j_client.delete_by_doc(doc_id)
    except Exception as e:
        degraded("neo4j_delete", e)
    await db.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
    await db.execute(delete(Document).where(Document.id == doc_id))
    await db.commit()
    # 缓存失效：文档删除后，关联的 QA 缓存标记过期（Phase 3）
    try:
        from app.services.cache_persist import cache_invalidate_for_doc_async
        await cache_invalidate_for_doc_async(doc_id)
    except Exception:
        pass
    # BM25 增量钩子：文档删除后标记脏，下次检索时重建
    try:
        from app.services import bm25_service
        bm25_service.mark_dirty()
    except Exception:
        pass


async def get_stats(db: AsyncSession) -> dict:
    """知识库统计：文档/分块/向量总数 + 状态/类型分布（双 collection 向量合计）。"""
    from sqlalchemy import func
    rows = (await db.execute(select(Document.status, func.count()).group_by(Document.status))).all()
    by_status = {r[0]: r[1] for r in rows}
    rows = (await db.execute(select(Document.doc_type, func.count()).group_by(Document.doc_type))).all()
    by_type = {r[0]: r[1] for r in rows}
    chunk_total = (await db.execute(select(func.count()).select_from(Chunk))).scalar() or 0
    vector_total = 0
    try:
        vector_total = milvus_client.num_entities(settings.MILVUS_COLLECTION) + \
                       milvus_client.num_entities(settings.MILVUS_COLLECTION_BGE)
    except Exception as e:
        degraded("kb_vector_count", e)
        vector_total = 0
    try:
        from app.core import metrics
        metrics.KB_DOCS.set(sum(by_status.values()))
        metrics.KB_CHUNKS.set(chunk_total)
        metrics.KB_VECTORS.set(vector_total)
        metrics.KB_VECTORIZED_DOCS.set(by_status.get("vectorized", 0))
    except Exception:
        pass
    return {
        "docTotal": sum(by_status.values()),
        "chunkTotal": chunk_total,
        "vectorTotal": vector_total,
        "byStatus": by_status,
        "byType": by_type,
    }


_bg_tasks: set = set()   # 持有后台 task 引用，防 GC 回收导致 task 不执行（asyncio 官方推荐 fire-and-forget）


async def _kg_extract_bg(doc_id: str, model_type: str | None = None) -> None:
    """后台抽取三元组写 MySQL+Neo4j（向量化后自动建图谱，形成数据链路）。

    独立 db session（fire-and-forget，不阻塞 vectorize 接口返回）。
    """
    from app.db.session import AsyncSessionLocal
    from app.services import kg_service
    async with AsyncSessionLocal() as db:
        try:
            res = await kg_service.extract_triples(db, doc_id, model_type)
            print(f"[kg] 文档 {doc_id} 自动抽取：{res.get('tripleCount', 0)} 条三元组")
        except Exception as e:
            print(f"[kg] 文档 {doc_id} 自动抽取失败：{e}")


# ===== chunk 编辑/重向量化 + 文档相似检测 + 版本 diff =====

async def update_chunk(db: AsyncSession, chunk_id: str, content: str) -> dict:
    """编辑单个 chunk 内容并重新向量化整篇文档（保证检索向量与新内容一致）。

    解决"改一个错字要删整篇重传"：chunk 级编辑后自动 refresh 向量。
    """
    content = (content or "").strip()
    if not content:
        raise BizError("chunk 内容不能为空", 400)
    chunk = (await db.execute(select(Chunk).where(Chunk.id == chunk_id))).scalar_one_or_none()
    if not chunk:
        raise BizError("chunk 不存在", 404)
    chunk.content = content
    chunk.char_count = len(content)
    await db.commit()
    try:
        await vectorize_document(db, chunk.doc_id)   # 双 collection 清旧写新
    except Exception as e:
        degraded("chunk_revectorize", e)
    return {"chunkId": chunk.id, "docId": chunk.doc_id, "charCount": chunk.char_count}


async def list_chunks(db: AsyncSession, doc_id: str) -> list[dict]:
    """列出文档全部分块（供 chunk 编辑选择）。"""
    rows = (await db.execute(select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_idx))).scalars().all()
    return [{"id": r.id, "chunkIdx": r.chunk_idx, "content": r.content, "section": r.section,
             "charCount": r.char_count} for r in rows]


async def find_similar_docs(db: AsyncSession, text: str, topk: int = 5) -> list[dict]:
    """文档去重/相似检测：文本向量化后在 Milvus 搜，按 doc_id 聚合返回最相似的已存在文档。"""
    text = (text or "").strip()
    if not text:
        return []
    from app.services import embedding_service
    vec = await embedding_service.embed_query(text)
    hits = milvus_client.search(settings.MILVUS_COLLECTION, vec, topk=topk * 3)
    if not hits:
        return []
    best: dict[str, float] = {}
    name_map: dict[str, str] = {}
    for h in hits:
        did = h.get("doc_id")
        if not did:
            continue
        score = float(h.get("score", 0) or 0)
        if did not in best or score > best[did]:
            best[did] = score
            name_map[did] = h.get("doc_name", "")
    rows = (await db.execute(select(Document.id, Document.doc_name).where(Document.id.in_(list(best.keys()))))).all()
    id2name = {r[0]: r[1] for r in rows}
    out = [{"docId": did, "docName": id2name.get(did, name_map.get(did, "")),
            "score": round(s, 3)} for did, s in sorted(best.items(), key=lambda x: -x[1]) if s >= 0.75]
    return out[:topk]


async def diff_versions(db: AsyncSession, doc_id: str, v1: int, v2: int) -> dict:
    """文档两版本内容 diff（difflib unified diff，仅对可解码为文本的版本）。"""
    import difflib
    from app.models.document_version import DocumentVersion
    rows = (await db.execute(
        select(DocumentVersion).where(DocumentVersion.doc_id == doc_id, DocumentVersion.version.in_([v1, v2]))
    )).scalars().all()
    vmap = {r.version: r for r in rows}
    if v1 not in vmap or v2 not in vmap:
        raise BizError("版本不存在", 404)

    def _text(obj_key: str) -> str:
        if not obj_key:
            return ""
        try:
            return minio_client.get_object_bytes(obj_key).decode("utf-8", errors="ignore")
        except Exception:
            return ""

    t1 = await asyncio.to_thread(_text, vmap[v1].minio_object)
    t2 = await asyncio.to_thread(_text, vmap[v2].minio_object)
    diff = list(difflib.unified_diff(t1.splitlines(), t2.splitlines(), fromfile=f"v{v1}", tofile=f"v{v2}", lineterm=""))
    changed = [d for d in diff if d.startswith(("+", "-")) and not d.startswith(("+++", "---"))]
    return {"v1": v1, "v2": v2, "v1Chars": len(t1), "v2Chars": len(t2),
            "changedLines": len(changed), "diff": diff[:500]}
