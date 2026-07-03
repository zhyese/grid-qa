"""FastAPI 应用入口。

运行（项目根目录）：
    uvicorn app.main:app --reload --host 127.0.0.1 --port 8001 --app-dir backend
"""
import asyncio
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

    # Nacos 配置覆盖（若 CONFIG_SOURCE=nacos，在连接任何服务前拉取覆盖 .env）
    try:
        from app.clients.nacos_client import apply_overrides
        n = await apply_overrides()
        if n:
            print(f"[nacos] 已从配置中心覆盖 {n} 个配置项")
    except Exception as e:
        print(f"[nacos] 配置覆盖跳过：{e}")

    from app.db.init_db import init_db

    await init_db()  # 建表 + 初始管理员

    from app.clients.minio_client import init_bucket

    await init_bucket()  # 确保 MinIO bucket

    from app.clients.milvus_client import ensure_collections

    ensure_collections()  # 确保 云 + bge 双 collection

    # 预热本地 bge 模型：首次加载需从 HF 下载(经代理 ~80s)，懒加载会让后端启动后
    # 首个问答触发该延迟(用户体感“同一问题偶尔 100s”)。提前在启动期加载进内存，
    # 之后每次 encode 仅 0.02s。离线/无 bge 环境跳过，不阻塞启动。
    try:
        from app.providers.embedding.bge_embedding import _get_model

        await asyncio.to_thread(_get_model)
        print("[bge] 本地模型预热完成")
    except Exception as e:
        print(f"[bge] 预热跳过：{e}")

    try:
        from app.clients import neo4j_client
        from app.core.obs import degraded
        await neo4j_client.ensure_constraint()  # Neo4j 知识图谱索引（未启用则跳过）
    except Exception as e:
        degraded("neo4j_init", e, "Neo4j 未启动?跳过")

    # 监控：预注册业务指标 0 值序列(让事件驱动指标事件发生前就“在场”)
    try:
        from app.core.metrics import init_metric_series
        init_metric_series()
    except Exception as e:
        print(f"[metrics] 业务指标预注册跳过：{e}")
    # 运行时配置：从 Redis 载入内存热读缓存(让 /system/config/* 改的 ef/temperature 真生效)
    try:
        from app.services import config_service
        await config_service.load_runtime()
    except Exception as e:
        print(f"[config] 运行时配置载入跳过：{e}")
    # 后台周期刷新组件健康(原仅 GET /health 才更新 → 看板常驻空值)
    app.state.component_health_task = asyncio.create_task(
        _refresh_component_health_loop()
    )
    # ---- 关闭 ----
    yield
    _task = getattr(app.state, "component_health_task", None)
    if _task:
        _task.cancel()
    try:
        from app.clients import neo4j_client
        await neo4j_client.close()
    except Exception:
        pass


app = FastAPI(title=settings.APP_NAME, version=settings.APP_VERSION, lifespan=lifespan)

# 限流（slowapi）
from slowapi import _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402

from app.core.limiter import limiter  # noqa: E402

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _probe_components() -> dict[str, str]:
    """探活 DB / MinIO / Milvus / Redis，返回 {component: "ok"|"down"}。

    /health 端点与后台周期刷新任务共用，避免健康探活逻辑两处维护。
    """
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
    return checks


def _sync_component_health(checks: dict[str, str]) -> None:
    """把探活结果同步到 Prometheus 组件健康指标(1=up/0=down)。"""
    try:
        from app.core import metrics

        for comp, st in checks.items():
            metrics.COMPONENT_HEALTH.labels(comp).set(1 if st == "ok" else 0)
    except Exception:
        pass


async def _refresh_component_health_loop() -> None:
    """周期刷新组件健康指标(每 30s)。

    原 COMPONENT_HEALTH 只在 GET /health 时才 set，看板每 10s 刷新但指标可能
    长期不动 → “基础组件健康”面板常驻空值。后台任务让 /metrics 始终携带近实时健康态。
    """
    while True:
        try:
            _sync_component_health(await _probe_components())
        except Exception:
            pass
        await asyncio.sleep(30)


@app.get("/health", tags=["系统"])
async def health():
    """健康检查：探活 DB / MinIO / Milvus / Redis。"""
    checks = await _probe_components()
    _sync_component_health(checks)

    # provider 配置态快照（仅看 key 是否配置；运行态可用性见 /api/system/health/providers）
    def _key_ok(role: str) -> bool:
        p = settings.LLM_PROVIDER if role == "llm" else settings.EMB_PROVIDER
        if role == "llm":
            return {"deepseek": bool(settings.DEEPSEEK_API_KEY),
                    "qwen": bool(settings.DASHSCOPE_API_KEY),
                    "doubao": bool(settings.ARK_API_KEY)}.get(p, False)
        return {"qwen": bool(settings.DASHSCOPE_API_KEY),
                "doubao": bool(settings.ARK_API_KEY), "bge": True}.get(p, False)

    providers = {
        "llm": {"provider": settings.LLM_PROVIDER, "keyConfigured": _key_ok("llm")},
        "embedding": {"provider": settings.EMB_PROVIDER, "keyConfigured": _key_ok("emb")},
    }
    all_ok = all(v == "ok" for v in checks.values())
    return success(
        data={"status": "healthy" if all_ok else "degraded", "checks": checks,
              "providers": providers, "version": settings.APP_VERSION}
    )


# ---- 路由挂载 ----
from app.routers import document, domain, kg, qa, retrieval, system  # noqa: E402

app.include_router(system.router, prefix=settings.API_PREFIX)
app.include_router(document.router, prefix=settings.API_PREFIX)
app.include_router(retrieval.router, prefix=settings.API_PREFIX)
app.include_router(qa.router, prefix=settings.API_PREFIX)
app.include_router(kg.router, prefix=settings.API_PREFIX)
app.include_router(domain.router, prefix=settings.API_PREFIX)


# ---- 全局异常：BizError -> 统一 {code, message, data}（HTTP 恒 200，业务码放 body）----
@app.exception_handler(BizError)
async def biz_error_handler(request: Request, exc: BizError):
    try:
        metrics.ERRORS.labels("biz", str(exc.code)).inc()
    except Exception:
        pass
    return JSONResponse(
        status_code=200,
        content=error(exc.message, exc.code, exc.data).model_dump(),
    )


# ---- Prometheus 指标 + 中间件 ----
import time  # noqa: E402

from prometheus_client import make_asgi_app  # noqa: E402

from app.core import metrics  # noqa: E402


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    try:
        metrics.REQUESTS.labels(request.method, request.url.path, str(response.status_code)).inc()
        metrics.LATENCY.labels(request.url.path).observe(time.time() - start)
        if response.status_code >= 500:
            metrics.ERRORS.labels("http5xx", str(response.status_code)).inc()
    except Exception:
        pass
    return response


from prometheus_client import CONTENT_TYPE_LATEST, generate_latest  # noqa: E402
from fastapi import Response  # noqa: E402


@app.get("/metrics")
async def metrics_endpoint():
    # 直接响应（避免 mount 的 trailing-slash 307，prometheus 采集 /metrics 不跟随重定向）
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
