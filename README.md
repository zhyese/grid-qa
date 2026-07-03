# 电网运维 RAG 智能问答系统

基于大模型 + RAG 的电网自主运维智能问答系统：**自然语言提问 → 智能路由 → 混合检索 → CRAG 自纠错 → 可信答案生成**，覆盖变电、配电、输电三大场景。

> 前端 Vue 3 · 后端 FastAPI · 三家云大模型可切换 · 双 Embedding 路由 · ★智能路由（sparse/dense/hybrid）· ★三级缓存（Redis→MySQL→LLM）· GraphRAG（Neo4j）· Corrective RAG · 多租户 · 多模态 VLM · Milvus + MinIO + MySQL + Redis + Neo4j + Nacos

---

## 系统架构（在线问答主链路）

```mermaid
flowchart TD
    classDef client fill:#e1f5fe,stroke:#03a9f4,stroke-width:2px
    classDef cache fill:#fff3e0,stroke:#ff9800,stroke-width:2px
    classDef routing fill:#fce4ec,stroke:#e91e63,stroke-width:2px,stroke-dasharray:5 3
    classDef retrieval fill:#fffde7,stroke:#f9a825,stroke-width:2px
    classDef llm fill:#ede7f6,stroke:#7e57c2,stroke-width:2px
    classDef storage fill:#e8f5e9,stroke:#4caf50,stroke-width:2px

    User(["用户提问"]):::client

    subgraph CACHE ["★ 三级缓存层"]
        direction TB
        L1["L1: Redis 热点<br/>10MB LRU · TTL=72h<br/>命中→1ms"]:::cache
        L2["L2: MySQL qa_cache<br/>MD5精确匹配<br/>命中→1-50ms"]:::cache
        L3["L3: LLM 全链路<br/>检索+生成<br/>命中→Write-Through双写"]:::cache
        L1 -->|miss| L2 -->|miss| L3
        L2 -->|"async回写"| L1
    end

    subgraph ROUTE ["★ 智能路由层 (Phase A)"]
        direction TB
        FEAT["特征提取<br/>长度·术语密度·类型·<br/>标准引用·数值·同义词"]:::routing
        TREE{"决策树<br/><1ms"}:::routing
        S["sparse<br/>仅BM25"]:::routing
        D["dense<br/>仅向量"]:::routing
        H["hybrid<br/>全链路"]:::routing
        FEAT --> TREE
        TREE -->|"短术语/标准引用"| S
        TREE -->|"故障口语/同义词/长NL"| D
        TREE -->|"默认/数值参数"| H
    end

    subgraph RETRIEVAL ["检索管线"]
        direction TB
        DENSE["双路Dense<br/>云1024维 + bge512维"]:::retrieval
        BM25["BM25 稀疏<br/>jieba分词+倒排索引"]:::retrieval
        RRF["RRF 融合"]:::retrieval
        RERANK["Rerank 重排<br/>gte-rerank-v2"]:::retrieval
        MMR["MMR 多样性"]:::retrieval
        PARENT["Parent 大块召回<br/>small-to-big"]:::retrieval
        DENSE --> RRF
        BM25 --> RRF
        RRF --> RERANK --> MMR --> PARENT
    end

    subgraph CRAG ["CRAG 自纠错"]
        direction TB
        GRADE{"分级<br/>v1:rerank分<br/>v2:per-doc LLM"}:::retrieval
        REWRITE["改写重检索"]:::retrieval
        REFUSE["refused 拒答<br/>零幻觉"]:::retrieval
        GRADE -->|"incorrect"| REWRITE
        REWRITE -->|"仍低分"| REFUSE
    end

    subgraph LLMGEN ["LLM 生成 + 后处理"]
        PROMPT["Prompt拼接<br/>大块+图谱+置信度"]:::llm
        GEN["流式SSE生成<br/>DeepSeek/Qwen/Doubao"]:::llm
        SAFE["脱敏+高风险标记"]:::llm
        JUDGE["异步LLM-judge<br/>真faithfulness"]:::llm
        PROMPT --> GEN --> SAFE --> JUDGE
    end

    subgraph STORE ["存储层"]
        MySQL[("MySQL<br/>chunks·对话·用户·<br/>★qa_cache")]:::storage
        Milvus[("Milvus 双路<br/>grid_chunks/_bge")]:::storage
        Redis[("Redis 7<br/>★10MB LRU<br/>热点+配置")]:::storage
        Neo4j[("Neo4j<br/>知识图谱")]:::storage
        MinIO[("MinIO<br/>原始文档")]:::storage
    end

    User --> CACHE
    L3 --> ROUTE
    ROUTE --> RETRIEVAL
    RETRIEVAL --> CRAG
    CRAG -->|"correct"| LLMGEN
    REFUSE --> User
    LLMGEN -->|"答案+引用+confidence"| User
    L3 -.->|"Write-Through<br/>Redis+MySQL"| Redis
    L3 -.->|"Write-Through"| MySQL
    RETRIEVAL -.-> Milvus
    PROMPT -.-> Neo4j
    PARENT -.-> MySQL

    style CACHE fill:#fff3e0,stroke:#ff9800
    style ROUTE fill:#fce4ec,stroke:#e91e63
    style RETRIEVAL fill:#fffde7,stroke:#f9a825
    style CRAG fill:#ffebee,stroke:#f44336
    style LLMGEN fill:#ede7f6,stroke:#7e57c2
    style STORE fill:#e8f5e9,stroke:#4caf50
```

