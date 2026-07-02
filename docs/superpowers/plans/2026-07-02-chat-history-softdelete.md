# 智能问答历史 · 批量软删 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给智能问答历史加「批量软删」——会话粒度 + 消息粒度两个批量删除端点，DB 行保留（`is_deleted` 标记），列表/历史过滤掉；现有单个删除一并改为软删。

**Architecture:** `Conversation`/`Message` 两表各加 `is_deleted: bool` 列（走 `init_db._COLUMN_MIGRATIONS` 幂等补列，不动 Alembic）。`conversation_service` 加 `is_deleted==False` 过滤、把单删改软删、新增两个 `batch_delete_*` 函数（均按 `username` 归属过滤）。路由加两个 `POST /qa/.../batch-delete` 端点（30/min 限流 + 操作日志）。前端 `Chat.vue` 侧栏会话列表 + 会话内 user 消息各加 checkbox + 批量删除按钮。

**Tech Stack:** FastAPI + async SQLAlchemy 2.0（aiomysql）/ Pydantic v2（后端）；Vue 3 + Vite（前端）。

## Global Constraints

- **软删 only**：绝不用 `DELETE` 物理删行；一律 `is_deleted = True`（用户硬约束「数据库不删」）。
- **列演进走 `init_db._COLUMN_MIGRATIONS`**：`(表, 列, "TINYINT(1) NOT NULL DEFAULT 0")`，幂等（MySQL 1060 忽略），不写 Alembic revision。
- **归属过滤**：所有删除/批量删除必须按 `username == 当前用户` 过滤；删他人/不存在 → 影响数 0，不报错。
- **`ids` 去重 + 上限 200**；端点限流 `30/minute`。
- **后端实现期间绝不带 `--reload` 运行**（P0 事故根因）；改动后干净重启验证。
- **特性分支做、验证通过再合 main**。
- 平台端口：后端 `8001`、MySQL `3307`、代理 `7897`。
- 现有 `DELETE /qa/conversations/{conv_id}` 签名/返回（`{"deleted": bool}`）不变，仅行为由硬删→软删。

## Testing Reality（重要，影响每个 task 的验证步骤）

本项目**无 DB 测试基建**（`tests/` 全是纯函数，无 sqlite/测试库/AsyncSession fixture）。本特性 service 强耦合真实 AsyncSession + SQL，建 DB mock 超出本特性范围（spec 已定）。因此：

- **能纯测的（模型属性、Pydantic schema）**：照常 TDD（写失败 pytest → 实现 → 通过）。
- **DB 耦合的（service 函数、路由）**：每 task 用 `py_compile` + **import smoke**（导入不报错）验证；**真实行为**留到 **Task 6 端到端验证**（合并前必做）。

---

## File Structure

- `backend/app/models/conversation.py` — `Conversation`/`Message` 加 `is_deleted` 列（modify）。
- `backend/app/db/init_db.py` — `_COLUMN_MIGRATIONS` 加 2 条（modify）。
- `backend/app/schemas/qa.py` — 加 `BatchDeleteRequest`（modify）。
- `backend/app/services/conversation_service.py` — 过滤 + 软单删 + `get_messages` 带 id + 两个 batch 函数（modify）。
- `backend/app/routers/qa.py` — 两个 `batch-delete` 端点 + 导入 `BatchDeleteRequest`（modify）。
- `frontend/src/api/index.js` — `batchDeleteConversations` / `batchDeleteMessages`（modify）。
- `frontend/src/views/Chat.vue` — 会话/消息 checkbox + 批量删除按钮 + handler（modify）。
- `tests/test_chat_softdelete.py` — 纯函数测试（模型属性 + schema）（create）。

---

### Task 1: 模型 + 迁移：`is_deleted` 列

**Files:**
- Modify: `backend/app/models/conversation.py:5,11-25`
- Modify: `backend/app/db/init_db.py:20-30`
- Test: `tests/test_chat_softdelete.py`

