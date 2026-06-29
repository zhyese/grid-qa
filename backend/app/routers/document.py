"""文档接口：上传 / 列表 / 详情。（解析、向量化、删除在 S4/S5 补全）"""
from typing import List

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import success
from app.db.session import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.services.document_service import list_documents, upload_documents
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
