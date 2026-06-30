# 电网运维 RAG 智能问答系统

基于大模型 + RAG 的电网自主运维智能问答系统：**自然语言提问 → 混合检索 → 自纠错 → 可信答案生成**，覆盖变电、配电、输电三大场景，为一线运维提供可直接落地的故障处理方案。

> 前端 Vue 3 · 后端 FastAPI · 三家云大模型（DeepSeek/阿里百炼/火山方舟）可切换 · 双 Embedding 路由（云 + 本地 bge）· GraphRAG（Neo4j 多跳）· Corrective RAG 自纠错 · Milvus + MinIO + MySQL + Redis

---

## ✨ 核心特性

### 🤖 问答与检索
- **三家云大模型可切换**：DeepSeek / 通义千问 / 豆包，均兼容 OpenAI 协议，配置即切；`/qa/answer` 的 `modelType` 支持按请求切换
- **双 Embedding 路由**：文档大走云、小走本地 bge（双 collection，向量空间隔离），检索双查融合
- **混合检索**：HNSW 稠密 + BM25 稀疏 + RRF 融合 + 百炼 gte-rerank 重排 + MMR 多样性
- **热点问答缓存**：高频问题 Redis 秒回（6.5s → 0.002s）
- **流式问答**：SSE 逐 token 输出（meta/token/done 三段事件）
- **多轮对话**：历史持久化，追问带上下文

### 🛡️ 可信与自纠错（2026 RAG 趋势）
- **★ Corrective RAG 自纠错**：检索后用 rerank 分数分级（correct/ambiguous/incorrect），低相关触发 query 改写重检索，仍无强相关则 **refused 保守拒答**（零幻觉），把事后 LLM-judge 升级为实时前置护栏
- **可信答案**：引用标注 + 高风险操作安全提示 + LLM-as-judge 幻觉评估（评测集 0%）
- **💡 智能推荐**：答完推 3 个相关追问，引导深挖

### 🧠 知识图谱
- **Neo4j 多跳推理**：LLM 抽取设备-故障-处置三元组，多跳影响链 + 枢纽实体分析（MySQL 做不到的因果传播）
- **🔗 GraphRAG**：问答融合知识图谱结构化上下文；向量化自动建图谱，读+写+删三链路打通，不再孤岛

### 📊 可观测与运维
- **Grafana 监控（20 面板）**：请求/延迟/LLM/Embedding/缓存/幻觉/反馈/知识库/CRAG/基础组件健康/静默降级…
- **★ 降级可观测**：业务/IO 失败统一 `DEGRADED` 指标 + 日志，盲降级不再被吞（Neo4j 挂/rerank 挂/缓存挂一目了然）
- **★ Provider 健康检查**：`/system/health/providers` 主动探测抓账户欠费/key 失效；`/health` 配置态快照
- **基础组件健康指标**：MySQL/Milvus/Redis/MinIO 探活结果进 Prometheus，Grafana 可监控可告警

### ⚙️ 工程地基
- **★ 检索质量回归门禁**：golden 问答集（12 条）+ recall/MRR 评测 + CI 门禁（recall<92% 退出码 1），把"感觉还行"变"数据说话"
- **配置全对齐**：`.env.example` 与 `config.py` 53 字段一一对应，新部署不踩坑
- **完整管理**：JWT 鉴权、角色权限、操作日志、Milvus/模型参数配置、健康探活、结构化日志
- **限流防护**：9 个关键接口限流（问答/检索/上传/解析/向量化/反馈/推荐/图谱抽取），防成本攻击
- **测试覆盖**：29 个单元测试（chunk/term/rrf/obs/kg/document/crag）
- **生产就绪**：Docker Compose 一键全栈（9 服务）、gunicorn 多 worker、Alembic 迁移、CI/CD

---

## 🏗️ 系统架构