**Interfaces:**
- Produces: `Conversation.is_deleted: Mapped[bool]`、`Message.is_deleted: Mapped[bool]`（后续 service/路由依赖）。

- [ ] **Step 0: 建特性分支**

```bash
git checkout -b feat/chat-history-softdelete
```

- [ ] **Step 1: 写失败测试（模型属性）**

创建 `tests/test_chat_softdelete.py`：

```python
"""智能问答历史软删：纯函数测试（模型属性 + schema）。DB 耦合逻辑靠 Task6 端到端验证。"""


def test_conversation_model_has_soft_delete_column():
    """Conversation / Message 两模型都应有 is_deleted 列（mapped_column 创建类属性，import 即可断言）。"""
    from app.models.conversation import Conversation, Message

    assert hasattr(Conversation, "is_deleted")
    assert hasattr(Message, "is_deleted")
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `cd backend && python -m pytest tests/test_chat_softdelete.py::test_conversation_model_has_soft_delete_column -v`
Expected: FAIL（`is_deleted` 还没加，hasattr 为 False）

- [ ] **Step 3: 改模型**

`backend/app/models/conversation.py` 第 5 行导入加 `Boolean`：

```python
from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
```

`Conversation`（第 11-16 行）末尾加列：

```python
class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    username: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
```

`Message`（第 19-25 行）末尾加列：

```python
class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    conversation_id: Mapped[str] = mapped_column(String(64), ForeignKey("conversations.id"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
```

- [ ] **Step 4: 改迁移补列**

`backend/app/db/init_db.py` 的 `_COLUMN_MIGRATIONS`（第 20-30 行）列表末尾追加 2 条：

```python
_COLUMN_MIGRATIONS = [
    ("chunks", "chunk_type", "VARCHAR(16) NOT NULL DEFAULT 'child'"),
    ("chunks", "parent_idx", "INT NOT NULL DEFAULT 0"),
    ("chunks", "section", "VARCHAR(256) NOT NULL DEFAULT ''"),
    ("feedbacks", "reason", "VARCHAR(256) NOT NULL DEFAULT ''"),
    ("feedbacks", "judge_supported", "FLOAT"),
    ("feedbacks", "judge_halluc", "FLOAT"),
    ("documents", "equipment_tags", "VARCHAR(512) NOT NULL DEFAULT ''"),
    ("documents", "tenant_id", "VARCHAR(64) NOT NULL DEFAULT 'default'"),
    ("users", "tenant_id", "VARCHAR(64) NOT NULL DEFAULT 'default'"),
    ("conversations", "is_deleted", "TINYINT(1) NOT NULL DEFAULT 0"),
    ("messages", "is_deleted", "TINYINT(1) NOT NULL DEFAULT 0"),
]
```

- [ ] **Step 5: 跑测试，确认通过**

Run: `cd backend && python -m pytest tests/test_chat_softdelete.py::test_conversation_model_has_soft_delete_column -v`
Expected: PASS

- [ ] **Step 6: py_compile + import smoke**

Run: `cd backend && python -m py_compile app/models/conversation.py app/db/init_db.py && python -c "from app.models.conversation import Conversation, Message; print('ok')"`
Expected: 打印 `ok`，无报错。

- [ ] **Step 7: 提交**

```bash
git add backend/app/models/conversation.py backend/app/db/init_db.py tests/test_chat_softdelete.py
git commit -m "feat(chat): Conversation/Message 加 is_deleted 软删列 + 迁移补列"
```

---

### Task 2: `BatchDeleteRequest` schema

**Files:**
- Modify: `backend/app/schemas/qa.py:1-5,54`
- Test: `tests/test_chat_softdelete.py`

**Interfaces:**
- Produces: `BatchDeleteRequest(ids: List[str])`（Task 4 路由依赖）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_chat_softdelete.py` 末尾追加：

```python
def test_batch_delete_request_accepts_ids():
    from app.schemas.qa import BatchDeleteRequest

    req = BatchDeleteRequest(ids=["conv1", "conv2"])
    assert req.ids == ["conv1", "conv2"]


def test_batch_delete_request_requires_ids():
    import pytest
    from pydantic import ValidationError

    from app.schemas.qa import BatchDeleteRequest

    with pytest.raises(ValidationError):
        BatchDeleteRequest()  # ids 缺省必须报错
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `cd backend && python -m pytest tests/test_chat_softdelete.py::test_batch_delete_request_accepts_ids tests/test_chat_softdelete.py::test_batch_delete_request_requires_ids -v`
Expected: FAIL（`BatchDeleteRequest` 不存在 → ImportError）

- [ ] **Step 3: 加 schema**

`backend/app/schemas/qa.py` 末尾（第 54 行 `ExportRequest` 之后）追加：

```python
class BatchDeleteRequest(BaseModel):
    ids: List[str]
```

（`List` 已在第 2 行 `from typing import List, Optional` 导入，无需改 import。）

- [ ] **Step 4: 跑测试，确认通过**

Run: `cd backend && python -m pytest tests/test_chat_softdelete.py -v`
Expected: PASS（3 个测试全过）

- [ ] **Step 5: 提交**

```bash
git add backend/app/schemas/qa.py tests/test_chat_softdelete.py
git commit -m "feat(qa): BatchDeleteRequest schema（批量软删请求体）"
```

---

### Task 3: `conversation_service` 过滤 + 软单删 + `get_messages` 带 id + 两个 batch 函数

**Files:**
- Modify: `backend/app/services/conversation_service.py:2,16-60`

**Interfaces:**
- Consumes: `Conversation.is_deleted` / `Message.is_deleted`（Task 1）。
- Produces:
  - `list_conversations` / `get_messages` 过滤 `is_deleted==False`；`get_messages` 多返回 `id` 字段。
  - `delete_conversation(db, username, conversation_id) -> bool`（签名/返回不变，行为改软删，级联软删该会话下消息）。
  - `batch_delete_conversations(db, username, ids: list[str]) -> int`。
  - `batch_delete_messages(db, username, ids: list[str]) -> int`。

> **DB 耦合，无单测**：本 task 用 py_compile + import smoke 验证；行为留 Task 6 端到端。

- [ ] **Step 1: 改 import（`delete` → `update`）**

`backend/app/services/conversation_service.py` 第 2 行：

```python
from sqlalchemy import desc, select, update
```

（原 `from sqlalchemy import delete, desc, select` —— `delete` 改软删后不再使用，换成 `update`。）

- [ ] **Step 2: `list_conversations` 加过滤**

第 16-25 行，`.where(Conversation.username == username)` 之后加一行 `is_deleted` 过滤：

```python
async def list_conversations(db: AsyncSession, username: str, keyword: str = "", limit: int = 50) -> list[dict]:
    stmt = select(Conversation).where(
        Conversation.username == username,
        Conversation.is_deleted == False,  # noqa: E712  软删过滤
    )
    if keyword:
        stmt = stmt.where(Conversation.title.like(f"%{keyword}%"))
    stmt = stmt.order_by(desc(Conversation.created_at)).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {"id": r.id, "title": r.title, "createdAt": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else ""}
        for r in rows
    ]
```

- [ ] **Step 3: `get_messages` 加过滤 + 返回 id**

第 28-38 行：

```python
async def get_messages(db: AsyncSession, conversation_id: str, limit: int = 6) -> list[dict]:
    """取最近 limit 条消息（按时间正序返回，供拼上下文）。"""
    rows = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.is_deleted == False)  # noqa: E712
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [{"id": r.id, "role": r.role, "content": r.content} for r in reversed(rows)]
```

> `id` 是**新增字段**，纯增量；既有消费者（`qa_service` 拼 context、`/qa/history` 前端）只读 `role`/`content`，不受影响。验证见 Step 6 grep。

- [ ] **Step 4: `delete_conversation` 改软删（含消息级联）**

第 46-60 行整段替换：

```python
async def delete_conversation(db: AsyncSession, username: str, conversation_id: str) -> bool:
    """软删对话(含其下消息)。仅能删自己的（ownership 校验）。DB 行保留，列表/历史过滤掉。"""
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.username == username,
                Conversation.is_deleted == False,  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if not conv:
        return False
    conv.is_deleted = True
    await db.execute(
        update(Message)
        .where(Message.conversation_id == conversation_id, Message.is_deleted == False)  # noqa: E712
        .values(is_deleted=True)
    )
    await db.commit()
    return True
