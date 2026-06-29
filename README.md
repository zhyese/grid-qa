# 电网运维 RAG 智能问答系统

自然语言提问 → 混合检索 → 可信答案生成，覆盖**变电 / 配电 / 输电**三大场景。

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | Vue 3 + Vite + Pinia + Axios |
| 后端 | Python + FastAPI + Uvicorn |
| LLM（云 API，可切换） | DeepSeek `deepseek-chat` / 阿里百炼 `qwen-plus` / 火山方舟豆包（endpoint_id） |
| Embedding（云 API，可切换） | 阿里百炼 `text-embedding-v3`(1024维) / 火山方舟 `doubao-embedding-text-240815` |
| 文档解析 | PaddleOCR（扫描件/图片）+ pdfplumber / python-docx（数字文档） |
| 向量库 | Milvus 2.3（IVF_FLAT + COSINE） |
| 对象存储 | MinIO（源文档） |
| 元数据 | MySQL 8（用户/文档/chunks/日志） |
| 缓存 | Redis 7（热点问答缓存） |
| 编排 | Docker Compose |

三家云模型均兼容 OpenAI 协议，统一用 `openai` SDK 对接，配置驱动切换。

## 目录结构

```
.
├── backend/                 # FastAPI 后端
│   ├── app/
│   │   ├── main.py          # 入口
│   │   ├── config.py        # .env 配置
│   │   ├── core/            # 统一响应、安全、异常
│   │   ├── db/ models/ schemas/
│   │   ├── routers/         # 对齐接口文档
│   │   ├── services/        # 业务编排
│   │   ├── providers/       # ★ 模型客户端抽象（三家可切换）
│   │   ├── clients/         # MinIO / Milvus 封装
│   │   └── rag/             # prompt / RRF / 引用
│   └── requirements.txt
├── frontend/                # Vue 3 前端（S9）
├── docker-compose.yml       # 基础设施
├── .env.example             # 配置模板
└── README.md
```

## 快速开始（S1：地基 + 骨架）

```bash
# 1. 启动基础设施（MySQL 映射到 3307，避开本机已装的 MySQL；MinIO 9000/9001）
cp .env.example .env
docker compose up -d mysql minio
docker compose ps          # 确认 mysql(healthy) / minio 运行

# 2. 后端（虚拟环境 + 依赖）
python -m venv venv
source venv/Scripts/activate     # Windows Git Bash
pip install -r backend/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 3. 启动（端口 8001：本机 8000 被 Manager.exe 占用，故固定 8001）
uvicorn app.main:app --reload --host 127.0.0.1 --port 8001 --app-dir backend

# 4. 验证
curl http://localhost:8001/health
# {"code":200,"message":"success","data":{"status":"healthy","version":"0.1.0"}}

# 接口文档：http://localhost:8001/docs
# MinIO 控制台：http://localhost:9001  (minioadmin/minioadmin)

# 5. 前端
npm --prefix frontend install --registry https://registry.npmmirror.com
npm --prefix frontend run dev
# 浏览器访问 http://localhost:5173  (admin / admin123)
```

## 开发切片进度

- [x] **S1** 地基 + 骨架 + `/health`
- [x] **S2** 认证（登录/注册/JWT/操作日志）
- [x] **S3** 文档上传（MinIO + 元数据入库）
- [x] **S4** 解析 + 分块（OCR + 术语归一化）
- [x] **S5** Embedding + Milvus（向量化存储）
- [x] **S6** 混合检索（向量 + BM25 + RRF）
- [x] **S7** RAG 问答（DeepSeek/百炼/火山 + 引用标注）
- [x] **S8** 配置接口 + 日志（角色/时间过滤）
- [x] **S9** 前端联调（Vue3 + Vite + Pinia）
- [x] **S10** 评测 + 性能（召回100% / 单请求0.95s）
- [x] **S11** 镜像化 + 全栈部署

## 评测结果（S10，6 文档 demo 库）

| 指标 | 结果 | 目标 |
|---|---|---|
| 检索召回率 recall@5 | **100%** (8/8) | ≥92% |
| 单请求检索延迟 | **0.95s** | ≤1.5s |
| 50 并发检索成功率 | **100%** (50/50) | 不崩 |

> 高并发下 P50 ~20s，瓶颈在百炼 embedding 云 API 限流（非 Milvus/系统瓶颈）。生产优化方向：本地 embedding 模型 / query 向量缓存 / 提升 API 配额。

评测脚本：`python scripts/seed_demo.py`（建库）→ `python scripts/eval_retrieval.py`（召回）→ `python scripts/benchmark.py 50`（压测）。

## 一键部署（Docker Compose）

```bash
# 1. 准备配置（填入三家云 API Key）
cp .env.example .env

# 2. 一键构建并启动全栈（MySQL/MinIO/Milvus/后端/前端 共 7 个服务）
docker compose up -d --build

# 3. 访问
#   前端:   http://localhost:5173   (admin / admin123)
#   后端:   http://localhost:8001/docs
#   MinIO:  http://localhost:9001
```

容器间用 service name 通信：backend 连 `mysql:3306` / `minio:9000` / `milvus:19530`（由 compose `environment` 覆盖 `.env` 中的 localhost，API Key 等仍由 `.env` 注入）。

## 配置说明

复制 `.env.example` 为 `.env` 并填入三家云服务的 API Key：
- `DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`、`ARK_API_KEY`
- 火山方舟需在控制台创建推理接入点，将 endpoint id 填入 `DOUBAO_LLM_ENDPOINT_ID`
- `LLM_PROVIDER` / `EMB_PROVIDER` 控制当前使用的模型；`EMBEDDING_DIM` 固定 1024

### 双 Embedding 路由（云 + 本地 bge）

- 文档总字数 > `DOC_SIZE_THRESHOLD`（默认 5000）→ **云 embedding**（百炼/火山），入 `grid_chunks`
- 小文档 → **本地 bge**（`BGE_MODEL`，默认 `bge-small-zh-v1.5` 512维，可换 `bge-large-zh-v1.5` 1024维改 `BGE_DIM`），入 `grid_chunks_bge`
- 两套向量空间独立，检索时 query 双路 embedding、双 collection 查询 + RRF 融合（保证各自向量一致性）
- 本地 bge 首次下载模型需访问 HuggingFace：设 `HF_ENDPOINT=https://hf-mirror.com` 或代理或预下到 HF 缓存
- 生产多并发用 gunicorn 多 worker（见下"生产部署"，Windows 用 uvicorn）

## 生产部署（多 worker）

开发用 `uvicorn --reload`（单进程）；Linux/Docker 生产用 gunicorn + uvicorn worker 多进程提并发：

```bash
pip install gunicorn
gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:8001 --app-dir backend
```
> Windows 不支持 gunicorn 的 fork，Windows 上仍用 `uvicorn` 单进程；多 worker 部署请在 Linux/Docker 环境。