```mermaid
graph TB
    FE["Vue3 前端 :5173<br/>Chat · Documents · Dashboard · KgGraph · 🩺Diagnose · Admin"]
    BE["FastAPI 后端 :8001 🛡️安全护栏<br/>system · document · retrieval · qa · kg · domain"]

    FE -->|"① 提问 / 上传 / 诊断任务 · JWT"| BE
    BE -.->|"⑦ SSE token + confidence + ⚠highRisk · 真faithfulness异步"| FE

    %% ===== 写入流：原文 → 结构感知分块(表格/父子) → 向量 + 图谱(消歧) =====
    FE -->|"上传 PDF/Word/Excel/图片"| MinIO
    BE -->|"② 表格/Excel/OCR → 结构感知分块(父子)"| MySQL
    BE -->|"设备自动打标 equipment_tags"| MySQL
    BE -->|"③ 大走云 / 小走 bge"| EMB
    EMB -->|"云向量 1024 维"| MilvusC
    EMB -.->|"bge 512 维"| MilvusB
    BE -.->|"④ 后台抽三元组(消歧+schema约束)"| Neo4j

    %% ===== 读取流：缓存 → 多策略检索 → CRAG → 父子召回 → 图谱 → 生成 =====
    BE -->|"⑤ 命中热点?"| Redis
    BE -->|"standalone消解 / HyDE / 多查询分解"| LLM
    BE -->|"双路稠密 + BM25 → RRF"| RR
    RR -->|"rerank → CRAG v1(top1) / v2(per-doc LLM)"| CRAG["★ CRAG 自纠错<br/>incorrect→改写重检索→refused"]
    CRAG -->|"parent 召回大块上下文"| MySQL
    BE -.->|"⑥ 图谱因果上下文"| Neo4j
    BE -->|"⑦ prompt + 置信度 → 脱敏/高风险标记"| LLM

    %% ===== 领域增强（/domain）复用检索 + 图谱 =====
    BE -.->|"诊断/两票/案例：多查询→检索→因果链→结构化输出"| LLM

    subgraph STORE["存储层（各存其职）"]
        MySQL[("MySQL 8<br/>chunks(父子/类型/章节)·三元组·设备标签·对话·反馈·日志")]
        MinIO[("MinIO<br/>源文档原文")]
        MilvusC[("Milvus grid_chunks<br/>云向量 HNSW")]
        MilvusB[("Milvus grid_chunks_bge<br/>bge 向量 HNSW")]
        Redis[("Redis<br/>热点缓存·query向量缓存·配置")]
        Neo4j[("Neo4j<br/>Entity·REL 多跳图(消歧收敛)")]
    end
    subgraph CLOUD["云模型 · openai SDK"]
        LLM["LLM<br/>DeepSeek / Qwen / Doubao"]
        EMB["Embedding<br/>百炼 v3 / 本地 bge"]
        RR["Rerank<br/>gte-rerank-v2"]
    end
```
## 系统架构 竖版
```mermaid
flowchart TD
    %% 定义样式（new=粉色虚线，标注 v2 新增节点，一眼看增量）
    classDef client fill:#e1f5fe,stroke:#03a9f4,stroke-width:2px;
    classDef process fill:#fff3e0,stroke:#ff9800,stroke-width:2px;
    classDef storage fill:#e8f5e9,stroke:#4caf50,stroke-width:2px;
    classDef llm fill:#f3e5f5,stroke:#9c27b0,stroke-width:2px;
    classDef new fill:#fce4ec,stroke:#e91e63,stroke-width:2px,stroke-dasharray:5 3;

    User(["用户/前端 Vue3<br/>Chat · 🩺Diagnose · Documents · Admin"]):::client

    subgraph WriteFlow ["① 文档写入流 (离线知识构建)"]
        direction TB
        Upload["上传 PDF/Word/Excel/图片"]:::process
        Parse["结构感知分块<br/>表格整体 · 父子两层"]:::new
        EmbedRoute{"双 Embedding 路由<br/>大走云 / 小走 bge"}:::process
        EqTag["设备自动打标 equipment_tags"]:::new
        ExtractKG["后台异步:LLM 抽三元组<br/>+ 实体消歧 / schema 约束"]:::new
    end

    subgraph StorageLayer ["各司其职的存储层"]
        direction TB
        MinIO[("MinIO 原文")]:::storage
        MySQL[("MySQL Chunks(父子/类型/章节)<br/>三元组 · 设备标签 · 对话 · 反馈")]:::storage
        MilvusC[("Milvus 云向量 1024维")]:::storage
        MilvusB[("Milvus bge向量 512维")]:::storage
        Neo4j[("Neo4j 图谱(消歧收敛)")]:::storage
        Redis[("Redis 热点缓存 · query向量")]:::storage
    end

    subgraph ReadFlow ["② 问答读取流 (在线 RAG · CRAG 自纠错 · 真可信)"]
        direction TB
        Guard["🛡️ prompt injection 告警"]:::new
        CheckCache{"命中热点缓存?"}:::process
        Standalone["standalone 指代消解<br/>+ HyDE / 多查询分解"]:::new
        Retrieval["混合检索:双路向量 + BM25 → RRF"]:::process
        Rerank["Rerank 重排"]:::process
        Parent["parent 召回大块上下文"]:::new
        CRAG{"CRAG 分级纠错<br/>v1 top1分 / v2 per-doc LLM"}:::new
        Rewrite["改写 Query 重检"]:::process
        Refuse["低置信度 refused 拒答"]:::process
        GenPrompt["融合大块+图谱+置信度 构建 Prompt"]:::process
        StreamGen["LLM SSE 流式生成"]:::process
        Safe["脱敏(PII) + ⚠高风险标记"]:::new
        Faith["异步:真 faithfulness<br/>LLM-judge 覆盖幻觉率"]:::new
    end

    subgraph DomainFlow ["③ 领域增强 (/domain · 复用检索+图谱)"]
        direction TB
        Diagnose["🩺 故障诊断:多查询→检索→因果链→原因排序"]:::new
        Similar["📚 相似历史案例检索"]:::new
        Ticket["📝 两票辅助生成"]:::new
    end

    subgraph CloudModels ["模型服务 Provider"]
        direction TB
        LLM_Chat["LLM DeepSeek / Qwen / Doubao"]:::llm
        LLM_Embed["云 Embedding 百炼/火山"]:::llm
        LLM_Rerank["Rerank gte-rerank-v2"]:::llm
    end

    %% 写入链路
    User --"上传文档"--> Upload
    Upload --> MinIO
    Upload --> Parse
    Parse --> MySQL
    Parse --> EqTag
    EqTag --> MySQL
    Parse --> EmbedRoute
    EmbedRoute --"大文档"--> LLM_Embed
    LLM_Embed --> MilvusC
    EmbedRoute --"小文档"--> MilvusB
    Parse -.-> ExtractKG
    ExtractKG --> Neo4j
    ExtractKG --> MySQL

    %% 读取链路
    User --"提问 · JWT"--> Guard
    Guard --> CheckCache
    CheckCache --"命中(秒回)"--> User
    CheckCache --"未命中"--> Standalone
    Standalone --> Retrieval
    Retrieval -.-> MilvusC
    Retrieval -.-> MilvusB
    Retrieval --> LLM_Rerank
    LLM_Rerank --> Rerank
    Rerank --> Parent
    Parent --> CRAG
    CRAG --"ambiguous/incorrect"--> Rewrite
    Rewrite -.-> Retrieval
    Rewrite --"仍低分"--> Refuse
    CRAG --"correct"--> GenPrompt
    GenPrompt -.-> Neo4j
    GenPrompt --> LLM_Chat
    LLM_Chat --> StreamGen
    StreamGen --> Safe
    Safe --"⚠highRisk + 引用 + 置信度"--> User
    Safe -.-> Faith
    Faith -.-> User
    StreamGen --"写缓存(TTL)"--> Redis
    Refuse --"拒答(零幻觉)"--> User

    %% 领域增强链路
    User -.->|"诊断/两票/案例"| Diagnose
    Diagnose -.-> Retrieval
    Diagnose -.-> Neo4j
    Diagnose --> Similar
    Diagnose --> Ticket
```