```

- [ ] **Step 5: 加两个 batch 函数**

文件末尾（`rename_conversation` 之后）追加：

```python
_MAX_BATCH = 200  # 单次批量删除上限（防超长 IN 列表）


async def batch_delete_conversations(db: AsyncSession, username: str, ids: list[str]) -> int:
    """批量软删会话（含其下消息）。仅删本人会话；返回实际软删条数。"""
    if not ids:
        return 0
    ids = list(dict.fromkeys(ids))[:_MAX_BATCH]  # 去重 + 截断上限
    # 仅取属于该用户、未删的会话（防 ids 里混入他人会话 id）
    owned = (
        await db.execute(
            select(Conversation.id).where(
                Conversation.id.in_(ids),
                Conversation.username == username,
                Conversation.is_deleted == False,  # noqa: E712
            )
        )
    ).scalars().all()
    if not owned:
        return 0
    res = await db.execute(
        update(Conversation).where(Conversation.id.in_(owned)).values(is_deleted=True)
    )
    # 级联软删这些（本人）会话下的消息
    await db.execute(
        update(Message)
        .where(Message.conversation_id.in_(owned), Message.is_deleted == False)  # noqa: E712
        .values(is_deleted=True)
    )
    await db.commit()
    return res.rowcount or 0


async def batch_delete_messages(db: AsyncSession, username: str, ids: list[str]) -> int:
    """批量软删消息。归属校验：只删属于该用户会话下的消息；返回实际软删条数。"""
    if not ids:
        return 0
    ids = list(dict.fromkeys(ids))[:_MAX_BATCH]
    # 先查该用户所有会话 id 集合，再只软删属于这些会话的消息（他人会话消息不动）
    owned_conv_ids = (
        await db.execute(select(Conversation.id).where(Conversation.username == username))
    ).scalars().all()
    if not owned_conv_ids:
        return 0
    res = await db.execute(
        update(Message)
        .where(
            Message.id.in_(ids),
            Message.conversation_id.in_(owned_conv_ids),
            Message.is_deleted == False,  # noqa: E712
        )
        .values(is_deleted=True)
    )
    await db.commit()
    return res.rowcount or 0
