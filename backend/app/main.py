"""FastAPI 应用入口。

运行方式（在项目根目录）：
    uvicorn app.main:app --reload --app-dir backend

S1 阶段：仅提供 /health，不连接外部服务；后续切片在 lifespan 中逐步初始化
DB / MinIO / Milvus，并在 include_router 处挂载业务路由。
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.core.response import success


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- 启动 ----
    # S2: await init_db()              # 建表 + 初始管理员
    # S3: minio_client.init_bucket()
    # S5: milvus_client.ensure_collection()
    # ---- 关闭 ----
    yield


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

# 开发期放开跨域；生产应收敛 allow_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["系统"])
async def health():
    """健康检查，无需鉴权。"""
    return success(data={"status": "healthy", "version": settings.APP_VERSION})


# ---- 后续切片挂载路由（S2+）----
# from app.routers import system, document, retrieval, qa
# app.include_router(system.router, prefix=settings.API_PREFIX)
# app.include_router(document.router, prefix=settings.API_PREFIX)
# app.include_router(retrieval.router, prefix=settings.API_PREFIX)
# app.include_router(qa.router, prefix=settings.API_PREFIX)
