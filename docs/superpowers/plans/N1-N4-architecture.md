# N1-N4 系统架构设计 + 任务分解

> **版本:** v1.0 | **日期:** 2026-07-14 | **架构师:** Bob（高见远）
> **输入:** 《N1-N4-incremental-PRD.md》(产品经理 Alice) + 《新功能增量调研报告.md》+ 全量代码审计
> **范围:** N1 Agent 长期记忆层 / N2 MCP 工具总线 / N3 数字孪生变电站 3D / N4 LLM 全链路可观测性

---

## 一、实现方案 + 框架选型

### 1.1 N4 LLM 全链路可观测性（P1，Week1 先行）

**核心思路：** 用 OpenTelemetry GenAI 语义约定把现有散点零件（`obs.degraded()` / `online_eval_service` / `cost_tracker` / `rag.judge` / `debug_search trace`）统一为标准化 span 体系，Langfuse 自托管做 trace 树可视化，Grafana 做 LLM 质量指标看板。

**框架选型：**

| 包 | 版本 | 用途 |
|---|---|---|
| `opentelemetry-sdk` | ≥1.28,<2 | OTel 核心 SDK（TracerProvider/SpanExporter） |
| `opentelemetry-exporter-otlp` | ≥1.28,<2 | OTLP HTTP 导出器（发到 Langfuse） |
| `opentelemetry-instrumentation` | ≥0.49,<1 | 自动仪表化基础（可选，手动包 span 更可控） |

> **不引入** `opentelemetry-genai` 独立包（该包尚处 alpha），改用 `opentelemetry-sdk` 原生 span + 手写 GenAI 语义约定 attribute（`gen_ai.*`），与 Langfuse 原生兼容。

**集成方式：零侵入注入 + 最小化改造**

- 新增 `core/otel_genai.py`：提供 `trace_span()` 上下文管理器装饰器 + `get_trace_id()` + `init_otel()`
- `providers/base.py`：`LLMProvider` 不改接口，在子类 `chat()`/`chat_with_tools()` 内包 span（3 个子类各加 ~10 行）
- `retrieval_service.mixed_search()`：入口出口包 `retrieve` span（+5 行）
- `query_rewrite.rewrite_query_v2()`：包 `query_rewrite` span（+3 行）
- `rerank_service`：包 `rerank` span（+3 行）
- `agent_runtime.run_agent()`：包 `agent` span（+5 行）
- `obs.degraded()`：新增一行 `span.add_event()` 把降级事件挂到当前 span
- `online_eval_service.eval_quality()`：评分结果作为 span attribute 附加
- `cost_tracker_service.record_token_usage()`：token/cost 作为 span metric attribute
- trace_id 用 `contextvars.ContextVar` 在请求生命周期内贯穿（不依赖 HTTP header，FastAPI async 友好）

**采样策略（Q6 确认）：**

```python
# core/otel_genai.py 采样逻辑
# 开发期：OTEL_SAMPLE_RATE=1.0（100% 采样，快速定位 bug）
# 上线后：OTEL_SAMPLE_RATE=0.1（10% 随机采样）
# 异常必采：任何 span 的 status=ERROR 或 faithfulness < FAITHFULNESS_GATE(0.85) → 强制导出
def _should_export(span) -> bool:
    if span.status.is_ok and _sample_rate < 1.0:
        return random.random() < _sample_rate
    return True  # 异常 span 永远导出
```

---

### 1.2 N1 Agent 长期记忆层（P0，Week2-3）

**核心思路：** 新增 `agent_memory_service.py`，提供 extract_facts / consolidate / recall / forget / decay 五函数。存储三层复用：Milvus（向量记忆）+ Neo4j（图记忆 user→preference→entity）+ Redis（热记忆）。注入点在 `agent_runtime.py` L177 messages 构造处，零侵入新增一条 system 消息。

**框架选型：** 无新框架，全复用现有基础设施。

| 组件 | 复用方式 |
|---|---|
| Milvus | 新建 `memory_collection`（dim=EMBEDDING_DIM，HNSW+COSINE，同 milvus_client.py 模式） |
| Neo4j | 新增 `(User)-[:PREFERS]->(Entity)` / `(User)-[:DIAGNOSED]->(Entity)` 关系，复用 neo4j_client._get() |
| Redis | `memory:hot:{user_id}` ZSet（score=last_hit_ts，热记忆秒级召回） |
| LLM | 复用 `get_llm_provider("deepseek")`（最便宜档，extract_facts prompt 极简化） |
| MySQL | 新增 `agent_memory` 表（审计 + 软删除 + 容量管理） |

**集成方式：**

```
agent_runtime.py run_agent():
  L177: messages 构造处新增
    recall_text = await agent_memory.recall(user_msg, ctx.get("username"), scope="user")
    messages = [
        {"role": "system", "content": persona.system_prompt},
        {"role": "system", "content": recall_text or ""},  # ← 新增（零侵入：空字符串=无记忆）
        {"role": "user", "content": user_msg},
    ]
  L232 return 前：
    if ctx and ctx.get("username"):
        asyncio.create_task(agent_memory.extract_and_consolidate(
            user_msg, answer, ctx["username"], model_type))  # fire-and-forget
```

**抽取频率（PRD 确认）：** 每轮对话结束后 fire-and-forget 异步触发；对工具调用型长对话累积 ≥3 轮才触发。

**遗忘/衰减策略（PRD 确认）：** 容量上限（单用户 500 条）+ 时间衰减（90 天未命中 weight×0.5，180 天×0.2），由 `decay()` 定时任务执行。

---

### 1.3 N3 数字孪生变电站 3D（P1，Week2-3 并行）

**核心思路：** 复用 `KgGraph3D.vue` 的 Three.js 引擎（Scene/Camera/Renderer/Light/raycaster/动画循环），新建 `DigitalTwin.vue` 渲染简化几何体（盒子+设备类型图标+标签）。后端 `twin_service.py` 聚合设备状态（riskScore + 告警状态 + 在工单），通过 WebSocket（复用 `ws_manager`）推送告警定位。