```

- [ ] **Step 6: py_compile + import smoke + grep 确认 `get_messages` 消费者不受 id 影响**

Run:
```bash
cd backend && python -m py_compile app/services/conversation_service.py && \
python -c "from app.services import conversation_service as s; print(hasattr(s,'batch_delete_conversations'), hasattr(s,'batch_delete_messages'))"
```
Expected: 打印 `True True`，无报错。

Run（确认 `get_messages` 所有调用方只读 role/content，新增 id 不破坏）:
```bash
cd backend && grep -rn "get_messages" app/
```
Expected: 列出调用点（如 `routers/qa.py` history、`services/qa_service.py` 拼 context），人工确认它们取的是 `["role"]`/`["content"]`，不因多了 `id` key 报错（dict 多 key 无害）。

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/conversation_service.py
git commit -m "feat(chat): service 软删过滤 + 单删改软删 + batch_delete_conversations/messages"
```

---

### Task 4: 路由两个 `batch-delete` 端点

**Files:**
- Modify: `backend/app/routers/qa.py:13-22,93`

**Interfaces:**
- Consumes: `BatchDeleteRequest`（Task 2）、`conversation_service.batch_delete_conversations/messages`（Task 3）。
- Produces: `POST /qa/conversations/batch-delete`、`POST /qa/messages/batch-delete`（Task 5 前端依赖）。

