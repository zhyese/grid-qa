# 对话控制：停止生成 / 重新生成 / 编辑重提 — 设计 spec

> 日期：2026-07-01 ｜ 选型：编辑重提=追加为新轮保留历史（用户确认）
> 状态：待用户审阅

## 背景与目标

`Chat.vue` 是高频页面，但三个最常用对话操作全缺：
- 流式生成中**无法中断**（`Chat.vue:85` 发送钮只 `:disabled="loading"`，无停止钮；零 AbortController）
- 答错**不能重新生成**（只能新建问题重问）
- 问题打错**不能编辑重提**（无就地编辑入口）

**目标**：补齐这三个高频操作，体验提升立竿见影。颗粒度小、不改后端架构。

## 设计

### 1. 停止生成
- 前端用 `AbortController` 中断流式请求（SSE/fetch 天然支持，WS 用 `ws.close()`）
- `api/index.js` 的 `streamAnswer` 接收外部 `signal`，传入 `fetch(..., {signal})`
- `Chat.vue`：loading 时发送钮变「停止」按钮，点击 `abortController.abort()`
- 中断后保留已收到的部分答案（标 `[已中断]`），存为该轮 assistant 消息（不调用后续 faithfulness/related）

### 2. 重新生成
- 每条 assistant 消息加「🔄 重新生成」按钮（hover 显示）
- 点击：以**原 query + 同 conversationId** 重新调 `streamAnswer`，结果作为**新一轮**追加（保留原答，历史可对比）
- 后端零改动（复用现有 `/qa/answer/stream`，缓存 key 相同会命中——需给 regenerate 加 `?regen=1` 绕过缓存，或在 `qa_service` 对 regenerate 跳过缓存读）

### 3. 编辑重提（追加为新轮）
- 每条 user 消息加「✏️ 编辑」按钮
- 点击：user 文本变可编辑 textarea + 「重提」「取消」
- 「重提」：以编辑后文本作为**新一轮追问**发送（`streamAnswer(editedQuery, ..., conversationId)`），原问答**保留**在历史
- 与重新生成共用发送链路；conversationId 不变保证多轮上下文连续

### 数据流（三操作共用）

```
用户操作 → Chat.vue 组装 (query, conversationId) → streamAnswer(signal) → SSE/WS
   ├─ 停止：abort() → fetch 中断 → 保留部分答案
   ├─ 重新生成：原query + regen=1 → 新一轮
   └─ 编辑重提：editedQuery → 新一轮（历史保留）
```

## 组件

### 前端（主要改动集中在此）
- `frontend/src/views/Chat.vue`：
  - 加 `abortCtrl` ref + 停止按钮（loading 态切换）
  - assistant 消息操作栏加「重新生成」
  - user 消息加「编辑」inline 编辑模式
- `frontend/src/api/index.js`：`streamAnswer(query, modelType, conversationId, onEvent, signal)` 增 `signal` 参数；`streamAnswerWS` 增返回 `ws` 句柄便于 close

### 后端（最小改动）
- `backend/app/routers/qa.py`：`/qa/answer/stream` 增可选 `regen: bool` query；`qa_service.stream_answer` 对 `regen=True` 跳过缓存读（写缓存仍可）
- 无需 cancel 端点（客户端断连即停，FastAPI/uvicorn 自动处理）

## 错误处理

- 中断后再次发送：正常新建 abortCtrl，不冲突
- 重新生成时网络失败：复用现有 SSE error 处理，toast 提示
- 编辑重提空文本：前端禁用「重提」按钮

## 测试

- 手动验证：停止（中断后部分答案留存）、重新生成（新增一轮且不命中旧缓存）、编辑重提（历史保留 + 新轮）
- 后端：`test_qa_stream_regen` 验证 `regen=True` 跳过缓存

## 范围（YAGNI）

- **不做**：服务端主动 cancel 信号广播（客户端断连已足够）
- **不做**：重新生成的多版本 diff 对比视图（后续「答案对比」feature 再做）
- **不做**：编辑 user 消息后改写历史（明确选了"追加新轮"，不改历史）
