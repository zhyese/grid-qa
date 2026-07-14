# N1-N4 新功能增量 PRD

> **版本:** v1.0 | **日期:** 2026-07-14 | **撰写人:** 产品经理 Alice（许清楚）
> **范围:** N1 Agent 长期记忆层 / N2 MCP 工具总线 / N3 数字孪生变电站 3D / N4 LLM 全链路可观测性
> **基准文档:** 《新功能增量调研报告.md》（含完整规格/接入点/工作量估算，本 PRD 不重复，仅聚焦范围确认/用户故事/排期/待确认）
> **代码审计依据:** `agent_runtime.py`(L177 注入点) / `agent_tools.py`(4 工具+ToolRegistry) / `KgGraph3D.vue`(Three.js 引擎) / `core/obs.py`(degraded+metrics) / `conversation_summary.py`(每6条会话内摘要) / `online_eval_service.py`(LLM-as-judge 三维采样) / `fault_prediction_service.py`(纯统计 riskScore)

---

## 一、范围确认（In-scope / Out-of-scope）

### N1 Agent 长期记忆层（P0）

| 维度 | 决策点 | 推荐方案 | 理由 |
|---|---|---|---|
| 记忆作用域 | 三层（用户级/设备级/组织级）都做？ | **本次只做用户级 + 设备级**，组织级暂不做 | 用户级（偏好/习惯）和设备级（该设备历史诊断结论）是运维直接体感最强、ROI 最高的两层；组织级涉及多租户权限模型与共享策略，复杂度高且本次无明确业务方对接，留作 P2 演进。代码上 `ctx` 已含 `username/tenant`，两层作用域用 `scope` 字段区分即可 |
| 抽取频率 | extract_facts 多久跑一次？ | **每轮对话结束后 fire-and-forget 异步触发**（不阻塞响应），但**对工具调用型长对话累积 ≥3 轮才触发** | 每轮抽取消耗 LLM 调用；对话内摘要已每 6 条压缩一次（`conversation_summary.py`）。记忆抽取与摘要正交：摘要管"会话内压缩"，抽取管"跨会话结构化事实"。异步触发不增加用户感知延迟。对极短问答（1-2 轮）跳过抽取省成本 |
| 遗忘/衰减策略 | 时间衰减 vs 容量上限 vs 结合？ | **两者结合：容量上限（单用户 500 条）+ 时间衰减（90 天未命中权重 ×0.5，180 天 ×0.2）** | 纯时间衰减会让长期有效的设备结论（如"1号主变曾发生过 SF6 泄漏"）被错误遗忘；纯容量上限会无差别淘汰。结合策略：召回时按 `权重×相关性` 排序，低分记忆在 consolidate 阶段被合并或淘汰。运维场景中"半年没再问的设备偏好"可降权但不删（保留可追溯） |
| 前端记忆管理 | 需要查看/编辑/删除界面？ | **本次做"只读查看 + 删除"，不做编辑** | 查看让用户对 Agent 记住什么有掌控感（信任问题）；删除是隐私/纠错刚需。编辑记忆会引入"人工污染记忆"的审计难题，且 extract_facts 已做消解，本次不做。复用 Admin.vue 加一个"记忆"Tab |

**In-scope（本次做）：**
- `agent_memory_service.py`：extract_facts / consolidate / recall / forget / decay 五函数
- 向量记忆复用 Milvus（新建 `memory_collection`）+ 图记忆复用 Neo4j（user→preference→entity）+ 热记忆复用 Redis
- `agent_runtime.py` L177 注入点：新增一条 system 消息注入召回记忆（零侵入）
- run_agent 结束后 fire-and-forget 触发 extract_facts + consolidate
- 前端 Admin.vue 新增"记忆"Tab（只读列表 + 删除）

**Out-of-scope（本次不做）：**
- 组织级记忆作用域（多租户共享策略）
- 记忆编辑功能（人工修改抽取结果）
- 记忆导入/导出
- 跨用户记忆联邦（隐私合规需单独评估）

