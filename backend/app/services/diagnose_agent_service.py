"""Agentic 诊断（适配层）—— 通用 Agent 引擎的 persona=diagnose 入口。

引擎主体已迁至 agent_runtime / agent_tools / agent_personas。本文件仅保留
diagnose_agent(db, symptom, model_type) 适配层：调 run_agent(DIAGNOSE_PERSONA)，
并把 AgentResult 映射为既有返回 schema（路由/前端零改动）。
"""
from app.services.agent_runtime import run_agent
from app.services.persona_store import get_persona


async def diagnose_agent(db, symptom, model_type=None):
    """Agentic 诊断：LLM 自主调工具多轮验证 → 既有响应 schema（不变）。"""
    persona = await get_persona("diagnose")
    result = await run_agent(
        db, persona, f"故障症状：{symptom}", model_type)
    return {
        "symptom": symptom,
        "diagnosis": result.answer,
        "steps": result.steps,
        "iterations": result.iterations,
        "degraded": result.degraded,
        "degradeReason": result.degrade_reason,
        "latencyMs": result.latency_ms,
    }
