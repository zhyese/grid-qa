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
from app.core.obs import degraded
from app.core.response import BizError, error, success


async def _wait_for_startup_dependencies() -> None:
    """在多容器同 Pod 启动时等待 MySQL；默认关闭以保持原启动行为。"""
    retries = max(0, int(getattr(settings, "STARTUP_DEPENDENCY_RETRIES", 0)))
    if retries <= 0:
        return
    interval = max(0.1, float(getattr(settings, "STARTUP_DEPENDENCY_INTERVAL", 2)))
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            from sqlalchemy import text
            from app.db.session import engine

            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except Exception as e:
            last_error = e
            if attempt == retries:
                break
            await asyncio.sleep(interval)
    raise RuntimeError(f"MySQL 启动等待超时（{retries} 次）: {last_error}")


async def _retry_startup_component(name: str, fn, *, threaded: bool = False) -> None:
    """可配置地重试同 Pod 组件初始化；默认 1 次保持原启动降级语义。"""
    retries = max(1, int(getattr(settings, "STARTUP_COMPONENT_RETRIES", 1)))
    interval = max(0.1, float(getattr(settings, "STARTUP_DEPENDENCY_INTERVAL", 2)))
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            if threaded:
                await asyncio.to_thread(fn)
            else:
                result = fn()
                if asyncio.iscoroutine(result):
                    await result
            return
        except Exception as e:
            last_error = e
            if attempt == retries:
                break
            await asyncio.sleep(interval)
    raise RuntimeError(f"{name} 启动等待超时（{retries} 次）: {last_error}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- 启动 ----
    from app.core.logging import setup_logging

    setup_logging()

    # 安全自检（占位密钥/默认口令 → WARN + grid_insecure_config 指标；不拦截只可见）
    try:
        from app.core.security_check import run_startup_check
        run_startup_check()
    except Exception as e:
        print(f"[security] 启动自检跳过：{e}")

    await _wait_for_startup_dependencies()

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

    try:
        await _retry_startup_component("MinIO", init_bucket)  # 确保 MinIO bucket
    except Exception as e:
        degraded("minio_init_bucket", e, "MinIO 未启动？跳过 bucket 初始化")

    from app.clients.milvus_client import ensure_collections

    try:
        await _retry_startup_component("Milvus", ensure_collections, threaded=True)
    except Exception as e:
        degraded("milvus_ensure_collections", e, "Milvus 未启动？跳过 collection 初始化")

    # N1：确保记忆 collection（memory_collection）存在
    if getattr(settings, "MEMORY_ENABLE", True):
        try:
            from app.clients.milvus_client import ensure_memory_collection
            await _retry_startup_component("Milvus memory", ensure_memory_collection, threaded=True)
            print("[memory] Milvus memory_collection 已就绪")
        except Exception as e:
            degraded("milvus_memory_collection", e, "记忆 collection 初始化跳过")

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
        await neo4j_client.ensure_constraint()  # Neo4j 知识图谱索引（未启用则跳过）
    except Exception as e:
        degraded("neo4j_init", e, "Neo4j 未启动?跳过")

    # N4：初始化 OpenTelemetry（OTLP → Langfuse）
    try:
        from app.core.otel_genai import init_otel
        init_otel()
        print(f"[otel] 已初始化，采样率={settings.OTEL_SAMPLE_RATE}，端点={settings.OTEL_ENDPOINT}")
    except Exception as e:
        print(f"[otel] 初始化跳过：{e}")

    # N2：加载 MCP registry（发现外部 MCP server → 注册进 ToolRegistry）
    try:
        from app.mcp.registry import mcp_registry
        await mcp_registry.load_from_config()
        n = mcp_registry.server_count()
        if n:
            print(f"[mcp] 已加载 {n} 个外部 MCP server")
            # 发现外部 MCP 工具 → 注册进 ToolRegistry
            from app.services.agent_tools import register_mcp_tools
            tool_count = await register_mcp_tools()
            if tool_count:
                print(f"[mcp] 已注册 {tool_count} 个外部 MCP 工具")
    except Exception as e:
        print(f"[mcp] registry 加载跳过：{e}")

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
    # LLM provider 健康度熔断（L1）：周期探活 chain 内各 provider → 写指标 + 熔断冷却
    try:
        from app.providers.llm_router import _refresh_llm_health_loop
        app.state.llm_health_task = asyncio.create_task(_refresh_llm_health_loop())
    except Exception as e:
        print(f"[llm] 健康探活后台任务启动跳过：{e}")
    # C2 缓存预热周期刷新：每 6h 回写热点 + golden 到 Redis（防 TTL 过期 + 覆盖新高频）
    if getattr(settings, "CACHE_WARMUP_ENABLE", True):
        try:
            from app.services.cache_warmup import warmup_loop
            app.state.cache_warmup_task = asyncio.create_task(warmup_loop())
            print("[cache] 预热 loop 已挂载（每 6h 刷新热点+golden）")
        except Exception as e:
            print(f"[cache] 预热 loop 启动跳过：{e}")
    # 缓存持久化：后台清理 + 指标刷新（Phase 2-3）
    if getattr(settings, "CACHE_PERSIST_ENABLE", False):
        try:
            from app.services.cache_persist import cleanup_loop, metrics_loop
            app.state.cache_cleanup_task = asyncio.create_task(
                cleanup_loop(settings.CACHE_PERSIST_CLEANUP_HOURS * 3600)
            )
            app.state.cache_metrics_task = asyncio.create_task(metrics_loop())
            print("[cache] 缓存持久化后台任务已启动")
        except Exception as e:
            print(f"[cache] 缓存持久化启动跳过：{e}")
    # 缓存预热：从 MySQL/golden_qa.json 预载高频问题到 Redis（Phase 3）
    if getattr(settings, "CACHE_WARMUP_ENABLE", True):
        try:
            from app.services.cache_warmup import warmup_hot_queries, warmup_from_file
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as _db:
                n = await warmup_hot_queries(_db, topk=50)
                if n:
                    print(f"[cache] 热点预热 {n} 条（来自 qa_cache hit_count Top-50）")
                m = await warmup_from_file()
                if m:
                    print(f"[cache] golden 预热 {m} 条")
        except Exception as e:
            print(f"[cache] 预热跳过：{e}")
    # 操作日志自动归档：每日把超保留期的日志导出 jsonl 再删（BRD §4.5.2）
    if getattr(settings, "LOG_ARCHIVE_ENABLE", True):
        try:
            from app.services.log_archive_service import archive_loop
            app.state.log_archive_task = asyncio.create_task(archive_loop())
            print("[log-archive] 日志归档后台任务已启动")
        except Exception as e:
            print(f"[log-archive] 启动跳过：{e}")
    # 三合一全量定时备份：每 3h（MySQL+Redis+Milvus）
    _backup_hours = float(getattr(settings, "BACKUP_CRON_HOURS", 3))
    if _backup_hours > 0:
        try:
            from app.services.backup_service import backup_all_loop
            app.state.backup_all_task = asyncio.create_task(backup_all_loop(_backup_hours))
            print(f"[backup] 定时全量备份后台任务已启动（每 {_backup_hours}h）")
        except Exception as e:
            print(f"[backup] 定时备份启动跳过：{e}")
    # 所有 provider、MCP 工具和运行时配置就绪后再消费积压任务，避免启动窗口内
    # 的主动诊断拿到不完整工具集。任务/事件先落 MySQL，进程重启后可恢复。
    if getattr(settings, "TASK_WORKERS_ENABLE", True):
        try:
            from app.tasks.lifecycle import start_background_workers

            await start_background_workers(app)
            print("[task-center] realtime/default/low worker 与事件 dispatcher 已启动")
        except Exception as e:
            degraded("task_center_start", e, "持久化任务 worker 启动失败")
    # 知识自进化定时扫描（周期 KNOWLEDGE_EVOLUTION_CRON_HOURS，<=0 关闭）
    try:
        from app.services.knowledge_evolution_service import evolution_cron_loop
        from app.config import settings as _settings
        if float(getattr(_settings, "KNOWLEDGE_EVOLUTION_CRON_HOURS", 24)) > 0:
            app.state.evolution_cron_task = asyncio.create_task(evolution_cron_loop("default"))
            print("[evolution] 知识自进化定时扫描后台任务已启动")
    except Exception as e:
        print(f"[evolution] 定时扫描启动跳过：{e}")
    # 证据补全定时深度补全+落库（周期 EVIDENCE_GAP_DEEP_INTERVAL 秒，<=0 关闭）
    try:
        from app.services.evidence_gap_service import deep_cron_loop
        from app.config import settings as _settings
        if float(getattr(_settings, "EVIDENCE_GAP_DEEP_INTERVAL", 180)) > 0:
            app.state.evidence_gap_cron_task = asyncio.create_task(deep_cron_loop("default"))
            print("[evidence-gap] 证据补全定时深度补全已启动")
    except Exception as e:
        print(f"[evidence-gap] 定时补全启动跳过：{e}")
    # 坏case修复率定时聚合（周期 FIX_RATE_CRON_MINUTES，FIX_RATE_ENABLE=False 或<=0 关闭）
    try:
        from app.services.feedback_fix_rate_service import fix_rate_cron_loop
        from app.config import settings as _settings
        if bool(getattr(_settings, "FIX_RATE_ENABLE", True)) and float(getattr(_settings, "FIX_RATE_CRON_MINUTES", 30)) > 0:
            app.state.fix_rate_cron_task = asyncio.create_task(fix_rate_cron_loop("default"))
            print("[fix-rate] 坏case修复率定时聚合后台任务已启动")
    except Exception as e:
        print(f"[fix-rate] 修复率定时聚合启动跳过：{e}")
    # N1 记忆衰减+软删物理删除 周期 loop（decay() 需 cron 触发；MEMORY_DECAY_CRON_HOURS<=0 关闭）
    try:
        from app.services.agent_memory_service import decay_loop
        from app.config import settings as _settings
        if float(getattr(_settings, "MEMORY_DECAY_CRON_HOURS", 24)) > 0:
            app.state.memory_decay_task = asyncio.create_task(
                decay_loop(float(_settings.MEMORY_DECAY_CRON_HOURS))
            )
            print(f"[memory] 记忆衰减后台任务已启动（每 {_settings.MEMORY_DECAY_CRON_HOURS}h）")
    except Exception as e:
        print(f"[memory] 记忆衰减 loop 启动跳过：{e}")
    # KG 每日对账（MySQL 镜像 vs Neo4j 边数；KG_RECONCILE_CRON_HOURS<=0 关闭）
    try:
        from app.services.kg_reconcile_service import reconcile_loop
        from app.config import settings as _settings
        if float(getattr(_settings, "KG_RECONCILE_CRON_HOURS", 24)) > 0:
            app.state.kg_reconcile_task = asyncio.create_task(
                reconcile_loop(float(_settings.KG_RECONCILE_CRON_HOURS))
            )
            print(f"[kg-reconcile] KG 对账后台任务已启动（每 {_settings.KG_RECONCILE_CRON_HOURS}h）")
    except Exception as e:
        print(f"[kg-reconcile] 对账 loop 启动跳过：{e}")
    # N7 报告定时自动生成（OPS_REPORT_CRON_HOURS<=0 关闭；完成通知 admin+editor）
    try:
        from app.config import settings as _settings
        _rc = float(getattr(_settings, "OPS_REPORT_CRON_HOURS", 0))
        if getattr(_settings, "OPS_REPORT_ENABLE", False) and _rc > 0:
            from app.services.ops_report_service import auto_report_loop
            app.state.ops_report_cron_task = asyncio.create_task(auto_report_loop(_rc))
            print(f"[ops-report] 定时报告后台任务已启动（每 {_rc}h）")
    except Exception as e:
        print(f"[ops-report] 定时报告 loop 启动跳过：{e}")
    # N5 演练超时结算 sweep（无人轮询的 running 演练后台兜底 finish；<=0 关闭）
    try:
        from app.config import settings as _settings
        _si = float(getattr(_settings, "DRILL_SWEEP_INTERVAL", 300))
        if getattr(_settings, "DRILL_SANDBOX_ENABLE", False) and _si > 0:
            from app.services.drill_service import sweep_loop
            app.state.drill_sweep_task = asyncio.create_task(sweep_loop(_si))
            print(f"[drill] 超时结算 sweep 已启动（每 {_si}s）")
    except Exception as e:
        print(f"[drill] sweep loop 启动跳过：{e}")
    # eval_matrix 夜间定时评测（EVAL_MATRIX_CRON_HOURS<=0 关闭）
    try:
        from app.config import settings as _settings
        _em = float(getattr(_settings, "EVAL_MATRIX_CRON_HOURS", 0))
        if _em > 0:
            from app.services.eval_matrix_service import eval_matrix_cron_loop
            app.state.eval_matrix_cron_task = asyncio.create_task(eval_matrix_cron_loop(_em))
            print(f"[eval-matrix] 定时评测后台任务已启动（每 {_em}h）")
    except Exception as e:
        print(f"[eval-matrix] 定时评测 loop 启动跳过：{e}")
    # ---- 关闭 ----
    yield
    try:
        from app.tasks.lifecycle import stop_background_workers

        await stop_background_workers(app)
    except Exception as e:
        degraded("task_center_stop", e)
    _task = getattr(app.state, "component_health_task", None)
    if _task:
        _task.cancel()
    _llm_health = getattr(app.state, "llm_health_task", None)
    if _llm_health:
        _llm_health.cancel()
    _cache_warmup = getattr(app.state, "cache_warmup_task", None)
    if _cache_warmup:
        _cache_warmup.cancel()
    _cache_cleanup = getattr(app.state, "cache_cleanup_task", None)
    if _cache_cleanup:
        _cache_cleanup.cancel()
    _cache_metrics = getattr(app.state, "cache_metrics_task", None)
    if _cache_metrics:
        _cache_metrics.cancel()
    _log_archive = getattr(app.state, "log_archive_task", None)
    if _log_archive:
        _log_archive.cancel()
    _backup_all = getattr(app.state, "backup_all_task", None)
    if _backup_all:
        _backup_all.cancel()
    _evo_cron = getattr(app.state, "evolution_cron_task", None)
    if _evo_cron:
        _evo_cron.cancel()
    _gap_cron = getattr(app.state, "evidence_gap_cron_task", None)
    if _gap_cron:
        _gap_cron.cancel()
    # M3：坏case修复率 cron 也需 cancel（getattr 防 AttributeError，未启用时不存在该属性）
    _fix_cron = getattr(app.state, "fix_rate_cron_task", None)
    if _fix_cron:
        _fix_cron.cancel()
    # N1 记忆衰减 loop 也需 cancel
    _memory_decay = getattr(app.state, "memory_decay_task", None)
    if _memory_decay:
        _memory_decay.cancel()
    # KG 对账 loop 也需 cancel
    _kg_reconcile = getattr(app.state, "kg_reconcile_task", None)
    if _kg_reconcile:
        _kg_reconcile.cancel()
    # 报告 cron / 演练 sweep / eval_matrix cron 也需 cancel
    for _attr in ("ops_report_cron_task", "drill_sweep_task", "eval_matrix_cron_task"):
        _t = getattr(app.state, _attr, None)
        if _t:
            _t.cancel()
    try:
        from app.clients import neo4j_client
        await neo4j_client.close()
    except Exception:
        pass
    try:
        from app.services.rerank_service import close_client
        await close_client()  # 释放 rerank 共享 httpx 连接池
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
                    "doubao": bool(settings.ARK_API_KEY),
                    "ollama": True}.get(p, False)
        return {"qwen": bool(settings.DASHSCOPE_API_KEY),
                "doubao": bool(settings.ARK_API_KEY), "bge": True}.get(p, False)

    providers = {
        "llm": {"provider": settings.LLM_PROVIDER, "keyConfigured": _key_ok("llm")},
        "embedding": {"provider": settings.EMB_PROVIDER, "keyConfigured": _key_ok("emb")},
    }
    observer_configured = bool(
        settings.LLM_USER_OBSERVER_ENABLED
        and settings.LLM_USER_OBSERVER_USER_HASH_SECRET
        and (settings.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT or settings.OTEL_EXPORTER_OTLP_ENDPOINT)
    )
    all_ok = all(v == "ok" for v in checks.values())
    return success(
        data={"status": "healthy" if all_ok else "degraded", "checks": checks,
              "providers": providers,
              "llmUserObserver": {"enabled": settings.LLM_USER_OBSERVER_ENABLED,
                                  "configured": observer_configured},
              "version": settings.APP_VERSION}
    )


