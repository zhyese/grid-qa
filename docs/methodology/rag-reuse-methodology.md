# RAG 能力资产强制复用方法论（操作手册）

> 定位：与 `engineering-methodology.md`（论文体，讲"为什么"）互补，本文是**操作手册体，讲"怎么强制搬"**。
> 事实源：2026-09-10 全仓四路并行审计（装配配置 / 服务耦合分档 / 工程化交付 / 前端资产），所有论断带 file:line 坐标。
> 用法：任何新 RAG 项目开工前，本文是唯一复用入口——**能力不从零写，从本仓搬**。

---

## 目录

- 第 0 章 资产全景与硬数据
- 第 1 章 复用决策模型（A/B/C 三档）
- 第 2 章 换行业机械工作量（26 个收口点）
- 第 3 章 强制复用机制（四个抓手）
- 第 4 章 组装流水线（新项目 10 步起盘）
- 第 5 章 工程不变量（14 条铁律，一条不能丢）
- 第 6 章 移植前必修清单（8 个已知缺陷）
- 第 7 章 扩展点契约（5 个 API 形状）
- 第 8 章 验证闭环（复用后的自证门禁）
- 第 9 章 资产索引（分档总表）

---

## 第 0 章 资产全景与硬数据

| 维度 | 数据 | 证据 |
|---|---|---|
| 后端模块 | services 77 + rag 12 + routing 3 + mcp 4 + tasks 4 + events 2 ≈ 102 文件（services/rag/core/providers 共 22,685 行） | `wc -l` 实测 |
| 配置体系 | config.py **232 字段**，其中 bool 开关 78（默认开 33 / 默认关 45），**默认关特性开关 44 个**（完整清单见 §3 抓手二） | `config.py:12-378` |
| 数据模型 | **33 张表** / 25 模型文件 / 8 个 Alembic 迁移 | `models/` grep `__tablename__` |
| 路由 | 16 个路由文件，12 个挂 `require_perm`，4 个走特殊鉴权（HMAC / token+IP 白名单 / require_admin） | `routers/` 抽查 |
| 装配 | lifespan 启动 14 步 + 后台 loop 11 个 + 中间件 3 层 + shutdown 4 步 | `main.py:63-320` |
| 测试 | **102 文件 / ~897 用例**，integration 标记 10 文件（`pytestmark` 模块级） | `tests/` |
| 交付 | docker compose **19 服务**、scripts 16 个、`.env.example` 126 字段 31 段 | `docker-compose.yml` / `scripts/` |
| 前端 | 29 文件 9,826 行，视图 **12 通用 : 7 领域** | `frontend/src` |

**分档硬结论**（第 1 章）：A 档零耦合 57 文件、B 档收口可复用 37 文件、C 档真领域仅 8 文件。**≈92% 的后端文件可复用**，真领域层薄到一删就走。

---

## 第 1 章 复用决策模型（A/B/C 三档）

### 1.1 判定规则（可自动化）

对每个文件执行领域词扫描：`变电|配电|输电|电网|电力|告警|两票|CIM|母线|间隔|变压器|开关柜|SCADA|调度`，按命中位置分档：

