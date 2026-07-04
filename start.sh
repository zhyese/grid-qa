#!/bin/bash
# ============================================================
# 电网运维 RAG 系统 — Docker 数据挂载 + 一键启动脚本
#
# 功能:
#   start       启动全部服务（含 MySQL 数据卷导入 + bind mount）
#   stop        停止全部服务
#   restart     重启全部服务
#   status      查看服务状态
#   logs <svc>  查看指定服务日志
#   import-mysql  从 data/mysql/ 导出数据到 Docker 命名卷
#   export-mysql  从 Docker 命名卷导出数据到 data/mysql/
#
# 用法: ./start.sh [start|stop|restart|status|logs|import-mysql|export-mysql]
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$SCRIPT_DIR/data"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

export COMPOSE_FILE

# 颜色
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ---- 确保本地 data/ 目录结构完整 ----
ensure_data_dirs() {
    info "检查 data/ 目录结构..."
    local dirs=(mysql minio milvus-minio etcd neo4j redis nacos prometheus grafana)
    for d in "${dirs[@]}"; do
        if [ ! -d "$DATA_DIR/$d" ]; then
            warn "创建 $DATA_DIR/$d"
            mkdir -p "$DATA_DIR/$d"
        fi
    done
    info "data/ 目录就绪"
}

# ---- 启动服务 ----
do_start() {
    ensure_data_dirs
    info "拉取基础镜像..."
    docker compose pull mysql minio etcd milvus-minio milvus redis neo4j nacos 2>/dev/null || true
    info "构建并启动全部服务（含 bind mount 数据目录）..."
    docker compose up -d --build
    echo ""
    info "等待服务就绪..."
    sleep 5
    do_status
    echo ""
    info "前端地址: http://localhost:5173"
    info "Grafana:   http://localhost:3000 (admin/admin)"
    info "MinIO:     http://localhost:9001 (minioadmin/minioadmin)"
    echo ""
    info "首次启动提示:"
    echo "  - 如 MySQL 启动失败，执行: ./start.sh import-mysql"
    echo "  - 如后端持续重启，执行: docker logs grid-backend --tail 30"
}

# ---- 停止服务 ----
do_stop() {
    info "停止全部服务..."
    docker compose down
    info "服务已停止（数据卷保留）"
}

# ---- 重启 ----
do_restart() {
    do_stop
    sleep 2
    do_start
}

# ---- 状态 ----
do_status() {
    echo ""
    echo "========== 服务状态 =========="
    docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" --filter "name=grid-"
    echo ""
    echo "========== 数据挂载 =========="
    for svc in redis minio etcd milvus-minio neo4j nacos prometheus grafana mysql; do
        local container="grid-$svc"
        if docker ps --format '{{.Names}}' --filter "name=$container" | grep -q . 2>/dev/null; then
            local mount=$(docker inspect "$container" --format '{{range .Mounts}}{{.Type}}:{{.Source}} {{end}}' 2>/dev/null | tr '\n' ' ')
            printf "  %-18s  %s\n" "$container" "${mount:-(无挂载)}"
        fi
    done
}

# ---- 日志 ----
do_logs() {
    local svc="${1:-backend}"
    docker compose logs -f --tail=50 "$svc"
}

# ---- MySQL 数据导入（从本地 data/mysql → Docker 命名卷） ----
do_import_mysql() {
    local vol_name="rag_mysql-data"
    local src="$DATA_DIR/mysql"
    local running=""

    # 检查源数据
    if [ ! -d "$src" ] || [ -z "$(ls -A "$src" 2>/dev/null)" ]; then
        error "data/mysql/ 目录为空，无可导入数据"
        exit 1
    fi

    # 找运行中的 MySQL 容器
    if docker ps --format '{{.Names}}' --filter "name=grid-mysql" | grep -q . 2>/dev/null; then
        running="true"
    fi

    if [ "$running" = "true" ]; then
        warn "MySQL 容器正在运行，先停止..."
        docker compose stop mysql
    fi

    # 检查命名卷
    if ! docker volume ls --format '{{.Name}}' | grep -q "^${vol_name}$" 2>/dev/null; then
        info "创建命名卷 $vol_name"
        docker volume create "$vol_name"
    fi

    # 用临时 alpine 容器拷贝数据到命名卷
    info "导入 data/mysql/ → Docker 命名卷 $vol_name ..."
    docker run --rm \
        -v "$vol_name":/target \
        -v "$(cygpath -w "$src" 2>/dev/null || echo "$src")":/source:ro \
        alpine:latest \
        sh -c "cp -a /source/. /target/ 2>/dev/null || cp -a /source/* /target/"

    info "MySQL 数据导入完成"
    info "执行 ./start.sh start 启动服务"
}

# ---- MySQL 数据导出（从 Docker 命名卷 → 本地 data/mysql） ----
do_export_mysql() {
    local vol_name="rag_mysql-data"
    local dst="$DATA_DIR/mysql"

    if ! docker volume ls --format '{{.Name}}' | grep -q "^${vol_name}$" 2>/dev/null; then
        error "命名卷 $vol_name 不存在"
        exit 1
    fi

    mkdir -p "$dst"
    info "导出 Docker 命名卷 $vol_name → data/mysql/ ..."
    docker run --rm \
        -v "$vol_name":/source:ro \
        -v "$(cygpath -w "$dst" 2>/dev/null || echo "$dst")":/target \
        alpine:latest \
        sh -c "cp -a /source/. /target/ 2>/dev/null || cp -a /source/* /target/"

    info "MySQL 数据导出完成: $dst"
}

# ---- 入口 ----
case "${1:-start}" in
    start)         do_start ;;
    stop)          do_stop ;;
    restart)       do_restart ;;
    status)        do_status ;;
    logs)          do_logs "${2:-backend}" ;;
    import-mysql)  do_import_mysql ;;
    export-mysql)  do_export_mysql ;;
    *)
        echo "用法: $0 {start|stop|restart|status|logs [svc]|import-mysql|export-mysql}"
        echo ""
        echo "  start         启动全部服务（默认）"
        echo "  stop          停止全部服务"
        echo "  restart       重启全部服务"
        echo "  status        查看服务状态 + 数据挂载"
        echo "  logs <svc>    查看指定服务日志（默认 backend）"
        echo "  import-mysql  从 data/mysql/ 导入到 Docker 命名卷"
        echo "  export-mysql  从 Docker 命名卷导出到 data/mysql/"
        exit 1
        ;;
esac
