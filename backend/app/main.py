"""FastAPI 应用入口。

运行（项目根目录）：
    uvicorn app.main:app --reload --host 127.0.0.1 --port 8001 --app-dir backend
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.core.response import BizError, error, success


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- 启动 ----
    from app.core.logging import setup_logging

    setup_logging()

    from app.db.init_db import init_db

    await init_db()  # 建表 + 初始管理员

    from app.clients.minio_client import init_bucket

    await init_bucket()  # 确保 MinIO bucket

    from app.clients.milvus_client import ensure_collection

    ensure_collection()  # 确保 Milvus collection
    # ---- 关闭 ----
    yield


app = FastAPI(title=settings.APP_NAME, version=settings.APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["系统"])
async def health():
    """健康检查：探活 DB / MinIO / Milvus / Redis。"""
    checks: dict[str, str] = {}

    try:
        from sqlalchemy import text

        from app.db.session import engine

        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["mysql"] = "ok"
    except Exception:
        checks["mysql"] = "down"

    try:
        from app.clients import minio_client

        minio_client.get_minio().bucket_exists(settings.MINIO_BUCKET)
        checks["minio"] = "ok"
    except Exception:
        checks["minio"] = "down"

    try:
        from app.clients import milvus_client

        milvus_client.num_entities()
        checks["milvus"] = "ok"
    except Exception:
        checks["milvus"] = "down"

    try:
        from app.clients import redis_client

        checks["redis"] = "ok" if await redis_client.ping() else "down"
    except Exception:
        checks["redis"] = "down"

    all_ok = all(v == "ok" for v in checks.values())
    return success(
        data={"status": "healthy" if all_ok else "degraded", "checks": checks, "version": settings.APP_VERSION}
    )


# ---- 路由挂载 ----
from app.routers import document, qa, retrieval, system  # noqa: E402

app.include_router(system.router, prefix=settings.API_PREFIX)
app.include_router(document.router, prefix=settings.API_PREFIX)
app.include_router(retrieval.router, prefix=settings.API_PREFIX)
app.include_router(qa.router, prefix=settings.API_PREFIX)


# ---- 全局异常：BizError -> 统一 {code, message, data}（HTTP 恒 200，业务码放 body）----
@app.exception_handler(BizError)
async def biz_error_handler(request: Request, exc: BizError):
    return JSONResponse(
        status_code=200,
        content=error(exc.message, exc.code, exc.data).model_dump(),
    )
