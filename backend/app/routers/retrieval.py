"""检索接口：混合检索（向量 + BM25 + RRF）。"""
import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.limiter import limiter
from app.core.response import success
from app.db.session import get_db
from app.dependencies import get_current_user, require_admin
from app.models.user import User
from app.schemas.retrieval import MixedRetrievalRequest
from app.services import retrieval_service

router = APIRouter(prefix="/retrieval", tags=["检索"])


@router.post("/mixed")
@limiter.limit("30/minute")
async def mixed(
    request: Request,
    body: MixedRetrievalRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    t0 = time.time()
    result = await retrieval_service.mixed_search(
        db, body.query, body.topK, doc_type=body.docType,
        model_type=body.modelType, equipment=body.equipment,
    )
    return success(
        {"retrievalList": result, "responseTime": round(time.time() - t0, 3)},
        "检索成功",
    )


@router.post("/debug")
@limiter.limit("10/minute")
async def debug(
    request: Request,
    body: MixedRetrievalRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """检索调试（admin）：返回改写/HyDE/multi-query/各路召回/RRF/rerank/MMR 全链路 trace +
    每条命中的分数归因 + 多样性指标 + 可选路由对比 + 可选 Context Relevance judge。

    参数 compareRoutes=true → 额外返回 4 路路由对比矩阵。
    """
    # 路由对比模式：并行跑多条路由
    route_comparison = None
    if body.compareRoutes:
        route_comparison = await retrieval_service.compare_routes(
            db, body.query, body.topK, model_type=body.modelType,
        )

    # 标准 debug trace（含多样性指标）
    trace = await retrieval_service.debug_search(
        db, body.query, body.topK, doc_type=body.docType,
        model_type=body.modelType, equipment=body.equipment,
    )

    # Context Relevance Judge（可选 LLM 定性评估，仅在 debug 模式手动触发）
    if route_comparison:
        trace["routeComparison"] = route_comparison

    return success(trace, "检索调试成功")


@router.post("/bm25/rebuild")
@limiter.limit("3/minute")
async def bm25_rebuild(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """BM25 索引全量重建（admin）。新文档默认增量进内存，但进程重启/异常后用此端点兜底重建。"""
    from app.services import bm25_service

    n = await bm25_service.rebuild(db)
    return success({"chunks": n}, f"BM25 索引重建完成（{n} 个分块）")
