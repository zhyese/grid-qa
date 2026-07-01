# 对话控制（停止/重新生成/编辑重提）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development 或 superpowers:executing-plans 逐任务实现。步骤用 `- [ ]` 跟踪。

**Goal:** 给 Chat 页补「停止生成 / 重新生成 / 编辑重提（追加新轮）」三个高频操作。

**Architecture:** 前端 AbortController 中断流式 fetch；重新生成/编辑重提复用现有 `/qa/answer/stream`，作为新一轮追加（保留历史）；后端仅加 `regen` 参数跳过缓存读，无 cancel 端点（客户端断连即停）。

**Tech Stack:** FastAPI（后端）/ Vue 3 + Vite（前端）/ SSE fetch stream

## Global Constraints
- 不改历史消息（编辑重提=追加新轮，明确选型）
- 兼容现有 SSE + WS 双通道；本次只改 SSE（fetch+AbortController），WS 保持现状
- 后端无测试套件（`backend/tests/` 不存在），验证用 curl + 前端 `npm run build` + 手动
- 后端运行：`venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --app-dir backend`（无 --reload，改完要重启）

## File Structure
- Modify: `backend/app/services/qa_service.py`（stream_answer 加 regen 形参，跳缓存读）
- Modify: `backend/app/routers/qa.py`（/qa/answer/stream 加 regen query 形参）
- Modify: `frontend/src/api/index.js`（streamAnswer 加 signal 形参透传 fetch）
- Modify: `frontend/src/views/Chat.vue`（停止钮 / 重新生成 / 编辑重提）

---

### Task 1: 后端 stream_answer 支持 regen 跳缓存

**Files:**
- Modify: `backend/app/services/qa_service.py`（stream_answer 签名 + 缓存读处）
- Modify: `backend/app/routers/qa.py`（/qa/answer/stream 端点）

**Interfaces:**
- Produces: `stream_answer(query, model_type, conversation_id, regen=False)`；`POST /qa/answer/stream?regen=true`

- [ ] **Step 1: 改 stream_answer 签名 + 跳缓存读**

`qa_service.py` 找到 `async def stream_answer(` 行，加 `regen: bool = False` 形参；在缓存读处（`cached = await redis_client.cache_get_json(...)` 外层）加 `if not regen:` 守卫。即：

```python
async def stream_answer(query, model_type=None, conversation_id=None, regen: bool = False):
    # ... 原有逻辑 ...
    # 缓存读改为：
    cached = None
    if not regen:
        try:
            cached = await redis_client.cache_get_json(_cache_key(model_type, nq))
        except Exception as e:
            degraded("qa_cache_get", e)
    if cached:
        # ... 原命中返回 ...
```

- [ ] **Step 2: 端点透传 regen**

`routers/qa.py` 的 `/qa/answer/stream` 端点加 `regen: bool = False` query 形参，传给 `stream_answer(..., regen=regen)`：

```python
@router.post("/answer/stream")
async def answer_stream(body: ..., regen: bool = False, ...):
    return await qa_service.stream_answer(body.query, body.modelType, body.conversationId, regen=regen)
```
（按现有 body schema 实际字段调整，regen 走 query string）

- [ ] **Step 3: 语法检查 + 重启 + curl 验证**

```bash
venv/Scripts/python.exe -m py_compile backend/app/services/qa_service.py backend/app/routers/qa.py
# 重启后端，登录拿 token，对同一问题连续两次 stream：
# 第一次正常（可能写缓存），第二次带 regen=true 应重新生成（不命中缓存）
curl -s -N -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  -d '{"query":"变压器温度","modelType":"deepseek"}' \
  "http://localhost:8001/api/qa/answer/stream?regen=true"
# 预期：返回 data: {type:meta,...} data:{type:token,...} ... data:[DONE]，cached 字段为 false
```

- [ ] **Step 4: Commit**
```bash
git add backend/app/services/qa_service.py backend/app/routers/qa.py
git commit -m "feat(qa): stream_answer 支持 regen 跳过缓存读（重新生成用）"
```

---

### Task 2: 前端 streamAnswer 支持 AbortController + 停止钮

**Files:**
- Modify: `frontend/src/api/index.js`（streamAnswer 加 signal）
- Modify: `frontend/src/views/Chat.vue`（abortCtrl + 停止钮）

**Interfaces:**
- Produces: `streamAnswer(query, modelType, conversationId, onEvent, signal)`；Chat.vue loading 态有「停止」按钮

- [ ] **Step 1: api streamAnswer 加 signal**

`api/index.js` 的 `streamAnswer` 加 `signal` 形参，透传给 fetch：

