#!/usr/bin/env bash
# ============================================================================
# 电网运维 RAG 系统 — 接收方 Linux 一键引导脚本
#
# 在发行包解压后的根目录运行。子命令:
#   ./install.sh up         构建并启动全部服务(首次会自动导入 grid_qa.sql)
#   ./install.sh stop       停止
#   ./install.sh restart    重启
#   ./install.sh status     查看状态
#   ./install.sh logs [svc] 查看日志(默认 backend)
#   ./install.sh reset-data 清空 MySQL 数据目录并重建(下次 up 重新导入 sql)
#
# 前置: Docker + Docker Compose v2 已安装。
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
COMPOSE="docker compose -f docker-compose.deploy.yml"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }
step()  { echo -e "\n${BLUE}==== $* ====${NC}"; }

# ---------- 工具 ----------
gen_secret() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32
  else head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}
gen_password() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -base64 18 | tr -d '/+=' | head -c 16
  else head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}
env_get() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2-; }
is_placeholder() { [ -z "${1:-}" ] || echo "$1" | grep -qi '<CHANGE_ME'; }

# ---------- 1. preflight ----------
preflight() {
  step "1/5 环境检查"
  command -v docker >/dev/null 2>&1 || { err "未找到 docker，请先安装 Docker"; exit 1; }
  if ! docker compose version >/dev/null 2>&1; then
    err "未找到 'docker compose' v2 插件(注意是 docker compose 子命令,非旧版 docker-compose)"
    exit 1
  fi
  info "docker / docker compose v2 就绪"
  # 磁盘空间 ≥ 5GB(镜像+数据)
  local free_kb
  free_kb=$(df -k . 2>/dev/null | awk 'NR==2{print $4}')
  if [ -n "${free_kb:-}" ] && [ "$free_kb" -lt 5242880 ] 2>/dev/null; then
    warn "可用磁盘 $((free_kb/1024))MB < 5GB,可能不足以构建镜像+承载数据"
  fi
}

# ---------- 2. .env ----------
ensure_env() {
  step "2/5 检查 .env"
  if [ ! -f .env ]; then
    if [ -f .env.template ]; then cp .env.template .env; info "已从 .env.template 创建 .env"
    else err "缺少 .env 与 .env.template"; exit 1; fi
  fi

  # 自动生成 JWT_SECRET
  if is_placeholder "$(env_get JWT_SECRET)"; then
    local s; s="$(gen_secret)"
    sed -i "s#^JWT_SECRET=.*#JWT_SECRET=$s#" .env
    info "已自动生成 JWT_SECRET"
  fi
  # 自动生成 ADMIN_PASSWORD 并回写
  if is_placeholder "$(env_get ADMIN_PASSWORD)"; then
    local p; p="$(gen_password)"
    sed -i "s#^ADMIN_PASSWORD=.*#ADMIN_PASSWORD=$p#" .env
    warn "已自动生成管理员密码(已写入 .env): ${GREEN}$p${NC}"
  fi

  # 校验 active provider 的 API Key
  local llm emb miss=0
  llm="$(env_get LLM_PROVIDER)"; emb="$(env_get EMB_PROVIDER)"
  check_key() {
    local provider="$1" field=""
    case "$provider" in
      deepseek) field="DEEPSEEK_API_KEY";;
      qwen)     field="DASHSCOPE_API_KEY";;
      doubao)   field="ARK_API_KEY";;
      *) warn "未知 LLM/EMB_PROVIDER=$provider,跳过其 key 校验"; return 0;;
    esac
    if is_placeholder "$(env_get "$field")"; then
      err "$field 未配置(provider=$provider)。请编辑 .env 填入真实 Key。"
      miss=1
    fi
  }
  check_key "$llm"; check_key "$emb"
  if [ "$miss" -ne 0 ]; then
    echo ""
    err "缺少必要 API Key,已终止。请编辑 $SCRIPT_DIR/.env 后重新 ./install.sh up"
    exit 1
  fi
  info "LLM=$llm  EMB=$emb  Key 已就绪"
}