### GraphRAG 数据链路（Neo4j 与原系统打通，不再孤岛）

```mermaid
flowchart LR
    subgraph W["① 写入 · 结构感知分块 + 自动建图谱（不阻塞接口）"]
        U[上传文件<br/>PDF/Word/Excel/图片] --> P[表格/Excel/OCR 解析]
        P --> SK[结构感知分块<br/>表格整体·父子两层]
        SK --> EQ[设备自动打标<br/>equipment_tags]
        SK --> V[Embedding 路由<br/>大走云/小走bge]
        V --> VMIL[("Milvus 向量<br/>grid_chunks / _bge")]
        SK -.->|后台异步| K[LLM 抽三元组<br/>+ 实体消歧/schema约束]
        K --> KG[("MySQL 三元组<br/>+ Neo4j 图")]
    end
    subgraph R["② 读取 · 多策略检索 + CRAG自纠错 + 真可信"]
        Q[提问] --> GUARD[🛡️ injection 告警]
        GUARD --> CC{"热点缓存<br/>命中?"}
        CC -->|命中| A1[秒回 cached]
        CC -->|未命中| ST[standalone 指代消解<br/>+ HyDE / 多查询分解]
        ST --> RT[检索<br/>双路向量 + BM25 → RRF]
        RT --> RRK[rerank 重排]
        RRK --> PE[parent 召回大块上下文]
        PE --> CG{"★CRAG分级<br/>v1 top1分 / v2 per-doc LLM"}
        CG -->|correct| PF["prompt<br/>文档大块+图谱"]
        CG -->|incorrect| RW[改写重检索]
        RW -.->|仍低分| RF[refused保守拒答]
        PF --> L[LLM 生成 + 脱敏/高风险标记]
        L --> A2[带引用+图谱N+置信度+⚠高风险]
        A2 -.->|异步| FJ[真faithfulness<br/>LLM-judge覆盖幻觉率]
        A2 -.->|写缓存| CC
    end
    subgraph D["③ 删除 · 五库联动清理"]
        DEL[删文档] --> CL["MinIO 原文 ·<br/>Milvus 双 collection ·<br/>MySQL chunks+三元组+设备标签 ·<br/>Neo4j 边"]
    end
    %% 闭环：写入产物 → 读取消费（数据真正流转）
    VMIL -. 检索 .-> RT
    KG -. 结构化上下文 .-> PF
```

### 单次问答数据流时序（含 CRAG 自纠错）

```mermaid
sequenceDiagram
    autonumber
    participant FE as 前端
    participant BE as 后端 /qa/answer
    participant Redis
    participant Milvus as Milvus(云+bge 双路)
    participant Neo4j
    participant LLM
    participant MySQL

    FE->>BE: 提问 + JWT
    BE->>BE: 🛡️ injection 告警 + 术语归一化
    BE->>Redis: 查热点缓存?
    alt 命中
        Redis-->>BE: 缓存答案
        BE-->>FE: 秒回(cached=true)
    else 未命中
        opt 多轮
            BE->>LLM: standalone 指代消解(消解"它/上面")
        end
        opt 开启
            BE->>LLM: HyDE 假设文档 / 多查询分解
        end
        par 双路 Embedding(可选 HyDE 文本)
            BE->>Milvus: 云向量稠密 TopK
        and
            BE->>Milvus: bge 向量稠密 TopK
        end
        BE->>BE: + BM25 → RRF 融合
        BE->>LLM: rerank 重排候选
        BE->>MySQL: parent 召回同组大块上下文
        BE->>BE: ★ CRAG 分级(v1 top1 / v2 per-doc LLM)
        alt incorrect(低相关)
            BE->>LLM: 改写 query 重检索
            BE->>BE: 再分级→仍低=refused
        end
        BE->>Neo4j: graph_context 因果三元组
        BE->>LLM: prompt(大块+图谱+置信度) 流式
        loop 逐 token
            LLM-->>BE: token
            BE-->>FE: SSE token
        end
        BE->>BE: 脱敏(PII_MASK) + 高风险关键词标记
        BE->>MySQL: 存对话消息
        BE->>Redis: 写热点缓存(TTL)
        BE-->>FE: done(图谱N·confidence·⚠highRisk·引用·耗时)
        FE->>BE: 异步拉取真 faithfulness(LLM-judge)
        BE-->>FE: 支撑率/幻觉率(覆盖展示)
    end
```

---

## 🛡️ Corrective RAG 自纠错（核心卖点）

2026 RAG 趋势（Self-RAG / CRAG / Adaptive RAG）的核心是**自纠错降幻觉**。传统 RAG「检索→生成」一次性管道，检索到垃圾还硬生成；本系统在中间插入分级 + 纠错闭环：

```
检索(rerank已给相关性分) → 分级:
  correct    top1 ≥ 0.6  → 证据充分，高置信正常作答
  ambiguous  中间         → 证据有限，prompt 追加"标注不确定"
  incorrect  top1 < 0.3   → 触发 query 改写重检索(force 绕过开关)
                          → 仍低分 → refused 保守拒答，LLM 不硬编造
```

- **信号源复用 rerank 分数**，不额外调评估 LLM（省钱 + 低延迟），与离线 `judge.py` 互补为实时前置护栏
- **rerank 未启用** → 降级 ambiguous（不误触发纠错）
- 答案透传 `confidence`（high/medium/refused）+ `cragAction`（normal/rewritten/refused），前端显示置信度标签（🟢高/🟡有限/🔴不足）
- Grafana「CRAG 检索分级」面板可观测分级分布