# ---- 路由挂载 ----
from app.routers import (  # noqa: E402
    document,
    ops_workbench,
    domain,
    kg,
    knowledge_governance,
    knowledge_evolution,
    integrations,
    memory,
    notification,
    qa,
    quality_event,
    realtime_event,
    retrieval,
    retrieval_tune_router,
    system,
    task_center,
    twin,
)
from app.mcp.server import router as mcp_router  # noqa: E402

app.include_router(system.router, prefix=settings.API_PREFIX)
app.include_router(ops_workbench.router, prefix=settings.API_PREFIX)
app.include_router(document.router, prefix=settings.API_PREFIX)
app.include_router(retrieval.router, prefix=settings.API_PREFIX)
app.include_router(retrieval_tune_router.router, prefix=settings.API_PREFIX)
app.include_router(qa.router, prefix=settings.API_PREFIX)
app.include_router(kg.router, prefix=settings.API_PREFIX)
app.include_router(domain.router, prefix=settings.API_PREFIX)
app.include_router(memory.router, prefix=settings.API_PREFIX)
app.include_router(notification.router, prefix=settings.API_PREFIX)
app.include_router(twin.router, prefix=settings.API_PREFIX)
app.include_router(task_center.router, prefix=settings.API_PREFIX)
app.include_router(quality_event.router, prefix=settings.API_PREFIX)
app.include_router(realtime_event.router, prefix=settings.API_PREFIX)
app.include_router(knowledge_governance.router, prefix=settings.API_PREFIX)
app.include_router(knowledge_evolution.router, prefix=settings.API_PREFIX)
app.include_router(integrations.router, prefix=settings.API_PREFIX)
app.include_router(mcp_router, prefix=settings.API_PREFIX)
if settings.WORKFLOW_ENABLE:  # 可视化工作流编排（关=现状不注册，零开销）
    from app.routers import workflow as workflow_router  # noqa: E402
    app.include_router(workflow_router.router, prefix=settings.API_PREFIX)