## 完整数据流时序（含缓存 + 路由 + CRAG）

```mermaid
sequenceDiagram
    autonumber
    participant FE as 前端
    participant BE as 后端 /qa/answer
    participant Router as ★智能路由
    participant Redis as ★Redis L1(10MB LRU)
    participant MySQL as ★MySQL L2(qa_cache)
    participant Milvus as Milvus 双路
    participant Neo4j as Neo4j 图谱
    participant LLM as LLM

    FE->>BE: ① 提问 + JWT

    rect rgb(255, 243, 224)
        Note right of BE: 🟠 三级缓存查询
        BE->>Redis: ② L1 查热点(仅单轮)
        alt L1 命中
            Redis-->>BE: 答案 (1ms)
            BE-->>FE: 秒回 cached=true
        else L1 miss
            BE->>MySQL: ③ L2 查qa_cache(MD5)
            alt L2 命中
                MySQL-->>BE: 答案 (1-50ms)
                BE->>Redis: async 回写L1
                BE-->>FE: 秒回 cached=true
            else L2 miss
                Note right of BE: 进入L3全链路
            end
        end
    end

    rect rgb(252, 228, 236)
        Note right of BE: 🔴 智能路由 (<1ms)
        BE->>Router: ④ 特征提取+决策树
        Router-->>BE: sparse|dense|hybrid|sparse_first
    end

    rect rgb(255, 253, 231)
        Note right of BE: 🟡 检索执行
        alt sparse (短术语/标准引用)
            BE->>BE: ⑤ BM25 only (~15ms)
        else dense (故障口语/同义词)
            BE->>Milvus: ⑤ 双路向量 (~25ms)
        else hybrid (默认)
            BE->>Milvus: ⑤ 双路Dense + BM25
            BE->>BE: RRF融合
        end
        opt 非sparse高置信
            BE->>LLM: ⑥ Rerank重排
        end
        BE->>MySQL: ⑦ Parent大块召回
    end

    rect rgb(255, 235, 238)
        Note right of BE: 🔴 CRAG 自纠错
        BE->>BE: ⑧ 分级(v1/v2)
        alt incorrect
            BE->>LLM: 改写重检索
            BE->>BE: 仍低→refused拒答
        end
    end

    BE->>Neo4j: ⑨ GraphRAG 因果链

    rect rgb(237, 231, 246)
        Note right of BE: 🟣 SSE流式生成
        BE-->>FE: ⑩ meta(引用+conversationId)
        BE->>LLM: ⑪ Prompt(大块+图谱+置信度)
        loop 逐token
            LLM-->>BE: token
            BE-->>FE: token(打字机)
        end
    end

    BE->>MySQL: ⑫ Write-Through双写(L2先→L1后)
    BE->>Redis: L1缓存(TTL=72h)
    BE-->>FE: ⑬ done(confidence·图谱·highRisk·cacheLayer)
```

## 知识写入链路（离线）

