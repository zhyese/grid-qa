# scripts/backfill_chunk_meta.py
"""历史存量 chunk 引用元数据回填。

对 metadata_complete=False 的 chunk，按 doc_id 从 MinIO 取原文重新结构化解析，
回填 page_num/bbox/section_path/table_header。失败保持 False（前端降级，不阻塞）。

用法：python scripts/backfill_chunk_meta.py [--tenant default] [--dry-run]
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sqlalchemy import select
from app.db.session import AsyncSessionLocal as async_session
from app.models.chunk import Chunk
from app.models.document import Document
from app.services import parse_service, chunk_service
from app.clients import minio_client


async def backfill(tenant: str, dry_run: bool) -> dict:
    stats = {"scanned": 0, "updated": 0, "skipped": 0, "failed": 0}
    async with async_session() as db:
        # 取待回填文档（含 metadata_complete=False chunk 的）
        rows = (await db.execute(
            select(Document).where(Document.tenant_id == tenant, Document.status == "vectorized")
        )).scalars().all()
        for doc in rows:
            stats["scanned"] += 1
            try:
                content = await asyncio.to_thread(minio_client.get_object_bytes, doc.minio_object)
                sections, _ = parse_service.parse_file_structured(doc.doc_name, content)
                structured = chunk_service.split_structured(sections)
                if not structured:
                    stats["skipped"] += 1
                    continue
                if dry_run:
                    stats["updated"] += 1
                    continue
                # 按 chunk_idx 回填（重新解析顺序应与 split_structured 一致；zip 对齐）
                chunks = (await db.execute(
                    select(Chunk).where(Chunk.doc_id == doc.id).order_by(Chunk.chunk_idx)
                )).scalars().all()
                for ch, meta in zip(chunks, structured):
                    ch.page_num = meta.get("page_num")
                    ch.bbox = meta.get("bbox")
                    ch.section_path = meta.get("section_path", "") or ch.section
                    ch.table_header = meta.get("table_header", "")
                    ch.metadata_complete = bool(ch.page_num is not None or ch.table_header)
                await db.commit()
                stats["updated"] += 1
            except Exception as e:
                print(f"[FAIL] doc={doc.id} {e}", file=sys.stderr)
                stats["failed"] += 1
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="default")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    res = asyncio.run(backfill(args.tenant, args.dry_run))
    print(res)
