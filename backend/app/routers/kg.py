"""知识图谱接口：抽取三元组 / 关系图谱 / 统计。"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.limiter import limiter
from app.core.response import success
from app.db.session import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.kg import KgExtractRequest
from app.services import kg_service
from app.services.log_service import write_log

router = APIRouter(prefix="/kg", tags=["知识图谱"])


@router.post("/extract")
@limiter.limit("5/minute")
async def extract(
    request: Request,
    body: KgExtractRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await kg_service.extract_triples(db, body.docId, body.modelType)
    await write_log(db, user.username, "图谱抽取", f"{data['docName']}：{data['tripleCount']}条三元组")
    return success(data, "抽取成功")


@router.get("/graph")
async def graph(
    entity: str = "",
    limit: int = 300,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await kg_service.get_graph(db, entity, limit)
    return success(data, "查询成功")


@router.get("/stats")
async def stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await kg_service.get_stats(db)
    return success(data, "查询成功")