**实测**：覆盖 query → correct/高置信正常作答；无关 query（如"量子纠缠原理"）→ refused，改写重检索后保守拒答零幻觉。

---

## 🛠️ 技术栈

| 层 | 选型 |
|---|---|
| 前端 | Vue 3 + Vite + Pinia + Vue Router + Axios + echarts |
| 后端 | Python 3.11+ · FastAPI · Uvicorn · SQLAlchemy 2.0(async) |
| LLM（云，可切换） | DeepSeek `deepseek-chat` / 百炼 `qwen-plus` / 火山豆包(endpoint_id) |
| Embedding（云） | 百炼 `text-embedding-v3`（1024维）/ 火山豆包 |
| Embedding（本地） | `bge-small-zh-v1.5`（512维，可换 large）· sentence-transformers |
| Rerank | 百炼 `gte-rerank-v2` |
| 文档解析 | pdfplumber / python-docx / PyMuPDF + rapidocr-onnxruntime（PaddleOCR 模型） |
| 向量库 | Milvus 2.4（HNSW + COSINE，双 collection） |
| 对象存储 | MinIO（源文档） |
| 元数据 | MySQL 8（用户/文档/chunks/日志/对话/三元组） |
| 缓存 | Redis 7（热点问答 + 配置持久化 + query 向量缓存） |
| **知识图谱** | **Neo4j 5（设备-故障-处置多跳推理，:Entity-[:REL]）** |
| 检索 | HNSW 稠密 + rank-bm25 + RRF + rerank + MMR + ★ CRAG 自纠错 |
| 监控 | Prometheus + Grafana（**20 面板**） |
| 编排 | Docker Compose（9 服务） |

---

## 📁 目录结构

```
.
├── backend/                      # FastAPI 后端
│   ├── app/
│   │   ├── main.py               # 入口（lifespan/CORS/health+providers/异常/metrics）
│   │   ├── config.py             # .env 配置（53 字段）
│   │   ├── core/
│   │   │   ├── response/security # 统一响应 / JWT+bcrypt
│   │   │   ├── logging           # loguru 结构化日志
│   │   │   ├── limiter           # slowapi 限流
│   │   │   ├── metrics           # Prometheus 指标（含 DEGRADED/CRAG/COMPONENT_HEALTH）
│   │   │   └── obs               # ★ 降级可观测 helper（degraded 日志+计数）
│   │   ├── db/                   # 异步会话/建表
│   │   ├── models/               # user/document/chunk/conversation/operation_log/kg_triple/feedback
│   │   ├── schemas/              # Pydantic 请求/响应
│   │   ├── routers/              # system/document/retrieval/qa/kg
│   │   ├── services/
│   │   │   ├── document_service  # 上传/解析/向量化(路由)/删除/★自动建图谱
│   │   │   ├── retrieval_service # 双查+RRF+rerank+MMR
│   │   │   ├── qa_service        # 缓存/多轮/CRAG分级纠错/prompt+图谱/LLM/智能推荐
│   │   │   ├── kg_service        # 三元组抽取/多跳推理/GraphRAG上下文
│   │   │   ├── bm25/rerank/embedding/conversation/term/config/log/query_rewrite/feedback
│   │   ├── providers/            # 模型抽象(三家LLM + 云/bge Embedding + 健康探测)
│   │   ├── clients/              # minio/milvus(双collection)/redis/neo4j
│   │   ├── rag/
│   │   │   ├── prompt_templates  # 系统 prompt（含低置信作答指令）
│   │   │   ├── crag              # ★ Corrective RAG 分级器(grade/confidence_of)
│   │   │   ├── rrf/mmr/citation/judge
│   │   └── data/{grid_terms.json,golden_qa.json}  # 术语词表 + ★检索回归基准
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/                     # Vue 3 前端
│   ├── src/{views,api,stores,router}
│   ├── Dockerfile + nginx.conf
│   └── package.json
├── scripts/                      # 评测/压测/建库/造数
│   ├── seed_demo.py              # 建 demo 知识库
│   ├── seed_extra.py             # ★ 补缺失主题文档(电容器/避雷器/电缆)
│   ├── eval_retrieval.py         # ★ 检索召回(recall/MRR/分类/报告/门禁)
│   ├── eval_qa.py                # LLM-as-judge 幻觉率
│   ├── validate_golden.py        # ★ golden 集 CI 格式校验
│   ├── benchmark.py              # 并发压测
│   └── gen_traffic.py            # ★ 造流量喂饱 Grafana 面板
├── tests/                        # pytest（29 用例）
├── grafana/provisioning/         # Grafana 数据源 + 20 面板 dashboard（自动 provisioning）
├── .github/workflows/test.yml    # ★ CI（golden 校验 + 单元测试）
├── docker-compose.yml            # 全栈编排（9 服务）
├── .env.example                  # 配置模板（53 字段全对齐）
└── README.md
```

---

## 🚀 快速开始（本地开发）

### 前置
- Docker Desktop（跑基础设施）
- Python 3.11+、Node 20+
- 三家云服务的 API Key（DeepSeek / 阿里百炼 / 火山方舟）

### 1. 启动基础设施

```bash
cp .env.example .env          # 填入三家 API Key
docker compose up -d mysql minio redis milvus neo4j   # 先起依赖（首次会拉镜像）
docker compose ps             # 确认 healthy
```

> 端口约定：MySQL 映射 **3307**（避开本机 MySQL）、后端 **8001**（避开占用 8000 的进程）、Milvus 19530、MinIO 9000/9001、Redis 6379、Neo4j 7474/7687、Grafana 3000、Prometheus 9090。

### 2. 启动后端

