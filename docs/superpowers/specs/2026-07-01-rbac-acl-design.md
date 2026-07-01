# RBAC 细粒度权限 + 文档级 ACL — 设计 spec

> 日期：2026-07-01 ｜ 选型：RBAC 角色-权限矩阵 + 文档级 ACL（用户确认）
> 状态：待用户审阅

## 背景与目标

现权限两层粗粒度：
- 角色只有 `admin | operator` 二元（`backend/app/models/user.py:21`），无中间职责（只读审计员/知识编辑员/调度员）
- 文档只有 `tenant_id` 隔离（`backend/app/models/document.py:23`），**同租户内任何人可见全部文档**，无部门/文档级授权

电网多部门多职责强需求：调度文档仅调度部门可见、涉密图纸受限、审计员只读。**目标**：补 RBAC 权限矩阵 + 文档级 ACL，是后续 SSO/脱敏审计的合规地基。

## 架构

### 层1：RBAC 角色-权限矩阵

```
permission（权限常量）   role_permission（角色↔权限）   user.role → 权限集
  doc:read                  admin      → 全部
  doc:upload                editor     → doc:*, qa:*, kg:edit
  doc:delete                auditor    → *:read（只读）
  qa:answer                 operator   → qa:*, doc:read
  kg:edit
  system:config（admin 专有）
  ...
```

- 权限用字符串常量枚举（`backend/app/core/permissions.py`）
- 角色绑定权限集（DB `role_permission` 表，或配置文件；首版 DB）
- 依赖注入 `require_perm("doc:delete")` 替换现有 `require_admin` 的细粒度场景

### 层2：文档级 ACL（部门 + 显式授权）

- `Document` 加字段：`dept`（部门）、`allowed_roles`（JSON 数组，可空=部门内全员）
- 检索后置 ACL 过滤：`retrieval_service` 元数据过滤层（现 `retrieval_service.py:154-159`）增 `dept/allowed_roles` 校验
- 文档 CRUD（list/preview/delete）加 ACL 校验：用户 `dept` ∈ 文档 `dept` 且角色 ∈ `allowed_roles`

## 组件

### 后端
- `backend/app/core/permissions.py`（新）：权限常量 + `ROLE_PERMISSIONS` 默认映射（admin/editor/operator/auditor）
- `backend/app/models/permission.py`（新）：`Permission`、`RolePermission` 表（Alembic 迁移）
- `backend/app/dependencies.py`：新增 `require_perm(perm: str)` 依赖；`require_admin` 保留=`require_perm("system:config")`
- `backend/app/models/document.py`：加 `dept: str`、`allowed_roles: str`（JSON）字段
- `backend/app/services/retrieval_service.py`：元数据过滤增 ACL（传 `user_dept`/`user_role`）
- `backend/app/services/document_service.py`：list/preview/delete 加 ACL 校验
- `backend/app/routers/document.py`：upload 接收 `dept`/`allowed_roles`；新增 `GET /document/{id}/perms` + `PUT /document/{id}/perms`（admin）
- `backend/app/routers/system.py`：新增 `GET /system/users` + `PUT /system/users/{id}/role`（admin）

### 前端
- `frontend/src/views/Admin.vue`：新增「用户管理」tab（用户列表 + 角色下拉改 + 禁用）
- `frontend/src/views/Documents.vue`：上传表单加部门/授权角色；文档行加「授权」按钮（弹窗设 dept/allowed_roles）

## 数据流（检索 ACL）

```
query → mixed_search → 候选 pool → 元数据过滤(tenant + doc_type + equipment + ★ACL: dept/allowed_roles) → MMR → 结果
```
ACL 过滤仅在后置元数据层加 2 个条件，不侵入检索算法。

## 兼容性

- 现有 `admin` → 自动映射全权限；`operator` → 现有业务权限（qa:*, doc:read）。**零破坏升级**
- 现有文档 `dept/allowed_roles` 为空 → 默认部门内全员可读（兼容旧行为）

## 错误处理

- 权限不足：`BizError("无权限", 403)`，前端 toast
- ACL 误配导致无可见文档：检索返回空（已有 No data 处理），不报错

## 测试

- `backend/tests/test_permissions.py`：权限矩阵单测（每角色对各 perm 的通过/拒绝）
- `backend/tests/test_acl.py`：检索 ACL 过滤集成测（跨部门用户查不到对方文档）
- 端点：document CRUD 跨角色鉴权测

## 范围（YAGNI）

- **不做**：用户级（非角色级）文档授权（只做到角色级，用户级 ACL 列表后续）
- **不做**：权限的运行时动态编辑 UI（首版用默认映射 + DB 表，UI 编辑后续）
- **不做**：字段级权限（如某字段脱敏）
- **不做**：SSO（本 spec 是 SSO 地基，SSO 单独 spec）
