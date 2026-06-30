"""可观测 helper：降级日志 + 计数统一入口。

把原本 `except Exception: pass` 的盲降级改为显式记录——失败必须可见（owner 底线）。
Grafana 通过 grid_degraded_total{tag="..."} 看到各路降级次数与原因，定位静默退化
（如百炼欠费致 rerank 失败、Neo4j 未启动致图谱降级、Redis 挂致缓存失效）。

用法：
    from app.core.obs import degraded
    try:
        graph = await kg_service.graph_context(nq)
    except Exception as e:
        degraded("kg_graph_context", e)
        graph = []
"""
from loguru import logger


def degraded(tag: str, exc: BaseException, msg: str = "") -> None:
    """记录一次业务/IO 降级（不抛出，调用方继续走兜底路径）。

    tag:  降级原因分类（小写下划线），如 'kg_neo4j'/'rerank'/'embed_cache'/'milvus_delete'。
    exc:  被捕获的异常。
    msg:  额外上下文（可选）。
    """
    try:
        from app.core import metrics

        metrics.DEGRADED.labels(tag).inc()
    except Exception:
        pass
    detail = f"{type(exc).__name__}: {exc}"
    logger.warning(f"[降级:{tag}] {detail}" + (f" | {msg}" if msg else ""))