**框架选型：**

| 包 | 版本 | 用途 |
|---|---|---|
| `three` | ^0.128.0（已有） | Three.js 3D 引擎，零新增依赖 |

> 无新 npm 包。Three.js 已在 package.json 中。

**集成方式：**

- 前端 `DigitalTwin.vue`：从 `KgGraph3D.vue` 提取 Three.js 初始化逻辑（Scene/Camera/Renderer/Light/raycaster/全屏），改为加载设备布局数据而非知识图谱节点
- 设备模型：`BoxGeometry` + `CanvasTexture`（设备类型图标）+ `Sprite`（标签），按坐标模板布局
- 着色：riskScore → HSL 色带（绿→黄→红），复用 `fault_prediction_service.predict()` 的 items
- 告警定位：WebSocket 订阅 `/ws/twin`，收到告警 → 相机飞到设备坐标 → 设备闪烁
- 故障传播链：点击设备 → 调 `kg_service.get_paths(entity, depth=3)` → 3D 高亮传播路径
- 设备详情侧栏：聚合 `alert_disposal_service` + `kg_service.graph_context` + `ticket_lifecycle_service`

**设备-空间坐标来源（Q7 确认）：** 首批 110kV 户外站手工配置坐标模板（~30 台设备），JSON 格式：

```json
{
  "stationId": "110kV-demo",
  "stationName": "110kV示范变电站",
  "areas": [
    {"id": "main_transformer_area", "name": "主变区", "position": [0, 0, 0], "size": [12, 0, 8]}
  ],
  "devices": [
    {
      "deviceId": "T1_main_transformer",
      "name": "1号主变压器",
      "type": "main_transformer",
      "area": "main_transformer_area",
      "position": [0, 1.5, 0],
      "size": [2.5, 3, 2.5],
      "kgEntity": "1号主变",
      "connections": ["T1_bushing", "T1_cooler", "T1_OLTC"]
    }
  ]
}
```

---

### 1.4 N2 MCP 工具总线（P1，Week4）

**核心思路：** 双向 — server 方向把 6 个能力暴露为 MCP tools（4 工具 + 图谱查询 + 混合检索），client 方向做框架+mock_scada 示例。用 MCP Python SDK 实现标准协议，schema 转换层把 OpenAI tool schema ↔ MCP tool schema 互转。

**框架选型：**

| 包 | 版本 | 用途 |
|---|---|---|
| `mcp` | ≥1.0,<2 | MCP Python SDK（server + client，stdio/SSE 传输） |

**集成方式：**

- **Server 方向** `mcp/server.py`：用 `@mcp.tool()` 装饰器包装现有 6 个能力，handler 内调 agent_tools 的 Tool.handler / kg_service.graph_context / retrieval_service.mixed_search。鉴权用简单 token + IP 白名单（PRD Q3 确认）
- **Client 方向** `mcp/client.py`：启动时从 `MCP_SERVERS` 配置发现外部 MCP server → 列出其 tools → schema 转换为 OpenAI 格式 → 注册进 `ToolRegistry` → `agent_runtime` 无感调用
- **Registry** `mcp/registry.py`：配置驱动的 server 列表管理，settings 增 `MCP_SERVERS` JSON 配置项
- **Mock server** `mcp/mock_scada_server.py`：示例 MCP server，提供 `query_telemetry(device_id)` 返回模拟遥测数据

**Schema 转换约定：**

```python
# MCP tool schema → OpenAI tool schema
def mcp_to_openai(mcp_tool: dict) -> dict:
    return {"type": "function", "function": {
        "name": mcp_tool["name"],
        "description": mcp_tool["description"],
        "parameters": mcp_tool["inputSchema"]}}

# OpenAI tool schema → MCP tool schema
def openai_to_mcp(oai_tool: dict) -> dict:
    fn = oai_tool["function"]
    return {"name": fn["name"], "description": fn["description"],
            "inputSchema": fn["parameters"]}
```

---

## 二、文件列表及相对路径

### 2.1 新建文件

| # | 文件路径 | 所属功能 | 说明 |
|---|---|---|---|
| 1 | `backend/app/core/otel_genai.py` | N4 | OTel GenAI span 包装器 + trace_id contextvar + 采样 + 初始化 |
| 2 | `backend/app/services/agent_memory_service.py` | N1 | 记忆五函数 + Milvus/Neo4j/Redis 三层存储 |
| 3 | `backend/app/routers/memory.py` | N1 | 记忆管理 API（列表/删除/统计） |
| 4 | `backend/app/mcp/__init__.py` | N2 | MCP 模块初始化 |
| 5 | `backend/app/mcp/server.py` | N2 | MCP Server：6 能力暴露 + token 鉴权 |
| 6 | `backend/app/mcp/client.py` | N2 | MCP Client：发现→注册→调用外部工具 |
| 7 | `backend/app/mcp/registry.py` | N2 | MCP Server 注册表 + 配置管理 |
| 8 | `backend/app/mcp/mock_scada_server.py` | N2 | 示例 mock MCP server（遥测查询） |
| 9 | `backend/app/services/twin_service.py` | N3 | 设备-空间映射 + 状态聚合 + 告警推送 |
| 10 | `backend/app/routers/twin.py` | N3 | 数字孪生 API（场景/设备详情/订阅） |
| 11 | `backend/app/data/station_layout_110kv.json` | N3 | 110kV 户外站布局模板（~30 设备坐标） |
| 12 | `frontend/src/views/DigitalTwin.vue` | N3 | 3D 数字孪生页面（Three.js 简化几何体） |
| 13 | `grafana/dashboards/llm_quality.json` | N4 | Grafana LLM 质量面板（P95/错误率/幻觉率/成本） |

### 2.2 修改文件