> **DB 耦合，无单测**：import smoke 验证；行为留 Task 6。

- [ ] **Step 1: import 加 `BatchDeleteRequest`**

`backend/app/routers/qa.py` 第 13-21 行的 schema import 列表，按字母序插入 `BatchDeleteRequest`：

```python
from app.schemas.qa import (
    BatchDeleteRequest,
    ExportRequest,
    FaithfulnessRequest,
    FeedbackRequest,
    QaAnswerRequest,
    RelatedRequest,
    RenameRequest,
    TermRequest,
)
```

- [ ] **Step 2: 加两个端点**

在第 93 行 `delete_conv` 函数之后、第 96 行 `history` 之前插入：

```python
@router.post("/conversations/batch-delete")
@limiter.limit("30/minute")
async def batch_delete_convs(
    request: Request,
    body: BatchDeleteRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """批量软删会话（DB 行保留，列表过滤）。仅删本人会话。"""
    n = await conversation_service.batch_delete_conversations(db, user.username, body.ids)
    await write_log(db, user.username, "批量删除会话", f"{n} 条")
    return success({"deleted": n}, f"已删除 {n} 条")


@router.post("/messages/batch-delete")
@limiter.limit("30/minute")
async def batch_delete_msgs(
    request: Request,
    body: BatchDeleteRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """批量软删消息（DB 行保留，历史过滤）。仅删本人会话下消息。"""
    n = await conversation_service.batch_delete_messages(db, user.username, body.ids)
    await write_log(db, user.username, "批量删除消息", f"{n} 条")
    return success({"deleted": n}, f"已删除 {n} 条")
```

> 路由顺序说明：现有 `/qa/conversations/{conv_id}` 是 PUT/DELETE，无 POST，故 `POST /qa/conversations/batch-delete` 无路径参数冲突。

- [ ] **Step 3: import smoke（FastAPI app 导入不报错）**

Run:
```bash
cd backend && python -m py_compile app/routers/qa.py && \
python -c "from app.routers.qa import router; print([r.path for r in router.routes if 'batch' in r.path])"
```
Expected: 打印含 `/qa/conversations/batch-delete` 和 `/qa/messages/batch-delete`，无报错。

- [ ] **Step 4: 提交**

```bash
git add backend/app/routers/qa.py
git commit -m "feat(qa): 批量软删会话/消息端点（30/min 限流 + 操作日志）"
```

---

### Task 5: 前端 API + `Chat.vue` checkbox/批量删除 UI

**Files:**
- Modify: `frontend/src/api/index.js:134-139`
- Modify: `frontend/src/views/Chat.vue`（template 侧栏 + 消息区，script 状态/handler，style）

**Interfaces:**
- Consumes: Task 4 两个端点。

- [ ] **Step 1: api 加两个函数**

`frontend/src/api/index.js` 第 136 行 `deleteConversation` 之后插入：

```js
export const batchDeleteConversations = (ids) =>
  request.post('/qa/conversations/batch-delete', { ids })
export const batchDeleteMessages = (ids) =>
  request.post('/qa/messages/batch-delete', { ids })
```

- [ ] **Step 2: `Chat.vue` import 加新 api + `getHistory` 已有**

第 117 行 import 末尾的 `renameConversation` 后追加（保持一行）：

```js
import { streamAnswer, streamAnswerWS, sendFeedback, getFaithfulness, getRelatedQuestions, getConversations, getHistory, deleteConversation, renameConversation, batchDeleteConversations, batchDeleteMessages, exportAnswer } from '../api'
```