- **A 档** = 零命中（或仅注释装饰性命中）→ 原样搬
- **B 档** = 命中全部落在**模块级常量 / data/*.json / prompt 字符串**内，业务逻辑零分支 → 搬 + 换收口点
- **C 档** = 领域逻辑写进业务分支 → 不搬，参考实现手法

两条审计纪律（实测踩过）：
1. `CIM` 会命中 `DECIMAL`（SQL 关键字子串）——扫描脚本须排除假阳性（曾误伤 `cost_tracker_service.py:120`）；
2. `调度`/`告警` 存在通用语义命中（"路由调度"、"降级告警"），须人工复核或用更精确词表。

### 1.2 B 档总规律（本次审计最重要的发现）

> **全库所有 LLM prompt 均以"你是电网…"开头（grep `你是(?!电网)` 零命中），无一处领域分支散落在业务逻辑里。**
> 领域知识 100% 收口在：1 个 prompt 模板文件 + 5 个词表 JSON + ~20 个模块级 prompt 常量。
> **换行业 = 换这 26 个收口点，主干拓扑零改动。**

### 1.3 分档总表（后端）

| 档 | 数量 | 构成 |
|---|---|---|
| **A** | **57** | services 41（auth/backup/bm25/cache_*/config_service/document/embedding/rerank/feedback*/task_*/workflow_service/quality_event*/governance_propagate 等）+ rag 7（rrf/mmr/crag/citation 三件套/semantic_cache/judge）+ routing 1（routing_service）+ mcp 2（client/registry）+ tasks 4（registry/worker/lifecycle/builtin）+ events 2（registry/worker） |
| **B** | **37** | services 30（qa_service/retrieval_service/agent_*/kg_*/knowledge_*/query_*/rewrite_*/chunk/hyde/multi_query/self_rag/standalone/term/workflow_engine 等）+ rag 4（crag_v2/prompt_templates/raptor + citation 细节）+ routing 2（query_classifier/config）+ mcp 1（server） |
| **C** | **8** | services 6（alert_disposal/cim_service/domain_service/realtime_event/ticket_audit/twin_service）+ rag 1（cim_parser）+ mcp 1（mock_scada_server） |

**C 档的挂接方式**：整个领域价值层只经两个注入点与 A 档基建衔接——`agent_tools` 的 Tool 注册表、`mcp/server._dispatch_tool` 的 handler 分发。**删除 C 档全层，A 档基建照常运行。** 这是"平台核心零领域知识"的结构性保证。

### 1.4 前端分档

| 档 | 文件 |
|---|---|
| A（零改级） | `QaTraceChart.vue`（259 行，契约逐字段对齐后端 `to_dict()`）、`AgentTrace.vue`、`utils/perm.js`、`AppLayout/Login/Profile`、`api/request.js`、`stores/auth.js` |
| A（骨架级，换数据源即用） | `Chat.vue`（SSE 流式+反馈+收藏）、`Documents.vue`（上传→解析→向量化）、`Admin.vue`（Tab 骨架）、`Dashboard/RetrievalDebug/TraceDiagnosis/QualityEvents/KnowledgeGovernance/KnowledgeEvolution` |
| C（领域） | `DigitalTwin/KgGraph/KgGraph3D/Diagnose/OperationsCenter/TicketLifecycle/FaultPrediction` + `three/` 两文件 |

---

## 第 2 章 换行业机械工作量（26 个收口点）

### 2.1 词表/词典类（5 个 JSON，换文件即换行业）

| 收口点 | 坐标 | 内容 |
|---|---|---|
| `data/grid_terms.json` | `term_service.py:8-10,41` | 术语归一总收口（别名/错别字→标准术语），chunk/检索/qa 全链引用 |
| `data/rewrite_fewshot.json` | `rewrite_strategy.py:21` | query 改写 few-shot 样例 |
| `data/ticket_rules.json` | `ticket_audit_service.py:8-10` | 工单审核规则（C 棡配套，可随层删除） |
| `data/semantic_rules.json` | `semantic_rule_service.py` | 维度→关键词→标签打标规则 |
| `data/golden_qa.json` | `backend/data/` | 评测种子集（新行业首批必换） |

### 2.2 代码内常量类（~20 处，全部模块级）