| # | 文件路径 | 所属功能 | 修改点 |
|---|---|---|---|
| 1 | `backend/requirements.txt` | 全部 | 新增 opentelemetry-sdk/exporter-otlp + mcp 包 |
| 2 | `backend/app/config.py` | 全部 | 新增 N1-N4 配置项（OTEL_* / MCP_* / MEMORY_* / TWIN_*） |
| 3 | `docker-compose.yml` | N4 | 新增 Langfuse 服务 + 依赖 |
| 4 | `backend/app/main.py` | 全部 | lifespan 初始化 OTel/MCP/Memory + 挂载新 router |
| 5 | `frontend/src/api/index.js` | N1/N3 | 新增 memory API + twin API |
| 6 | `backend/app/providers/base.py` | N4 | LLMProvider 增加可选 span 回调 |
| 7 | `backend/app/providers/llm/deepseek_llm.py` | N4 | chat/chat_with_tools 包 LLM span |
| 8 | `backend/app/providers/llm/qwen_llm.py` | N4 | chat/chat_with_tools 包 LLM span |
| 9 | `backend/app/providers/llm/doubao_llm.py` | N4 | chat/chat_with_tools 包 LLM span |
| 10 | `backend/app/core/obs.py` | N4 | degraded() 新增 span event 输出 |
| 11 | `backend/app/services/retrieval_service.py` | N4 | mixed_search 包 retrieve span |
| 12 | `backend/app/services/query_rewrite.py` | N4 | rewrite_query_v2 包 query_rewrite span |
| 13 | `backend/app/services/rerank_service.py` | N4 | rerank 包 rerank span |
| 14 | `backend/app/services/agent_runtime.py` | N1/N4 | L177 记忆注入 + 结束后 extract + agent span |
| 15 | `backend/app/services/agent_tools.py` | N2 | ToolRegistry 支持动态注册外部 MCP tool |
| 16 | `backend/app/services/online_eval_service.py` | N4 | eval 结果作为 span attribute 附加 |
| 17 | `backend/app/services/cost_tracker_service.py` | N4 | token/cost 作为 span metric |
| 18 | `backend/app/rag/judge.py` | N4 | judge 结果作为 trace 评分 |
| 19 | `backend/app/clients/milvus_client.py` | N1 | 新增 memory_collection 的 ensure/insert/search |
| 20 | `backend/app/clients/neo4j_client.py` | N1 | 新增 user→preference→entity 图操作 |
| 21 | `frontend/src/views/Admin.vue` | N1 | 新增"记忆"Tab（只读列表+删除） |
| 22 | `frontend/src/views/RetrievalDebug.vue` | N4 | 升级为统一 trace 树可视化 |
| 23 | `frontend/src/router/index.js` | N3 | 新增 /twin 路由 |
| 24 | `frontend/src/core/ws_manager.py` | N3 | 新增 twin WebSocket 通道（或复用 broadcast） |

---

## 三、数据结构和接口（类图）

```mermaid
classDiagram
    class AgentMemoryService {
        +extract_facts(messages: list~dict~, model_type: str) list~MemoryFact~
        +consolidate(user_id: str, facts: list~MemoryFact~) int
        +recall(query: str, user_id: str, scope: str) str
        +forget(memory_id: str) bool
        +decay() int
        +extract_and_consolidate(user_msg: str, answer: str, user_id: str, model_type: str) void
        -_milvus_search(query_vec: list~float~, user_id: str, topk: int) list~dict~
        -_neo4j_traverse(user_id: str, entities: list~str~) list~dict~
        -_redis_hot_get(user_id: str) list~dict~
        -_update_weight(memory_id: str, hit: bool) void
    }

    class MemoryFact {
        +fact_id: str
        +user_id: str
        +scope: str
        +fact_text: str
        +entity: str
        +category: str
        +weight: float
        +created_at: datetime
        +last_hit_at: datetime
        +hit_count: int
        +deleted_at: datetime~None~
    }

    class McpServer {
        +start(host: str, port: int) void
        +stop() void
        -_register_tools() void
        -_tool_search_regulation(query: str) str
        -_tool_query_equipment_graph(entity: str) str
        -_tool_search_similar_case(symptom: str) str
        -_tool_draft_ticket(task: str) str
        -_tool_graph_query(entity: str, depth: int) str
        -_tool_mixed_search(query: str, topk: int) str
        -_verify_token(token: str) bool
    }

    class McpClient {
        +discover(servers: list~str~) list~dict~
        +register_tools(registry: ToolRegistry) int
        +call_tool(server_url: str, tool_name: str, args: dict) str
        -_convert_schema(mcp_tool: dict) dict
    }

    class McpRegistry {
        +servers: list~McpServerConfig~
        +load_from_config() void
        +get_server(name: str) McpServerConfig~None~
        +list_tools() list~dict~
    }

    class TwinService {
        +get_station_layout(station_id: str) dict
        +get_device_status(device_id: str) dict
        +get_station_overview(station_id: str) dict
        +push_alert_location(alert: dict) void
        +get_fault_chain(device_id: str, depth: int) list~dict~
        -_load_layout(station_id: str) dict
        -_aggregate_status(device_id: str) DeviceStatus
        -_color_by_risk(risk_score: float) tuple
    }

    class DeviceNode {
        +deviceId: str
        +name: str
        +type: str
        +position: tuple~float, float, float~
        +size: tuple~float, float, float~
        +kgEntity: str
        +connections: list~str~
    }

    class DeviceStatus {
        +deviceId: str
        +riskScore: float
        +riskLevel: str
        +alertStatus: str
        +ticketStatus: str
        +color: str
        +blink: bool
    }

    class OtelGenaiWrapper {
        +init_otel(endpoint: str, sample_rate: float) void
        +trace_span(name: str, kind: SpanKind) ContextManager
        +get_trace_id() str
        +set_attribute(key: str, value: any) void
        +add_event(name: str, attrs: dict) void
        +record_exception(exc: Exception) void
        +force_export(span: Span) void
    }

    class TraceContext {
        +trace_id: str
        +span_id: str
        +parent_span_id: str~None~
        +sampled: bool
    }

    %% 现有类（复用关系）
    class ToolRegistry {
        +register(tool: Tool) void
        +get(name: str) Tool~None~
        +schemas_for(names: list~str~) list~dict~
        +run(db, model_type, name, args, ctx) tuple
    }

    class LLMProvider {
        <<abstract>>
        +chat(messages, temperature, max_tokens) str
        +stream(messages) AsyncIterator~str~
        +chat_with_tools(messages, tools, tool_choice) dict
    }

    class AgentRuntime {
        +run_agent(db, persona, user_msg, model_type, registry, ctx) AgentResult
    }

    %% 关系
    AgentMemoryService --> MemoryFact : 管理
    AgentMemoryService ..> ToolRegistry : recall 不依赖工具
    AgentRuntime --> AgentMemoryService : recall注入 + extract触发
    AgentRuntime --> ToolRegistry : 调用工具
    McpClient --> McpRegistry : 发现server
    McpClient --> ToolRegistry : 注册外部tool
    McpServer ..> ToolRegistry : 包装现有tool.handler
    TwinService --> DeviceNode : 加载布局
    TwinService --> DeviceStatus : 聚合状态
    TwinService ..> AgentRuntime : 不依赖
    OtelGenaiWrapper --> TraceContext : 管理
    OtelGenaiWrapper ..> LLMProvider : 包装span
    OtelGenaiWrapper ..> AgentRuntime : 包装span
    OtelGenaiWrapper ..> TwinService : 包装span
```