- [ ] **Step 3: 加状态（选中集合）**

在 `const conversations = ref([])`（约第 152 行）附近加：

```js
const selectedConvs = ref(new Set())   // 批量选中会话 id
const selectedMsgs = ref(new Set())    // 批量选中消息 id
```

- [ ] **Step 4: 模板——侧栏会话列表加 checkbox + 批量工具条**

把第 5-6 行之间（`<button class="btn btn-primary new-btn" ...>` 之后、`<input class="input" v-model="searchKw" ...>` 之前）插入批量工具条：

```html
<div class="conv-batch" v-if="selectedConvs.size">
  <span class="hint">已选 {{ selectedConvs.size }}</span>
  <button class="btn btn-danger btn-sm" @click="batchRemoveConvs">🗑️ 批量删除</button>
  <button class="btn btn-ghost btn-sm" @click="selectedConvs = new Set()">取消</button>
</div>
```

把第 10-17 行的 `<template v-else>` 块内（`<div class="conv-title">` 之前）插入 checkbox：

```html
<template v-else>
  <input type="checkbox" class="conv-check" :checked="selectedConvs.has(c.id)" @click.stop="toggleConv(c.id)" title="选中以批量删除" />
  <div class="conv-title">{{ c.title || '(无标题对话)' }}</div>
  <div class="conv-time">{{ c.createdAt }}</div>
  <div class="conv-ops" @click.stop>
    <a @click="startRename(c)" title="重命名">✏️</a>
    <a class="danger" @click="removeConv(c)" title="删除">🗑️</a>
  </div>
</template>
```

- [ ] **Step 5: 模板——会话内 user 消息加 checkbox + 消息批量工具条**

在第 26 行 `<div class="msg-list" ref="msgListEl">` 之后插入消息批量工具条：

```html
<div class="msg-batch" v-if="selectedMsgs.size">
  <span class="hint">已选 {{ selectedMsgs.size }} 条消息</span>
  <button class="btn btn-danger btn-sm" @click="batchRemoveMsgs">🗑️ 批量删除</button>
  <button class="btn btn-ghost btn-sm" @click="selectedMsgs = new Set()">取消</button>
</div>
```

把第 33-36 行 user 消息 `<template v-else>` 块改为（仅在 `m.id` 存在时渲染 checkbox——历史加载的消息有 id，当轮新发消息无 id 不勾选）：

```html
<template v-else>
  <label v-if="m.id" class="msg-check" @click.stop><input type="checkbox" :checked="selectedMsgs.has(m.id)" @change="toggleMsg(m.id)" /></label>
  {{ m.content }}
  <a class="edit-btn" @click="startEdit(m)" title="编辑后重新提问">✏️</a>
</template>
```

- [ ] **Step 6: script 加 handler 函数**

在 `removeConv` 函数（约第 296 行）之后追加：

```js
function toggleConv(id) {
  const s = new Set(selectedConvs.value); s.has(id) ? s.delete(id) : s.add(id); selectedConvs.value = s
}
async function batchRemoveConvs() {
  const ids = [...selectedConvs.value]
  if (!ids.length) return
  if (!confirm(`删除选中的 ${ids.length} 个对话？`)) return
  try {
    const r = await batchDeleteConversations(ids)
    const n = (r.data && r.data.deleted) || 0
    selectedConvs.value = new Set()
    await loadConversations()
    if (currentConvId.value && ids.includes(currentConvId.value)) newChat()
    toast(`已删除 ${n} 条`)
  } catch (e) { toast('删除失败') }
}
function toggleMsg(id) {
  const s = new Set(selectedMsgs.value); s.has(id) ? s.delete(id) : s.add(id); selectedMsgs.value = s
}
async function batchRemoveMsgs() {
  const ids = [...selectedMsgs.value]
  if (!ids.length) return
  if (!confirm(`删除选中的 ${ids.length} 条消息？`)) return
  try {
    const r = await batchDeleteMessages(ids)
    const n = (r.data && r.data.deleted) || 0
    selectedMsgs.value = new Set()
    messages.value = messages.value.filter((m) => !ids.includes(m.id))   // 本地移除已删 user 消息
    toast(`已删除 ${n} 条`)
  } catch (e) { toast('删除失败') }
}
```