```mermaid
flowchart LR
    classDef io fill:#e1f5fe,stroke:#03a9f4,stroke-width:2px
    classDef process fill:#ffffff,stroke:#ff9800,stroke-width:2px
    classDef storage fill:#e8f5e9,stroke:#4caf50,stroke-width:2px

    Upload["上传<br/>PDF/Word/Excel/图片"]:::io
    Parse["结构感知分块<br/>表格整体·父子两层"]:::process
    EmbedRoute{"双Embedding路由<br/>大→云1024维<br/>小→bge512维"}:::process
    EqTag["设备自动打标"]:::process
    KG["后台:LLM抽取三元组<br/>schema约束(13关系)<br/>归一/去重/过滤"]:::process

    MinIO[("MinIO 原文")]:::storage
    MySQL[("MySQL<br/>chunks·三元组·设备")]:::storage
    MilvusC[("Milvus 云向量")]:::storage
    MilvusB[("Milvus bge向量")]:::storage
    Neo4j[("Neo4j 图谱")]:::storage

    Upload --> MinIO
    Upload --> Parse --> MySQL
    Parse --> EqTag --> MySQL
    Parse --> EmbedRoute
    EmbedRoute -->|"大文档"| MilvusC
    EmbedRoute -->|"小文档"| MilvusB
    Parse -.-> KG --> Neo4j
    KG --> MySQL
```

---

## ✨ 核心特性

### 🤖 问答与检索
- **★ 智能路由 (Phase A)**：查询特征自动选择检索路径——短术语/标准引用→sparse(BM25)，故障口语/同义词→dense(向量)，默认→hybrid(全链路)。60%+ 查询跳过冗余检索分支
- **★ 三级缓存**：Redis L1 (10MB LRU, 72h TTL) → MySQL L2 (qa_cache 表) → LLM L3。命中率 ~20%→75%，加权延迟 ~10s→3s，API 费用节省 ~75%
- **三家云大模型可切换**：DeepSeek / 通义千问 / 豆包，`modelType` 按请求切换
- **双 Embedding 路由**：大文档→云(1024维)、小文档→本地 bge(512维)，双 collection 并行检索
- **混合检索**：HNSW Dense + BM25 Sparse + RRF + Rerank + MMR + Parent 大块召回
- **流式问答**：SSE 逐 token (meta/token/done 三段) + WebSocket 双向流
- **多轮对话**：历史持久化 + 指代消解 + 上下文拼接

### 🛡️ 可信与自纠错
- **★ Corrective RAG**：rerank 分级(correct/ambiguous/incorrect)→改写重检索→refused 拒答。零幻觉前置护栏
- **可信答案**：引用标注 + 高风险安全提示 + LLM-judge 异步幻觉评估
- **智能推荐**：答完推 3 个相关追问

### 🧠 知识图谱
- **Neo4j 多跳推理**：设备→故障→处置因果传播。LLM 抽取三元组(schema 约束 13 关系)→归一消歧→双写 MySQL+Neo4j
- **GraphRAG**：问答融合图谱结构化上下文；读写删三链路打通

### 📊 可观测
- **Grafana 22+ 面板**：请求/延迟/LLM/Embedding/缓存分层/★路由决策/CRAG/幻觉/反馈/基础组件健康/静默降级
- **降级可观测**：`DEGRADED` 指标 + loguru warning，盲降级不再被吞
- **Provider 健康探测**：主动抓账户欠费/key 失效

### ⚙️ 工程地基
- **Golden 回归门禁**：30 条问答集 + recall/MRR + CI 门禁
- **测试**：69 单元测试 + 12 自测通过
- **限流**：9 个关键接口
- **Docker Compose**：11 服务一键编排
- **★ 完整部署包**：`make_package.py` 导出全部 9 个服务数据(MySQL/Redis/Milvus/MinIO/Neo4j/Prometheus/Grafana/Nacos) + 源码 → tar.gz (~45MB)，远端 `docker compose up -d --build` 即可运行

---

## 🛠️ 技术栈

| 层 | 选型 |
|---|---|
| 前端 | Vue 3 + Vite + Pinia + Vue Router + Axios + echarts |
| 后端 | Python 3.11+ · FastAPI · Uvicorn · SQLAlchemy 2.0(async) |
| LLM | DeepSeek `deepseek-chat` / 百炼 `qwen-plus` / 火山豆包 |
| Embedding | 百炼 `text-embedding-v3`(1024维) / 本地 `bge-small-zh-v1.5`(512维) |
| Rerank | 百炼 `gte-rerank-v2` |
| 向量库 | Milvus 2.4（HNSW + COSINE，双 collection） |
| 对象存储 | MinIO（源文档） |
| 元数据 | MySQL 8 |
| 缓存 | ★ Redis 7（10MB allkeys-lru + QA_CACHE_TTL=72h + ★ MySQL L2 冷备） |
| 知识图谱 | Neo4j 5（:Entity-[:REL]，2118 节点） |
| 监控 | Prometheus + Grafana + ★ 缓存分层/路由决策面板 |
| 编排 | Docker Compose（11 服务） |

