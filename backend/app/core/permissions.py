"""RBAC 权限常量 + 角色-权限默认映射。

设计见 docs/superpowers/specs/2026-07-01-rbac-acl-design.md（用户已确认）。
- 权限用字符串常量（资源:动作），细到 endpoint 级
- 角色绑定权限集（ROLE_PERMISSIONS 默认映射，首版硬编码；role_permission 表支持 DB 覆盖，后续）
- admin 永远全权限；auditor 永远只读
- 兼容：旧 admin/operator 自动映射，零破坏升级

核心纯函数 has_perm(role, perm) 供 require_perm 依赖与单测复用。
"""
# ===== 权限常量（资源:动作）=====
# 文档
DOC_READ = "doc:read"
DOC_UPLOAD = "doc:upload"
DOC_DELETE = "doc:delete"
DOC_MANAGE = "doc:manage"            # 改文档级 ACL（dept/allowed_roles）
# 问答 / 反馈
QA_ANSWER = "qa:answer"
FEEDBACK_READ = "feedback:read"
FEEDBACK_MANAGE = "feedback:manage"  # 标 golden / 删反馈
# 知识图谱
KG_READ = "kg:read"
KG_EDIT = "kg:edit"
# 领域增强（诊断 / 两票 / 相似案例）
DOMAIN_USE = "domain:use"
# 系统管理
SYSTEM_CONFIG = "system:config"      # admin 专有：Milvus/模型/persona/routing 配置
USER_MANAGE = "user:manage"          # 用户管理：改角色 / dept
ALERT_READ = "alert:read"
ALERT_MANAGE = "alert:manage"
AUDIT_READ = "audit:read"            # agent 工具调用审计
OPTIMIZER_MANAGE = "optimizer:manage"
EVIDENCE_MANAGE = "evidence:manage"
METRIC_READ = "metric:read"          # 成本 / 评测趋势看板

# admin 全权限标记（require_perm 见 "*" 直接放行）
ADMIN_ALL = "*"


def _wc(resource: str) -> str:
    """资源通配前缀，如 doc:* 覆盖 doc:read/upload/delete/manage。"""
    return f"{resource}:*"


# ===== 角色 → 权限集（默认映射，首版硬编码）=====
# admin 全权；editor 文档全权 + 问答 + 图谱编辑 + 领域；
# operator 一线运维日常（问答 + 读文档 + 读图谱 + 领域）；
# auditor 全只读（审计员）。
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {ADMIN_ALL},
    "editor": {
        DOC_READ, DOC_UPLOAD, DOC_DELETE, DOC_MANAGE,
        QA_ANSWER, FEEDBACK_READ,
        KG_READ, KG_EDIT, DOMAIN_USE,
    },
    "operator": {
        DOC_READ, QA_ANSWER, FEEDBACK_READ,
        KG_READ, DOMAIN_USE,
    },
    "auditor": {
        DOC_READ, QA_ANSWER, FEEDBACK_READ,
        KG_READ, DOMAIN_USE,
        ALERT_READ, AUDIT_READ, METRIC_READ,
    },
}

VALID_ROLES = set(ROLE_PERMISSIONS.keys())


def role_perms(role: str) -> set[str]:
    """角色的权限集（code 默认；DB 覆盖后续扩展，合并 role_permission 表）。"""
    return ROLE_PERMISSIONS.get(role, set())


def has_perm(role: str, perm: str) -> bool:
    """角色是否拥有某权限。纯函数，require_perm 依赖 + 单测复用。

    - admin（含 "*"）全放行
    - 精确命中 perm → 通过
    - 资源通配：perm=doc:delete，perms 含 doc:* → 通过
    """
    perms = role_perms(role)
    if not perms:
        return False
    if ADMIN_ALL in perms:
        return True
    if perm in perms:
        return True
    resource = perm.split(":", 1)[0]
    return _wc(resource) in perms
