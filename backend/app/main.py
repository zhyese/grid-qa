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
    from app.db.init_db import init_db

    await init_db()  # S2: 建表 + 初始管理员
    # S3: from app.clients.minio_client import init_bucket; await init_bucket()
    # S5: from app.clients.milvus_client import ensure_collection; ensure_collection()
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
    return success(data={"status": "healthy", "version": settings.APP_VERSION})


# ---- 路由挂载 ----
from app.routers import system  # noqa: E402

app.include_router(system.router, prefix=settings.API_PREFIX)
# S3+: app.include_router(document.router, prefix=settings.API_PREFIX)
# S6:  app.include_router(retrieval.router, prefix=settings.API_PREFIX)
# S7:  app.include_router(qa.router, prefix=settings.API_PREFIX)


# ---- 全局异常：BizError -> 统一 {code, message, data}（HTTP 恒 200，业务码放 body）----
@app.exception_handler(BizError)
async def biz_error_handler(request: Request, exc: BizError):
    return JSONResponse(
        status_code=200,
        content=error(exc.message, exc.code, exc.data).model_dump(),
    )