---

### N2 MCP 工具总线（P1）

| 维度 | 决策点 | 推荐方案 | 理由 |
|---|---|---|---|
| 方向 | server（暴露能力）+ client（消费外部）都做？ | **server 和 client 都做，但 client 先做框架 + 示例 mock server** | server 方向是本系统成为"电网运维领域首个 MCP server"的生态先发点，价值明确且 `agent_tools.py` 的 ToolRegistry.schema 已是 OpenAI 格式，转换成本低。client 方向若配套接真实外部系统（SCADA/OMS/PMS）需各系统开放接口，本次周期内无法落实，先做框架+mock 验证链路，真实接入留后续迭代 |
| server 暴露范围 | 暴露哪些能力？ | **首批暴露 4 个工具 + 图谱查询 + 混合检索（共 6 个），建票(draft_ticket)带权限** | 4 个工具已封装好可直接转 MCP schema；`kg_service.graph_context` 和 `retrieval_service.mixed_search` 是本系统核心能力，暴露后外部 Agent 可消费。建票是写操作（生成操作票草案），按现有 `tool_permissions` 机制带 role=admin 限制 |
| client 真实接入 | 接真实外部系统？ | **不接，先做 mock MCP server 示例（如 mock_scada_server 提供遥测数据）** | 真实 SCADA/OMS 接口需电网方授权与联调，超出本次范围。mock server 用于验证 client 发现→注册→调用链路完整性，并为后续真实接入提供模板 |

**In-scope：**
- `backend/app/mcp/server.py`：6 个能力包装为 MCP tools/resources
- `backend/app/mcp/client.py`：从 registry 发现 server → 动态注册进 ToolRegistry → agent_runtime 无感调用
- 1 个 mock MCP server 示例（mock_scada，提供设备遥测查询）
- `providers/factory.py` 旁新增 `mcp_registry`，settings 增 `MCP_SERVERS` 配置项

**Out-of-scope：**
- 真实外部系统（SCADA/OMS/PMS/气象）接入
- MCP server 的 OAuth/鉴权完整体系（本次用简单 token，生产级鉴权后续）
- 外部工具调用的计费/限流治理

---

### N3 数字孪生变电站 3D（P1）

| 维度 | 决策点 | 推荐方案 | 理由 |
|---|---|---|---|
| 模型精度 | 真实 BIM/CAD（glTF/glb）vs 简化几何体（盒子+标签）？ | **本次用简化几何体（盒子+设备类型图标+标签），预留 glTF 加载接口** | 真实 BIM 模型获取需设计院提供 CAD/BIM 源文件并做格式转换，工作量大（调研报告估 ~13 人天）。简化几何体可在 ~8 人天内出 Demo，视觉上已能体现"设备空间布局+告警定位+故障传播链"核心价值，满足竞标/演示需求。预留 `loadModel(url)` 接口，后续替换 glTF 零改造 |
| 首批场景 | 户内站/户外站/配电房？ | **首批做 1 个典型户外站（110kV 变电站主变+间隔布局）** | 户外站设备布局直观、空间感强、演示效果好；110kV 是常见电压等级，案例丰富。户内站（开关柜密集）和配电房（空间小）布局差异大，本次先做 1 个标杆场景验证引擎，后续按场景模板扩展 |
| 实时数据源 | mock vs 接现有服务？ | **接现有 `fault_prediction_service`（riskScore）+ `alert_disposal_service`（告警状态），不 mock** | 这两个服务已有真实数据流（riskScore 按频次/趋势算，告警有完整状态机），直接对接即可让孪生场景呈现真实风险热力与告警定位，无需造 mock 数据。`ticket_lifecycle_service` 的在工单设备也接入（3D 中高亮在工设备） |