# ---------- 等待 mysql 健康 ----------
wait_mysql() {
  step "等待 MySQL 就绪(首次启动会自动导入 grid_qa.sql,约 30-90s)..."
  local i
  for i in $(seq 1 60); do
    if [ "$(docker inspect grid-mysql --format '{{.State.Health.Status}}' 2>/dev/null)" = "healthy" ]; then
      info "MySQL healthy"
      # 校验 documents 表已导入
      if docker exec grid-mysql sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" grid_qa -N -e "SELECT COUNT(*) FROM documents"' 2>/dev/null | grep -qE '[0-9]+'; then
        local cnt; cnt=$(docker exec grid-mysql sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" grid_qa -N -e "SELECT COUNT(*) FROM documents"' 2>/dev/null)
        info "documents 表已就绪: ${cnt} 行"
        return 0
      fi
    fi
    printf '.'; sleep 3
  done
  warn "MySQL 60*3s 内未确认 documents 表;若首次启动较慢可稍后 ./install.sh status 复查"
}

# ---------- 等待 backend ----------
wait_backend() {
  step "等待 backend /health 200..."
  local i
  for i in $(seq 1 40); do
    if curl -sf http://localhost:8001/health >/dev/null 2>&1; then
      info "backend /health 200"
      return 0
    fi
    printf '.'; sleep 3
  done
  warn "backend 120s 内未响应 /health;执行 docker logs grid-backend --tail 30 排查"
}

# ---------- 同步 admin 密码为 .env 值 ----------
sync_admin_password() {
  local pw; pw="$(env_get ADMIN_PASSWORD)"
  [ -z "$pw" ] && return 0
  info "同步 admin 密码为 .env 中的 ADMIN_PASSWORD(覆盖随包 dump 内的开发密码)..."
  docker exec grid-backend python -c "
import asyncio
from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.models.user import User
from sqlalchemy import update
async def r():
    async with AsyncSessionLocal() as s:
        await s.execute(update(User).where(User.username=='admin').values(password_hash=hash_password('$pw')))
        await s.commit()
asyncio.run(r())
print('[ok] admin password synced')
" 2>/dev/null && return 0
  warn "admin 密码同步失败(非致命);可用开发包内原密码或后台改密"
}

# ---------- up ----------
do_up() {
  preflight
  ensure_env
  # 确保数据目录齐备:prometheus/nacos 不打包数据,首次由容器自建;
  # 此处保证宿主目录存在,避免 Docker bind mount 以 root 建成后容器进程写不进
  mkdir -p data/mysql data/redis data/prometheus data/nacos
  step "3/5 构建并启动(首次 build 拉 pip/npm 依赖,约 5-15 分钟)"
  $COMPOSE up -d --build
  wait_mysql
  wait_backend
  sync_admin_password
  print_access
}

print_access() {
  step "5/5 完成"
  local admin_pw; admin_pw="$(env_get ADMIN_PASSWORD)"
  echo -e "${GREEN}========================================${NC}"
  echo -e "${GREEN} 服务已启动${NC}"
  echo -e "${GREEN}========================================${NC}"
  echo " 前端:     http://localhost:5173"
  echo " 后端API:  http://localhost:8001/health"
  echo " Grafana:  http://localhost:3000   (admin/admin)"
  echo " MinIO控制台: http://localhost:9001 (minioadmin/minioadmin)"
  echo " Neo4j Browser: http://localhost:7474 (neo4j/neo4j123456)"
  echo ""
  echo " 管理员登录: admin / ${admin_pw:-<见.env>}"
  echo -e " ${YELLOW}首次问答会触发 provider 调用,请确保网络可达云 API${NC}"
  echo ""
  echo " 常用: ./install.sh status | logs | stop | restart"
}

# ---------- 其它子命令 ----------
do_stop()    { $COMPOSE down; info "已停止"; }
do_restart() { $COMPOSE restart; info "已重启"; }
do_status()  {
  echo "==== 容器状态 ===="
  docker ps --filter "name=grid-" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
  echo ""
  echo "==== backend 健康 ===="
  curl -sf http://localhost:8001/health 2>/dev/null | head -c 200 || echo "(backend 未响应)"
  echo ""
}
do_logs()    { $COMPOSE logs -f --tail=50 "${1:-backend}"; }
do_reset()   {
  warn "将停止服务并清空 data/mysql(下次 up 重新从 grid_qa.sql 导入)。数据卷 neo4j/milvus 等保留。"
  read -r -p "确认? [y/N] " ans
  [ "$ans" = "y" ] || [ "$ans" = "Y" ] || { info "已取消"; exit 0; }
  $COMPOSE down
  rm -rf data/mysql
  info "已清空 MySQL 数据目录。执行 ./install.sh up 重新初始化"
}

# ---------- 入口 ----------
case "${1:-up}" in
  up)         do_up ;;
  stop)       do_stop ;;
  restart)    do_restart ;;
  status)     do_status ;;
  logs)       do_logs "${2:-backend}" ;;
  reset-data) do_reset ;;
  -h|--help|help|"")
    sed -n '2,18p' "$0"
    echo ""
    echo "子命令: up | stop | restart | status | logs [svc] | reset-data"
    ;;
  *) err "未知命令: $1"; echo "运行 ./install.sh --help 查看用法"; exit 1 ;;
esac