if settings.OPS_REPORT_ENABLE:  # N7 运维报告智能生成（关=现状不注册）
    from app.routers import ops_report  # noqa: E402
    app.include_router(ops_report.router, prefix=settings.API_PREFIX)
if settings.DOC_COLLAB_ENABLE:  # 文档协作批注+电子签批（关=现状不注册）
    from app.routers import doc_collab  # noqa: E402
    app.include_router(doc_collab.router, prefix=settings.API_PREFIX)
if settings.DRILL_SANDBOX_ENABLE:  # N5 故障仿真演练沙箱（关=现状不注册）
    from app.routers import drill  # noqa: E402
    app.include_router(drill.router, prefix=settings.API_PREFIX)


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

try:  # noqa: E402
    from prometheus_client import make_asgi_app  # noqa: E402,F401  # 保留引用备用（/metrics 走自定义路由）
except ImportError:  # pragma: no cover
    make_asgi_app = None

from app.core import metrics  # noqa: E402


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start = time.time()
    from app.core.llm_user_observer import finish_http, http_server_span

    response = None
    with http_server_span(request) as observer_span:
        try:
            response = await call_next(request)
        except Exception as exc:
            finish_http(
                observer_span, status_code=500,
                duration_ms=(time.time() - start) * 1000, error=exc,
            )
            raise
        trace_id = finish_http(
            observer_span, status_code=response.status_code,
            duration_ms=(time.time() - start) * 1000,
        )
        if trace_id:
            response.headers["X-Trace-ID"] = trace_id
    try:
        metrics.REQUESTS.labels(request.method, request.url.path, str(response.status_code)).inc()
        metrics.LATENCY.labels(request.url.path).observe(time.time() - start)
        if response.status_code >= 500:
            metrics.ERRORS.labels("http5xx", str(response.status_code)).inc()
    except Exception:
        pass
    # X-Cache-Hit：从 request.state 读取缓存层，注入响应头（供调试/监控）
    cache_layer = getattr(request.state, "cache_layer", None)
    if cache_layer:
        response.headers["X-Cache-Hit"] = cache_layer
    return response


from prometheus_client import CONTENT_TYPE_LATEST, generate_latest  # noqa: E402
from fastapi import Response  # noqa: E402


@app.get("/metrics")
async def metrics_endpoint():
    # 直接响应（避免 mount 的 trailing-slash 307，prometheus 采集 /metrics 不跟随重定向）
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
