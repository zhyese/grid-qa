"""Milvus 向量库封装（pymilvus 2.4 经典 API：connections + Collection）。

Collection: pk(VARCHAR,64) + embedding(FLOAT_VECTOR,EMBEDDING_DIM) + text(VARCHAR,4096)
           + doc_id(VARCHAR,64) + doc_name(VARCHAR,256) + chunk_idx(INT64)
索引: IVF_FLAT + COSINE。
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
        connections.connect(
            alias="default", host=settings.MILVUS_HOST, port=str(settings.MILVUS_PORT)
        )
        _connected = True


def ensure_collection() -> None:
    _connect()
    name = settings.MILVUS_COLLECTION
    if not utility.has_collection(name):
        fields = [
            FieldSchema("pk", DataType.VARCHAR, is_primary=True, max_length=64),
            FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=settings.EMBEDDING_DIM),
            FieldSchema("text", DataType.VARCHAR, max_length=4096),
            FieldSchema("doc_id", DataType.VARCHAR, max_length=64),
            FieldSchema("doc_name", DataType.VARCHAR, max_length=256),
            FieldSchema("chunk_idx", DataType.INT64),
        ]
        col = Collection(name, CollectionSchema(fields, "电网运维知识分块"), using="default")
        col.create_index(
            "embedding",
            {"index_type": "IVF_FLAT", "metric_type": "COSINE", "params": {"nlist": 1024}},
        )
        print(f"[milvus] 已创建 collection: {name} (dim={settings.EMBEDDING_DIM})")
    Collection(name).load()


def insert_chunks(vectors, texts, doc_ids, doc_names, chunk_idxs) -> int:
    _connect()
    col = Collection(settings.MILVUS_COLLECTION)
    pks = [f"{doc_ids[i]}_{chunk_idxs[i]}" for i in range(len(vectors))]
    col.insert([pks, list(vectors), list(texts), list(doc_ids), list(doc_names), list(chunk_idxs)])
    col.flush()
    return len(vectors)


def search(query_vec, topk: int = 10) -> list[dict]:
    _connect()
    col = Collection(settings.MILVUS_COLLECTION)
    col.load()
    res = col.search(
        [query_vec],
        "embedding",
        param={"metric_type": "COSINE", "params": {"nprobe": 16}},
        limit=topk,
        output_fields=["text", "doc_id", "doc_name", "chunk_idx"],
    )
    out = []
    for hit in res[0]:
        e = hit.entity
        out.append(
            {
                "text": e.get("text"),
                "doc_id": e.get("doc_id"),
                "doc_name": e.get("doc_name"),
                "chunk_idx": e.get("chunk_idx"),
                "score": float(hit.score),
            }
        )
    return out


def delete_by_doc(doc_id: str) -> None:
    _connect()
    Collection(settings.MILVUS_COLLECTION).delete(f'doc_id == "{doc_id}"')


def num_entities() -> int:
    _connect()
    return Collection(settings.MILVUS_COLLECTION).num_entities