```bash
python -m venv venv
source venv/Scripts/activate                      # Windows Git Bash
pip install -r backend/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
uvicorn app.main:app --reload --host 127.0.0.1 --port 8001 --app-dir backend
```

### 3. 启动前端

```bash
npm --prefix frontend install --registry https://registry.npmmirror.com
npm --prefix frontend run dev
```

### 4. 访问
- 前端：http://localhost:5173 （admin / admin123）
- 接口文档：http://localhost:8001/docs
- 健康检查：http://localhost:8001/health
- Grafana：http://localhost:3000 （admin/admin）
- MinIO 控制台：http://localhost:9001 （minioadmin/minioadmin）
- Neo4j Browser：http://localhost:7474 （neo4j/neo4j123456）

---

## ⚙️ 配置说明（.env）

复制 `.env.example` 为 `.env` 并填入（53 字段与 `config.py` 一一对应，均有默认值，仅 API Key 必填）：

| 配置 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` / `ARK_API_KEY` | 三家云 API Key（必填，留空则该 provider 不可用） |
| `DOUBAO_LLM_ENDPOINT_ID` | 火山豆包推理接入点 id（`ep-xxxx`，非模型名） |
| `LLM_PROVIDER` / `EMB_PROVIDER` | 默认 LLM / 云 Embedding（`deepseek`/`qwen`/`doubao`） |
| `EMBEDDING_DIM` | 云向量维度，固定 1024 |
| `BGE_MODEL` / `BGE_DIM` / `DOC_SIZE_THRESHOLD` | 本地 bge 模型/维度 + 文档大小路由阈值（默认 5000） |
| `MILVUS_COLLECTION` / `MILVUS_COLLECTION_BGE` | 双 collection（云 1024 / bge 512） |
| `RERANK_ENABLE` / `RERANK_MODEL` | 重排开关 / 百炼 gte-rerank-v2 |
| `MMR_ENABLE` / `MMR_LAMBDA` / `QUERY_REWRITE_ENABLE` | MMR 多样性 / query 改写开关 |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` / `KG_RAG_ENABLE` | 知识图谱连接 + GraphRAG 开关 |
| `★ CRAG_ENABLE` / `CRAG_HIGH` / `CRAG_LOW` | Corrective RAG 开关 + 分级阈值（0.6/0.3） |
| `JWT_SECRET` / `ADMIN_PASSWORD` | 鉴权密钥 / 默认管理员密码 |
| `REDIS_URL` / `QA_CACHE_TTL` | Redis 地址 / 问答缓存秒数 |

> ⚠️ 真实 API Key 只放 `.env`（已被 .gitignore 忽略），切勿提交。`.env.example` 保持空值模板。

---

## 🔌 API 接口

统一响应：`{"code": 200, "message": "...", "data": {...}}`；除登录/注册/健康检查外，需 `Authorization: Bearer <token>`。

### 系统
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/system/login` | 登录，返回 token |
| POST | `/api/system/register` | 注册用户（仅 admin） |
| GET | `/api/system/logs` | 操作日志（admin 全部 / operator 仅自己，时间过滤） |
| POST/GET | `/api/system/config/milvus` | Milvus 索引参数配置（仅 admin，Redis 持久化） |
| POST/GET | `/api/system/config/model` | 模型参数配置（仅 admin） |
| GET | `/api/system/health/providers` | ★ Provider 主动探测（抓欠费/key失效，仅 admin） |

### 文档
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/document/upload` | 上传（form-data，PDF/Word/TXT/图片，批量≤5/单≤100M） |
| GET | `/api/document/list` | 文档列表（分页 + 关键字） |
| GET | `/api/document/stats` | 知识库统计（文档/分块/向量 + 状态/类型分布） |
| POST | `/api/document/parse` | 解析分块（数字文档 + OCR + 术语归一化） |
| POST | `/api/document/vector/generate` | 向量化（按文档大小路由云/bge，返回 embeddingRoute） |
| DELETE | `/api/document/delete` | 删除（联动 MinIO + Milvus 双 collection + MySQL + Neo4j） |

### 检索与问答
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/retrieval/mixed` | 混合检索（双 collection + BM25 + RRF + rerank） |
| POST | `/api/qa/answer` | 智能问答（缓存 + 多轮 + CRAG 自纠错 + 引用/安全提示/confidence） |
| POST | `/api/qa/answer/stream` | 流式问答（SSE 逐 token） |
| GET | `/api/qa/conversations` | 对话列表 |
| PUT/DELETE | `/api/qa/conversations/{id}` | 重命名 / 删除对话 |
| GET | `/api/qa/history` | 对话历史消息 |
| POST | `/api/qa/term/normalize` | 术语归一化 |
| POST | `/api/qa/related` | 智能推荐 3 个相关追问（独立接口，不拖慢流式） |
| POST | `/api/qa/feedback` | 问答反馈（👍/👎 沉淀坏 case） |

### 知识图谱（Neo4j 多跳推理）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/kg/extract` | LLM 抽取文档三元组（双写 MySQL+Neo4j） |
| GET | `/api/kg/graph?entity=` | 关系图谱（Cypher 邻居，Neo4j 不可用回退 MySQL） |
| GET | `/api/kg/path?entity=&depth=` | **多跳影响链**（设备→故障→处置因果传播） |
| GET | `/api/kg/influence` | 枢纽实体（出度排行，找核心设备） |
| GET | `/api/kg/stats` | 图谱统计（三元组/实体/关系数 + 文档分布） |

### 系统/健康
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（探活 MySQL/MinIO/Milvus/Redis + ★ provider 配置快照） |
| GET | `/metrics` | Prometheus 指标 |

---

## 🧠 双 Embedding 路由

不同 Embedding 模型向量空间不兼容，必须分 collection：