| 类别 | 坐标 |
|---|---|
| QA 主 prompt（唯一收口文件，且有管理端热覆盖） | `rag/prompt_templates.py:3-19` + `get_system_prompt():22-29` |
| 路由分类特征 | `routing/query_classifier.py:12-53`（_CORE_TERMS/_STANDARD_RE/_NUMERIC_RE/_FAULT_WORDS 四常量） |
| KG 关系白名单 | `kg_normalize.py:16-30` |
| 治理词表 | `knowledge_governance_service.py:95-104` |
| 回流分类规则 | `knowledge_backbone.py:22-32` |
| 改写缩写集 | `rewrite_strategy.py:16` |
| 插件高危词 | `plugin_registry.py:71,80`（插件机制本身零耦合） |
| persona prompts ×6 | `agent_personas.py:9,37,72,105,132,161` |
| 支线 prompt 常量 | hyde:17 / multi_query:20 / query_plan:17,30 / query_rewrite:17,45 / self_rag:10,18-19 / standalone_query:3 / conversation_summary:18 / kg_service:24-40 / debate:30-48 / knowledge_evolution:114 / crag_v2:74,87 / raptor:30 / workflow_engine:238 / multimodal:50-95 / agent_memory:52-55 |
| qa_service 内 3 处文案 | `qa_service.py:726`（注入拒答）/ `:1842-1843`（相关问题 prompt）/ `:736`（term 归一入口） |
| collection 名 | `clients/milvus_client.py:3-4`（grid_chunks / grid_chunks_bge） |
| Prometheus 指标前缀 | `core/metrics.py:7-103` 统一 `grid_*`（横切，换行业批量改名或加配置层） |

### 2.3 工作量结论

新行业落地 = **改 5 个 JSON + ~21 处模块级常量 + 删 C 档 8 文件与 7 个前端视图**。qa_service 本体 94KB 除 3 处文案外全部通用（星型 hub：静态依赖仅 5 service + 2 rag，其余 15+ 模块函数内懒加载，`qa_service.py:25-33`）。

---

## 第 3 章 强制复用机制（四个抓手）

> 底层逻辑：**复用靠约定必败，靠结构才成**。以下四个抓手把"应该复用"变成"不复用就过不了 CI"。

### 抓手一：模板仓库固化（一次性动作）

从本仓 fork 出 `rag-template`：
1. 删 C 档 8 文件 + 7 个领域视图 + `three/`；
2. 执行 §6 必修清单（8 项缺陷此时修，成本最低）；
3. 26 收口点替换为新行业首例（电力→医疗/法务/金融任选）作为模板默认；
4. golden_qa.json 留 3 条示例条目 + `validate_golden.py` 保持 CI 门禁。

之后所有新项目 `git clone rag-template` 起盘，**不从零建仓**。

### 抓手二：领域收口唯一化（domain/ 包）

把 §2 的 26 收口点物理归位到 `backend/app/domain/` 包：

```
backend/app/domain/
├── terms.json          # 原 grid_terms.json
├── fewshot.json        # 原 rewrite_fewshot.json
├── rules.json          # 原 semantic_rules.json
├── prompts.py          # ~20 个模块级 prompt 常量统一 import（含 prompt_templates 的行业段）
└── classifier.py       # query_classifier 四常量 + routing/config.py 词表路径
```

B 档文件 import 方向反转：**业务文件不得自持领域常量，只能从 domain/ 包读**。

### 抓手三：CI 三重门禁（`scripts/check_reuse_gates.py`，新增）

| 门禁 | 规则 | fail 条件 |
|---|---|---|
| ① 领域词扫描 | 对 A 档 57 文件跑 §1.1 词表（排除 DECIMAL 假阳性） | A 档出现任何领域词 → 退出码 1 |
| ② 核心冻结 | 仓库根维护 `reuse-manifest.json`（A 档 57 文件 + rag 7 算法的 hash）；PR diff 触碰清单内文件且不在 `ALLOWED_CORE_EDITORS` 白名单 | 告警注释 + 非白名单作者 fail |
| ③ 配置对齐 | config.py 字段集 == .env.example 键集（本次审计发现 `SELF_RAG_ENABLE` 在 .env.example 重复两次，L114/L144） | 集合差非空 → fail |

与既有门禁叠加：validate_golden（CI 内）→ pytest not integration（CI 内）→ eval 三维（夜间）。**新项目的 CI 从第一天就有这五道闸。**

### 抓手四：装配序即文档

lifespan 的 14 步启动序列 + 11 个后台 loop（`main.py:63-267`）就是新项目的装配 checklist——顺序不可乱（例：task worker 必须在 provider/MCP/热配置就绪后才消费积压任务，`main.py:220-221` 注释明言）。新项目加后台任务 = 复制既有模式：`asyncio.create_task` 挂 `app.state` + shutdown 段 `getattr` 防 AttributeError 地 cancel（`main.py:276-310`）。

