/**
 * 前端 RBAC 权限镜像：与 backend/app/core/permissions.py 的 ROLE_PERMISSIONS 保持一致。
 *
 * 后端 require_perm 是真相之源；此处仅用于 UI 层「提前隐藏/禁用」无权限的菜单、Tab、按钮，
 * 避免用户点了才收 403。后端依旧独立鉴权，前端可见性不构成安全边界。
 *
 * 角色矩阵：
 * - admin    全权（通配 '*'）
 * - editor   文档全权 + 问答 + 反馈读 + 图谱编辑 + 领域
 * - operator 问答 + 读文档 + 读图谱 + 领域 + 反馈读
 * - auditor  全只读 + 审计/告警/指标读 + 领域
 */
const ADMIN_ALL = '*'

const ROLE_PERMISSIONS = {
  admin: new Set([ADMIN_ALL]),
  editor: new Set([
    'doc:read', 'doc:upload', 'doc:delete', 'doc:manage',
    'qa:answer', 'feedback:read',
    'kg:read', 'kg:edit', 'domain:use',
  ]),
  operator: new Set([
    'doc:read', 'qa:answer', 'feedback:read',
    'kg:read', 'domain:use',
  ]),
  auditor: new Set([
    'doc:read', 'qa:answer', 'feedback:read',
    'kg:read', 'domain:use',
    'alert:read', 'audit:read', 'metric:read',
  ]),
}

/**
 * 角色是否拥有某权限。与后端 has_perm 同语义：
 * admin 全放行；精确命中或资源通配（doc:* 覆盖 doc:delete）则通过。
 */
export function hasPerm(role, perm) {
  const perms = ROLE_PERMISSIONS[role]
  if (!perms) return false
  if (perms.has(ADMIN_ALL)) return true
  if (perms.has(perm)) return true
  const resource = perm.split(':', 1)[0]
  return perms.has(`${resource}:*`)
}

/** 角色中文标签（顶栏/菜单展示用）。 */
export const ROLE_LABEL = {
  admin: '管理员',
  editor: '编辑员',
  operator: '操作员',
  auditor: '审计员',
}