---

## 四、程序调用流程（时序图）

### 4.1 N1：用户提问 → 记忆召回 → 注入 → LLM → 抽取 → 整合

```mermaid
sequenceDiagram
    participant U as 用户
    participant AR as agent_runtime.run_agent
    participant AMS as agent_memory_service
    participant M as Milvus(memory_collection)
    participant N as Neo4j(user→pref→entity)
    participant R as Redis(热记忆)
    participant LLM as LLM Provider
    participant DB as MySQL(审计)

    U->>AR: 提问 "1号主变油温高怎么处理"
    AR->>AMS: recall("1号主变油温高", user_id="zhang", scope="user")
    AMS->>R: ZRANGE memory:hot:zhang 0 4
    R-->>AMS: 热记忆2条
    AMS->>M: search(memory_collection, query_vec, topk=5, filter=user_id)
    M-->>AMS: 向量记忆3条
    AMS->>N: MATCH (u:User)-[:PREFERS]->(e) WHERE u.id="zhang"
    N-->>AMS: 图记忆1条(偏好:关注主变类设备)
    AMS-->>AR: "用户偏好：关注主变类设备；历史诊断：1号主变曾排除冷却器故障"
    AR->>AR: messages = [system_prompt, memory_system, user_msg]
    AR->>LLM: chat_with_tools(messages, tools)
    LLM-->>AR: 调用 query_equipment_graph("1号主变")
    AR->>AR: 工具执行...（多轮ReAct）
    LLM-->>AR: 最终答案
    AR-->>U: 返回答案

    Note over AR,DB: fire-and-forget 异步触发（不阻塞响应）
    AR->>AMS: extract_and_consolidate(user_msg, answer, user_id, model_type)
    AMS->>LLM: extract_facts prompt（DeepSeek最便宜档）
    LLM-->>AMS: ["用户负责110kV城东站","关注1号主变","已排除冷却器故障"]
    AMS->>AMS: consolidate（去重/消解/合并）
    AMS->>M: insert memory_collection（新事实向量）
    AMS->>N: MERGE (u)-[:PREFERS]->(e {name:"1号主变"})
    AMS->>R: ZADD memory:hot:zhang {fact_id: ts}
    AMS->>DB: INSERT agent_memory（审计+容量管理）
```

### 4.2 N2：Agent 发现外部 MCP tool → 注册 → 调用 mock_scada

```mermaid
sequenceDiagram
    participant AR as agent_runtime
    participant MC as mcp_client
    participant MR as mcp_registry
    participant TR as ToolRegistry
    participant MS as mock_scada_server
    participant U as 用户

    Note over MR,MS: 启动时初始化
    MR->>MR: load_from_config() → MCP_SERVERS=["mock_scada://localhost:9100"]
    MC->>MR: discover(servers)
    MC->>MS: MCP list_tools 请求
    MS-->>MC: [{name:"query_telemetry", inputSchema:{deviceId}}]
    MC->>MC: mcp_to_openai(schema转换)
    MC->>TR: register(Tool("query_telemetry", ...))
    TR-->>MC: 注册成功

    U->>AR: "雷雨天气下线路跳闸，查实时遥测"
    AR->>TR: schemas_for(persona.allowed_tools + external_tools)
    TR-->>AR: [search_regulation, query_telemetry, ...]
    AR->>AR: LLM 决定调用 query_telemetry(deviceId="line_110kV_01")
    AR->>TR: run("query_telemetry", {deviceId:"line_110kV_01"})
    TR->>MC: 转发到外部 MCP server
    MC->>MS: MCP call_tool("query_telemetry", {deviceId:"line_110kV_01"})
    MS-->>MC: {voltage: 115.2kV, current: 280A, temp: 42°C}
    MC-->>TR: "电压115.2kV/电流280A/温度42°C"
    TR-->>AR: 工具结果
    AR->>AR: LLM 综合遥测+知识库给出诊断
    AR-->>U: "线路电压偏低，结合雷雨天气判断可能雷击..."
```

### 4.3 N3：告警事件 → twin_service → 3D 定位推送 → 前端高亮