**In-scope：**
- `frontend/src/views/DigitalTwin.vue`：复用 KgGraph3D.vue 的 Three.js 引擎
- 简化几何体设备模型（盒子+图标+标签）+ 110kV 户外站布局模板
- 设备 riskScore 着色（复用 fault_prediction）+ 告警定位（复用 alert_disposal）+ 故障传播链高亮（复用 kg_service.graph_context 多跳路径）
- 点击设备弹出侧栏（实时参数+知识图谱+历史告警+操作票）
- `backend/app/services/twin_service.py`：设备-空间位置映射 + 状态聚合 + 告警→3D 定位推送（复用 #4 WebSocket）

**Out-of-scope：**
- 真实 BIM/CAD/glTF 模型导入（预留接口，本次不实现转换）
- 户内站/配电房等其他场景模板
- AR/VR 空间交互（具身智能方向，独立功能）
- 设备实时遥测数据流（SCADA 对接，依赖 N2 client 真实接入）
- 巡检机器人路径规划可视化

---

### N4 LLM 全链路可观测性（P1）

| 维度 | 决策点 | 推荐方案 | 理由 |
|---|---|---|---|
| 后端方案 | Langfuse 自托管 vs 仅 OTel SDK + 现有 Grafana？ | **Langfuse 自托管（Docker Compose 一键部署）+ OTel GenAI SDK 采集** | 仅 OTel SDK + Grafana 能看指标但**缺 trace 树可视化**，多 Agent 的 handoff 调试需要图谱式 trace（业界共识）。Langfuse MIT 协议、Docker Compose 部署与现有栈一致、原生接受 OTel span、自带 trace 树 UI。Grafana 继续做指标看板，Langfuse 做 trace 下钻，分工清晰。多一个服务但部署成本极低 |
| trace 覆盖 | 仅 LLM 调用 vs 全链路？ | **全链路：query_rewrite→retrieve→rerank→llm→judge**，每段一个 span，同一 trace_id 贯穿 | 仅 LLM 调用看不出"是检索召回差还是 LLM 幻觉"——这是当前 bug 最常藏的点。全链路 span 才能定位质量瓶颈在哪个环节。`RetrievalDebug.vue` 已有散点 trace，本次标准化为 OTel span 后前端升级为统一 trace 树 |
| 漂移检测 | 先采集+看板 vs 连告警一起做？ | **本次做"指标采集 + Grafana 看板"，告警规则配置好但默认关闭** | 采集+看板是观测基础，必须做。告警（如 faithfulness 周环比降 >10%）规则配好但不默认开启——避免开发期噪声告警干扰，待基线稳定后由运维开启。`online_eval_service` 已有三维评分数据，接入 OTel span attribute 即可 |

**In-scope：**
- `backend/app/core/otel_genai.py`：OTel GenAI 语义约定包装 LLM/retrieval/agent 调用
- 全链路 span：query_rewrite→retrieve→rerank→llm→judge，trace_id 贯穿
- 整合现有零件：`obs.degraded()`→span event / `online_eval_service`→span attribute / `cost_tracker`→span metric / `rag/judge`→trace 评分
- Langfuse 自托管（docker-compose 新增服务）+ Grafana LLM 质量面板（P95延迟/错误率/幻觉率/token成本趋势）
- `RetrievalDebug.vue` 升级为统一 trace 树可视化
- 漂移检测规则配置（默认关闭）

**Out-of-scope：**
- 告警主动推送（本次只配规则不开启）
- 用户反馈与 trace 的自动关联分析（feedback→trace 反向溯源）
- 多模型 A/B 实验的 trace 对比（属 #11 A/B 测试范围）
- 前端性能监控（属 #12 范围）

---

## 二、用户故事

### N1 Agent 长期记忆层

| # | 用户故事 |
|---|---|
| N1-US1 | 作为**变电运维工程师**，我希望 Agent 能记住"我负责的变电站列表和常问的设备类型"，这样我每次提问不用重复说明"我是某某站的、关注的是 1 号主变"，直接得到贴合我工作场景的答案。 |
| N1-US2 | 作为**运维工程师**，我希望 Agent 能记住"上次诊断 1 号主变油温高时已排除了冷却器故障、待确认是否负载过高"，这样我隔天继续追问时它能接续上次的诊断上下文，而不是从头问一遍。 |
| N1-US3 | 作为**运维班长**，我希望能查看 Agent 记住了哪些关于我和我班组的事实，并能删除不准确的记忆，这样我对 Agent 的"记忆"有掌控感，不会担心它记住错误信息误导后续判断。 |

