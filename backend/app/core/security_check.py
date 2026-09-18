"""启动安全自检：占位密钥/默认口令 → WARN 日志 + grid_insecure_config 指标。

评审结论（2026-09-18）：RBAC/ACL 做到文档级，但入口是弱密钥+默认口令——
"装甲门配挂锁"。本模块不做拦截（开发环境合法用默认值），只做可见性：
启动时逐项 WARN + 指标计数，Grafana/巡检可按 grid_insecure_config > 0 告警。
"""
import logging

logger = logging.getLogger("app")

# 与 config.py 默认值同步维护
_JWT_PLACEHOLDER = "please-change-this-to-a-random-long-string"
_ADMIN_DEFAULT = "admin123"


def insecure_settings() -> list[dict]:
    """返回命中的不安全配置列表 [{code, detail}]（不回传真实值，防日志泄密）。"""
    from app.config import settings

    found: list[dict] = []
    if settings.JWT_SECRET == _JWT_PLACEHOLDER:
        found.append({"code": "jwt_secret_placeholder",
                      "detail": "JWT_SECRET 仍是占位符——任意人可伪造 token，必须换随机长串"})
    if settings.ADMIN_PASSWORD == _ADMIN_DEFAULT:
        found.append({"code": "admin_password_default",
                      "detail": "ADMIN_PASSWORD 仍是出厂默认弱口令——首次启动后立即改密"})
    if settings.DEBUG:
        found.append({"code": "debug_on",
                      "detail": "DEBUG=true（开发态：详细报错外泄，生产必须 false）"})
    return found


def run_startup_check() -> int:
    """lifespan 启动时调用：逐项 WARN + 指标置数。返回命中数（0=干净）。"""
    from app.core.metrics import INSECURE_CONFIG

    items = insecure_settings()
    INSECURE_CONFIG.set(len(items))
    for it in items:
        logger.warning("[security] 不安全配置 %s：%s", it["code"], it["detail"])
    if not items:
        logger.info("[security] 启动自检通过：无占位密钥/默认口令")
    return len(items)