```mermaid
sequenceDiagram
    participant AD as alert_disposal_service
    participant TS as twin_service
    participant WS as ws_manager
    participant FP as fault_prediction_service
    participant KG as kg_service
    participant DT as DigitalTwin.vue
    participant U as 值班员

    Note over DT: 页面加载
    DT->>TS: GET /twin/station/110kV-demo/layout
    TS-->>DT: {areas, devices[30], connections}
    DT->>TS: GET /twin/station/110kV-demo/overview
    TS->>FP: predict(days=30)
    FP-->>TS: items[{title, riskScore, riskLevel}]
    TS->>TS: aggregate_status（设备→riskScore→颜色映射）
    TS-->>DT: {devices:[{id, color, blink, riskLevel}]}
    DT->>DT: Three.js 渲染3D场景（盒子+着色+标签）
    DT->>WS: WebSocket connect /ws/twin

    Note over AD,DT: 告警发生
    AD->>TS: push_alert_location({severity:"critical", title:"1号主变油温高", device:"T1"})
    TS->>TS: 查 layout → T1 position=[0, 1.5, 0]
    TS->>WS: broadcast({type:"alert", deviceId:"T1", position:[0,1.5,0], severity:"critical"})
    WS-->>DT: {type:"alert", deviceId:"T1", ...}
    DT->>DT: 相机飞到 T1 坐标 + 设备红色闪烁
    U->>DT: 点击 T1 设备
    DT->>TS: GET /twin/device/T1_main_transformer/detail
    TS->>KG: graph_context("1号主变", 8)
    TS->>KG: get_paths("1号主变", depth=3)
    KG-->>TS: 传播链 [1号主变→过载→10kV母线→用户停电]
    TS-->>DT: {riskScore, alertStatus, kgContext, faultChain, tickets}
    DT->>DT: 侧栏弹出 + 3D高亮传播链路径
```

### 4.4 N4：用户提问 → 全链路 span（trace_id 贯穿）

```mermaid
sequenceDiagram
    participant U as 用户
    participant QA as qa_service
    participant OG as otel_genai
    participant QR as query_rewrite
    participant RS as retrieval_service
    participant RR as rerank_service
    participant LLM as LLM Provider
    participant JD as rag/judge
    participant OE as online_eval_service
    participant CT as cost_tracker
    participant LF as Langfuse

    U->>QA: POST /qa/answer {query}
    QA->>OG: trace_span("rag_pipeline", ROOT)
    Note over OG: trace_id = uuid4() 存入 contextvar

    QA->>OG: trace_span("query_rewrite")
    OG->>QR: rewrite_query_v2(query)
    QR-->>OG: rewritten_query
    OG->>OG: span.set_attribute("query.original", query)
    OG->>OG: span.set_attribute("query.rewritten", rewritten_query)

    QA->>OG: trace_span("retrieve")
    OG->>RS: mixed_search(db, rewritten_query)
    RS-->>OG: contexts[10]
    OG->>OG: span.set_attribute("retrieval.hit_count", 10)

    QA->>OG: trace_span("rerank")
    OG->>RR: rerank(query, docs)
    RR-->>OG: ranked_docs
    OG->>OG: span.set_attribute("rerank.model", "gte-rerank-v2")

    QA->>OG: trace_span("llm.generate", GEN_AI)
    OG->>LLM: chat(messages, temperature)
    LLM-->>OG: answer
    OG->>OG: span.set_attribute("gen_ai.usage.prompt_tokens", ...)
    OG->>OG: span.set_attribute("gen_ai.usage.completion_tokens", ...)
    OG->>CT: record_token_usage(...)
    OG->>OG: span.set_attribute("cost.tokens", total_tokens)

    QA->>OG: trace_span("judge", EVAL)
    OG->>JD: judge_hallucination(answer, sources)
    JD-->>OG: {faithfulness: 0.92}
    OG->>OE: eval_quality(query, answer, contexts)
    OE-->>OG: {relevance: 0.85, faithfulness: 0.92, completeness: 0.88}
    OG->>OG: span.set_attribute("eval.faithfulness", 0.92)

    alt faithfulness < 0.85
        OG->>OG: force_export(span) ← 异常必采
    end

    OG->>LF: OTLP HTTP export(trace)
    Note over LF: Langfuse trace 树可视化
    QA-->>U: answer

    Note over U,LF: 用户反馈"答得不好"时
    U->>QA: 查看 RetrievalDebug.vue trace 树
    QA->>LF: GET trace by trace_id
    LF-->>QA: span 树（query_rewrite→retrieve→rerank→llm→judge）
```

---

## 五、任务列表（核心产出）

### 任务依赖关系

```mermaid
graph TD
    T01[T01: 项目基础设施<br/>依赖+配置+入口+API]
    T02[T02: N4 LLM全链路可观测性<br/>otel_genai+span注入+trace树]
    T03[T03: N1 Agent长期记忆层<br/>memory_service+注入+Admin Tab]
    T04[T04: N3 数字孪生3D<br/>DigitalTwin+twin_service+布局]
    T05[T05: N2 MCP工具总线<br/>server+client+registry+mock]

    T01 --> T02
    T01 --> T03
    T01 --> T04
    T01 --> T05
    T02 -.-> T03
    T02 -.-> T04
    T02 -.-> T05
    T03 -.-> T05

    style T01 fill:#4a90d9,color:#fff
    style T02 fill:#e67e22,color:#fff
    style T03 fill:#e74c3c,color:#fff
    style T04 fill:#2ecc71,color:#fff
    style T05 fill:#9b59b6,color:#fff
```

> 实线 = 强依赖（必须先完成）；虚线 = 弱依赖（受益但不阻塞，T02 的 span 能覆盖 T03/T04/T05 的调用链，但后者不依赖前者完成）

### 任务详情