```
向量化：文档字数 > DOC_SIZE_THRESHOLD(5000) → 云(1024维) → grid_chunks
       文档字数 ≤ 阈值                       → 本地 bge(512维) → grid_chunks_bge

检索：query 双路 embedding → 两 collection 各查 → RRF 融合 → rerank
```

- 本地 bge 解决云 API 并发限流瓶颈（小文档无限流）
- bge 模型首次下载需访问 HuggingFace：设 `HF_ENDPOINT=https://hf-mirror.com` 或代理或预下到 HF 缓存
- 换 bge-large：`BGE_MODEL=BAAI/bge-large-zh-v1.5` + `BGE_DIM=1024`

---

## 📊 可观测性（Prometheus + Grafana 20 面板）

`docker compose up -d` 包含 Prometheus（:9090）+ Grafana（:3000），dashboard 自动 provisioning（uid=`grid-qa`）。

**20 面板分组**：
- **流量**：HTTP 请求速率 / 延迟 P95 / 系统错误率
- **问答**：问答总数(按模型) / 缓存命中 / 缓存命中率(%) / 答案幻觉率 / 用户反馈(👍/👎)
- **模型**：LLM 调用次数+延迟 / Embedding 调用+延迟 / Rerank 调用+延迟 / 检索延迟
- **知识**：知识库规模(文档/分块/向量) / 向量化路由(云/bge)
- **★ 自纠错**：CRAG 检索分级（correct/ambiguous/incorrect）
- **★ 健康**：基础组件健康（MySQL/Milvus/Redis/MinIO，1=up/0=down）/ 静默降级（按原因 tag）

> 降级面板（`grid_degraded_total`）由 `app/core/obs.degraded()` 统一上报：业务/IO 失败被兜底时计数 + loguru warning，Neo4j 挂 / rerank 挂(百炼欠费) / 缓存挂一目了然。面板空载 = 系统健康。

造数喂面板：`python scripts/gen_traffic.py`（并发问答 + 缓存命中 + 反馈 + 检索，覆盖各面板）。

---

## ✅ 质量保障

### 单元测试（29 用例）
```bash
venv/Scripts/python -m pytest tests/ -v   # chunk/term/rrf/obs/kg/document/crag
```

### 检索质量回归门禁（golden 集）
`backend/data/golden_qa.json` 12 条电网运维典型问题（变电 7 / 输电 2 / 配电 3），作为检索召回回归基准：
```bash
python scripts/seed_demo.py && python scripts/seed_extra.py   # 建库(含缺失主题)
python scripts/eval_retrieval.py --topk 5 --threshold 0.92    # recall/MRR/分类/报告 + 门禁
```
- 输出 recall@K / MRR / 无结果率 / 分类召回 / 失败 case 明细 + markdown 报告（`reports/`）
- recall < 阈值退出码 1，可作 CI/定时门禁

### CI（`.github/workflows/test.yml`）
main 分支 push/PR 触发：装依赖（排除 torch）→ golden 集格式校验 → 单元测试。真实召回评测需服务，本地/定时跑。

---

## 📈 评测结果

| 指标 | 结果 | 目标 |
|---|---|---|
| 检索召回率 recall@5 | **100%** (12/12) | ≥92% |
| MRR | **0.944**（11/12 命中 Top1） | — |
| 单请求检索延迟 | **0.95s** | ≤1.5s |
| 50 并发检索成功率 | **100%** (50/50) | 不崩 |
| LLM-as-judge 幻觉率 | **0%** (6 问) | ≤5% |
| 热点缓存命中 | **6.5s → 0.002s** | — |

> 数据驱动闭环：补库前 recall 75%（电容器/避雷器/电缆未覆盖）→ 诊断根因为知识库广度（非算法，MRR 0.694 高）→ 补 3 份文档 → recall 100%。

---

## 🐳 一键部署（Docker Compose）

```bash
cp .env.example .env
docker compose up -d --build      # 9 服务：mysql/minio/redis/etcd/milvus-minio/milvus/neo4j/backend/frontend
```

容器间用 service name 通信：backend 连 `mysql:3306` / `minio:9000` / `milvus:19530` / `redis:6379` / `neo4j:7687`（由 compose `environment` 覆盖 `.env` 中的 localhost，API Key 仍由 `.env` 注入）。

---

## 🏭 生产部署（多 worker）

开发用 `uvicorn --reload`（单进程）；Linux/Docker 生产用 gunicorn 多 worker 提并发：

```bash
pip install gunicorn
gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:8001 --app-dir backend
```

> Windows 不支持 gunicorn 的 fork，Windows 上仍用 `uvicorn` 单进程；多 worker 部署在 Linux/Docker 环境。

---

## 💻 开发指南

### 切换 LLM
改 `.env` 的 `LLM_PROVIDER`（`deepseek`/`qwen`/`doubao`），或请求时传 `modelType` 按需切换。

### 添加术语归一化
编辑 `backend/app/data/grid_terms.json`：`"别名": "标准术语"`（重启生效）。

### 看日志
`data/logs/app.log`（loguru，50MB 轮转 / 10 天保留）；降级 warning 统一 `[降级:tag]` 前缀。

### 数据库迁移
Alembic（`backend/migrations`）：`alembic upgrade head`。env.py 从 settings 读 DB 连接。

---

## ❓ FAQ

**Q: PaddleOCR 为什么用 rapidocr-onnxruntime？**
A: paddlepaddle 3.3.1 在 Windows 有 onednn PIR 引擎 bug（关 oneDNN/PIR/monkey-patch 均无效）。rapidocr 用的是 PaddleOCR 官方 PP-OCR 模型 + onnxruntime 后端，识别效果等同、规避引擎 bug。生产 Linux 可切回原生 paddleocr。