- [ ] **Step 7: `selectConv` / `newChat` 清空消息选中**

把第 191-195 行 `selectConv` 改为（开头清空 selectedMsgs）：

```js
async function selectConv(id) {
  selectedMsgs.value = new Set()
  currentConvId.value = id
  const r = await getHistory(id)
  messages.value = (r.data || []).map((m) => ({ id: m.id, role: m.role, content: m.content, sources: [], time: 0, halluc: 0, query: m.role === 'user' ? m.content : '', fb: '' }))
}
```

把第 196 行 `newChat` 改为：

```js
function newChat() { currentConvId.value = ''; messages.value = []; selectedMsgs.value = new Set() }
```

- [ ] **Step 8: style 加最小样式**

在 `<style scoped>` 内（如 `.empty.small` 之前）追加：

```css
.conv-batch { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; font-size: 12px; }
.conv-batch .hint { color: var(--text-soft); }
.conv-check { margin-right: 6px; cursor: pointer; }
.msg-batch { position: sticky; top: 0; z-index: 6; display: flex; align-items: center; gap: 8px; padding: 6px 10px; margin-bottom: 8px; background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-sm); font-size: 12px; }
.msg-check { display: inline-flex; align-items: center; margin-right: 6px; cursor: pointer; opacity: .7; }
.msg-check:hover { opacity: 1; }
```

- [ ] **Step 9: 前端构建验证**

Run: `cd frontend && npm run build`
Expected: 构建成功，无编译错误（Vite 报错则修）。

- [ ] **Step 10: 提交**

```bash
git add frontend/src/api/index.js frontend/src/views/Chat.vue
git commit -m "feat(chat): 会话/消息 checkbox + 批量软删 UI"
```

---

### Task 6: 端到端验证（合并前必做）

**Files:** 无（验证 only）

> 这是 DB 耦合逻辑（Task 3/4）的唯一行为门禁。**后端绝不用 `--reload` 启动**。

- [ ] **Step 1: 干净启动后端（无 --reload）**

```bash
cd backend && python -m uvicorn app.main:app --host 0.0.0.0 --port 8001
```
（观察日志：`init_db` 跑完，`_ensure_columns` 给 conversations/messages 补了 `is_deleted` 列；无报错。）

- [ ] **Step 2: 登录拿 token**

```bash
curl -s -X POST http://localhost:8001/api/system/login -H "Content-Type: application/json" -d '{"username":"admin","password":"<ADMIN_PASSWORD>"}'
```
Expected: 返回含 `token`。记为 `$T`。

- [ ] **Step 3: 造数据——建 1 个会话 + 发 2 条问答（产生 user/assistant 消息）**

```bash
curl -s -X POST http://localhost:8001/api/qa/answer -H "Authorization: Bearer $T" -H "Content-Type: application/json" -d '{"query":"主变压器温度异常如何处置"}' > /tmp/ans1.json
curl -s -X POST http://localhost:8001/api/qa/answer -H "Authorization: Bearer $T" -H "Content-Type: application/json" -d '{"query":"SF6断路器漏气如何处理","conversationId":"<ans1里的conversationId>"}' > /tmp/ans2.json
```
Expected: 两条都返回 `data.conversationId`。

- [ ] **Step 4: 取会话列表 + 历史，拿到 conv id 与 message id**

```bash
curl -s http://localhost:8001/api/qa/conversations -H "Authorization: Bearer $T"
curl -s "http://localhost:8001/api/qa/history?conversationId=<convId>" -H "Authorization: Bearer $T"
```
Expected: history 返回的每条消息**含 `id` 字段**（Task 3 Step 3 改动生效）；记录 user 消息的 id。