| 任务ID | 所属功能 | 任务名 | 描述 | 依赖 | 优先级 | 预计工作量 | 涉及文件 |
|---|---|---|---|---|---|---|---|
| **T01** | 全部 | 项目基础设施 | 新增全部依赖包（opentelemetry-sdk/exporter-otlp/mcp）、N1-N4 配置项（OTEL_*/MCP_*/MEMORY_*/TWIN_*）、docker-compose 新增 Langfuse 服务、main.py lifespan 初始化（OTel/MCP/Memory collection ensure）、前端 api/index.js 新增全部新 API 函数声明、package.json 确认 three.js 已有 | 无 | P0 | 2 人天 | `backend/requirements.txt`[修改]<br/>`backend/app/config.py`[修改]<br/>`docker-compose.yml`[修改]<br/>`backend/app/main.py`[修改]<br/>`frontend/src/api/index.js`[修改]<br/>`frontend/package.json`[确认] |
| **T02** | N4 | N4 LLM 全链路可观测性 | 新建 `core/otel_genai.py`（trace_span 装饰器 + trace_id contextvar + 采样策略 + init_otel）；在 3 个 LLM provider 子类包 LLM span；retrieval_service 包 retrieve span；query_rewrite 包 rewrite span；rerank 包 rerank span；agent_runtime 包 agent span；obs.degraded() 输出 span event；online_eval/cost_tracker/judge 结果附加 span attribute；RetrievalDebug.vue 升级为统一 trace 树（从 Langfuse API 拉 trace 数据渲染 span 树）；Grafana LLM 质量面板 JSON | T01 | P0 | 5 人天 | `backend/app/core/otel_genai.py`[新建]<br/>`backend/app/providers/base.py`[修改]<br/>`backend/app/providers/llm/deepseek_llm.py`[修改]<br/>`backend/app/services/retrieval_service.py`[修改]<br/>`backend/app/core/obs.py`[修改]<br/>`frontend/src/views/RetrievalDebug.vue`[修改] |
| **T03** | N1 | N1 Agent 长期记忆层 | 新建 `agent_memory_service.py`（extract_facts/consolidate/recall/forget/decay + Milvus memory_collection + Neo4j user→pref→entity + Redis 热记忆）；agent_runtime.py L177 注入 recall + 结束后 fire-and-forget extract_and_consolidate；新建 `routers/memory.py`（列表/删除/统计 API）；milvus_client 新增 memory_collection 操作；neo4j_client 新增用户偏好图操作；Admin.vue 新增"记忆"Tab（只读列表+软删除） | T01 | P0 | 6 人天 | `backend/app/services/agent_memory_service.py`[新建]<br/>`backend/app/routers/memory.py`[新建]<br/>`backend/app/services/agent_runtime.py`[修改]<br/>`frontend/src/views/Admin.vue`[修改] |
| **T04** | N3 | N3 数字孪生变电站 3D | 新建 `twin_service.py`（设备-空间映射+状态聚合+告警推送+故障链）；新建 `routers/twin.py`（场景/设备详情/订阅 API）；新建 `station_layout_110kv.json`（~30 设备坐标模板）；新建 `DigitalTwin.vue`（复用 KgGraph3D Three.js 引擎，简化几何体+着色+告警闪烁+传播链高亮+设备详情侧栏）；router/index.js 新增 /twin 路由；ws_manager 新增 twin 通道 | T01 | P1 | 6 人天 | `frontend/src/views/DigitalTwin.vue`[新建]<br/>`backend/app/services/twin_service.py`[新建]<br/>`backend/app/routers/twin.py`[新建]<br/>`backend/app/data/station_layout_110kv.json`[新建]<br/>`frontend/src/router/index.js`[修改] |
| **T05** | N2 | N2 MCP 工具总线 | 新建 `mcp/server.py`（6 能力暴露+token 鉴权）；新建 `mcp/client.py`（发现→schema转换→注册→调用）；新建 `mcp/registry.py`（配置驱动 server 列表）；新建 `mcp/mock_scada_server.py`（示例遥测查询）；agent_tools.py ToolRegistry 支持动态注册外部 MCP tool；factory.py 旁初始化 MCP registry | T01 | P1 | 5 人天 | `backend/app/mcp/server.py`[新建]<br/>`backend/app/mcp/client.py`[新建]<br/>`backend/app/mcp/registry.py`[新建]<br/>`backend/app/mcp/mock_scada_server.py`[新建]<br/>`backend/app/services/agent_tools.py`[修改] |

### 排期映射

```
Week 1     │ T01 项目基础设施（2天）→ T02 N4 可观测性（5天）
           │   └ T02 完成后 N1/N3 的开发即可被 trace 覆盖
           │
Week 2-3   │ T03 N1 记忆层（6天）  ←─并行─→  T04 N3 数字孪生（6天）
           │   └ 不同代码区域（services/ vs frontend/），可并行
           │
Week 4     │ T05 N2 MCP 工具总线（5天）
           │
Week 5-6   │ 集成联调 + N4 trace 验证全链路 + 演示打磨
```

---

## 六、依赖包列表

### 6.1 Python 新增包（requirements.txt 追加）

```
# ===== N4 LLM 全链路可观测性 =====
opentelemetry-sdk>=1.28,<2          # OTel 核心 SDK
opentelemetry-exporter-otlp>=1.28,<2 # OTLP HTTP 导出器（→ Langfuse）

# ===== N2 MCP 工具总线 =====
mcp>=1.0,<2                          # MCP Python SDK（server + client）
```

### 6.2 npm 包（package.json 确认/新增）

```json
{
  "dependencies": {
    "three": "^0.128.0"   // 已有，N3 复用，零新增 npm 依赖
  }
}
```

> **无新增 npm 包。** Three.js 已在 package.json 中，N3 的 DigitalTwin.vue 复用同一依赖。N1/N4 前端改动均在现有 Vue 组件内，无新依赖。

### 6.3 Docker 新增服务（docker-compose.yml 追加）