### N2 MCP 工具总线

| # | 用户故事 |
|---|---|
| N2-US1 | 作为**系统架构师**，我希望本系统的检索/图谱/建票能力能以 MCP 标准暴露，这样外部 Agent（如公司 AI 中台、第三方运维平台）能即插即用调用本系统能力，无需逐个写对接代码。 |
| N2-US2 | 作为**运维工程师**，我希望 Agent 在回答时能自动调用外部工具（如查实时气象、查 SCADA 遥测），这样遇到"雷雨天气下线路跳闸"这类问题时能结合实时数据给出更准诊断，而不用我手动去多个系统查。 |
| N2-US3 | 作为**平台运营方**，我希望新增外部工具只需在配置里加一个 MCP server 地址，这样工具扩展成本从"写 Python handler+schema+注册"降为"改配置"，运维工具生态可持续扩张。 |

### N3 数字孪生变电站 3D

| # | 用户故事 |
|---|---|
| N3-US1 | 作为**变电站值班员**，我希望在 3D 场景中看到全站设备的空间布局和实时运行状态（正常/告警/风险），这样我一眼就能掌握全站健康度，告警发生时能立刻知道是哪台设备、在哪个位置。 |
| N3-US2 | 作为**运维工程师**，我希望在 3D 场景中看到故障的影响传播链（如主变故障→哪些出线受影响→哪些用户停电），这样我评估故障影响范围时不用查图纸，在 3D 上直观看到传播路径。 |
| N3-US3 | 作为**演示/竞标场景**，我希望 3D 孪生场景有视觉冲击力（设备着色+告警闪烁+传播链动画），这样在客户演示和领导汇报时能直观展现系统智能化水平。 |

### N4 LLM 全链路可观测性

| # | 用户故事 |
|---|---|
| N4-US1 | 作为**系统运维工程师**，我希望看到每次问答的全链路 trace（从 query 改写到最终答案），这样用户反馈"答得不好"时我能快速定位是检索召回差、rerank 失效、还是 LLM 幻觉，而不是翻散落的日志拼凑。 |
| N4-US2 | 作为**算法/质量负责人**，我希望看到 faithfulness/relevance/completeness 的趋势看板和漂移检测，这样模型升级或 prompt 调整后能及时发现质量回退，而不是等用户投诉才知道。 |
| N4-US3 | 作为**平台运营方**，我希望看到 token 成本与质量的关系看板，这样我能判断"贵的模型是否真的带来质量提升"，做成本-质量权衡决策。 |

---

## 三、优先级与实现顺序建议

### 依赖关系分析

| 关系 | 说明 |
|---|---|
| N1 → N2 | **弱前置**：N2 的 client 方向调用外部工具时，有记忆的 Agent 能更好决策"何时调哪个工具"。但 N2 server 方向（暴露能力）不依赖 N1。两者可并行，N1 先行 1 周即可 |
| N1 → N3 | **无依赖**：N3 是前端可视化，N1 是后端记忆，正交 |
| N4 → 其他 | **建议最先做**：N4 是观测基础设施，先做完能让 N1/N2/N3 的开发质量可被 trace 追踪，bug 可定位。N4 改造区域（core/providers 层）与 N1（services/agent_memory）、N2（mcp/agent_tools）、N3（frontend+twin_service）不重叠，不阻塞 |
| N2 ↔ N3 | **无依赖**：N2 后端工具协议，N3 前端 3D，可并行 |

### 建议排布（总周期 ~6 周）