- [ ] **Step 5: 批量软删消息**

```bash
curl -s -X POST http://localhost:8001/api/qa/messages/batch-delete -H "Authorization: Bearer $T" -H "Content-Type: application/json" -d '{"ids":["<某条user消息id>"]}'
```
Expected: `{"code":0,"data":{"deleted":1},"msg":"已删除 1 条"}`。
再拉 history → 该消息不再返回；直查 DB `SELECT id,is_deleted FROM messages WHERE id='<那条>'` → `is_deleted=1`（行仍在）。

- [ ] **Step 6: 批量软删会话**

```bash
curl -s -X POST http://localhost:8001/api/qa/conversations/batch-delete -H "Authorization: Bearer $T" -H "Content-Type: application/json" -d '{"ids":["<convId>"]}'
```
Expected: `{"code":0,"data":{"deleted":1},"msg":"已删除 1 条"}`。
再拉 conversations → 该会话不再返回；直查 DB `SELECT id,is_deleted FROM conversations WHERE id='<convId>'` → `is_deleted=1`。

- [ ] **Step 7: 归属安全——用别人的 token 删（应 deleted=0）**

如无第二用户，跳过；否则用非 owner token 调 batch-delete 同一 convId：
Expected: `{"data":{"deleted":0}}`，DB `is_deleted` 仍为 0。

- [ ] **Step 8: 前端联调**

浏览器打开前端 → 登录 → 新建对话发几条 → 侧栏勾选会话 → 点「批量删除」→ 列表移除 + toast；会话内勾选 user 消息 → 「批量删除」→ 消息移除 + toast。

Expected: UI 行为符合预期，无控制台报错。

- [ ] **Step 9: （无需提交；验证通过即可合并）**

全部通过后执行合并（见 Execution Handoff 后的 `finishing-a-development-branch`）。

---

## Self-Review

**1. Spec 覆盖**：
- is_deleted 列（Conversation+Message）→ Task 1 ✓
- init_db._COLUMN_MIGRATIONS 补列 → Task 1 Step 4 ✓
- list_conversations / get_messages 过滤 → Task 3 Step 2/3 ✓
- delete_conversation 改软删 → Task 3 Step 4 ✓
- batch_delete_conversations（含归属）→ Task 3 Step 5 ✓
- batch_delete_messages（归属：先查本人 conv_ids）→ Task 3 Step 5 ✓
- 两个 batch-delete 端点 + 限流 30/min + write_log → Task 4 ✓
- BatchDeleteRequest schema → Task 2 ✓
- 前端 api 两函数 → Task 5 Step 1 ✓
- Chat.vue 会话/消息 checkbox + 批量删除按钮 → Task 5 ✓
- ids 上限 200 → Task 3 `_MAX_BATCH` ✓
- 测试策略（py_compile + import smoke + 端到端）→ 每 task Step 6/3 + Task 6 ✓
- YAGNI（无恢复端点/无 deleted_at/无 admin 视图）→ 未做，符合 ✓

**2. 占位符扫描**：无 TBD/TODO；每步含完整代码或确切命令。`<ADMIN_PASSWORD>`/`<convId>` 等是 e2e 步骤里的运行时实参（非计划占位），属正常。

**3. 类型一致性**：`batch_delete_conversations(db, username, ids: list[str]) -> int` / `batch_delete_messages(...)` 在 Task 3 定义、Task 4 调用，签名一致；前端 `batchDeleteConversations(ids)` / `batchDeleteMessages(ids)` 在 api 定义、Chat.vue 调用，名一致；`selectedConvs`/`selectedMsgs` 全程 Set，`toggleConv`/`toggleMsg`/`batchRemoveConvs`/`batchRemoveMsgs` 名一致。