**Q: pymilvus 为什么用 2.4？**
A: 2.3 的 grpcio 在 Python 3.13 Windows 无预编译 wheel。2.4 兼容且支持 py3.13。另需 `setuptools<81`（pymilvus 用 pkg_resources，setuptools≥81 已移除）。

**Q: 向量化后检索不到？**
A: 确认文档已 `parse`（chunks 表有数据）再 `vector/generate`；HNSW 切换需重建 collection（drop 后重新向量化）。

**Q: bge 模型下载失败？**
A: 设 `HF_ENDPOINT=https://hf-mirror.com` 或 `HTTPS_PROXY`，或预下模型到 HF 缓存目录。

**Q: 阿里百炼账户欠费怎么办？**
A: 欠费致 qwen LLM + text-embedding-v3 + gte-rerank 失败 → 检索降级（rerank 失败回退 RRF）。`/system/health/providers` 主动探测可抓欠费；Grafana「静默降级」面板看 rerank/embed 降级计数。恢复需充值百炼（不能简单切 `EMB_PROVIDER=bge` 救急——双 collection 维度不同）。

---

## 📋 功能复盘（全量功能清单）

**55+ 次增量提交**｜6 存储（MySQL/MinIO/Milvus×2/Redis/Neo4j）｜后端 5 router·15 service·7 表｜前端 6 页面｜Grafana 20 面板｜三家云模型 + 本地 bge｜29 单元测试

### 🤖 核心 RAG 问答链路
| 功能 | 技术细节 |
|---|---|
| 智能问答 `/qa/answer` | term 归一化→检索→★CRAG 分级纠错→prompt→LLM→引用+安全提示+confidence |
| 热点缓存 | Redis 缓存单轮答案，6.5s → 0.002s |
| 流式问答 SSE | stream_answer 三段事件(meta/token/done) + 前端 fetch+ReadableStream |
| 多轮对话 | conversations/messages 表，近 3 轮上下文 |
| 幻觉评估 | LLM-as-judge 启发式，评测集 0% |
| **★ Corrective RAG** | rerank 分级 + incorrect 改写重检索 + refused 拒答，实时护栏 |
| **★ GraphRAG** | graph_context 查 Neo4j 三元组拼 prompt【知识图谱】段，答案带 🔗图谱N |

### 🔍 检索引擎
| 功能 | 技术细节 |
|---|---|
| 混合检索 | Milvus 稠密 Top20 + BM25 稀疏 Top20 + RRF 融合(k=60) |
| HNSW 索引 | COSINE + M16/efConstruction200 |
| Rerank 重排 | 百炼 gte-rerank-v2，失败兜底 RRF（降级可观测） |
| MMR 多样性 | λ=0.6 相关性 vs 多样性 |
| Query 改写 / docType 过滤 | LLM 改写(可选，CRAG 纠错 force 调用) + 文档类型元数据过滤 |

### 🧩 知识图谱 + GraphRAG（Neo4j）
| 功能 | 技术细节 |
|---|---|
| 三元组抽取 `/kg/extract` | LLM 分块抽(6块/批)，JSON 容错解析，双写 MySQL+Neo4j |
| 关系图谱 `/kg/graph` | Cypher 查邻居（Neo4j 不可用回退 MySQL） |
| **★ 多跳影响链 `/kg/path`** | `MATCH path=(n)-[:REL*1..N]->(m)`，故障因果传播（MySQL 做不到） |
| 枢纽实体 `/kg/influence` | 出度排行找核心设备 |
| GraphRAG 数据链路 | 读(问答融合)+写(向量化自动建)+删(联动清) 三链路打通 |

### 💻 前端能力（6 页面）
| 页面 | 功能 |
|---|---|
| **Chat** | Markdown+代码高亮 / 流式打字机 / 引用溯源点击定位+复制 / 对话管理(增删改查搜索) / 智能推荐追问 / 🔗图谱N标签 / ★置信度标签(高绿/中黄/拒红) / 暗色 / 移动端响应式 |
| **Documents** | 拖拽上传 + 进度条 + 类型状态筛选 + 批量勾选 + 骨架屏 |
| **Dashboard** | echarts 统计仪表盘（饼图+柱图+卡片） |
| **KgGraph** | 关系图谱(echarts力导向) / 多跳影响链(chain→rels箭头) / 枢纽出度 三 tab |
| **Admin** | 操作日志 + Milvus/模型配置 |
| **Login** | 登录注册 + JWT |

### 📊 可观测与运维
Grafana **20 面板** · Prometheus 指标 · ★降级可观测(DEGRADED) · ★CRAG 分级 · ★基础组件健康 · ★Provider 探测 · 健康检查 · loguru 结构化日志 · CI/CD(ci-cd+main 分支) · Alembic 迁移 · slowapi 限流(9 接口) · pytest(29) · gunicorn 多 worker · ★检索回归门禁(golden+CI)

---

## 🗺️ 开发进度

**基础链路 S1–S11**：地基→认证→文档上传→解析+OCR→Embedding+Milvus→混合检索→RAG问答→配置+日志→前端联调→评测+性能→镜像化 ✅

**优化 O1–O10**：Redis缓存→rerank→分块语义→HNSW→流式SSE→多轮对话→LLM-judge→可观测→pytest→生产化 ✅

**双 Embedding P1–P3**：本地bge→双collection路由→检索双查融合 ✅

**性能质量 Q1–Q10**：双embed并行→query向量缓存→限流→MMR→query改写→docType过滤→Alembic→CI→CD/Prometheus→问答反馈 ✅

**前端 F1–F8**：Markdown高亮→流式打字机→引用溯源→对话管理→文档增强→统计仪表盘→暗色模式→体验优化 ✅

**Grafana 监控**：流式埋点修复 + 新指标，dashboard 9→**20** 面板 ✅

