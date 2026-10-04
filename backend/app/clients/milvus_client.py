"""Milvus 双 collection 封装（pymilvus 2.4）。

- grid_chunks：云 embedding（百炼/火山，EMBEDDING_DIM=1024）
- grid_chunks_bge：本地 bge（BGE_DIM，文档小走本地）
两套向量空间独立，检索时双查融合。索引 HNSW + COSINE。
"""
from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)

from app.config import settings

_connected = False
_col_cache: dict = {}   # Collection 复用 + load 一次（避免每次 search 重复 LoadCollection RPC，实测偶发拖到 1.7s）


def document_collections() -> tuple[tuple[str, int], ...]:
    """返回当前部署实际使用的文档 collection 及维度。

    纯 BGE 模式只创建、加载和维护 BGE collection，避免本地演示环境
    访问历史 1024 维云向量空间。
    """
    if settings.EMB_PROVIDER == "bge":
        return ((settings.MILVUS_COLLECTION_BGE, settings.BGE_DIM),)
    return (
        (settings.MILVUS_COLLECTION, settings.EMBEDDING_DIM),
        (settings.MILVUS_COLLECTION_BGE, settings.BGE_DIM),
    )


def primary_document_collection() -> str:
    """返回与当前主 embedding provider 匹配的文档 collection。"""
    if settings.EMB_PROVIDER == "bge":
        return settings.MILVUS_COLLECTION_BGE
    return settings.MILVUS_COLLECTION


def _connect():
    global _connected
    if not _connected:
        connections.connect(alias="default", host=settings.MILVUS_HOST, port=str(settings.MILVUS_PORT))
        _connected = True


def _get_col(name: str):
    """复用 Collection 对象（首次 load 一次）。search 直接用缓存的 col，不再重复 LoadCollection RPC。"""
    col = _col_cache.get(name)
    if col is None:
        col = Collection(name)
        col.load()
        _col_cache[name] = col
    return col


def _run_with_reload(name: str, op):
    """在已 load 的 collection 上执行 op(col)；遇 "collection not loaded" 清缓存重载后重试一次。

    Milvus 重启 / compaction / 内存回收都会 release 集合，此时**写路径（delete/query）
    同样会失败**（不只是 search）。缓存里的 Collection 对象不会自动恢复，
    必须丢弃后重建对象才能触发 load。
    """
    col = _get_col(name)
    try:
        return op(col)
    except Exception as e:
        if "not loaded" not in str(e).lower():
            raise
        _col_cache.pop(name, None)
        col = _get_col(name)
        return op(col)


def _ensure_one(name: str, dim: int) -> None:
    if not utility.has_collection(name):
        fields = [
            FieldSchema(name="pk", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=4096),
            FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="doc_name", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="chunk_idx", dtype=DataType.INT64),
        ]
        col = Collection(name, CollectionSchema(fields, "电网运维知识分块"), using="default")
        col.create_index(
            "embedding",
            {"index_type": "HNSW", "metric_type": "COSINE", "params": {"M": 16, "efConstruction": 200}},
        )
        print(f"[milvus] 已创建 collection: {name} (dim={dim}, HNSW)")
    Collection(name).load()


def ensure_collections() -> None:
    """确保当前部署所需的文档 collection 存在。"""
    _connect()
    for name, dim in document_collections():
        _ensure_one(name, dim)


def insert_chunks(collection_name, vectors, texts, doc_ids, doc_names, chunk_idxs) -> int:
    _connect()
    pks = [f"{doc_ids[i]}_{chunk_idxs[i]}" for i in range(len(vectors))]

    def _op(col):
        col.insert([pks, list(vectors), list(texts), list(doc_ids),
                    list(doc_names), list(chunk_idxs)])
        col.flush()

    _run_with_reload(collection_name, _op)
    return len(vectors)


def _do_search(col, query_vec, topk, ef):
    return col.search(
        [query_vec], "embedding",
        param={"metric_type": "COSINE", "params": {"ef": ef or 64}},
        limit=topk, output_fields=["text", "doc_id", "doc_name", "chunk_idx"],
    )


def search(collection_name, query_vec, topk: int = 10, ef: int | None = None) -> list[dict]:
    _connect()
    # Milvus 可能 release 缓存的 collection（compaction/内存/重启）→ 由 _run_with_reload 重载 + retry
    res = _run_with_reload(collection_name, lambda col: _do_search(col, query_vec, topk, ef))
    out = []
    for hit in res[0]:
        e = hit.entity
        out.append({
            "text": e.get("text"), "doc_id": e.get("doc_id"),
            "doc_name": e.get("doc_name"), "chunk_idx": e.get("chunk_idx"),
            "score": float(hit.score),
        })
    return out