```
Week 1     │ N4 可观测性先行（core/otel_genai.py + Langfuse 部署）
           │ → 完成后 N1/N2/N3 开发即可被 trace 覆盖
           │
Week 2-3   │ N1 Agent 记忆层（P0，~10 人天）
           │   └ 注入点改造 + extract/consolidate/recall + 前端记忆Tab
           │ N2 MCP server 方向并行启动（~5 人天，暴露 6 能力）
           │   └ 与 N1 改造区域不同（mcp/ vs services/agent_memory），可并行
           │
Week 4-5   │ N2 MCP client 方向（~4 人天，框架+mock server）
           │ N3 数字孪生（~8 人天，简化几何体+110kV 户外站）
           │   └ 前端工作，与 N2 client 后端并行
           │
Week 6     │ 集成联调 + N4 trace 验证全链路 + 演示打磨
```

**排期要点：**
1. **N4 最先做**（1 周）：观测先行，后续三个功能的开发质量可追踪，降低集成期 debug 成本
2. **N1 紧随其后**（2 周）：P0 优先级，且是 Agent 智能化的基础设施
3. **N2 server 与 N1 并行**：不同代码区域，server 方向价值明确先落地
4. **N3 放最后**（2 周）：视觉演示型功能，放最后便于集中打磨演示效果，且不阻塞其他后端功能
5. **N2 client 在 N1 之后**：client 调用外部工具时 Agent 决策质量受益于 N1 记忆

---

## 四、待确认问题

| # | 问题 | 影响范围 | 建议 | 需谁拍板 |
|---|---|---|---|---|
| Q1 | **N1 记忆抽取用哪个 LLM？** 复用现有 DeepSeek/Qwen/Doubao 之一，还是引入轻量小模型（降成本）？extract_facts 是高频调用，成本敏感 | N1 成本、provider/factory.py 扩展 | 建议复用现有 DeepSeek（最便宜档），prompt 极简化（只抽原子事实）。若成本仍高再评估端侧小模型 | Z + 架构师 |
| Q2 | **N1 记忆的隐私合规边界？** 记忆存储用户对话中抽取的事实，是否需用户明示同意？删除后是否需审计留痕？ | N1 合规设计、前端记忆Tab | 建议首次启用记忆时弹窗告知+同意，删除走软删除（保留审计日志 30 天）。需法务确认 | Z + 法务 |
| Q3 | **N2 MCP server 对外暴露的鉴权方案？** 本次用简单 token，但对外暴露检索/图谱/建票能力，安全需明确 | N2 server 安全、是否可对外 | 建议本次 token 鉴权 + IP 白名单，仅内网/可信 Agent 调用。对外公网暴露需配套 OAuth，留后续 | Z + 安全 |
| Q4 | **N3 是否有近期竞标/演示节点？** 若有则 N3 优先级提到 P0 并压缩周期 | N3 排期、模型精度取舍 | 若 4 周内有演示，N3 提前到 Week2-3 与 N1 并行；若无则维持 Week4-5 | Z |
| Q5 | **N4 Langfuse 自托管的运维归属？** 新增一个服务需人维护（升级/备份/告警接入），谁负责？ | N4 长期运维 | 建议纳入现有 Docker Compose 统一管理，运维归平台组。需确认是否有专人 | Z + 运维 |
| Q6 | **N4 trace 采样率？** 全量 trace 成本高（每次问答多个 span），采样多少？ | N4 成本/可观测性 completeness | 建议开发期 100%（快速定位），上线后降至 10%+异常必采。`online_eval_service` 已有采样模式可复用 | 架构师 |
| Q7 | **N3 设备-空间位置映射数据从哪来？** 简化几何体也需设备坐标，是手工配置还是从现有图谱推断？ | N3 数据准备、twin_service 设计 | 建议首批 110kV 站手工配置坐标模板（~30 台设备），后续做配置界面。从图谱推断坐标不可靠 | 架构师 |

---

*本 PRD 聚焦范围确认与决策建议，技术实现细节见《新功能增量调研报告.md》。待 Q1-Q7 确认后可进入架构设计阶段。*