---

## 📁 目录结构

```
.
├── backend/
│   ├── app/
│   │   ├── main.py                   # 入口（lifespan/CORS/health/metrics/★X-Cache-Hit）
│   │   ├── config.py                 # .env 配置（★含 ROUTING_ENABLE/CACHE_PERSIST_ENABLE）
│   │   ├── core/
│   │   │   ├── response/security     # 统一响应 / JWT+bcrypt
│   │   │   ├── logging               # loguru 结构化日志
│   │   │   ├── limiter               # slowapi 限流
│   │   │   ├── metrics               # ★ Prometheus（含CACHE_HIT/ROUTING_DECISION等）
│   │   │   └── obs                   # ★ 降级可观测 helper
│   │   ├── db/                       # 异步会话/建表/★qa_cache DDL
│   │   ├── models/                   # user/document/chunk/conversation/★qa_cache/kg_triple/feedback
│   │   ├── schemas/                  # Pydantic 请求/响应
│   │   ├── routers/                  # system/document/retrieval/qa/kg/domain
│   │   ├── services/
│   │   │   ├── document_service      # 上传/解析/向量化(路由)/删除/★缓存失效
│   │   │   ├── retrieval_service     # ★路由感知三路径检索(sparse/dense/hybrid)
│   │   │   ├── qa_service            # ★三级缓存 + ★路由注入 + CRAG + LLM
│   │   │   ├── cache_persist         # ★Write-Through双写 + 后台清理
│   │   │   ├── cache_warmup          # ★热点预热 + golden预加载
│   │   │   ├── kg_service            # 三元组抽取/多跳推理/GraphRAG
│   │   │   └── bm25/rerank/embedding/conversation/...
│   │   ├── routing/                  # ★ Phase A 智能路由模块
│   │   │   ├── config                # 阈值/开关/AB测试
│   │   │   ├── query_classifier      # 6维特征+决策树
│   │   │   └── routing_service       # 调度+降级链
│   │   ├── providers/                # 三家LLM + 云/bge Embedding
│   │   ├── clients/                  # minio/milvus(双collection)/redis/neo4j
│   │   └── rag/                      # crag/rrf/mmr/citation/judge/prompt_templates
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/                         # Vue 3 前端 (Dify/Linear风+科技蓝)
│   ├── src/{views,api,stores,router}
│   └── Dockerfile + nginx.conf
├── grafana/provisioning/
│   ├── dashboards/
│   │   ├── grid-qa.json              # 主监控面板(22面板)
│   │   └── cache-monitor.json        # ★ 缓存分层监控面板(7面板)
│   ├── alerting/                     # 告警规则/通知
│   └── datasources/
├── kb_seed/                          # 种子知识库(10份运维文档)
├── docker-compose.yml                # 开发编排(11服务)
├── docker-compose.deploy.yml         # ★ 远端部署编排(bind mount)
├── make_package.py                   # ★ 一键导出+打包
├── .env.deploy                       # ★ 远端部署模板(API Key已剥离)
├── .env.example
└── README.md
```

---

## 🚀 快速开始

### 前置
- Docker Desktop
- Python 3.11+、Node 20+
- 三家云 API Key（DeepSeek / 阿里百炼 / 火山方舟）

### 1. 启动基础设施

```bash
cp .env.example .env          # 填入 API Key
docker compose up -d mysql minio redis milvus neo4j
docker compose ps             # 确认 healthy
```

### 2. 启动后端