---

## 第 4 章 组装流水线（新项目 10 步起盘）

| 步 | 动作 | 验证点 |
|---|---|---|
| 1 | `clone rag-template`，改名，删旧 git 历史 | — |
| 2 | `.env` 填三必填 key（DEEPSEEK/DASHSCOPE/ARK），端口按 §5 铁律避冲突 | `docker compose config` 无错 |
| 3 | 写 `domain/` 包：5 JSON + prompts.py（新行业词表与 prompt） | 门禁③字段对齐过 |
| 4 | 选 embedding 策略：云 1024 维 / bge 512 维（**切 provider 或改维 → 必须重建 Milvus collection**，铁律 §5-8） | 启动日志 `ensure_collections` ok |
| 5 | golden_qa.json 换行业种子 ≥10 条（含 relevant_docs 分级） | `validate_golden.py` 退出码 0 |
| 6 | `docker compose up -d --build`（源码 bake 进镜像，铁律 §5-7） | `/health` 200，`/metrics` 有预注册序列 |
| 7 | `seed_demo.py` 改行业文档后建库 | 文档列表非空，chunk 向量化完成 |
| 8 | `eval_suite.py` 三维门禁 | 汇总报告 PASS（retrieval/生成/引用阈值见 §8） |
| 9 | 冒烟：登录 → 提问 → Chat 内嵌瀑布图 13 节点齐全 → Grafana 面板有数 | trace `bottleneck` 字段有值 |
| 10 | 首个 baseline commit + `reports/eval_suite_*.md` 归档 | 门禁②hash 清单入库 |

> 30 分钟起盘的前提是模板已固化（抓手一）。首次模板固化约 1-2 天（含 §6 必修），是一次性成本。

---

## 第 5 章 工程不变量（14 条铁律）

> 这些不是风格建议，是拿事故换的约束。搬代码时**逐条对照，一条不能丢**。

| # | 铁律 | 依据 |
|---|---|---|
| 1 | **降级而非崩溃**：所有外部依赖调用包 try/except，失败走 `obs.degraded()` 记 DEGRADED 指标，主链路绝不中断 | `core/obs.py:1-8`；lifespan 每个可选组件都这样包 |
| 2 | **特性开关默认关 = 现状行为**：44 个默认关开关，每家新能力 opt-in，前端零改动 | `config.py` 44 个 False 开关清单（§3 抓手二可查全） |
| 3 | **缓存 key 含版本串**：`citation_cache_version()` 把 citation 开关+治理代际 G+CRAG V3 拼 key；新增影响答案语义的开关必须加进版本串 | `config.py:393-404` |
| 4 | **trace 失败绝不影响主链路**：TraceCollector 每方法整体 try/except; pct 按"已打点耗时之和"算（防未打点稀释） | `core/qa_trace.py:132-133,178-182` |
| 5 | **异步落库走 fire-and-forget + 独立 AsyncSession**（bg-task 复用请求 session → 并发 500，有案底） | `models/qa_trace.py` 文件头注释 |
| 6 | **事件驱动指标必须预注册 0 值序列**，否则事件发生前在 /metrics 隐身 | `core/metrics.py:init_metric_series`，`main.py:149-153` |
| 7 | **源码 bake 进镜像，无 bind mount**：改源码/改 .env → 必须 `up -d --build` 重建容器 | `backend/Dockerfile:29` |
| 8 | **切 embedding provider / 改维度 → 重建 Milvus collection**（双向量空间不混：云 1024→grid_chunks，bge 512→grid_chunks_bge） | `clients/milvus_client.py:3-4` |
| 9 | **依赖钉子**：`pymilvus>=2.4,<2.5` + `setuptools<81`（pkg_resources）；OCR 用 `rapidocr-onnxruntime`（规避 paddle Win bug） | requirements |
| 10 | **容器内不设 HTTPS_PROXY**：宿主代理仅回环，容器经 host.docker.internal 连不上，设了则所有出站发往不可达代理 | compose 实测教训 |
| 11 | **bge 离线三件套**：`HF_HUB_OFFLINE=1` + 宿主 HF 缓存 ro 挂载 + 启动期预热（`main.py:111-117`，避免首问卡 ~80s） | `docker-compose.yml:218-226` |
| 12 | **BizError → HTTP 恒 200**，业务码进 body `{code,message,data}` | `core/response.py` |
| 13 | **路由薄、逻辑进 service**：routers 只做校验 + require_perm + 调 service + 写操作日志 | 16 路由文件实测形态 |
| 14 | **任务先落库再消费**：PersistentTask/DomainEvent Outbox，进程重启可恢复；worker 心跳续租 + stale 回收 | `tasks/worker.py:33-63` |

