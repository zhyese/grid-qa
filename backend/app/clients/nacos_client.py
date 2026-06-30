"""Nacos 配置中心客户端：httpx 调 Nacos open API 读配置（免 SDK，跨平台稳）。

Nacos 2.x open API：GET {server}/nacos/v1/cs/configs?dataId=&group=&tenant(namespace)
返回 properties 文本（key=value），解析为 dict。CONFIG_SOURCE=nacos 时启动拉取覆盖 .env；
本地无 Nacos 时保持 env（默认），降级不报错。
"""
import httpx

from app.config import settings
from app.core.obs import degraded


async def fetch_config() -> dict:
    """从 Nacos 拉取配置，解析为 dict。失败抛异常（调用方降级 .env）。"""
    params = {"dataId": settings.NACOS_DATA_ID, "group": settings.NACOS_GROUP}
    if settings.NACOS_NAMESPACE:
        params["tenant"] = settings.NACOS_NAMESPACE
    async with httpx.AsyncClient(timeout=5) as c:
        resp = await c.get(f"{settings.NACOS_SERVER}/nacos/v1/cs/configs", params=params)
        resp.raise_for_status()
        text = resp.text
    # 解析 properties（key=value，# 注释）
    cfg: dict = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        cfg[k.strip()] = v.strip()
    return cfg


async def apply_overrides() -> int:
    """CONFIG_SOURCE=nacos 时拉取配置覆盖 settings（启动时，连接服务前）。返回覆盖字段数。"""
    if (getattr(settings, "CONFIG_SOURCE", "env") or "env").lower() != "nacos":
        return 0
    try:
        cfg = await fetch_config()
    except Exception as e:
        degraded("nacos_fetch", e, "降级 .env")
        return 0
    n = 0
    for k, v in cfg.items():
        if hasattr(settings, k):
            try:
                setattr(settings, k, v)
                n += 1
            except Exception:
                pass  # pydantic 校验失败的字段跳过
    return n
