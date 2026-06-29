"""文档接口：上传 / 列表 / 解析 / 向量化 / 删除。"""
from typing import List

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import success
from app.db.session import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.document import ParseRequest, VectorRequest
from app.services.document_service import (
    delete_document,
    list_documents,
    parse_documents,
    upload_documents,
    vectorize_document,
)
from app.services.log_service import write_log

router = APIRouter(prefix="/document", tags=["文档处理"])


@router.post("/upload")
async def upload(
    files: List[UploadFile] = File(...),
    docType: str = Form("运维手册"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await upload_documents(db, files, docType, user.username)
    await write_log(
        db, user.username, "文档上传",
        f"成功 {len(data['successList'])} 份，失败 {len(data['failList'])} 份",
    )
    return success(data, "上传成功")


@router.get("/list")
async def document_list(
    keyword: str = Query(""),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await list_documents(db, keyword)
    return success(data, "查询成功")


@router.post("/parse")
async def parse(
    body: ParseRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    results = await parse_documents(db, body.docIds)
    await write_log(db, user.username, "文档解析", f"解析 {len(results)} 份文档")
    return success(results, "解析成功")


@router.post("/vector/generate")
async def vector_generate(
    body: VectorRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await vectorize_document(db, body.docId)
    await write_log(
        db, user.username, "向量生成",
        f"文档 {body.docId} 生成 {data['vectorCount']} 条向量",
    )
    return success(data, "向量生成存储成功")


@router.delete("/delete")
async def delete_doc(
    docId: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await delete_document(db, docId)
    await write_log(db, user.username, "文档删除", f"删除文档 {docId}")
    return success(None, "删除成功")
