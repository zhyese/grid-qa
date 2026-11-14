# Agentic RAG Implementation Plan

> 用户已在链路说明后要求“开始编码实现”。复用既有普通/深度开关，普通模式保持不变。

**Goal:** 深度问答通过只读工具收集可追溯证据，有限补查，返回引用、缺口和明确停止原因。
**Architecture:** 新增 EvidenceReader 和专用编排服务，避免改动供运维/工单共用的通用 Agent 引擎。qa_service 在 agentMode 且 AGENTIC_RETRIEVAL_ENABLE 下接入。复用现有 SSE tool_step/meta/token/done。
**Tech Stack:** FastAPI async、SQLAlchemy、Vue、pytest、Node test、现有 provider factory。
**Spec:** 本聊天上一轮经用户确认的主问答链路。

## Global Constraints

- 全部在 E:/Projects/grid-ops-rag-qa；保留既有未提交修改，数据库/模型/索引不迁移。
- 只读工具白名单固定，禁止工单/知识发布/控制设备；租户和角色部门来自服务端，不接受模型覆盖。
- 最多3轮、6工具调用、60秒总预算；工具15秒、模型20秒上限（均不能超过剩余总预算）。
- 证据上限12条；每条最多1800字符、原文补查最多3600字符；给模型的上下文有长度上限。
- 不将无来源答案标高置信；合法引用+数字支持仅为结构校验，不声称证明全部语义。
- 深度首版禁用共享答案缓存及长期记忆读写，普通模式不变。所有失败返回有限结论/明确原因，不转入无门禁的旧Agent。
- 所有特性默认关，部署时仅打开已有AGENTIC_RETRIEVAL_ENABLE。

## Interfaces

### 证据工具（agentic_evidence_service.py）

`EvidenceReader(db, *, tenant: str, role: str, dept: str)`
- `async execute(tool: str, args: dict) -> dict`: `{evidence: [...], note: str}`。
- `async revalidate(evidence: list[dict]) -> list[dict]`: 剔除权限/治理/来源内容变化的证据，保留 evidenceId 等编排字段。
- `TOOL_SCHEMAS`: OpenAI函数工具schemas，固定 `search_documents(query, doc_type?)`、`search_cases(query)`、`open_document(doc_id, chunk_idx)`、`query_graph(entity)`、`query_telemetry(device_id, metric?, hours?)`。
- 证据字段：`key`(来源唯一标识)、`kind`(document/graph/telemetry)、`text`、`docId/docName/chunkId/chunkIdx/pageNum/sectionPath/version`（文档/图谱）、`sourceHash`；遥测带deviceId/start/end/unit。不得丢失tenant/ACL边界。

### 编排（agentic_rag_service.py，主代理）

`async run_agentic_rag(db, query, model_type=None, *, tenant='default', role='', dept='', history=None, on_step=None, reader=None, provider=None) -> dict`
- 模型选择工具；后端执行白名单并累计证据，评估当前证据包而非偷偷重新检索。
- 每轮评估明确缺口/冲突/澄清问题；无新增连续2轮停止，预算耗尽停止。
- 最终返回 `answer,sources,evidencePack,status,stopReason,missingEvidence,conflicts,rounds,toolCalls,toolsUsed,confidence,degraded,verification`。
- `status`: completed/partial/insufficient/clarification；stopReason为机器枚举，不曝光异常密钥。
- 引用使用 `[1]`等编号，对应sources顺序；未知引用或无证据数值剔除并降级，不能静默标绿。

### SSE / 前端

- `tool_step.step`: 兼容iter/tool/args/result/error，加phase；显示简短执行摘要而非模型隐藏推理。
- `meta`: sources、conversationId、agentMode=true、agenticRag=true。
- `done`: 上述status等置于`agenticResult: {status,stopReason,missingEvidence,conflicts,rounds,toolCalls,verification}`；顶层sources/evidencePack/agenticRag、confidence、degraded、toolsUsed、iterations、annotatedAnswer。
- 普通done事件/重复空done不能清除已收到结果。深度模式强制SSE，不能落到不支持agentMode的WS路径。

## Tasks

- [x] 证据工具红绿：租户/部门/角色、治理撤回、原文定位、图谱来源、遥测空结果、内容变更复核。
- [x] 编排红绿：工具白名单、3轮6次、超时取消、无新增、空包拒答、引用/数字过滤、真实缺口。
- [x] 接线红绿：agentMode路由、普通不变、缓存隔离、会话/历史、SSE取消清理。
- [x] 前端红绿：结果状态、缺口、来源、重复done、强制SSE。
- [x] 全套测试、前端构建、独立只读review、E盘仅重建backend/frontend、真实SSE冒烟。

## Review Focus

- 模型伪造工具名/租户/来源ID；未授权读取与引用绕过。
- 进行中权限/文档/治理变化；过期来源不能继续输出。
- 模型不调工具、评估格式错、工具超时、用户中止，均有确定停止行为。
- 不支持的数字/引用不能通过UI成为已验证高置信结论。
- 普通RAG与旧其他persona不能被本功能改变；不混用普通缓存。

## 实施记录与验收（2026-11-14）

- 实现模块：agentic_evidence_service、agentic_rag_service、agentic_stream_service；保留旧通用Agent供其他persona使用。
- 独立审查发现单位大小写/前导小数/Unicode负号漏洞，已补红绿回归并统一完整数字解析；不折叠SI前缀大小写。校验范围仅引用ID和原文数字/单位对应，不声称已证明全部语义。
- 另发现历史丢证据：新增agentic_answers独立JSON快照表（启动create_all建表，不ALTER messages），与user/assistant消息原子提交；历史按owner/tenant和当前来源权限复核，保留原citationIndex。旧遥测不冒充实时结果。
- 历史入口增加owner/软删/tenant边界。新深度多轮读取同一安全历史服务；来源变化时隐藏旧结论与过程自由文本。
- 新消息时间采用DB NOW()，避免容器UTC与MySQL本地时区相差8小时导致乱序。
- 深度问答不读写普通答案缓存，也不使用长期记忆；memoryRead/memoryWrite请求会返回明确不支持提示。普通问答默认路径未改。
- 全套：1224 passed, 1 skipped, 37 deselected；5项既有依赖/pytest警告。日志reports/agentic-suite-final-20261114.log。
- 前端node:test 26通过；Vite生产构建成功；golden格式校验109条通过。未运行ruff（现有容器未安装）。
- 浏览器真实问答：电流互感器原理问题，1轮2次工具、11来源，编号引用可打开原文；显示检索降级与语义未全面核验，而非默认高置信或零幻觉率。
- 切换会话重开：11来源、阶段、预算及停止原因保持；明确历史快照提示。截图reports/agentic-evidence-ui-20261114.png。
- 实际无遥测问题：1轮1次工具，status=clarification、sources=0，明确要求补设备/时段，不用规程限值伪造实际油温；未缓存。
- 部署前快照data/backups/pre_agentic_20261114.sql；仅更新E盘backend/frontend。文档仍164、三元组11176；没有迁移BGE、清理测试语料或执行工单/设备操作。

## 尚存边界（不由本功能伪装解决）

云向量/重排账户异常与测试语料混入仍会影响召回，当前只透出降级，未承诺质量门槛达标。全量BGE影子集合仍未切换。新Agentic功能是受控检索/证据编排，不替代现场专业审核或形成自动控制闭环。
