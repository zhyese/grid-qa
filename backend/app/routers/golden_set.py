"""F1 数据资产与标注工作台 API；薄路由，默认关闭（GOLDEN_SET_ENABLE）。"""

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.permissions import GOLDEN_MANAGE, GOLDEN_READ
from app.core.response import BizError, success
from app.db.session import get_db
from app.dependencies import require_perm
from app.models.golden_set import GoldenCase
from app.models.user import User
from app.schemas.golden_set import (
    GoldenCaseRequest,
    GoldenIngestRequest,
    GoldenPublishRequest,
)
from app.services import golden_set_service as golden


def enabled():
    if not settings.GOLDEN_SET_ENABLE:
        raise BizError("数据资产工作台未启用（GOLDEN_SET_ENABLE）", 403)


router = APIRouter(
    prefix="/golden-set", tags=["数据资产与标注工作台"], dependencies=[Depends(enabled)]
)


@router.get("/stats")
async def golden_stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(GOLDEN_READ)),
):
    return success(await golden.stats(db, user.tenant_id or "default"))


@router.get("/categories")
async def golden_categories(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(GOLDEN_READ)),
):
    """分类下拉数据源（已去重）。"""
    rows = (await db.execute(
        select(GoldenCase.category).where(
            GoldenCase.tenant_id == (user.tenant_id or "default"),
            GoldenCase.is_deleted == 0,
        ).distinct().order_by(GoldenCase.category)
    )).scalars().all()
    return success({"categories": [c for c in rows if c]})


@router.get("/cases")
async def golden_list(
    keyword: str = Query("", max_length=200),
    category: str = Query("", max_length=32),
    difficulty: str = Query("", max_length=16),
    labelStatus: str = Query("", max_length=16),
    page: int = Query(1, ge=1, le=10000),
    size: int = Query(20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(GOLDEN_READ)),
):
    return success(await golden.list_cases(
        db, user.tenant_id or "default", keyword=keyword, category=category,
        difficulty=difficulty, label_status=labelStatus, page=page, size=size,
    ))


@router.post("/cases")
async def golden_create(
    body: GoldenCaseRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(GOLDEN_MANAGE)),
):
    return success(await golden.upsert_case(
        db, user.tenant_id or "default", user.username, body.model_dump()))


@router.put("/cases/{case_id}")
async def golden_update(
    case_id: str,
    body: GoldenCaseRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(GOLDEN_MANAGE)),
):
    return success(await golden.upsert_case(
        db, user.tenant_id or "default", user.username, body.model_dump(), case_id=case_id))


@router.delete("/cases/{case_id}")
async def golden_delete(
    case_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(GOLDEN_MANAGE)),
):
    return success(await golden.delete_case(db, user.tenant_id or "default", case_id))


@router.post("/import")
async def golden_import(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(GOLDEN_MANAGE)),
):
    """CSV / XLSX 批量导入（列名支持中文别名，见 golden_set_service._norm_row）。"""
    content = await file.read()
    if not content:
        raise BizError("文件为空", 400)
    rows = golden.parse_upload(file.filename or "", content)
    res = await golden.batch_import(db, user.tenant_id or "default", user.username, rows)
    return success({**res, "parsed": len(rows)}, "导入完成")


@router.post("/ingest-quality-events")
async def golden_ingest(
    body: GoldenIngestRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(GOLDEN_MANAGE)),
):
    """把质量事件里的坏 case 拉成待标注草稿（数据飞轮：坏 case → 标注任务）。"""
    res = await golden.ingest_quality_events(
        db, user.tenant_id or "default", user.username, limit=body.limit)
    return success(res, "已拉取待标注草稿")


@router.post("/publish")
async def golden_publish(
    body: GoldenPublishRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(GOLDEN_MANAGE)),
):
    """发布版本：校验 + 写回 backend/data/golden_qa.json + 记录哈希链版本。"""
    return success(await golden.publish(
        db, user.tenant_id or "default", user.username, note=body.note))


@router.get("/versions")
async def golden_versions(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(GOLDEN_READ)),
):
    return success(await golden.list_versions(db, user.tenant_id or "default", limit=limit))


@router.get("/versions/diff")
async def golden_version_diff(
    v1: int = Query(..., ge=1),
    v2: int = Query(..., ge=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(GOLDEN_READ)),
):
    return success(await golden.diff_versions(db, user.tenant_id or "default", v1, v2))


@router.get("/export/laya")
async def golden_export_laya(
    labelStatus: str = Query("", max_length=16),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(GOLDEN_READ)),
):
    """一键导出 Laya 三问题原语冷启动格式（choice/noul/score）。"""
    return success(await golden.export_laya(
        db, user.tenant_id or "default", label_status=labelStatus))
