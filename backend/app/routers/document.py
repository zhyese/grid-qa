"""文档接口：上传 / 列表 / 解析 / 向量化 / 删除。"""
from typing import List

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.limiter import limiter
from app.core.response import success
from app.db.session import get_db
from app.dependencies import get_current_user, require_perm
from app.models.user import User
from app.schemas.document import BatchVectorRequest, ParseRequest, VectorRequest
from app.services.document_service import (
    delete_document,
    get_preview,
    get_stats,
    list_documents,
    list_versions,
    parse_documents,
    rollback_version,
    upload_documents,
    vectorize_document,
    vectorize_documents,
)
from app.services.log_service import write_log

router = APIRouter(prefix="/document", tags=["文档处理"])


@router.post("/upload")
@limiter.limit("10/minute")
async def upload(
    request: Request,
    files: List[UploadFile] = File(...),
    docType: str = Form("运维手册"),
    dept: str = Form(""),
    allowedRoles: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm("doc:upload")),
):
    data = await upload_documents(db, files, docType, user.username, user.tenant_id,
                                  dept=dept, allowed_roles=allowedRoles)
    await write_log(
        db, user.username, "文档上传",
        f"成功 {len(data['successList'])} 份，失败 {len(data['failList'])} 份",
    )
    return success(data, "上传成功")


@router.get("/list")
async def document_list(
    keyword: str = Query(""),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await list_documents(db, keyword, page, size, user.tenant_id,
                                user_dept=user.dept, user_role=user.role)
    return success(data, "查询成功")


@router.get("/stats")
async def document_stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await get_stats(db)
    return success(data, "查询成功")


@router.get("/preview/{doc_id}")
async def preview_doc(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """文档在线预览：返回原文流（PDF/图片/文本），前端按 MIME 渲染。"""
    from fastapi.responses import Response

    content, mt = await get_preview(db, doc_id, user_dept=user.dept, user_role=user.role)
    return Response(content, media_type=mt)


@router.get("/{doc_id}/versions")
async def doc_versions(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """文档版本历史（换版归档列表）。"""
    data = await list_versions(db, doc_id)
    return success(data, "查询成功")


@router.post("/rollback")
async def rollback(
    docId: str = Query(...),
    version: int = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """回滚到指定版本：恢复旧版 MinIO 对象，清向量待重新解析。"""
    data = await rollback_version(db, docId, version)
    await write_log(db, user.username, "版本回滚", f"{docId} → v{version}")
    return success(data, "回滚成功，请重新解析向量化")


@router.post("/parse")
@limiter.limit("10/minute")
async def parse(
    request: Request,
    body: ParseRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    results = await parse_documents(db, body.docIds)
    await write_log(db, user.username, "文档解析", f"解析 {len(results)} 份文档")
    return success(results, "解析成功")


@router.post("/vector/generate")
@limiter.limit("10/minute")
async def vector_generate(
    request: Request,
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


@router.post("/vector/batch")
@limiter.limit("10/minute")
async def vector_batch(
    request: Request,
    body: BatchVectorRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """批量向量化：复用单条向量化，单个失败不中断，返回 successList/failList。"""
    data = await vectorize_documents(db, body.docIds)
    await write_log(
        db, user.username, "批量向量生成",
        f"成功 {len(data['successList'])} 份，失败 {len(data['failList'])} 份",
    )
    return success(data, "批量向量生成完成")


@router.delete("/delete")
async def delete_doc(
    docId: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm("doc:delete")),
):
    await delete_document(db, docId, user_dept=user.dept, user_role=user.role)
    await write_log(db, user.username, "文档删除", f"删除文档 {docId}")
    return success(None, "删除成功")


@router.get("/{doc_id}/perms")
async def doc_perms(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm("doc:manage")),
):
    """文档授权信息（dept / allowed_roles）。admin/editor 可查。"""
    from app.services.document_service import get_document
    doc = await get_document(db, doc_id)
    return success({"docId": doc_id, "dept": doc.dept or "",
                    "allowedRoles": doc.allowed_roles or ""}, "查询成功")


@router.put("/{doc_id}/perms")
async def update_doc_perms(
    doc_id: str,
    dept: str = Query(""),
    allowedRoles: str = Query(""),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm("doc:manage")),
):
    """修改文档授权（dept / allowed_roles）。admin/editor 可改。"""
    from sqlalchemy import update
    from app.models.document import Document
    await db.execute(update(Document).where(Document.id == doc_id).values(
        dept=dept, allowed_roles=allowedRoles))
    await db.commit()
    await write_log(db, user.username, "文档授权", f"{doc_id} → dept={dept} roles={allowedRoles}")
    return success({"docId": doc_id, "dept": dept, "allowedRoles": allowedRoles}, "授权已更新")
