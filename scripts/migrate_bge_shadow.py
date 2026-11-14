"""只向新集合建立完整 BGE 索引；不删旧集合、不改生产配置/文档状态。

容器：python /app/scripts/migrate_bge_shadow.py --target <新集合> --manifest /app/reports/<报告>.json
切换前停止写入，再加 --verify-only 复核实时源数据与构建快照一致。
"""
import argparse
import asyncio
import hashlib
import json
import math
import re
import sys
from pathlib import Path


def validate_target(target, existing):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,200}', target):
        raise ValueError('invalid target collection')
    if target in existing:
        raise ValueError('target already exists; refusing to overwrite')


def fingerprint(records):
    return hashlib.sha256(json.dumps(records, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def validate_records(records):
    if not records:
        raise ValueError('source is empty')
    seen = set()
    for r in records:
        key = f"{r['doc_id']}_{r['chunk_idx']}"
        if key in seen:
            raise ValueError('duplicate source key: ' + key)
        seen.add(key)
        if not r['content'].strip() or len(r['content'].encode('utf-8')) > 65535:
            raise ValueError('empty/oversized content: ' + key)
        if len(key.encode('utf-8')) > 64 or len(r['doc_name'].encode('utf-8')) > 1024:
            raise ValueError('oversized metadata: ' + key)


def payload(records, vectors):
    return [dict(pk=f"{r['doc_id']}_{r['chunk_idx']}", embedding=v, text=r['content'],
                 doc_id=r['doc_id'], doc_name=r['doc_name'], chunk_idx=r['chunk_idx'])
            for r, v in zip(records, vectors)]


async def build_batches(records, embed, insert, *, dim=512, batch_size=32):
    validate_records(records)
    if batch_size < 1:
        raise ValueError('batch_size must be positive')
    for start in range(0, len(records), batch_size):
        batch = records[start:start + batch_size]
        texts = [(r.get('context_line', '').strip() + '\n' + r['content']).strip()
                 if r.get('context_line', '').strip() else r['content'] for r in batch]
        vectors = await embed(texts)
        if len(vectors) != len(batch):
            raise ValueError('embedding count mismatch')
        for vector in vectors:
            if (len(vector) != dim or not all(math.isfinite(x) for x in vector)
                    or abs(sum(x*x for x in vector) - 1) > .02):
                raise ValueError('invalid embedding vector')
        await insert(payload(batch, vectors))
        print(f'embedded={start + len(batch)}/{len(records)}', flush=True)


def verify_records(source, indexed):
    expected = {f"{r['doc_id']}_{r['chunk_idx']}":
                (r['doc_id'], r['doc_name'], r['chunk_idx'], r['content']) for r in source}
    actual = {r['pk']: (r['doc_id'], r['doc_name'], r['chunk_idx'], r['text']) for r in indexed}
    if len(indexed) != len(expected) or actual != expected:
        raise ValueError('indexed payload differs from source (count/key/content)')


def validate_manifest(report, target, source_fingerprint, signature, dim, physical_dim):
    if (report.get('verified') is not True or report.get('target') != target
            or report.get('sourceFingerprint') != source_fingerprint
            or report.get('signature') != signature or report.get('dim') != dim
            or physical_dim != dim):
        raise ValueError('manifest/source/model/dimension mismatch; do not cut over')


async def read_source():
    from sqlalchemy import select
    from app.db.session import AsyncSessionLocal
    from app.models.chunk import Chunk
    from app.models.document import Document
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Chunk.id, Chunk.doc_id, Document.doc_name, Chunk.chunk_idx,
                                       Chunk.content, Chunk.context_line, Document.tenant_id)
                                .join(Document, Document.id == Chunk.doc_id)
                                .order_by(Chunk.doc_id, Chunk.chunk_idx))).all()
    return [dict(chunk_id=r[0], doc_id=r[1], doc_name=r[2], chunk_idx=r[3], content=r[4],
                 context_line=r[5] or '', tenant_id=r[6]) for r in rows]


def read_index(collection):
    iterator = collection.query_iterator(expr='pk != ""', batch_size=512,
                                        output_fields=['pk', 'doc_id', 'doc_name', 'chunk_idx', 'text'],
                                        consistency_level='Strong')
    rows = []
    try:
        while True:
            batch = iterator.next()
            if not batch:
                return rows
            rows.extend(batch)
    finally:
        iterator.close()


async def run(args):
    from pymilvus import connections, utility, Collection, CollectionSchema, FieldSchema, DataType
    from app.config import settings
    from app.db.session import engine
    from app.providers.embedding.bge_embedding import BgeEmbedding
    import torch
    torch.set_num_threads(args.threads)
    connections.connect(host=settings.MILVUS_HOST, port=settings.MILVUS_PORT)
    records = await read_source()
    validate_records(records)
    manifest = Path(args.manifest)
    existing = set(utility.list_collections())
    if args.verify_only:
        report = json.loads(manifest.read_text(encoding='utf-8'))
        col = Collection(args.target)
        physical_dim = next(f.params.get('dim') for f in col.schema.fields if f.name == 'embedding')
        provider = BgeEmbedding()
        validate_manifest(report, args.target, fingerprint(records), provider.model_signature,
                          provider.dim, physical_dim)
        col.load()
        verify_records(records, read_index(col))
        print(json.dumps({'verified': True, 'target': args.target, 'rows': len(records)}))
        await engine.dispose()
        return
    validate_target(args.target, existing)
    if manifest.exists():
        raise ValueError('manifest already exists; choose a new run')
    preserved = {n: Collection(n).num_entities for n in existing}
    fields = [FieldSchema(name='pk', dtype=DataType.VARCHAR, is_primary=True, max_length=64),
              FieldSchema(name='embedding', dtype=DataType.FLOAT_VECTOR, dim=settings.BGE_DIM),
              FieldSchema(name='text', dtype=DataType.VARCHAR, max_length=65535),
              FieldSchema(name='doc_id', dtype=DataType.VARCHAR, max_length=64),
              FieldSchema(name='doc_name', dtype=DataType.VARCHAR, max_length=1024),
              FieldSchema(name='chunk_idx', dtype=DataType.INT64)]
    col = Collection(args.target, CollectionSchema(fields, 'Full BGE migration, source MySQL chunks'))
    provider = BgeEmbedding()
    async def insert(batch):
        await asyncio.to_thread(col.insert, batch)
    await build_batches(records, provider.embed, insert, dim=provider.dim, batch_size=args.batch_size)
    col.flush()
    col.create_index('embedding', {'index_type': 'HNSW', 'metric_type': 'COSINE',
                                   'params': {'M': 16, 'efConstruction': 200}})
    col.load()
    verify_records(records, read_index(col))
    if fingerprint(await read_source()) != fingerprint(records):
        raise ValueError('source changed during build; do not cut over')
    after = {n: Collection(n).num_entities for n in existing}
    if preserved != after:
        raise ValueError('an existing collection changed during build; investigate before cutover')
    report = dict(target=args.target, provider='bge', signature=provider.model_signature,
                  dim=provider.dim, rows=len(records), documents=len({r['doc_id'] for r in records}),
                  sourceFingerprint=fingerprint(records), preserved=preserved, verified=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    await engine.dispose()


if __name__ == '__main__':
    for base in (Path('/app'), Path(__file__).resolve().parents[1] / 'backend'):
        sys.path.insert(0, str(base))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--batch-size', type=int, default=32)
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--verify-only', action='store_true')
    asyncio.run(run(parser.parse_args()))