def delete_by_doc(doc_id: str) -> None:
    """从当前部署使用的文档 collection 联动删除。

    注意：Milvus 的 delete 要求集合处于 loaded 状态（与 search 一样会被 release 影响），
    故走 _run_with_reload —— 否则 Milvus 重启后"重建/删除文档"会直接失败。
    """
    _connect()
    for name, _dim in document_collections():
        if utility.has_collection(name):
            _run_with_reload(name, lambda col, e=f'doc_id == "{doc_id}"': col.delete(e))


def count_by_doc(doc_id: str) -> dict[str, int]:
    """按 doc_id 统计各文档 collection 命中实体数（治理 dry-run 候选报告用，不删除）。

    任一 collection 统计失败按 0 计并 degraded——dry-run 只读，绝不阻塞主流程。
    """
    _connect()
    out: dict[str, int] = {}
    for name, _dim in document_collections():
        try:
            if not utility.has_collection(name):
                out[name] = 0
                continue
            rows = _run_with_reload(
                name, lambda col, e=f'doc_id == "{doc_id}"':
                col.query(expr=e, output_fields=["count(*)"]))
            out[name] = int(rows[0]["count(*)"]) if rows else 0
        except Exception as e:
            from app.core.obs import degraded
            degraded(f"milvus_count_by_doc_{name}", e)
            out[name] = 0
    return out


def num_entities(collection_name: str | None = None) -> int:
    _connect()
    return Collection(collection_name or primary_document_collection()).num_entities


# ===== N1 Agent 记忆 collection =====

def ensure_memory_collection() -> None:
    """确保 memory_collection 存在（dim=EMBEDDING_DIM, HNSW+COSINE）。

    与 grid_chunks 不同的是字段集：pk/embedding/text/user_id/scope/entity/category。
    """
    _connect()
    name = settings.MEMORY_COLLECTION
    if not utility.has_collection(name):
        fields = [
            FieldSchema(name="pk", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=settings.EMBEDDING_DIM),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=4096),
            FieldSchema(name="user_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="scope", dtype=DataType.VARCHAR, max_length=16),
            FieldSchema(name="entity", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="category", dtype=DataType.VARCHAR, max_length=32),
        ]
        col = Collection(name, CollectionSchema(fields, "Agent 长期记忆向量"), using="default")
        col.create_index(
            "embedding",
            {"index_type": "HNSW", "metric_type": "COSINE", "params": {"M": 16, "efConstruction": 200}},
        )
        print(f"[milvus] 已创建 collection: {name} (dim={settings.EMBEDDING_DIM}, HNSW, memory)")
    Collection(name).load()


def insert_memory(fact_id: str, embedding: list[float], fact_text: str,
                  user_id: str, scope: str, entity: str, category: str) -> None:
    """向 memory_collection 插入一条记忆向量。"""
    _connect()
    col = Collection(settings.MEMORY_COLLECTION)
    col.insert([[fact_id], [embedding], [fact_text], [user_id], [scope], [entity], [category]])
    col.flush()


def search_memory(query_vec: list[float], user_id: str, topk: int = 5) -> list[dict]:
    """在 memory_collection 中按 user_id 过滤做向量检索。"""
    _connect()
    col = Collection(settings.MEMORY_COLLECTION)
    col.load()
    expr = f'user_id == "{user_id}"'
    res = col.search(
        [query_vec], "embedding",
        param={"metric_type": "COSINE", "params": {"ef": 64}},
        limit=topk, expr=expr,
        output_fields=["text", "user_id", "scope", "entity", "category"],
    )
    out: list[dict] = []
    for hit in res[0]:
        e = hit.entity
        out.append({
            "pk": hit.id,
            "text": e.get("text"),
            "user_id": e.get("user_id"),
            "scope": e.get("scope"),
            "entity": e.get("entity"),
            "category": e.get("category"),
            "score": float(hit.score),
        })
    return out


def delete_memory(fact_id: str) -> None:
    """从 memory_collection 删除一条记忆（按 pk）。"""
    _connect()
    col = Collection(settings.MEMORY_COLLECTION)
    col.delete(f'pk == "{fact_id}"')
    col.flush()