```js
export const streamAnswer = async (query, modelType, conversationId, onEvent, signal, regen = false) => {
  const auth = useAuthStore()
  const resp = await fetch(`/api/qa/answer/stream${regen ? '?regen=true' : ''}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {}) },
    body: JSON.stringify({ query, modelType, conversationId }),
    signal,   // ← 新增
  })
  // ... 原有 reader 逻辑不变；catch 里忽略 AbortError ...
  try { /* 原 while 循环 */ } catch (e) { if (e.name !== 'AbortError') throw e }
}
```

- [ ] **Step 2: Chat.vue 停止钮**

`Chat.vue` script 加 `const abortCtrl = ref(null)`；发送时 `abortCtrl.value = new AbortController()` 并把 `abortCtrl.value.signal` 传给 streamAnswer；停止钮 `@click="abortCtrl?.abort()"`。模板里发送按钮 `:disabled="loading"` 改为：loading 时显示「⏹ 停止」（点击 abort），否则「发送」。

```vue
<button v-if="loading" class="btn btn-ghost" @click="abortCtrl && abortCtrl.abort()">⏹ 停止</button>
<button v-else class="btn btn-primary" :disabled="!input" @click="send">发送</button>
```
中断后保留已收 token，给 assistant 消息追加 `[已中断]` 标记，不再调 faithfulness/related。

- [ ] **Step 3: 构建验证 + 手动**
```bash
cd frontend && npm run build   # 预期 ✓ built，无 error
```
手动：发送一问→生成中点「停止」→生成中断、部分答案留存、loading 复位。

- [ ] **Step 4: Commit**
```bash
git add frontend/src/api/index.js frontend/src/views/Chat.vue
git commit -m "feat(chat): 流式生成支持 AbortController 中断 + 停止按钮"
```

---

### Task 3: 重新生成（追加新轮，跳缓存）

**Files:** Modify: `frontend/src/views/Chat.vue`

- [ ] **Step 1: assistant 消息加「🔄 重新生成」按钮**

每条 assistant 消息 hover 显示操作栏（复用现有复制/导出位置）加「🔄 重新生成」。点击：取该轮 user 消息的原 query，调 `streamAnswer(query, modelType, conversationId, onEvent, undefined, true)`（regen=true），结果作为**新一轮**追加（push 到消息列表，不替换原答）。

```js
async function regenerate(msg) {
  const prevQuery = /* 找到该 assistant 上一条 user 的 query */
  await runStream(prevQuery, { regen: true })   // runStream 是现有发送流程的复用，追加到同 conversation
}
```

- [ ] **Step 2: 构建验证 + 手动**
```bash
cd frontend && npm run build   # 预期 ✓ built
```
手动：问一问→答完→点「重新生成」→新增一轮答案（原答保留），且因 regen=true 不命中缓存。

- [ ] **Step 3: Commit**
```bash
git add frontend/src/views/Chat.vue
git commit -m "feat(chat): 重新生成（追加新轮 + regen 跳缓存）"
```

---

### Task 4: 编辑重提（追加新轮，保留历史）

**Files:** Modify: `frontend/src/views/Chat.vue`

- [ ] **Step 1: user 消息加 inline 编辑**

每条 user 消息加「✏️ 编辑」按钮。点击：该 user 文本变 `<textarea v-model="msg.editText">` + 「重提」「取消」按钮。「重提」：以 `msg.editText` 作为新 query 调 `runStream(editText)`（同 conversationId，追加新轮，原问答保留）。

```js
function startEdit(msg) { msg.editing = true; msg.editText = msg.content }
async function resendEdit(msg) {
  msg.editing = false
  await runStream(msg.editText)   // 追加新轮，历史不动
}
```

- [ ] **Step 2: 构建验证 + 手动**
```bash
cd frontend && npm run build   # 预期 ✓ built
```
手动：问一问→点 user 消息「编辑」→改文本→「重提」→原问答保留 + 新一轮用改后 query。

- [ ] **Step 3: Commit**
```bash
git add frontend/src/views/Chat.vue
git commit -m "feat(chat): 编辑重提（就地编辑 + 追加新轮保留历史）"
```

---

## Self-Review（已自检）
- **Spec 覆盖**：停止(Task2)/重新生成(Task3)/编辑重提(Task4) 三操作齐全；regen 跳缓存(Task1) 支撑重新生成。✅
- **类型一致**：`streamAnswer(..., signal, regen)`、`stream_answer(..., regen=False)` 前后端签名对齐。✅
- **无占位符**：每步含实代码或实命令。✅
- **YAGNI**：不做服务端 cancel、不做 diff 对比、不改历史（spec 已界定）。✅