---

## 第 6 章 移植前必修清单（8 个已知缺陷）

> 审计发现的"文档说的和代码跑的不一样"。模板固化时（抓手一步骤 2）一次修完，别把缺口搬进新项目。

| # | 缺陷 | 证据 | 修法 |
|---|---|---|---|
| 1 | 前端权限词表漂移：视图检查的 `system:config/user:manage/feedback:manage/alert:manage/optimizer:manage/evidence:manage` 6 权限不在 `ROLE_PERMISSIONS` 矩阵，靠 admin 通配符糊住 | `perm.js:15-31` vs `Admin.vue:4-22` | 权限常量统一进一个词表文件，矩阵补全 |
| 2 | recall 门禁口径漂移：CI 注释说 92%，脚本默认 0.85 | `test.yml:26` vs `eval_retrieval.py:83` | 统一 0.92 或注释改 0.85，二选一 |
| 3 | `eval_generation.py` 只出控制台不写 md 报告（eval_suite 文档串声称有） | `eval_generation.py:88` | 补写 `reports/eval_generation_*.md` |
| 4 | ci.yml 与 test.yml 职责重叠（后者是超集） | 两 workflow 步骤对比 | 模板只保留 test.yml |
| 5 | compose 健康检查仅 4/19 服务，backend depends_on 无条件启动（靠 restart 自愈） | `docker-compose.yml:186-192` | 模板补 backend/frontend healthcheck + `condition: service_healthy` |
| 6 | `main.py:179` getattr 兜底默认值永不生效（config 默认 True） | `main.py:179` vs `config.py:115` | 删 getattr 直接读 settings |
| 7 | `sceneEnvironment.js` 自称零外部资源，实际 GLTFLoader 加载 Kenney glb | `sceneEnvironment.js:13,634-649` | C 档资产，若搬 3D 需连 `/models/` 目录 |
| 8 | `TraceCollector.record()` 与 `QaTrace` 落库无测试覆盖（曾因 record 漏实现引发全链 500） | codegraph blast radius | 模板固化时补两用例 |

---

## 第 7 章 扩展点契约（5 个 API 形状）

新项目加能力 = 在这 5 个注册点挂东西，**不改主干**：

| 扩展点 | 注册方式 | 关键约束 |
|---|---|---|
| **插件** | `plugin_registry.register(name, desc, hooks, enabled)`，HOOKS = `query_preprocess / retrieval_filter / answer_postprocess`（`plugin_registry.py:16`） | 单插件异常 degraded 吞掉不中断；run_hook 串行链式传值 |
| **Agent 工具** | `Tool` dataclass + `ToolRegistry.register`；handler 签名 `(db, model_type, **args)`（`agent_runtime.py:36-59`） | run 内强制角色权限 + tenant 剥离 + per-tool 异常隔离 + fire-and-forget 审计 |
| **Persona** | code 注册（`agent_personas.py`）或 DB 覆盖（`persona_store.py:20-28`，DB enabled 时 merge） | max_iter 默认 6；异常走 fallback 降级函数 |
| **工作流节点** | `workflow_engine` 写一个 executor 进 `_COND_EXECUTORS`（无 db）或 `_DB_EXECUTORS`（统一签名 `(db, params, run_input, ctx, user, upstream_key)`）+ validate 放行（`:288-298`） | 7 节点类型；Kahn 拓扑；单节点 120s / 整 run 240s 预算 |
| **后台任务/事件** | `@task_handler("type")` / `@subscribe_event("pattern.*")`；`TaskContext`/`EventContext` frozen dataclass 传租户边界 | enqueue/publish 均幂等（idempotency_key）；心跳续租；subscriber 名全局唯一 |

