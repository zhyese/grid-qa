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
```

## 开发切片进度

- [x] **S1** 地基 + 骨架 + `/health`
- [x] **S2** 认证（登录/注册/JWT/操作日志）
- [x] **S3** 文档上传（MinIO + 元数据入库）
- [x] **S4** 解析 + 分块（OCR + 术语归一化）
- [x] **S5** Embedding + Milvus（向量化存储）
- [x] **S6** 混合检索（向量 + BM25 + RRF）
- [ ] S7 RAG 问答
- [ ] S8 配置 + 日志
- [ ] S9 前端联调
- [ ] S10 评测 + 性能
- [ ] S11 镜像化 + 全栈部署

## 配置说明

复制 `.env.example` 为 `.env` 并填入三家云服务的 API Key：
- `DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`、`ARK_API_KEY`
- 火山方舟需在控制台创建推理接入点，将 endpoint id 填入 `DOUBAO_LLM_ENDPOINT_ID`
- `LLM_PROVIDER` / `EMB_PROVIDER` 控制当前使用的模型；`EMBEDDING_DIM` 固定 1024
