"""文档服务：上传、列表、解析分块、向量化、删除。"""
import asyncio
import uuid
from typing import List

from fastapi import UploadFile
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import minio_client, milvus_client
from app.config import settings
from app.core.response import BizError
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.kg_triple import KgTriple
from app.services import chunk_service, embedding_service, parse_service

# 允许的扩展名（接口主推 PDF，MVP 放开常见格式含扫描件/图片）
ALLOWED_EXT = {".pdf", ".doc", ".docx", ".txt", ".md", ".png", ".jpg", ".jpeg"}
MAX_FILES = 5
MAX_SINGLE_SIZE = 100 * 1024 * 1024  # 100MB


def _ext(name: str) -> str:
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


async def upload_documents(
    db: AsyncSession, files: List[UploadFile], doc_type: str, username: str
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
            doc = Document(
                id=doc_id, doc_name=name, doc_type=doc_type,
                minio_object=object_name, file_size=len(content), upload_user=username,
            )
            db.add(doc)
            await db.commit()
            success_list.append(name)
        except Exception as e:
            fail_list.append(f"{name}({e})")
    return {"successList": success_list, "failList": fail_list}


async def list_documents(db: AsyncSession, keyword: str = "", page: int = 1, size: int = 20) -> dict:
    from sqlalchemy import func
    stmt = select(Document)
    cnt = select(func.count()).select_from(Document)
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


async def parse_documents(db: AsyncSession, doc_ids: List[str]) -> list[dict]:
    """从 MinIO 取文件 → 解析(数字/扫描OCR) → 分块入 chunks 表。"""
    results = []
    for doc_id in doc_ids:
        doc = await get_document(db, doc_id)
        content = await asyncio.to_thread(minio_client.get_object_bytes, doc.minio_object)
        text, is_scanned = parse_service.parse_file(doc.doc_name, content)
        if is_scanned or not text.strip():
            text = await asyncio.to_thread(parse_service.ocr_by_name, doc.doc_name, content)
        chunks = chunk_service.split_text(text)
        await db.execute(delete(Chunk).where(Chunk.doc_id == doc_id))  # 重新解析先清旧分块
        for i, c in enumerate(chunks):
            db.add(Chunk(doc_id=doc_id, chunk_idx=i, content=c, char_count=len(c)))
        doc.status = "parsed"
        doc.chunk_count = len(chunks)
        await db.commit()
        results.append({"docId": doc_id, "chunkCount": len(chunks), "chunkList": chunks[:5]})
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
    except Exception:
        pass
    return {
        "docId": doc_id, "vectorCount": len(vectors),
        "milvusCollection": collection, "embeddingRoute": route, "docChars": total_chars,
    }


async def delete_document(db: AsyncSession, doc_id: str) -> None:
    """联动删除 MinIO 文件 + Milvus 向量 + MySQL(chunks+document)。"""
    doc = await get_document(db, doc_id)
    try:
        await asyncio.to_thread(minio_client.remove_object, doc.minio_object)
    except Exception:
        pass
    try:
        milvus_client.delete_by_doc(doc_id)
    except Exception:
        pass
    # 联动删知识图谱：MySQL 三元组 + Neo4j 边
    await db.execute(delete(KgTriple).where(KgTriple.doc_id == doc_id))
    try:
        from app.clients import neo4j_client
        await neo4j_client.delete_by_doc(doc_id)
    except Exception:
        pass
    await db.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
    await db.execute(delete(Document).where(Document.id == doc_id))
    await db.commit()


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
    except Exception:
        vector_total = 0
    try:
        from app.core import metrics
        metrics.KB_DOCS.set(sum(by_status.values()))
        metrics.KB_CHUNKS.set(chunk_total)
        metrics.KB_VECTORS.set(vector_total)
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