```bash
python -m venv venv
source venv/Scripts/activate
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
- MinIO：http://localhost:9001 （minioadmin/minioadmin）
- Neo4j：http://localhost:7474 （neo4j/neo4j123456）

---

## ⚙️ 核心配置（.env）

| 配置 | 说明 | 默认值 |
|------|------|--------|
| `LLM_PROVIDER` | 默认大模型 | `deepseek` |
| `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` / `ARK_API_KEY` | 三家云 API Key | 必填 |
| `★ QA_CACHE_TTL` | Redis 缓存秒数 | `259200` (72h) |
| `★ CACHE_PERSIST_ENABLE` | MySQL L2 缓存 | `true` |
| `★ ROUTING_ENABLE` | 智能路由开关 | `true` |
| `★ CRAG_ENABLE` | CRAG 自纠错 | `true` |
| `RERANK_ENABLE` / `MMR_ENABLE` | 重排/多样性 | `true` |
| `KG_RAG_ENABLE` | GraphRAG 图谱融合 | `true` |

---

## 🔌 API 接口

统一响应：`{"code": 200, "message": "...", "data": {...}}`

### 问答
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/qa/answer` | 智能问答（★三级缓存+★路由+CRAG） |
| POST | `/api/qa/answer/stream` | 流式问答（SSE） |
| GET | `/api/qa/conversations` | 对话列表 |
| GET | `/api/qa/history?conversationId=` | 对话历史 |
| POST | `/api/qa/feedback` | 问答反馈(👍/👎) |
| POST | `/api/qa/faithfulness` | LLM-judge 幻觉评估 |
| POST | `/api/qa/related` | 智能推荐 3 个追问 |

### 文档
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/document/upload` | 上传（PDF/Word/TXT/图片） |
| POST | `/api/document/parse` | 解析分块 + OCR |
| POST | `/api/document/vector/generate` | 向量化(双路由) |
| DELETE | `/api/document/delete` | 删除(联动MinIO+Milvus双collection+MySQL+Neo4j+★缓存失效) |
| GET | `/api/document/stats` | 知识库统计 |

### 知识图谱
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/kg/extract` | LLM 抽取三元组 |
| GET | `/api/kg/graph?entity=` | 关系图谱 |
| GET | `/api/kg/path?entity=&depth=` | 多跳影响链 |
| GET | `/api/kg/influence` | 枢纽实体排行 |

---

## 🐳 一键部署

```bash
cp .env.example .env
docker compose up -d --build
```

---

## 📦 完整部署包（含全部数据）

```bash
# 打包（本地）
python make_package.py    # → grid-qa-deploy-*.tar.gz (~45MB)

# 部署（远端 Linux + Docker）
scp grid-qa-deploy-*.tar.gz user@remote:/opt/
cd /opt && tar xzf grid-qa-deploy-*.tar.gz
cp .env.deploy .env       # ⚠️ 填入真实 API Key
docker compose -f docker-compose.deploy.yml up -d --build
curl http://localhost:8001/health
```

包内含全部 9 个服务数据（MySQL/Redis/Milvus/MinIO/Neo4j/Prometheus/Grafana/Nacos），解压 ~970MB，远端开箱即用。

---

## 📊 评测结果

| 指标 | 结果 | 目标 |
|---|---|---|
| 检索召回率 recall@5 | **100%** | ≥92% |
| MRR | **0.944** | — |
| ★ 缓存命中率 | **~75%** (原 ~20%) | — |
| ★ Redis 缓存命中延迟 | **0.001s** | — |
| ★ 加权平均延迟 | **~3s** (原 ~10s) | — |
| LLM-judge 幻觉率 | **0%** | ≤5% |

---

## 🗺️ 开发进度

**基础链路 S1–S11** ✅ · **优化 O1–O10** ✅ · **双 Embedding P1–P3** ✅ · **质量 Q1–Q10** ✅ · **前端 F1–F8** ✅ · **健壮性地基 P0–P2** ✅

**★ 2026 新增**：
- ✅ 三级缓存 Redis(LRU)→MySQL→LLM （命中率 20%→75%）
- ✅ 智能路由 Phase A （sparse/dense/hybrid 自适应）
- ✅ 完整部署包 （数据+源码一键打包）
- ✅ 分层 TTL （手册7d/案例3d/实时5min）
- ✅ 缓存热点预热 + 文档更新失效
- ✅ Grafana 缓存分层 + 路由决策面板

---

## 🔮 路线图

| 阶段 | 内容 | 状态 |
|------|------|------|
| **Phase A** | 智能路由（规则决策树） | ✅ 完成 |
| **Phase B** | BMX 第三路稀疏信号 / Milvus 原生稀疏向量 | 📋 计划中 |
| **Phase C** | ML 自适应路由 + 自动阈值调优 | 📋 计划中 |
