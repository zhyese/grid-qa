"""数据备份与恢复（MySQL 元数据层）。

纯 Python 实现（SHOW CREATE TABLE + SELECT * → .sql 文件），无需宿主 mysqldump 二进制，
backend 容器内自洽。恢复=读取 .sql 逐条执行（DROP→CREATE→INSERT）。

范围说明：仅覆盖 MySQL 元数据（用户/文档/分块/对话/反馈/票据/日志等）。
MinIO 源文档与 Milvus 向量属对象/向量存储，体量大且有独立快照机制，标注「手动/离线」，
不纳入本接口（恢复后若 MinIO/Milvus 未动，文档对象与向量仍可继续访问）。
"""
import datetime
import os

from sqlalchemy import text

from app.core.response import BizError
from app.db.session import engine

BACKUP_DIR = "data/backups"


def _sql_val(v) -> str:
    """单值转 SQL 字面量（NULL/数字/字符串/字节）。"""
    if v is None:
        return "NULL"
    if isinstance(v, bytes):
        return f"0x{v.hex()}"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    return f"'{s}'"


def _safe_filename(filename: str) -> str:
    """防路径穿越：仅允许 backup 目录下的 .sql 文件名。"""
    if not filename or "/" in filename or "\\" in filename or ".." in filename or not filename.endswith(".sql"):
        raise BizError("非法备份文件名", 400)
    path = os.path.join(BACKUP_DIR, filename)
    return path


async def backup_mysql() -> dict:
    """全库 dump → data/backups/mysql_YYYYMMDD_HHMMSS.sql。返回文件名/大小/表数。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"mysql_{ts}.sql"
    path = os.path.join(BACKUP_DIR, filename)
    lines = []
    table_count = 0
    row_count = 0
    async with engine.begin() as conn:
        tables = [r[0] for r in (await conn.execute(text("SHOW TABLES"))).fetchall()]
        lines.append(f"-- grid_qa backup @ {ts}, tables={len(tables)}")
        for t in tables:
            table_count += 1
            create_sql = (await conn.execute(text(f"SHOW CREATE TABLE `{t}`"))).fetchall()[0][1]
            lines.append(f"DROP TABLE IF EXISTS `{t}`;")
            lines.append(create_sql.rstrip(";") + ";")
            rows = (await conn.execute(text(f"SELECT * FROM `{t}`"))).fetchall()
            if not rows:
                continue
            cols = list(rows[0]._mapping.keys())
            col_list = ",".join(f"`{c}`" for c in cols)
            for row in rows:
                vals = ",".join(_sql_val(v) for v in row)
                lines.append(f"INSERT INTO `{t}` ({col_list}) VALUES ({vals});")
                row_count += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return {"filename": filename, "size": os.path.getsize(path),
            "tables": table_count, "rows": row_count}


async def list_backups() -> list:
    """列出全部备份（按时间倒序）。"""
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if not fn.endswith(".sql"):
            continue
        st = os.stat(os.path.join(BACKUP_DIR, fn))
        out.append({
            "filename": fn,
            "size": st.st_size,
            "createdAt": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return out


async def restore_mysql(filename: str) -> dict:
    """从 .sql 恢复：逐条执行（DROP→CREATE→INSERT）。⚠ 覆盖当前数据。"""
    path = _safe_filename(filename)
    if not os.path.isfile(path):
        raise BizError("备份文件不存在", 404)
    with open(path, "r", encoding="utf-8") as f:
        sql = f.read()
    # 按 ; 分号切句，跳过注释行；备份由本模块生成，结构可控
    stmts = []
    for s in sql.split(";\n"):
        s = s.strip()
        if s and not s.startswith("--"):
            stmts.append(s)
    async with engine.begin() as conn:
        for s in stmts:
            await conn.execute(text(s))
    return {"filename": filename, "executed": len(stmts)}


async def delete_backup(filename: str) -> dict:
    """删除一个备份文件。"""
    path = _safe_filename(filename)
    if os.path.isfile(path):
        os.remove(path)
    return {"filename": filename, "deleted": True}
