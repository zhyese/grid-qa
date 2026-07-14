"""数据备份与恢复（三合一：MySQL + Redis + Milvus）。

纯 Python 实现，backend 容器内自洽（无宿主 mysqldump/redis-cli/milvus-backup 二进制依赖）：
- MySQL: SHOW CREATE TABLE + SELECT * → .sql（DROP→CREATE→INSERT 恢复）
- Redis: SCAN 全 key + DUMP(含 PTTL) → .json（RESTORE 恢复；全量逻辑备份，等同全量快照）
- Milvus: query_iterator 导出两 collection 全量向量(base64) → .json（drop+ensure+insert 恢复）

一键：backup_all() 产 3 文件 + manifest_{ts}.json（元信息）；restore_all(ts) 一键恢复。
定时：backup_all_loop() 每 3h 自动全量备份（main.py 启动）。

⚠️ Redis 全量逻辑备份说明：dump.rdb 文件在 grid-redis 容器内、backend 拿不到（除非共享卷），
故用 SCAN+DUMP/RESTORE 等价全量（所有 key 含缓存都备/恢复），不动 docker 架构。
"""
import base64
import datetime
import json
import os

from sqlalchemy import text

from app.core.response import BizError
from app.db.session import engine

BACKUP_DIR = "data/backups"


# ========== 通用 helper ==========

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
    """防路径穿越：仅允许 backup 目录下的 .sql/.json 文件名（排除 manifest_ 前缀）。"""
    if (not filename or "/" in filename or "\\" in filename or ".." in filename
            or filename.startswith("manifest_")
            or not (filename.endswith(".sql") or filename.endswith(".json"))):
        raise BizError("非法备份文件名", 400)
    return os.path.join(BACKUP_DIR, filename)


def _ts() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


# ========== MySQL ==========