---

## 第 8 章 验证闭环（复用后的自证门禁）

复用是否成立，不靠声称，靠门禁说话——**五道闸随核心一起搬**：

| 层 | 门禁 | 阈值/口径 | 时机 |
|---|---|---|---|
| CI | `validate_golden.py` | golden 集格式合法（query/expect/category 非空，relevant_docs 1-3 级） | 每次 push |
| CI | `pytest -m "not integration"` | 全绿（模板基线 ~897 用例的大部分） | 每次 push |
| CI（新增） | `check_reuse_gates.py` 三查 | §3 抓手三 | 每次 PR |
| 夜间/本地 | `eval_retrieval.py` | recall ≥ 阈值（修完 §6-2 后统一口径）退出码 1 = FAIL | 数据/开关变更后 |
| 夜间/本地 | `eval_generation.py` | 平均 supported_ratio ≥ FAITHFULNESS_GATE(0.85) | LLM/prompt 变更后 |
| 夜间/本地 | `eval_citation.py` | 关联率 ≥ 0.8（--no-nli 为免云 key CI 档） | citation 变更后 |
| 运行时 | 每请求 trace 瀑布图 | `bottleneck` 有值、span 覆盖 13 节点、`X-Trace-ID` 响应头 | 冒烟即验 |
| 运行时 | `/metrics` + Grafana | COMPONENT_HEALTH 30s 刷新（后台探活，非仅 /health 触发）；DEGRADED 计数常驻 | 部署后 |

编排入口：`eval_suite.py` 一键三维 + 汇总报告落 `reports/eval_suite_<ts>.md` + 总门禁退出码。

---

## 第 9 章 资产索引（速查）

- **检索核心**：`retrieval_service.py`（混合检索编排）→ `rag/rrf.py`（融合）→ `rerank_service.py` → `rag/mmr.py`（去冗余）
- **可信核心**：`rag/crag.py` + `crag_v2.py`（分级自纠错）→ `rag/citation*.py`（角标+受控索引+四层校验）→ `rag/judge.py`（LLM-judge）
- **LLM 基建**：`providers/factory.py` + `llm_router.py`（L0 fallback 链 / L1 熔断 / L2 分档）
- **缓存**：Redis L1（`hotqa:`/`qa:`）→ `rag/semantic_cache.py` L1.5 → `cache_persist.py` L2 Write-Through → `cache_warmup.py` 预热
- **底座范式**：`tasks/`（持久任务）· `events/`（Outbox）· `plugin_registry` · `agent_runtime` · `workflow_engine` · `quality_event_bus`
- **可观测**：`core/qa_trace.py`（231 行，零耦合）→ `QaTraceChart.vue`（259 行）· `core/metrics.py` · `core/obs.py` · `otel_genai.py` → Langfuse
- **安全**：`core/permissions.py` + `dependencies.require_perm` + `utils/perm.js`（前后端同词表，修 §6-1 后真正对齐）
- **运维**：`backup_service`（纯 Python 三合一）· `log_archive_service` · `config_service`（rt_* 热配置）· `start.sh`
- **评测**：`scripts/eval_*.py` ×5 + `validate_golden.py` + golden_qa.json 闭环（低分 → 质量事件 → evidence_gap → 回流 golden）

---

## 附：与姊妹项目的一致性

通用 RAG 平台项目（2026 年从本仓空白重建）已验证同一命题：**平台核心零领域知识 + 行业差异收口 DomainAdapter + AST 护栏焊死**。其已知教训同样适用于本方法论的抓手三：rrf 归一化裂缝修完 CRAG 拒答才真生效——**搬 rrf.py 别只搬公式，归一化语义一起搬**。

*事实源版本：main @ f2fd613（2026-09-10 审计）。*
