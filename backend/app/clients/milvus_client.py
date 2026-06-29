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


def _connect():
    global _connected
    if not _connected:
        connections.connect(alias="default", host=settings.MILVUS_HOST, port=str(settings.MILVUS_PORT))
        _connected = True


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
    """确保 云 + bge 双 collection 存在。"""
    _connect()
    _ensure_one(settings.MILVUS_COLLECTION, settings.EMBEDDING_DIM)
    _ensure_one(settings.MILVUS_COLLECTION_BGE, settings.BGE_DIM)


def insert_chunks(collection_name, vectors, texts, doc_ids, doc_names, chunk_idxs) -> int:
    _connect()
    col = Collection(collection_name)
    pks = [f"{doc_ids[i]}_{chunk_idxs[i]}" for i in range(len(vectors))]
    col.insert([pks, list(vectors), list(texts), list(doc_ids), list(doc_names), list(chunk_idxs)])
    col.flush()
    return len(vectors)


def search(collection_name, query_vec, topk: int = 10) -> list[dict]:
    _connect()
    col = Collection(collection_name)
    col.load()
    res = col.search(
        [query_vec], "embedding",
        param={"metric_type": "COSINE", "params": {"ef": 64}},
        limit=topk, output_fields=["text", "doc_id", "doc_name", "chunk_idx"],
    )
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
    """联动删除云 + bge 两个 collection。"""
    _connect()
    for name in (settings.MILVUS_COLLECTION, settings.MILVUS_COLLECTION_BGE):
        if utility.has_collection(name):
            Collection(name).delete(f'doc_id == "{doc_id}"')


def num_entities(collection_name: str | None = None) -> int:
    _connect()
    return Collection(collection_name or settings.MILVUS_COLLECTION).num_entities