**跨端高级功能**：
- ✅ 知识图谱（MySQL → Neo4j 多跳推理）
- ✅ 智能推荐（答完推 3 个相关追问）
- ✅ GraphRAG 数据链路（Neo4j 融入问答，读+写+删三链路）
- ⬜ 语音问答（砍掉：百炼 TTS 不兼容 OpenAI 协议、收益有限）

**★ 健壮性地基 P0–P2**：
- ✅ P0-1 盲降级显式化（obs + DEGRADED 指标 + Grafana 面板，26 处）
- ✅ P0-2 .env.example 全量对齐（53 字段）
- ✅ P0-3 Provider 健康检查（主动探测 + /health 快照）
- ✅ P1 测试 9→29 + 限流 5→9 接口
- ✅ P2 golden 回归集 + CI 门禁（recall 75%→100% 闭环）

**★ 2026 趋势**：
- ✅ Corrective RAG 自纠错（分级 + 改写重检索 + refused 拒答，零幻觉）
- ✅ 基础组件健康监控（COMPONENT_HEALTH 指标 + Grafana 面板）

---

## 🚀 v2 增强模块（2026 RAG 前沿 + 电网领域深化）

> 在原有模块之上新增，**不改动既有链路**；以下功能均带配置开关，默认行为兼容 v1。

### 🔴 数据质量地基
- **结构感知分块 + Parent-Child（small-to-big）**：表格整体成块不被切两半；正文先切父块(大窗口)再切子块，检索用子块(精度)、召回同组父块全文给 LLM(完整上下文)，解决长规程跨块。`Chunk` 表加 `chunk_type/parent_idx/section`，开关 `SMALL_TO_BIG_ENABLE`。
- **表格/Excel 结构化解析**：PDF/Word 表格转 markdown 保留结构；新增 `.xlsx` 台账/定值单解析（openpyxl）。`parse_service.parse_file_structured`。

### 🔴 可信度真相化 + 反馈闭环
- **真 faithfulness（替代粗糙启发式）**：流式 done 后前端异步拉 `/qa/faithfulness`，LLM-judge 判定答案支撑率覆盖"幻觉率"展示；dislike 自动异步打 judge 分。开关 `ONLINE_FAITHFULNESS_ENABLE`。
- **反馈管理台 + 一键回流 golden**：Admin 页坏 case 看板（judge 分/理由/纠错），「标为 golden」自动写入 `golden_qa.json`，CI 门禁永久覆盖该 case。golden 集 12→**30** 条。
- **生成质量门禁**：`scripts/eval_generation.py` 端到端 faithfulness 评测（`FAITHFULNESS_GATE=0.85`，低于退出码 1）。

### 🟢 检索增强五件套（2026 RAG 趋势）
- **CRAG v2 per-doc grading**：LLM 逐条评估证据相关性，识破"top1 高分但其余全无关"的伪相关。`CRAG_PERDOC_ENABLE`。
- **多轮指代消解**：追问"它的处置步骤呢"→改写为带上下文独立查询再检索。`STANDALONE_REWRITE_ENABLE`。
- **多查询分解**：复杂问题拆子问题并行检索，跨查询 RRF 融合。`MULTI_QUERY_ENABLE`。
- **HyDE**：LLM 先生成假设答案，用其向量做 dense 检索，提升短/口语问题召回。`HYDE_ENABLE`。

### 🟡 电网领域深化（核心价值）
- **🩺 故障诊断 Agent（`POST /domain/diagnose`）**：症状→多查询分解→并行检索 + 图谱因果→可能原因排序(高/中/低)+处置+风险。前端 `/diagnose` 页 `Diagnose.vue`。
- **📚 相似历史案例检索（`POST /domain/similar-case`）**：限定故障案例库，"历史上类似故障怎么处理的"。
- **📝 两票辅助生成（`POST /domain/ticket`）**：操作任务→检索规程→结构化操作票(步骤/安全措施/风险点)，含安全前置提示。
- **🛡️ 安全合规（电网强监管）**：入站 prompt injection 告警(`SAFETY_FILTER_ENABLE`) + 答案敏感信息脱敏(`PII_MASK_ENABLE`) + 高风险操作 badge(答案含停电/接地/倒闸等词，前端标红提示)。
- **🏷️ 设备台账关联**：文档解析自动打设备标签，检索可按 `equipment` 过滤精确到具体设备；文档列表展示关联设备。

#### 🩺 故障诊断 Agent 数据流（POST /domain/diagnose）

```mermaid
flowchart LR
    S[故障症状] --> DM[LLM 多查询分解<br/>排查方向]
    DM --> RT[每方向并行检索<br/>双路向量 + BM25]
    RT --> DED[去重合并 TopN]
    DED --> KG[Neo4j 因果链上下文]
    DED --> SYN[LLM 综合诊断]
    KG --> SYN
    SYN --> OUT["可能原因排序 高/中/低<br/>+ 处置 + 依据 + ⚠风险"]
    OUT -.同基建.-> SC[相似历史案例<br/>/domain/similar-case]
    OUT -.同基建.-> TK[两票辅助生成<br/>/domain/ticket]
```

### 🧠 图谱质量保障
- **实体消歧 + Schema 约束**：`#1主变 / 1号主变 / 主变` → 统一 `主变压器`；关系收敛到白名单(发生/表现为/处置方法/原因/影响/...)，无法归类归"相关"保留连通性。`services/kg_normalize.py`。

### 📊 新增可观测指标
- `grid_safety_block_total{kind}`（安全事件）、`grid_domain_calls_total{feature}`（故障诊断/两票/相似案例调用），可接入 Grafana。

### 🧪 测试覆盖
- 单元测试 29 → **64**（新增：结构分块/表格解析/CRAG v2 分级/安全合规/图谱消歧/golden 覆盖度）。

---

## 📄 许可

本项目用于电网运维智能问答学习与内部部署。云模型 API 使用遵循各自服务条款。
