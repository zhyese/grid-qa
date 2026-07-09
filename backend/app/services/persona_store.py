"""Persona 配置 store：DB 覆盖 code persona + 支持 DB 创建全新 persona。

get_persona(name)：DB enabled → 覆盖 code persona（prompt/工具/参数）或构造纯 DB persona（fallback 从 registry）。
"""
import copy
import json

from sqlalchemy import desc, func, select

from app.core.obs import degraded
from app.db.session import AsyncSessionLocal
from app.models.persona_config import PersonaConfig
from app.services.agent_personas import ALERT_PERSONA, DIAGNOSE_PERSONA, QA_PERSONA
from app.services.agent_personas import _alert_fallback, _diagnose_fallback, _qa_fallback
from app.services.agent_runtime import Persona

# code persona 注册表（fallback 的来源；DB 覆盖时保留这些 fallback）
_CODE_PERSONAS = {"diagnose": DIAGNOSE_PERSONA, "qa": QA_PERSONA, "alert": ALERT_PERSONA}

# fallback registry：纯 DB persona（code 无）按 fallback_key 映射到 code fallback 函数
_FALLBACK_REGISTRY = {"qa": _qa_fallback, "diagnose": _diagnose_fallback, "alert": _alert_fallback, "none": None}


async def get_persona(name: str):
    """取 persona：DB enabled → merge/构造；code 无 DB 也无 → None。"""
    code = _CODE_PERSONAS.get(name)
    cfg = None
    try:
        async with AsyncSessionLocal() as db:
            cfg = (await db.execute(
                select(PersonaConfig).where(PersonaConfig.name == name)
            )).scalar_one_or_none()
    except Exception as e:
        degraded("persona_get", e)
    if not cfg or not cfg.enabled:
        return code  # code persona（None if code 无）
    # DB enabled：覆盖 code 或构造纯 DB persona
    if code:
        p = copy.copy(code)
    else:
        fb = _FALLBACK_REGISTRY.get(cfg.fallback_key or "none")
        p = Persona(name=name, system_prompt=cfg.system_prompt or "", allowed_tools=[],
                    max_iter=6, temperature=0.2, max_tokens=1500, output_format="text",
                    fallback=fb, config_source="db")
    if cfg.system_prompt:
        p.system_prompt = cfg.system_prompt
    if cfg.allowed_tools:
        try:
            tools = json.loads(cfg.allowed_tools)
            if isinstance(tools, list):
                p.allowed_tools = [str(t) for t in tools]
        except Exception:
            pass
    if cfg.max_iter:
        p.max_iter = cfg.max_iter
    if cfg.temperature is not None:
        p.temperature = cfg.temperature
    if cfg.max_tokens:
        p.max_tokens = cfg.max_tokens
    if cfg.output_format:
        p.output_format = cfg.output_format
    p.config_source = "db"
    return p


async def list_configs() -> dict:
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(PersonaConfig).order_by(desc(PersonaConfig.updated_at))
            )).scalars().all()
            return {
                "codePersonas": sorted(_CODE_PERSONAS.keys()),
                "fallbackKeys": sorted(_FALLBACK_REGISTRY.keys()),
                "configs": [{
                    "id": r.id, "name": r.name, "systemPrompt": r.system_prompt,
                    "allowedTools": r.allowed_tools, "maxIter": r.max_iter,
                    "temperature": r.temperature, "maxTokens": r.max_tokens,
                    "outputFormat": r.output_format, "enabled": bool(r.enabled),
                    "fallbackKey": r.fallback_key or "",
                    "isCustom": r.name not in _CODE_PERSONAS,
                    "updatedAt": r.updated_at.strftime("%Y-%m-%d %H:%M:%S") if r.updated_at else "",
                } for r in rows],
            }
    except Exception as e:
        degraded("persona_list", e)
        return {"codePersonas": sorted(_CODE_PERSONAS.keys()),
                "fallbackKeys": sorted(_FALLBACK_REGISTRY.keys()), "configs": []}


async def upsert_config(name: str, system_prompt: str, allowed_tools: str,
                        max_iter, temperature, max_tokens, output_format, enabled: bool,
                        fallback_key: str = "") -> dict:
    """新增/更新 persona 配置。纯 DB persona（code 无）需 fallback_key 映射 fallback。"""
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(PersonaConfig).where(PersonaConfig.name == name)
            )).scalar_one_or_none()
            if row is None:
                row = PersonaConfig(name=name)
                db.add(row)
            row.system_prompt = system_prompt or ""
            row.allowed_tools = allowed_tools or ""
            row.max_iter = max_iter
            row.temperature = temperature
            row.max_tokens = max_tokens
            row.output_format = output_format
            row.enabled = bool(enabled)
            row.fallback_key = fallback_key or ""
            await db.commit()
            return {"name": name, "ok": True}
    except Exception as e:
        degraded("persona_upsert", e)
        return {"name": name, "ok": False, "error": f"{type(e).__name__}: {e}"}


async def delete_config(name: str) -> dict:
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(PersonaConfig).where(PersonaConfig.name == name)
            )).scalar_one_or_none()
            if row is None:
                return {"name": name, "ok": False, "error": "不存在"}
            await db.delete(row)
            await db.commit()
            return {"name": name, "ok": True}
    except Exception as e:
        degraded("persona_delete", e)
        return {"name": name, "ok": False, "error": f"{type(e).__name__}: {e}"}
