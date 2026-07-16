"""项目内建任务 handler。

只在 worker 启动时导入，避免普通 API 进程导入重量级业务模块。
"""
from app.db.session import AsyncSessionLocal
from app.tasks.registry import TaskContext, task_handler


@task_handler("retrieval_tune_run")
async def retrieval_tune_run(payload: dict, context: TaskContext) -> dict:
    from app.services import retrieval_tune_service

    async with AsyncSessionLocal() as db:
        result = await retrieval_tune_service.run_scan(db)
    return {"taskId": context.task_id, "result": result}


@task_handler("knowledge.scan")
async def knowledge_governance_scan(payload: dict, context: TaskContext) -> dict:
    """知识时效与冲突扫描；租户边界以不可伪造的 TaskContext 为准。"""
    from app.services.knowledge_governance_service import (
        handle_knowledge_governance_scan,
    )

    return await handle_knowledge_governance_scan(payload, context)


@task_handler("knowledge_evolution.scan")
async def knowledge_evolution_scan(payload: dict, context: TaskContext) -> dict:
    """知识自进化扫描：dislike 聚类→盲区→LLM 草稿。租户边界以 TaskContext 为准。"""
    from app.services import knowledge_evolution_service

    async with AsyncSessionLocal() as db:
        result = await knowledge_evolution_service.run_scan(
            db, context.tenant_id,
            since_hours=payload.get("since_hours", 168),
            model_type=payload.get("model_type"),
        )
    return {"taskId": context.task_id, "result": result}


# 导入模块会注册 ``proactive_ops.process``。保留业务模块自身的 handler 身份，
# 避免管理进程与 worker 进程分别注册两个不同 wrapper。
from app.services import realtime_event_service as _realtime_event_service  # noqa: E402,F401
from app.services import alert_disposal_service as _alert_disposal_service  # noqa: E402,F401