```yaml
  # ---------- Langfuse：LLM trace 可视化（N4）----------
  langfuse:
    image: langfuse/langfuse:latest
    container_name: grid-langfuse
    restart: unless-stopped
    depends_on:
      - postgres-langfuse
    environment:
      DATABASE_URL: postgresql://langfuse:langfuse@postgres-langfuse:5432/langfuse
      NEXTAUTH_SECRET: ${LANGFUSE_NEXTAUTH_SECRET:-langfuse-secret-2026}
      NEXTAUTH_URL: http://localhost:3001
      SALT: ${LANGFUSE_SALT:-langfuse-salt-2026}
      NEXT_PUBLIC_SIGNUP_DISABLED: "false"
    ports:
      - "3001:3000"   # Langfuse UI（避开 Grafana 的 3000）
    volumes:
      - ./data/langfuse:/data

  # ---------- Langfuse 专用 PostgreSQL ----------
  postgres-langfuse:
    image: postgres:15-alpine
    container_name: grid-langfuse-db
    restart: unless-stopped
    environment:
      POSTGRES_USER: langfuse
      POSTGRES_PASSWORD: langfuse
      POSTGRES_DB: langfuse
    volumes:
      - ./data/langfuse-db:/var/lib/postgresql/data
```

> Langfuse 纳入现有 Docker Compose 统一管理（PRD Q5 确认），端口 3001 避开 Grafana 的 3000。

---

## 七、共享知识（跨文件约定）

### 7.1 trace_id 传递约定（N4 基础设施，其他功能遵循）

```python
# core/otel_genai.py
import contextvars
from opentelemetry import trace

# trace_id 通过 contextvars 在 async 调用链中自动传递
_current_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")

def get_trace_id() -> str:
    """获取当前请求的 trace_id（N1/N2/N3 可用于日志关联）"""
    span = trace.get_current_span()
    if span and span.is_recording():
        return format(span.get_span_context().trace_id, "032x")
    return _current_trace_id.get()

def trace_span(name: str, kind=trace.SpanKind.INTERNAL):
    """上下文管理器：创建 span 并自动关联到当前 trace。
    用法：
        with trace_span("retrieve") as span:
            span.set_attribute("retrieval.hit_count", len(results))
            results = await mixed_search(...)
    """
    tracer = trace.get_tracer("grid-qa")
    return tracer.start_as_current_span(name, kind=kind)
```

- **所有 N1/N2/N3 新增代码**在日志输出时附带 `get_trace_id()`，实现日志↔trace 双向关联
- **前端**通过 API 响应头 `X-Trace-Id` 获取 trace_id，在 RetrievalDebug.vue 中用它查 Langfuse trace 树

### 7.2 记忆作用域 scope 字段约定（N1）

```python
# scope 枚举值
SCOPE_USER = "user"      # 用户级：偏好/习惯/常问设备类型
SCOPE_DEVICE = "device"  # 设备级：该设备历史诊断结论/待确认项

# recall 时 scope 参数：
#   scope="user" → 只召回用户级记忆
#   scope="device" → 只召回设备级记忆（从 query 中提取设备名过滤）
#   scope="all" → 两层都召回，合并后按 weight×relevance 排序

# 记忆 category 枚举值（extract_facts prompt 约束）：
CATEGORY_PREFERENCE = "preference"    # 用户偏好（如"关注主变类设备"）
CATEGORY_DIAGNOSIS = "diagnosis"      # 诊断结论（如"排除冷却器故障"）
CATEGORY_PENDING = "pending"          # 待确认项（如"待确认是否负载过高"）
```

### 7.3 MCP tool schema 与 OpenAI tool schema 转换约定（N2）

```python
# 现有 Tool.schema 属性返回的格式（agent_runtime.py L36-38）：
{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}

# MCP tool inputSchema 就是 JSON Schema，与 OpenAI parameters 格式一致
# 转换是 1:1 映射，只需重包装：
#   MCP → OpenAI: {"type":"function","function":{"name":mcp.name,"description":mcp.desc,"parameters":mcp.inputSchema}}
#   OpenAI → MCP: {"name":oai.function.name,"description":oai.function.description,"inputSchema":oai.function.parameters}

# 外部 MCP tool 注册进 ToolRegistry 时，handler 统一包装为：
async def _mcp_tool_handler(db, model_type, **args):
    result = await mcp_client.call_tool(server_url, tool_name, args)
    return result  # str，LLM 可读摘要
```

### 7.4 设备-空间坐标数据结构约定（N3）

```python
# station_layout_110kv.json 结构（Q7 确认）
{
  "stationId": "110kV-demo",
  "stationName": "110kV示范变电站",
  "voltageLevel": "110kV",
  "type": "outdoor",  # outdoor | indoor | distribution_room
  "areas": [
    {"id": "main_transformer_area", "name": "主变区", "position": [0,0,0], "size": [12,0,8]},
    {"id": "110kV_switchyard", "name": "110kV配电装置区", "position": [15,0,0], "size": [10,0,12]},
    {"id": "10kV_switchgear_room", "name": "10kV开关室", "position": [-12,0,0], "size": [8,4,6]}
  ],
  "devices": [
    {
      "deviceId": "T1_main_transformer",
      "name": "1号主变压器",
      "type": "main_transformer",  # 用于选择图标/几何体尺寸
      "area": "main_transformer_area",
      "position": [0, 1.5, 0],     # 相对于 area 的局部坐标
      "size": [2.5, 3, 2.5],       # 盒子尺寸 [w, h, d]
      "kgEntity": "1号主变",        # 关联 Neo4j Entity.name（知识图谱/故障链查询用）
      "connections": ["T1_bushing", "T1_cooler", "T1_OLTC"]  # 物理连接设备（传播链可视化用）
    }
    // ... ~30 台设备
  ]
}

# 设备类型枚举（决定 Three.js 几何体+图标）：
DEVICE_TYPES = {
    "main_transformer": {"icon": "🔀", "color": 0x3498db, "size": [2.5, 3, 2.5]},
    "circuit_breaker": {"icon": "⚡", "color": 0xe74c3c, "size": [1, 2, 1]},
    "disconnector": {"icon": "🔌", "color": 0x2ecc71, "size": [0.8, 1.5, 0.8]},
    "current_transformer": {"icon": "📊", "color": 0xf39c12, "size": [0.6, 2, 0.6]},
    "potential_transformer": {"icon": "📈", "color": 0x9b59b6, "size": [0.6, 2, 0.6]},
    "lightning_arrester": {"icon": "🌩️", "color": 0x1abc9c, "size": [0.4, 2.5, 0.4]},
    "busbar": {"icon": "➖", "color": 0x95a5a6, "size": [8, 0.3, 0.3]},
    "cable": {"icon": " cable", "color": 0x34495e, "size": [0.3, 0.3, 6]},
}
```

