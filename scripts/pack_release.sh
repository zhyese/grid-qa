#!/usr/bin/env bash
# ============================================================================
# 电网运维 RAG 系统 — 发行包打包脚本
#
# 在开发机(Windows Git Bash 或 Linux)上运行，产出自包含发行包：
#   grid-qa-release-<ver>.tar.gz
#
# 包内含：源码 + MySQL 逻辑 dump + Milvus/Neo4j/etcd/MinIO 数据 + bge 模型缓存
# 接收方解压 → cp .env.template .env 填 Key → ./install.sh up 即用。
#
# 用法:
#   bash scripts/pack_release.sh            # 全量(含 neo4j 图谱 + grafana 插件)
#   bash scripts/pack_release.sh --slim     # 瘦身:跳过 neo4j 与 grafana data(约省 560MB)
#
# 前置: docker compose 全栈已在跑(grid-mysql / grid-backend 等容器 Up)。
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

# ---- 颜色 ----
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }
step()  { echo -e "\n${BLUE}==== $* ====${NC}"; }

# ---- 参数 ----
SLIM=0
for a in "$@"; do
  case "$a" in
    --slim) SLIM=1; info "启用瘦身模式: 跳过 neo4j 与 grafana data";;
    -h|--help) sed -n '2,20p' "$0"; exit 0;;
    *) err "未知参数: $a"; exit 1;;
  esac
done

# ---- 版本号 ----
VER="$(git describe --tags --always 2>/dev/null || echo "")"
if [ -z "$VER" ]; then VER="$(date +%Y%m%d)"; fi
PKG="grid-qa-release-$VER"
REL="$REPO_DIR/release/$PKG"

info "仓库目录: $REPO_DIR"
info "发行版本: $VER"
info "暂存目录: $REL"

# ---- 前置检查 ----
step "1/6 前置检查"
command -v docker >/dev/null || { err "未找到 docker"; exit 1; }
docker ps --filter "name=grid-mysql" --format '{{.Names}}' | grep -q . \
  || { err "grid-mysql 容器未运行，请先 docker compose up -d"; exit 1; }
docker ps --filter "name=grid-backend" --format '{{.Names}}' | grep -q . \
  || { err "grid-backend 容器未运行"; exit 1; }
info "容器就绪"

# ---- 清理旧的暂存 ----
rm -rf "$REPO_DIR/release"
mkdir -p "$REL/data" "$REL/docker-entrypoint-initdb.d"

# ---- 2. MySQL 逻辑 dump ----
step "2/6 导出 MySQL → grid_qa.sql"
# 不带 --databases: 避免 initdb 时 CREATE DATABASE 与 MYSQL_DATABASE 冲突
# 库由 compose 的 MYSQL_DATABASE 预建，dump 只含 DROP/CREATE TABLE + INSERT，与 init_db.create_all 幂等
# 用 MYSQL_USER/MYSQL_PASSWORD（非 root）：root 密码仅首次建库时由 env 设,之后随数据卷漂移不可靠;
#   MYSQL_USER 对 MYSQL_DATABASE 有 ALL 权限,后端亦用此连,稳定可用。
docker exec grid-mysql sh -c \
  'exec mysqldump -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" \
     --single-transaction --routines --triggers --default-character-set=utf8mb4 \
     --no-tablespaces "$MYSQL_DATABASE"' \
  > "$REL/docker-entrypoint-initdb.d/grid_qa.sql" 2>/dev/null
SQL_LINES=$(wc -l < "$REL/docker-entrypoint-initdb.d/grid_qa.sql")
SQL_SIZE=$(du -h "$REL/docker-entrypoint-initdb.d/grid_qa.sql" | cut -f1)
grep -q "CREATE TABLE \`documents\`" "$REL/docker-entrypoint-initdb.d/grid_qa.sql" \
  || { err "dump 缺少 documents 表，异常"; exit 1; }
info "grid_qa.sql: ${SQL_LINES} 行, ${SQL_SIZE}"

# ---- 3. 模型缓存(bge) ----
step "3/6 导出模型缓存 → data/hf-cache"
# 先触发 bge 加载(已缓存则秒返),保证模型落盘
info "预热 bge 模型(若已缓存则秒返)..."
docker exec grid-backend python -c \
  "from app.providers.embedding.bge_embedding import _get_model; _get_model()" >/dev/null 2>&1 || \
  warn "bge 预热失败(可能首次未下载),继续导出现有缓存"
docker cp grid-backend:/root/.cache "$REL/data/hf-cache" 2>/dev/null
if [ -d "$REL/data/hf-cache/huggingface/hub" ]; then
  info "HF 缓存导出: $(ls "$REL/data/hf-cache/huggingface/hub" | wc -l) 个模型"
else
  warn "未找到 HF 缓存目录,接收方首次启动将联网下载 bge"
fi

