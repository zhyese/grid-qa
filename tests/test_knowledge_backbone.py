"""共享 AI 文档的块身份与独立父组回归。"""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.chunk import Chunk
from app.models.document import Document
from app.services.knowledge_backbone import persist_chunk


@pytest.fixture
def vector_boundary(monkeypatch):
    from app.clients import milvus_client
    from app.providers import factory

    vectors = {}

    async def embed(texts):
        return [[1.0, 0.0] for _ in texts]

    def insert(collection, embeddings, contents, doc_ids, names, indices):
        for doc_id, idx, content in zip(doc_ids, indices, contents):
            vectors[f"{doc_id}_{idx}"] = content

    monkeypatch.setattr(factory, "get_embedding_provider", lambda provider: SimpleNamespace(embed=embed))
    monkeypatch.setattr(milvus_client, "insert_chunks", insert)
    monkeypatch.setattr(milvus_client, "primary_document_collection", lambda: "test-chunks")
    return vectors


async def _document_with_chunks(db, indices):
    db.add(Document(id="ai-evo-north", doc_name="AI", minio_object="ai", tenant_id="north", chunk_count=len(indices)))
    for idx in indices:
        db.add(Chunk(id=f"c{idx}", doc_id="ai-evo-north", chunk_idx=idx, parent_idx=idx, content=f"草稿{idx}"))
    await db.commit()


@pytest.mark.asyncio
async def test_append_after_middle_deletion_does_not_reuse_surviving_vector_key(test_db, vector_boundary):
    """COUNT 产生 idx=2 会覆盖仍存在的草稿2；MAX+1 应产生3。"""
    await _document_with_chunks(test_db, [0, 2])
    vector_boundary["ai-evo-north_2"] = "草稿2"
    chunk_id = await persist_chunk(test_db, "新草稿", "新草稿", "north")
    await test_db.commit()
    chunk = await test_db.get(Chunk, chunk_id)
    assert chunk.chunk_idx == 3
    assert vector_boundary == {"ai-evo-north_2": "草稿2", "ai-evo-north_3": "新草稿"}


@pytest.mark.asyncio
async def test_new_draft_has_its_own_parent_group(test_db, vector_boundary):
    await _document_with_chunks(test_db, [0, 1])
    chunk_id = await persist_chunk(test_db, "独立草稿", "独立草稿", "north")
    await test_db.commit()
    chunk = await test_db.get(Chunk, chunk_id)
    siblings = (await test_db.execute(select(Chunk.content).where(
        Chunk.doc_id == chunk.doc_id, Chunk.parent_idx == chunk.parent_idx,
    ))).scalars().all()
    assert siblings == ["独立草稿"]


@pytest.mark.asyncio
async def test_chunk_count_tracks_live_rows_not_max_index(test_db, vector_boundary):
    await _document_with_chunks(test_db, [0, 4])
    await persist_chunk(test_db, "新草稿", "新草稿", "north")
    await test_db.commit()
    assert (await test_db.get(Document, "ai-evo-north")).chunk_count == 3


@pytest.mark.asyncio
async def test_allocation_uses_document_lock_and_current_chunk_read(test_db, vector_boundary, monkeypatch):
    """MySQL可重复读事务必须先锁文档，再用当前读分配编号，避免等锁后读旧快照。"""
    from sqlalchemy.dialects import mysql

    await _document_with_chunks(test_db, [0, 2])
    statements = []
    execute = test_db.execute

    async def record(statement, *args, **kwargs):
        statements.append(str(statement.compile(dialect=mysql.dialect())))
        return await execute(statement, *args, **kwargs)

    monkeypatch.setattr(test_db, "execute", record)
    await persist_chunk(test_db, "新草稿", "新草稿", "north")
    doc_locks = [i for i, sql in enumerate(statements) if "FROM documents" in sql and "FOR UPDATE" in sql]
    chunk_reads = [i for i, sql in enumerate(statements) if "FROM chunks" in sql]
    assert doc_locks and chunk_reads
    assert doc_locks[0] < chunk_reads[0]
    assert all("FOR UPDATE" in statements[i] for i in chunk_reads)
