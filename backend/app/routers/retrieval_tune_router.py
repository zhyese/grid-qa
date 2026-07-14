"""检索调参建议接口（只建议模式，不自动应用）。"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.limiter import limiter
from app.core.permissions import SYSTEM_CONFIG
from app.core.response import success
from app.db.session import get_db
from app.dependencies import require_perm
from app.models.user import User

router = APIRouter(prefix="/system/retrieval", tags=["检索调参"])


@router.post("/tune")
@limiter.limit("1/minute")
async def tune(request: Request, db: AsyncSession = Depends(get_db),
               user: User = Depends(require_perm(SYSTEM_CONFIG))):
    """触发检索参数扫描（异步：复用①队列 default；①未落地则后台 create_task）。"""
    from app.services import retrieval_tune_service
    try:
        from app.tasks.registry import enqueue  # ①异步队列已落地时走队列
        await enqueue("default", "retrieval_tune_run")
        return success({"mode": "queued"}, "扫描已入队，稍后查看报告")
    except Exception:
        # ①未落地：后台 create_task（独立 session，不阻塞响应）
        import asyncio
        from app.db.session import AsyncSessionLocal

        async def _run():
            async with AsyncSessionLocal() as _db:
                await retrieval_tune_service.run_scan(_db)

        asyncio.create_task(_run())
        return success({"mode": "background"}, "扫描已在后台运行，稍后查看报告")


@router.get("/tune/report")
async def tune_report(user: User = Depends(require_perm(SYSTEM_CONFIG))):
    """读检索调参报告缓存。"""
    from app.services import retrieval_tune_service
    return success(retrieval_tune_service.get_tune_report(), "查询成功")
