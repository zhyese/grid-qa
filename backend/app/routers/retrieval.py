"""检索接口：混合检索（向量 + BM25 + RRF）。"""
import time

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import success
from app.db.session import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.retrieval import MixedRetrievalRequest
from app.services import retrieval_service

router = APIRouter(prefix="/retrieval", tags=["检索"])


@router.post("/mixed")
async def mixed(
    body: MixedRetrievalRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    t0 = time.time()
    result = await retrieval_service.mixed_search(
        db, body.query, body.topK, doc_type=body.docType, model_type=body.modelType
    )
    return success(
        {"retrievalList": result, "responseTime": round(time.time() - t0, 3)},
        "检索成功",
    )
