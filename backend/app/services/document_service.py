"""文档服务：上传（存 MinIO + 入库）、列表、详情、删除。"""
import asyncio
import uuid
from typing import List

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import minio_client
from app.core.response import BizError
from app.models.document import Document

# 允许的扩展名（接口文档主推 PDF，MVP 放开常见格式含扫描件/图片）
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
            # 同步 SDK 放到线程池，避免阻塞事件循环
            await asyncio.to_thread(
                minio_client.put_object,
                object_name,
                content,
                len(content),
                f.content_type or "application/octet-stream",
            )
            doc = Document(
                id=doc_id,
                doc_name=name,
                doc_type=doc_type,
                minio_object=object_name,
                file_size=len(content),
                upload_user=username,
            )
            db.add(doc)
            await db.commit()
            success_list.append(name)
        except Exception as e:  # 单个失败不影响其余
            fail_list.append(f"{name}({e})")
    return {"successList": success_list, "failList": fail_list}


async def list_documents(db: AsyncSession, keyword: str = "") -> list[dict]:
    stmt = select(Document).order_by(Document.created_at.desc())
    if keyword:
        stmt = stmt.where(Document.doc_name.like(f"%{keyword}%"))
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "docId": r.id,
            "docName": r.doc_name,
            "docType": r.doc_type,
            "status": r.status,
            "chunkCount": r.chunk_count,
            "uploadUser": r.upload_user,
            "createdAt": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
        }
        for r in rows
    ]


async def get_document(db: AsyncSession, doc_id: str) -> Document:
    doc = (await db.execute(select(Document).where(Document.id == doc_id))).scalar_one_or_none()
    if not doc:
        raise BizError("文档不存在", 404)
    return doc