# ---- 4. 源码(tar-pipe,排除缓存/产物) ----
step "4/6 拷贝源码"
# backend: 排除 __pycache__/.pytest_cache/运行态 dump
mkdir -p "$REL/backend"
tar -C backend \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
  --exclude='data/backups' --exclude='.mypy_cache' \
  -cf - . | tar -C "$REL/backend" -xf -
# frontend: 排除 node_modules/dist(接收方 --build 重建)
mkdir -p "$REL/frontend"
tar -C frontend \
  --exclude='node_modules' --exclude='dist' --exclude='.vite' \
  -cf - . | tar -C "$REL/frontend" -xf -
info "backend/ frontend/ 拷贝完成(已排除 node_modules/dist/__pycache__)"

# ---- 5. 数据卷(bind mount 目录原样拷贝) ----
step "5/6 拷贝数据卷"
copy_data() {  # copy_data <dir> <label>
  local src="data/$1"
  if [ -d "$src" ]; then
    mkdir -p "$REL/data/$1"
    cp -a "$src/." "$REL/data/$1/" 2>/dev/null || cp -r "$src/." "$REL/data/$1/"
    info "$2: $(du -sh "$REL/data/$1" 2>/dev/null | cut -f1)"
  else
    warn "$2: $src 不存在,跳过"
  fi
}
copy_data milvus-minio "Milvus 向量数据"
copy_data etcd        "Milvus 元数据(etcd)"
copy_data minio       "源文档(MinIO)"
if [ "$SLIM" -eq 0 ]; then
  copy_data neo4j    "知识图谱(Neo4j)"
  copy_data grafana  "Grafana 插件/仪表盘数据"
else
  info "瘦身模式: 跳过 neo4j 与 grafana data(接收方无知识图谱/需自建仪表盘)"
fi
# 注: redis(缓存运行态)/prometheus(指标运行态)/nacos(默认不依赖)/mysql(由 sql 重建) 不打包

# ---- 6. 单文件 + 打包 ----
step "6/6 组装配置文件并打包"
cp docker-compose.deploy.yml "$REL/"
cp prometheus.yml "$REL/"
cp -r grafana "$REL/"
cp install.sh "$REL/"
cp .env.template "$REL/"
[ -f 使用手册.md ] && cp 使用手册.md "$REL/"
[ -f README.md ] && cp README.md "$REL/"
# 空目录占位,确保接收方 data/ 结构完整
# prometheus/nacos 虽不打包含运行态数据(compose 仍声明了 bind mount),
# 但必须留空目录占位,否则接收方 bind mount 时 Docker 以 root 自动建目录,
# nacos(uid 1000)/prometheus(nobody) 进程写不进 → 容器启动失败/restarting
mkdir -p "$REL/data/mysql" "$REL/data/redis" "$REL/data/prometheus" "$REL/data/nacos"

# 写一个接收方读我
cat > "$REL/快速开始.md" <<EOF
# 接收方快速开始
1. 确保 docker 与 docker compose v2 已安装
2. cp .env.template .env
3. 编辑 .env,填掉所有 <CHANGE_ME_*> (3 个 API Key 必填,install.sh 会校验)
4. ./install.sh up
5. 访问 前端 http://localhost:5173 (admin / 你设的 ADMIN_PASSWORD)
   Grafana http://localhost:3000 (admin/admin)
详见 使用手册.md「Linux 一键部署」小节。
EOF

# 打包
cd "$REPO_DIR/release"
info "压缩中(可能需 1-3 分钟)..."
tar -czf "$PKG.tar.gz" "$PKG"
# 跨平台 sha256: sha256sum(Linux) / shasum -a 256(macOS) / openssl(兜底)
if command -v sha256sum >/dev/null 2>&1; then
  SHA=$(sha256sum "$PKG.tar.gz" | cut -d' ' -f1)
elif command -v shasum >/dev/null 2>&1; then
  SHA=$(shasum -a 256 "$PKG.tar.gz" | cut -d' ' -f1)
else
  SHA=$(openssl dgst -sha256 "$PKG.tar.gz" | awk '{print $NF}')
fi
SIZE=$(du -h "$PKG.tar.gz" | cut -f1)

echo ""
echo -e "${GREEN}========================================================${NC}"
echo -e "${GREEN} 打包完成${NC}"
echo -e "${GREEN}========================================================${NC}"
echo " 产物:   release/$PKG.tar.gz"
echo " 体积:   $SIZE"
echo " sha256: $SHA"
echo ""
echo " 分发与使用:"
echo "   1) 把 grid-qa-release-$VER.tar.gz 拷给接收方"
echo "   2) 接收方: tar -xzf grid-qa-release-$VER.tar.gz"
echo "   3) cd grid-qa-release-$VER && cp .env.template .env && (填 Key)"
echo "   4) ./install.sh up"
