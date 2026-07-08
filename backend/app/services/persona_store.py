"""Persona 配置 store：DB 覆盖 code persona + admin CRUD。

get_persona(name)：DB 有 enabled 覆盖则 merge 到 code persona（prompt/工具/参数），
fallback 保留 code 的（callable 不能入库）；否则返 code persona。
"""
import copy
import json

from sqlalchemy import desc, func, select

from app.core.obs import degraded
from app.db.session import AsyncSessionLocal
from app.models.persona_config import PersonaConfig
from app.services.agent_personas import ALERT_PERSONA, DIAGNOSE_PERSONA, QA_PERSONA

# code persona 注册表（fallback 的来源；DB 覆盖时保留这些 fallback）
_CODE_PERSONAS = {"diagnose": DIAGNOSE_PERSONA, "qa": QA_PERSONA, "alert": ALERT_PERSONA}


async def get_persona(name: str):
    """取 persona：DB enabled 覆盖 → merge 到 code；否则 code。找不到返 None。"""
    code = _CODE_PERSONAS.get(name)
    if code is None:
        return None
    cfg = None
    try:
        async with AsyncSessionLocal() as db:
            cfg = (await db.execute(
                select(PersonaConfig).where(PersonaConfig.name == name)
            )).scalar_one_or_none()
    except Exception as e:
        degraded("persona_get", e)
    if not cfg or not cfg.enabled:
        return code
    p = copy.copy(code)
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
    """列出所有 DB persona 配置 + code persona 清单（admin 看板用）。"""
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(PersonaConfig).order_by(desc(PersonaConfig.updated_at))
            )).scalars().all()
            return {
                "codePersonas": sorted(_CODE_PERSONAS.keys()),
                "configs": [{
                    "id": r.id, "name": r.name, "systemPrompt": r.system_prompt,
                    "allowedTools": r.allowed_tools, "maxIter": r.max_iter,
                    "temperature": r.temperature, "maxTokens": r.max_tokens,
                    "outputFormat": r.output_format, "enabled": bool(r.enabled),
                    "updatedAt": r.updated_at.strftime("%Y-%m-%d %H:%M:%S") if r.updated_at else "",
                } for r in rows],
            }
    except Exception as e:
        degraded("persona_list", e)
        return {"codePersonas": sorted(_CODE_PERSONAS.keys()), "configs": []}


async def upsert_config(name: str, system_prompt: str, allowed_tools: str,
                        max_iter, temperature, max_tokens, output_format, enabled: bool) -> dict:
    """新增或更新 persona 配置（按 name 唯一）。"""
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