async def backup_mysql() -> dict:
    """全库 dump → mysql_{ts}.sql。返回文件名/大小/表数/行数。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = _ts()
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


async def restore_mysql(filename: str) -> dict:
    """从 .sql 恢复：逐条执行（DROP→CREATE→INSERT）。⚠ 覆盖当前数据。"""
    path = _safe_filename(filename)
    if not os.path.isfile(path):
        raise BizError("备份文件不存在", 404)
    with open(path, "r", encoding="utf-8") as f:
        sql = f.read()
    stmts = []
    for s in sql.split(";\n"):
        s = s.strip()
        if s and not s.startswith("--"):
            stmts.append(s)
    async with engine.begin() as conn:
        for s in stmts:
            await conn.execute(text(s))
    return {"filename": filename, "executed": len(stmts)}


# ========== Redis（全量逻辑备份 SCAN+DUMP/RESTORE）==========

async def backup_redis() -> dict:
    """SCAN 全 key + DUMP(含 PTTL) → redis_{ts}.json。返回文件名/大小/key 数。"""
    from app.clients.redis_client import get_redis
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = _ts()
    filename = f"redis_{ts}.json"
    path = os.path.join(BACKUP_DIR, filename)
    r = get_redis()
    dump: dict = {}
    async for k in r.scan_iter(match="*", count=200):
        try:
            ttl = await r.pttl(k)  # ms；-1=永久，-2=已过期不存在
            data = await r.dump(k)
            if data is not None:
                dump[k] = {"dump": base64.b64encode(data).decode(), "ttl": ttl}
        except Exception:
            pass
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": ts, "keys": dump}, f, ensure_ascii=False)
    return {"filename": filename, "size": os.path.getsize(path), "keys": len(dump)}


async def restore_redis(filename: str) -> dict:
    """从 redis_{ts}.json 恢复：逐 key RESTORE（replace=True 覆盖）。⚠ 覆盖当前 key。"""
    from app.clients.redis_client import get_redis
    path = _safe_filename(filename)
    if not os.path.isfile(path):
        raise BizError("备份文件不存在", 404)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    r = get_redis()
    restored = 0
    for k, v in data.get("keys", {}).items():
        try:
            raw = base64.b64decode(v["dump"])
            ttl = v.get("ttl", -1)
            ms = 0 if (ttl == -1 or ttl is None or ttl < 0) else int(ttl)  # RESTORE ttl=0=永久
            await r.restore(k, ms, raw, replace=True)
            restored += 1
        except Exception:
            pass
    return {"filename": filename, "restored": restored}


# ========== Milvus（向量全量导出/恢复）==========

def _dump_milvus_sync() -> dict:
    """同步：遍历两 collection 全量 entity（query_iterator），embedding→base64 紧凑。"""
    import numpy as np
    from pymilvus import Collection
    from app.config import settings
    from app.clients import milvus_client
    milvus_client.ensure_collections()  # 连接(default alias) + load 两 collection，复用项目连接
    out: dict = {}
    for cname in [settings.MILVUS_COLLECTION, settings.MILVUS_COLLECTION_BGE]:
        col = Collection(cname)
        fields = [f.name for f in col.schema.fields]
        ents: list = []
        # query_iterator 全量遍历（pymilvus 2.4，PK 自动分页）
        itr = col.query_iterator(output_fields=fields, batch_size=1000)
        while True:
            batch = itr.next()
            if not batch:
                break
            ents.extend(batch)
        try:
            itr.close()
        except Exception:
            pass
        for e in ents:
            emb = e.get("embedding")
            if isinstance(emb, list):
                e["embedding"] = base64.b64encode(
                    np.asarray(emb, dtype=np.float32).tobytes()).decode()
        out[cname] = {"fields": fields, "count": len(ents), "entities": ents}
        try:
            col.release()
        except Exception:
            pass
    return out


async def backup_milvus() -> dict:
    """两 collection 全量向量 → milvus_{ts}.json。返回文件名/大小/向量总数。"""
    import asyncio
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = _ts()
    filename = f"milvus_{ts}.json"
    path = os.path.join(BACKUP_DIR, filename)
    data = await asyncio.to_thread(_dump_milvus_sync)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": ts, "collections": data}, f, ensure_ascii=False)
    total = sum(c["count"] for c in data.values())
    return {"filename": filename, "size": os.path.getsize(path), "vectors": total}


def _restore_milvus_sync(data: dict) -> dict:
    """同步：drop 两 collection → ensure_collections 重建空 schema → insert_chunks 灌回。"""
    import numpy as np
    from pymilvus import Collection
    from app.clients import milvus_client
    milvus_client.ensure_collections()  # 重建空 collection（含 schema + HNSW 索引）
    restored = 0
    for cname, cdata in data.get("collections", {}).items():
        col = Collection(cname)
        col.load()
        texts, vecs, doc_ids, doc_names, chunk_idxs = [], [], [], [], []
        for e in cdata.get("entities", []):
            emb = e.get("embedding")
            if isinstance(emb, str):
                emb = np.frombuffer(base64.b64decode(emb), dtype=np.float32).tolist()
            vecs.append(emb)
            texts.append(e.get("text", ""))
            doc_ids.append(e.get("doc_id", ""))
            doc_names.append(e.get("doc_name", ""))
            chunk_idxs.append(e.get("chunk_idx", 0))
        if vecs:
            milvus_client.insert_chunks(cname, vecs, texts, doc_ids, doc_names, chunk_idxs)
            restored += len(vecs)
        try:
            col.release()
        except Exception:
            pass
    return {"vectors": restored}


async def restore_milvus(filename: str) -> dict:
    """从 milvus_{ts}.json 恢复：drop 现有两 collection → 重建 → 灌回向量。⚠ 覆盖。"""
    import asyncio
    from pymilvus import utility
    from app.config import settings
    path = _safe_filename(filename)
    if not os.path.isfile(path):
        raise BizError("备份文件不存在", 404)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    def _drop():
        from app.clients import milvus_client
        milvus_client._connect()  # 确保 default 连接（utility.has_collection/drop 需要）
        for cname in [settings.MILVUS_COLLECTION, settings.MILVUS_COLLECTION_BGE]:
            try:
                if utility.has_collection(cname):
                    utility.drop_collection(cname)
            except Exception:
                pass
    await asyncio.to_thread(_drop)
    res = await asyncio.to_thread(_restore_milvus_sync, data)
    res["filename"] = filename
    return res


# ========== 一键备份/恢复（manifest 元信息）==========

async def backup_all() -> dict:
    """三合一备份：MySQL + Redis + Milvus → 3 文件 + manifest_{ts}.json 元信息。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = _ts()
    m = await backup_mysql()
    r = await backup_redis()
    v = await backup_milvus()
    manifest = {
        "ts": ts,
        "createdAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "files": {"mysql": m["filename"], "redis": r["filename"], "milvus": v["filename"]},
        "meta": {
            "mysqlTables": m["tables"], "mysqlRows": m["rows"],
            "redisKeys": r["keys"], "milvusVectors": v["vectors"],
        },
        "totalSize": m["size"] + r["size"] + v["size"],
    }
    with open(os.path.join(BACKUP_DIR, f"manifest_{ts}.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)
    return manifest


async def restore_all(ts: str) -> dict:
    """一键恢复：读 manifest_{ts}.json → 恢复 MySQL+Redis+Milvus。⚠ 全量覆盖。"""
    mp = os.path.join(BACKUP_DIR, f"manifest_{ts}.json")
    if not os.path.isfile(mp):
        raise BizError("备份不存在", 404)
    with open(mp, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    files = manifest.get("files", {})
    res: dict = {}
    # 顺序：Milvus 先（drop 重建最重）→ MySQL（元数据）→ Redis（缓存/配置）
    for key, fn, fn_restore in [
        ("milvus", files.get("milvus"), restore_milvus),
        ("mysql", files.get("mysql"), restore_mysql),
        ("redis", files.get("redis"), restore_redis),
    ]:
        if not fn:
            res[key] = {"skipped": True}
            continue
        try:
            res[key] = await fn_restore(fn)
        except Exception as e:
            res[key] = {"error": f"{type(e).__name__}: {e}"}
    res["ts"] = ts
    return res


async def list_backups() -> list:
    """列出全部备份（读 manifest_*.json，按 ts 倒序，含元信息供前端展示）。"""
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if not (fn.startswith("manifest_") and fn.endswith(".json")):
            continue
        try:
            with open(os.path.join(BACKUP_DIR, fn), "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception:
            pass
    return out


async def delete_backup_all(ts: str) -> dict:
    """删除一次备份（manifest + 3 数据文件）。"""
    mp = os.path.join(BACKUP_DIR, f"manifest_{ts}.json")
    if not os.path.isfile(mp):
        raise BizError("备份不存在", 404)
    with open(mp, "r", encoding="utf-8") as f:
        m = json.load(f)
    deleted = []
    for fn in m.get("files", {}).values():
        p = os.path.join(BACKUP_DIR, fn)
        if os.path.isfile(p):
            os.remove(p)
            deleted.append(fn)
    os.remove(mp)
    return {"ts": ts, "deleted": deleted}


async def delete_backup(filename: str) -> dict:
    """删除单个数据文件（兼容旧接口）。"""
    path = _safe_filename(filename)
    if os.path.isfile(path):
        os.remove(path)
    return {"filename": filename, "deleted": True}


async def backup_all_loop(interval_hours: int = 3):
    """定时全量备份（main.py lifespan 启动）。首次延迟 60s 避启动峰值，之后每 interval_hours。"""
    import asyncio
    from app.core.obs import degraded
    await asyncio.sleep(60)
    while True:
        try:
            m = await backup_all()
            print(f"[backup] 定时全量备份完成 ts={m['ts']} "
                  f"mysql={m['meta']['mysqlRows']}行 redis={m['meta']['redisKeys']}key "
                  f"milvus={m['meta']['milvusVectors']}向量")
        except Exception as e:
            degraded("backup_schedule", e)
        await asyncio.sleep(interval_hours * 3600)
