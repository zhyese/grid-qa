---
name: rebuild-after-change
description: 改了项目代码/配置后，判断该 rebuild 哪个镜像、哪些走 HMR、.env 改动要不要重建容器。避免"改了没生效"的最常见坑。
---

# 改代码后 rebuild 规则

## 核心前提
**后端源码 bake 进 Docker 镜像（无 bind mount）**——改了源码，运行中的容器还是旧代码，必须 rebuild 镜像才生效。前端相反（Vite dev server HMR，改完即生效）。

## 决策表

| 改了什么 | 怎么生效 | 命令 |
|---|---|---|
| `backend/app/**/*.py`（后端源码）| **rebuild backend 镜像** | `docker compose up -d --build backend` |
| `.env`（环境变量）| **重建 backend 容器**（env 进容器）| `docker compose up -d backend` |
| `docker-compose.yml`（服务配置，如 redis maxmemory）| 重建对应容器 | `docker compose up -d <service>` |
| `frontend/src/**/*.vue` 或 `.js` | **Vite HMR 自动热更新**，不用 rebuild | 浏览器刷新即可 |
| `backend/requirements.txt`（依赖）| rebuild backend（重装依赖层）| `docker compose up -d --build backend` |
| `frontend/package.json`（依赖）| rebuild frontend / npm install | `docker compose up -d --build frontend` 或本地 `npm i` |
| Alembic 迁移文件 | 跑迁移 | `docker exec grid-backend alembic upgrade head`（或 create_all 兜底）|

## 验证生效
```bash
# 后端：rebuild 后等启动（bge 预热慢，sleep 12-20s），curl health
docker compose up -d --build backend
for i in 1 2 3 4 5 6 7 8; do
  H=$(curl -s -m 5 http://localhost:8001/health 2>/dev/null|head -c10)
  [ -n "$H" ] && { echo ready; break; }; sleep 6
done

# 前端：curl 编译输出确认无错 + 含新代码
curl -s http://localhost:5173/src/views/Admin.vue | grep -c "<新函数名>"
curl -s http://localhost:5173/src/views/Admin.vue | grep -ci "syntaxerror"
```

## 常见坑
1. **改了后端没 rebuild** → "代码没生效"。必须 `--build`。
2. **改了 .env 没重建容器** → env 没进容器。`up -d`（不带 --build 也行，重建容器重读 env）。
3. **rebuild 后 health 空** → bge 预热慢，sleep 不够。循环等 ready。
4. **前端 dev server 没起** → 5173 不通。本地 `cd frontend && npm run dev`（不是容器）。
5. **改 compose 的 redis command** → `up -d redis` 重建，但会清空内存（AOF 重载）；或 `redis-cli CONFIG SET` 立即生效不丢缓存。

## 端口速查
8001 backend / 5173 frontend(dev) / 3307 mysql / 6379 redis / 3000 grafana / 9090 prometheus / 7474+7687 neo4j
