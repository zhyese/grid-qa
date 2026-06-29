# 电网运维 RAG 智能问答系统

基于大模型 + RAG 的电网自主运维智能问答系统：**自然语言提问 → 混合检索 → 可信答案生成**，覆盖变电、配电、输电三大场景，为一线运维提供可直接落地的故障处理方案。

> 前端 Vue 3 · 后端 FastAPI · 三家云大模型（DeepSeek/阿里百炼/火山方舟）可切换 · 双 Embedding 路由（云 + 本地 bge）· Milvus + MinIO + MySQL + Redis

---

## ✨ 核心特性

- **三家云大模型可切换**：DeepSeek / 通义千问 / 豆包，均兼容 OpenAI 协议，配置即切；`/qa/answer` 的 `modelType` 支持按请求切换
- **双 Embedding 路由**：文档大走云、小走本地 bge（双 collection，向量空间隔离），检索双查融合
- **混合检索**：HNSW 稠密 + BM25 稀疏 + RRF 融合 + 百炼 gte-rerank 重排
- **文档解析**：数字文档（PDF/Word/TXT）+ 扫描件/图片 OCR（PaddleOCR PP-OCR 模型）
- **热点问答缓存**：高频问题 Redis 秒回（6.5s → 0.002s）
- **流式问答**：SSE 逐 token 输出
- **多轮对话**：历史持久化，追问带上下文
- **可信答案**：引用标注 + 安全提示 + LLM-as-judge 幻觉评估（评测集 0%）
- **完整管理**：JWT 鉴权、角色权限、操作日志、Milvus/模型参数配置、健康探活、结构化日志
- **生产就绪**：Docker Compose 一键全栈、gunicorn 多 worker、pytest 测试

---

## 🏗️ 系统架构

```
┌──────────────┐     ┌──────────────────────────────────────────────┐
│  Vue3 前端    │────▶│            FastAPI 后端 (8001)                │
│  5173        │◀────│  登录/问答/文档/检索/管理                      │
└──────────────┘     └──────┬───────────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
┌──────────────┐   ┌────────────────┐   ┌────────────────┐
│ 检索增强生成  │   │  文档处理链路    │   │   存储层        │
│              │   │                │   │                │
│ 术语归一化   │   │ 上传→解析→分块  │   │ MySQL 元数据   │
│ 混合检索     │   │  (PDF/OCR)     │   │ MinIO 源文档   │
│ RRF+rerank   │   │                │   │ Milvus 向量×2  │
│ Prompt+LLM   │   │ Embedding 路由  │   │ Redis 缓存/配置 │
│ 引用/安全提示 │   │ 大→云 小→bge   │   │                │
└──────┬───────┘   └────────────────┘   └────────────────┘
       │
       ▼
┌──────────────────────────────────────┐
│  云模型 (openai SDK 统一对接)         │
│  LLM:  DeepSeek / Qwen / Doubao      │
│  Embed: 百炼 text-embedding-v3       │
│  Rerank: 百炼 gte-rerank-v2          │
│  本地:  bge-small-zh (sentence-tf)   │
└──────────────────────────────────────┘
```

---

## 🛠️ 技术栈

| 层 | 选型 |
|---|---|
| 前端 | Vue 3 + Vite + Pinia + Vue Router + Axios |
| 后端 | Python 3.11+ · FastAPI · Uvicorn · SQLAlchemy 2.0(async) |
| LLM（云，可切换） | DeepSeek `deepseek-chat` / 百炼 `qwen-plus` / 火山豆包(endpoint_id) |
| Embedding（云） | 百炼 `text-embedding-v3`（1024维）/ 火山豆包 |
| Embedding（本地） | `bge-small-zh-v1.5`（512维，可换 large）· sentence-transformers |
| Rerank | 百炼 `gte-rerank-v2` |
| 文档解析 | pdfplumber / python-docx / PyMuPDF + rapidocr-onnxruntime（PaddleOCR 模型） |
| 向量库 | Milvus 2.4（HNSW + COSINE，双 collection） |
| 对象存储 | MinIO（源文档） |
| 元数据 | MySQL 8（用户/文档/chunks/日志/对话） |
| 缓存 | Redis 7（热点问答 + 配置持久化） |
| 检索 | HNSW 稠密 + rank-bm25 + RRF + rerank |
| 编排 | Docker Compose |

---

## 📁 目录结构

```
.
├── backend/                      # FastAPI 后端
│   ├── app/
│   │   ├── main.py               # 入口（lifespan/CORS/health/异常）
│   │   ├── config.py             # .env 配置
│   │   ├── core/                 # 响应/安全(JWT+bcrypt)/日志(loguru)/异常
│   │   ├── db/                   # 异步会话/建表
│   │   ├── models/               # user/document/chunk/conversation/operation_log
│   │   ├── schemas/              # Pydantic 请求/响应
│   │   ├── routers/              # system/document/retrieval/qa
│   │   ├── services/             # 业务编排
│   │   │   ├── document_service  # 上传/解析/向量化(路由)/删除
│   │   │   ├── retrieval_service # 双查+RRF+rerank
│   │   │   ├── qa_service        # 缓存/多轮/prompt/LLM/后处理
│   │   │   ├── bm25/rerank/embedding/conversation/term/config/log
│   │   ├── providers/            # ★ 模型抽象(三家LLM + 云/bge Embedding)
│   │   ├── clients/              # minio/milvus(双collection)/redis
│   │   ├── rag/                  # prompt/rrf/citation/judge
│   │   └── data/grid_terms.json  # 电网术语词表
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/                     # Vue 3 前端
│   ├── src/{views,api,stores,router}
│   ├── Dockerfile + nginx.conf
│   └── package.json
├── scripts/                      # 评测/压测/建库
│   ├── seed_demo.py  eval_retrieval.py  eval_qa.py  benchmark.py
├── tests/                        # pytest（11 用例）
├── docker-compose.yml            # 全栈编排（8 服务）
├── .env.example                  # 配置模板
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
docker compose up -d mysql minio redis milvus   # 先起依赖（首次会拉镜像）
docker compose ps             # 确认 healthy
```

