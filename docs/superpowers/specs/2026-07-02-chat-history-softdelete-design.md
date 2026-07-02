# 智能问答历史 · 批量软删 — 设计 spec

> 日期：2026-07-02 ｜ 选型：`is_deleted: bool` 软删列（用户确认，方案 A）
> 状态：待用户审阅

## 背景与目标

智能问答页（Chat）的历史只能**单个硬删**（`DELETE /qa/conversations/{id}` → 行直接没），两个不足：① 不能批量；② 物理删除丢数据、无审计。

**目标**：① 支持会话、单条消息两个粒度的**批量删除**；② **软删**——加 `is_deleted` 列，DB 行保留（审计/可恢复），列表与历史过滤掉。现有单个删除一并改为软删（一致性）。

## 模型：加 `is_deleted` 列

- `Conversation` + `is_deleted: Mapped[bool] = mapped_column(default=False, index=True)`
- `Message` + `is_deleted: Mapped[bool] = mapped_column(default=False, index=True)`

> 建表走 `init_db._COLUMN_MIGRATIONS` 幂等补列（项目既有老库演进机制），无需 Alembic revision。

## 后端组件

### `conversation_service`（改 + 新增）
- `list_conversations(db, username, keyword)`：加 `.where(Conversation.is_deleted == False)`。
- `get_messages(db, conversation_id, limit)`：加 `.where(Message.is_deleted == False)`。
- `delete_conversation(db, username, conv_id)`：**改硬删 → 软删**（`conv.is_deleted = True`，校验 `username` 归属），签名/返回（`bool`）不变。
- 新增 `batch_delete_conversations(db, username, ids: list[str]) -> int`：`UPDATE conversations SET is_deleted=True WHERE id IN ids AND username=:u AND is_deleted=False`，返回影响行数。
- 新增 `batch_delete_messages(db, username, ids: list[str]) -> int`：软删消息，**校验归属**——先查该用户所有 `conversation_id` 集合，再 `UPDATE messages SET is_deleted=True WHERE id IN ids AND conversation_id IN (该用户 conv_ids) AND is_deleted=False`，返回影响行数（非本人会话的消息不动）。

### 路由 `routers/qa.py`
- `DELETE /qa/conversations/{conv_id}`（现有，行为变软删，签名不变）。
- 新增 `POST /qa/conversations/batch-delete`，body `BatchDeleteRequest{ids: list[str]}` → `batch_delete_conversations`，`@limiter.limit("30/minute")` + `write_log("批量删除会话", N)`。
- 新增 `POST /qa/messages/batch-delete`，body `BatchDeleteRequest{ids: list[str]}` → `batch_delete_messages`，`@limiter.limit("30/minute")` + `write_log`。
- 均依赖 `get_current_user`（用户删自己的）。

### schema `schemas/qa.py`
- 新增 `BatchDeleteRequest(BaseModel): ids: list[str]`。

## 前端

### `frontend/src/api/index.js`
- `batchDeleteConversations(ids) => request.post('/qa/conversations/batch-delete', { ids })`
- `batchDeleteMessages(ids) => request.post('/qa/messages/batch-delete', { ids })`
- 现有 `deleteConversation(id)` 不变（后端已软删）。

### `frontend/src/views/Chat.vue`
- **侧栏会话列表**：每项加 checkbox（`v-model` 绑选中集合 `selectedConvs`），顶部加「批量删除」按钮（`selectedConvs.length >= 1` 启用），点击 → `batchDeleteConversations` → 本地从列表移除 + 清空选中 + toast「已删除 N 条」。
- **会话内消息**：user 消息加 checkbox（`selectedMsgs`）+「批量删除」按钮 → `batchDeleteMessages` → 本地从消息列表移除 + toast。
- 单个删除（现有 trash 按钮）保持，行为变软删（无需前端改）。

## 数据流

```
勾选会话/消息 → POST batch-delete {ids} → service: UPDATE is_deleted=True（过滤 username 归属）
  → 返回影响数 N → 前端本地移除（列表/历史不再含）→ DB 行保留（审计）
GET conversations/history → .where(is_deleted==False) → 软删项不返回
```

## 权限 / 错误处理
- 只软删**本人**数据（`username` 过滤）；删他人/不存在 → 影响数 0，不报错。
- 批量上限：service 层 `ids` 截断到合理上限（如 200），防超长。
- 限流 30/minute。
- `is_deleted` 已 True 的再删幂等（仍是 True）。

## 测试
本项目既有测试**全是纯函数**（无 DB fixture，无 sqlite/测试库基建）。本功能的 service 逻辑强耦合 AsyncSession + 真实 SQL，建 DB mock 基建超出本特性范围。因此验证策略与既有 domain/qa 端点一致：
- **py_compile + import smoke**：`conversation_service` / `qa.py` 路由 / `BatchDeleteRequest` 能正常导入、`init_metric_series` 不报错。
- **真实端到端**（合并前）：登录 → 建几个会话/消息 → 调 batch-delete → 验证 list/history 不再返回、DB 行仍在（直查 `is_deleted=1`）。
- 若后续项目引入 sqlite 测试库，再补 `batch_delete` 归属/幂等/过滤的单测（记为后续改进，不阻塞本特性）。

## 范围（YAGNI）
- **不做**：恢复端点（DB 留底，admin 可直查 SQL 恢复）、`deleted_at` 时间戳（`is_deleted` 够）、admin 查"已删"视图、消息级单个删除端点（只要批量）。
