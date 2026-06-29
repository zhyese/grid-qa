"""文档服务：上传、列表、解析分块、删除。"""
import asyncio
import uuid
from typing import List

from fastapi import UploadFile
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import minio_client
from app.core.response import BizError
from app.models.chunk import Chunk
from app.models.document import Document
from app.services import chunk_service, parse_service

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


async def list_documents(db: AsyncSession, keyword: str = "") -> list[dict]:
    stmt = select(Document).order_by(Document.created_at.desc())
    if keyword:
        stmt = stmt.where(Document.doc_name.like(f"%{keyword}%"))
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "docId": r.id, "docName": r.doc_name, "docType": r.doc_type,
            "status": r.status, "chunkCount": r.chunk_count, "uploadUser": r.upload_user,
            "createdAt": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
        }
        for r in rows
    ]


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
            # 扫描件/图片走 PaddleOCR（CPU 密集，放线程池）
            text = await asyncio.to_thread(parse_service.ocr_by_name, doc.doc_name, content)
        chunks = chunk_service.split_text(text)
        # 重新解析时先清旧分块
        await db.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
        for i, c in enumerate(chunks):
            db.add(Chunk(doc_id=doc_id, chunk_idx=i, content=c, char_count=len(c)))
        doc.status = "parsed"
        doc.chunk_count = len(chunks)
        await db.commit()
        results.append({"docId": doc_id, "chunkCount": len(chunks), "chunkList": chunks[:5]})
    return results


async def delete_document(db: AsyncSession, doc_id: str) -> None:
    """删除 MinIO 文件 + MySQL(chunks+document)。S5 补 Milvus 向量删除。"""
    doc = await get_document(db, doc_id)
    try:
        await asyncio.to_thread(minio_client.remove_object, doc.minio_object)
    except Exception:
        pass  # 对象已不存在则忽略
    await db.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
    await db.execute(delete(Document).where(Document.id == doc_id))
    await db.commit()
    # S5: from app.clients.milvus_client import delete_by_doc; delete_by_doc(doc_id)