> 端口约定：MySQL 映射 **3307**（避开本机 MySQL）、后端 **8001**（避开占用 8000 的进程）、Milvus 19530、MinIO 9000/9001、Redis 6379。

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
- MinIO 控制台：http://localhost:9001 （minioadmin/minioadmin）

---

## ⚙️ 配置说明（.env）

复制 `.env.example` 为 `.env` 并填入：

| 配置 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` / `ARK_API_KEY` | 三家云 API Key |
| `DOUBAO_LLM_ENDPOINT_ID` | 火山豆包推理接入点 id（`ep-xxxx`，非模型名） |
| `LLM_PROVIDER` | 默认 LLM：`deepseek` / `qwen` / `doubao` |
| `EMB_PROVIDER` | 默认云 Embedding：`qwen` / `doubao` |
| `EMBEDDING_DIM` | 云向量维度，固定 1024 |
| `BGE_MODEL` / `BGE_DIM` | 本地 bge 模型与维度（默认 bge-small-zh-v1.5 / 512） |
| `DOC_SIZE_THRESHOLD` | 文档字数阈值（默认 5000），超过走云 Embedding |
| `RERANK_ENABLE` / `RERANK_MODEL` | 重排开关 / 百炼 gte-rerank-v2 |
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
| GET | `/api/system/logs` | 操作日志（admin 全部 / operator 仅自己，支持时间过滤） |
| POST/GET | `/api/system/config/milvus` | Milvus 索引参数配置（仅 admin，Redis 持久化） |
| POST/GET | `/api/system/config/model` | 模型参数配置（仅 admin） |

### 文档
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/document/upload` | 上传（form-data，PDF/Word/TXT/图片，批量≤5/单≤100M） |
| GET | `/api/document/list` | 文档列表（分页 page/size + 关键字） |
| POST | `/api/document/parse` | 解析分块（数字文档 + OCR + 术语归一化） |
| POST | `/api/document/vector/generate` | 向量化（按文档大小路由云/bge，返回 embeddingRoute） |
| DELETE | `/api/document/delete` | 删除（联动 MinIO + Milvus 双 collection + MySQL） |

### 检索与问答
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/retrieval/mixed` | 混合检索（双 collection + BM25 + RRF + rerank） |
| POST | `/api/qa/answer` | 智能问答（热点缓存 + 多轮上下文 + 引用/安全提示） |
| POST | `/api/qa/answer/stream` | 流式问答（SSE 逐 token） |
| GET | `/api/qa/conversations` | 对话列表 |
| GET | `/api/qa/history` | 对话历史消息 |
| POST | `/api/qa/term/normalize` | 术语归一化 |
| GET | `/health` | 健康检查（探活 MySQL/MinIO/Milvus/Redis） |

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

## 📊 评测结果（6 文档 demo 库）

| 指标 | 结果 | 目标 |
|---|---|---|
| 检索召回率 recall@5 | **100%** (8/8) | ≥92% |
| 单请求检索延迟 | **0.95s** | ≤1.5s |
| 50 并发检索成功率 | **100%** (50/50) | 不崩 |
| LLM-as-judge 幻觉率 | **0%** (6 问) | ≤5% |
| 热点缓存命中 | **6.5s → 0.002s** | — |

评测脚本：
```bash
python scripts/seed_demo.py        # 建 6 文档知识库
python scripts/eval_retrieval.py   # 检索召回
python scripts/eval_qa.py          # LLM-as-judge 幻觉率
python scripts/benchmark.py 50     # 并发压测
```

> 高并发 P50 ~20s 的瓶颈在云 Embedding API 限流（非 Milvus/系统）。小文档走本地 bge 后本地路并发大幅提升；进一步降延迟可全量本地 bge + gunicorn 多 worker。

---

## 🐳 一键部署（Docker Compose）

```bash
cp .env.example .env
docker compose up -d --build      # 8 服务：mysql/minio/redis/etcd/milvus-minio/milvus/backend/frontend
```

容器间用 service name 通信：backend 连 `mysql:3306` / `minio:9000` / `milvus:19530` / `redis:6379`（由 compose `environment` 覆盖 `.env` 中的 localhost，API Key 仍由 `.env` 注入）。

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

### 跑测试
```bash
venv/Scripts/python -m pytest tests/ -v   # 11 用例（含集成）
```

### 看日志
`data/logs/app.log`（loguru，50MB 轮转 / 10 天保留）。

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

---

## 🗺️ 开发进度

**基础链路 S1–S11**（每片做完即验证+推送）
- [x] S1 地基+骨架 · S2 认证 · S3 文档上传 · S4 解析+分块+OCR · S5 Embedding+Milvus
- [x] S6 混合检索 · S7 RAG 问答 · S8 配置+日志 · S9 前端联调 · S10 评测+性能 · S11 镜像化

**优化 O1–O10**
- [x] O1 Redis 热点缓存 · O2 rerank · O3 分块语义 · O4 HNSW · O5 流式 SSE
- [x] O6 多轮对话 · O7 LLM-as-judge · O8 可观测性 · O9 pytest · O10 生产化（分页+配置持久化）

**双 Embedding P1–P3**
- [x] P1 本地 bge · P2 双 collection 文档路由 · P3 检索双查融合

---

## 📄 许可

本项目用于电网运维智能问答学习与内部部署。云模型 API 使用遵循各自服务条款。