### 7.5 API 响应格式约定

所有新增 API 遵循现有 `{code, data, message}` 格式（`core/response.py` 的 `success()`/`error()`）。

### 7.6 软删除约定（N1 记忆 + 审计）

```python
# agent_memory 表 deleted_at 字段：
#   NULL = 有效记忆
#   非NULL = 已软删除（保留30天审计日志后物理删除）
# recall 查询 WHERE deleted_at IS NULL
# forget(memory_id) → UPDATE SET deleted_at = NOW()
# 定时任务 cleanup → DELETE WHERE deleted_at < NOW() - INTERVAL 30 DAY
```

---

## 八、待明确事项

### 8.1 架构师确认事项

| # | 问题 | 决策 |
|---|---|---|
| Q6 | N4 trace 采样率 | **确认 PRD 建议：开发期 100%（OTEL_SAMPLE_RATE=1.0），上线后 10% + 异常必采。** 实现方式：`core/otel_genai.py` 内 `_should_export(span)` 逻辑——span status=ERROR 或 eval.faithfulness < FAITHFULNESS_GATE(0.85) 时强制导出，否则按 `OTEL_SAMPLE_RATE` 概率采样。采样率通过 `config.py` 的 `OTEL_SAMPLE_RATE` 配置项控制，可在运行时通过 `/system/config/otel` API 热调（复用现有 config_service 模式） |
| Q7 | N3 设备-空间坐标来源 | **确认 PRD 建议：首批 110kV 站手工配置坐标模板。** 数据结构见 §7.4，存储为 `backend/app/data/station_layout_110kv.json`（~30 台设备），twin_service 启动时加载到内存。后续如需配置界面，可新增 `routers/twin.py` 的 PUT /twin/station/{id}/layout 端点 + 前端编辑器（本次不做）。从图谱推断坐标不可靠——Neo4j Entity 无空间属性 |

### 8.2 设计风险与决策点

| # | 风险/决策点 | 影响 | 缓解措施 |
|---|---|---|---|
| R1 | **N1 extract_facts 的 LLM 调用成本** | 每轮对话后异步调 DeepSeek，虽是最便宜档但仍增成本 | ① prompt 极简化（只抽原子事实，≤200 token 输出）；② 工具调用型长对话累积 ≥3 轮才触发；③ 极短问答（1-2 轮）跳过；④ 复用 DeepSeek（¥0.0005/1K input token，单次抽取成本 <¥0.001） |
| R2 | **N4 OTel span 包装的延迟开销** | 每次 LLM/retrieval 调用多一次 span 创建+属性设置 | OTel span 创建 ~0.01ms，属性设置 ~0.001ms，相对 LLM 调用（500ms+）可忽略。采样率 <1.0 时未采样的 span 仅创建不导出，开销更低 |
| R3 | **N3 Three.js 30 设备的性能** | 30 个 BoxGeometry + 30 个 Sprite + 连线，低端 GPU 可能卡 | ① 盒子用低多边形（8 顶点）；② 标签用 Sprite（始终面向相机）；③ 连线用 LineSegments 批量渲染；④ 动画帧率限制 30fps；⑤ 全屏时才开抗锯齿。实测 30 设备 <5MB 显存 |
| R4 | **N2 MCP SDK 版本兼容性** | MCP Python SDK 仍在快速迭代，1.x 可能有 breaking change | 锁定 `mcp>=1.0,<2`，schema 转换层隔离 SDK 变更影响。mock_scada_server 作为独立进程，SDK 升级不影响主系统 |
| R5 | **N1 记忆注入对现有 Agent 链路的回归风险** | L177 新增 system 消息可能影响 LLM 输出质量 | ① recall 返回空字符串时 = 无记忆 = 零行为变化（向后兼容）；② ctx=None 时跳过 recall（diagnose 老链路零回归）；③ 逐步灰度：先 scope="user" 再扩展 |
| R6 | **Langfuse 专用 PostgreSQL 的运维负担** | 新增一个 PG 实例 | ① 用 alpine 镜像（~80MB）；② 数据量小（trace 保留 30 天自动清理）；③ 纳入 Docker Compose 统一管理，无额外运维流程 |

### 8.3 假设

1. **DeepSeek API 可用性**：N1 extract_facts 依赖 DeepSeek API 可用。若 DeepSeek 故障，extract_facts 降级跳过（不影响主流程，记忆不更新但不报错）。
2. **Milvus memory_collection 初始化**：在 `main.py` lifespan 中 `ensure_collections()` 时一并创建，与现有 grid_chunks 同模式。
3. **Neo4j User 节点**：假设用户首次产生记忆时自动 MERGE 创建 `(u:User {id: username})`，不依赖外部用户表同步。
4. **Langfuse OTLP 端点**：Langfuse 原生接受 OTLP HTTP（`http://langfuse:3000/api/public/otel`），无需额外 adapter。
5. **前端 Three.js 版本**：`three@0.128.0` 已支持本方案所需的全部 API（BoxGeometry/Sprite/Raycaster/LineSegments）。若后续升级 three.js 版本需回归测试。

---

*本架构设计基于 2026-07-14 全量代码审计。所有注入点、接口、存储复用方式均经代码验证。任务列表按 Z 拍板的排期（N4先行→N1+N3并行→N2→集成）排列，工程师可直接按表执行。*
