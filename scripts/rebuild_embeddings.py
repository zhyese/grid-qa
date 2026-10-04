"""H1 检索底座升级：Milvus 向量全量重建 + 向量空间签名重置。

**什么时候必须跑**：改了 `QWEN_EMB_MODEL`（如 text-embedding-v3 → v4）、`EMBEDDING_DIM`
或 `EMB_PROVIDER`（如 qwen ↔ bge）之后。旧向量与新 query 向量不在同一空间，
不重建 = 检索静默全错（不报错，只是召回崩掉）。

做三件事：
1. 对每个已向量化文档重跑 `document_service.vectorize_document`（内部先清旧再写新）；
2. 重建后重置 `embed:model_sig:*` 签名（清除漂移告警）；
3. 打印汇总，供 `scripts/eval_retrieval.py` 做 recall≥92% 验收。

用法：
    # 容器内（推荐，复用后端 env / Milvus 网络）
    docker exec grid-backend python /app/scripts/rebuild_embeddings.py --yes
    # 本机（需 Milvus/MySQL/MinIO 可达）
    python scripts/rebuild_embeddings.py --yes

验收（重建后必跑，不达线即回滚模型名 + 再重建一次）：
    python scripts/eval_retrieval.py --topk 5 --threshold 0.92

退出码：0 全部成功；1 存在失败文档或无可重建文档。
"""
import argparse
import asyncio
import sys
from pathlib import Path

# 容器内 /app 为代码根；本机则用仓库根（scripts/ 的上一级）
_ROOT = Path("/app")
if not (_ROOT / "app").exists():
    _ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.models.document import Document  # noqa: E402


async def _run(limit: int | None, doc_ids_arg: list[str] | None = None) -> int:
    from app.services import document_service, embedding_service
    from app.providers.factory import get_embedding_provider

    # 重建只换向量，不需要重跑 KG 三元组抽取：vectorize_document 会在 KG_AUTO_EXTRACT_ENABLE
    # 开启时后台触发 LLM 抽取，而抽取对同一文档内容幂等（按 (s,r,o) 去重）→ 纯属白烧 LLM。
    # 此处仅关闭本进程的抽取（其余入口如 /kg/extract、文档重新解析不受影响）。
    if getattr(settings, "KG_AUTO_EXTRACT_ENABLE", False):
        settings.KG_AUTO_EXTRACT_ENABLE = False
        print("[rebuild] 已临时关闭 KG 自动抽取（重建不需要重抽三元组）")

    prov = get_embedding_provider(settings.EMB_PROVIDER)
    print(f"[rebuild] provider={settings.EMB_PROVIDER} model={getattr(prov, 'model', '-')} "
          f"dim={prov.dim} signature={prov.model_signature}")

    if doc_ids_arg:
        doc_ids = doc_ids_arg
    else:
        async with AsyncSessionLocal() as db:
            stmt = select(Document.id).where(Document.status == "vectorized")
            if limit:
                stmt = stmt.limit(limit)
            doc_ids = list((await db.execute(stmt)).scalars().all())

    if not doc_ids:
        print("[rebuild] 没有 status=vectorized 的文档可重建（先解析+向量化文档）")
        return 1

    print(f"[rebuild] 待重建文档 {len(doc_ids)} 篇")
    ok, failed = 0, []
    for i, doc_id in enumerate(doc_ids, 1):
        async with AsyncSessionLocal() as db:
            try:
                res = await document_service.vectorize_document(db, doc_id)
                ok += 1
                print(f"[rebuild] {i}/{len(doc_ids)} {doc_id} → {res.get('vectorCount')} vectors "
                      f"({res.get('milvusCollection')})")
            except Exception as e:
                failed.append((doc_id, f"{type(e).__name__}: {e}"))
                print(f"[rebuild] {i}/{len(doc_ids)} {doc_id} 失败：{e}")

    print(f"[rebuild] 完成：成功 {ok} / 失败 {len(failed)}")
    for doc_id, err in failed:
        print(f"  - {doc_id}: {err}")

    # 只有「全量且全部成功」才允许重置签名。--limit / --doc-ids 属局部重建，
    # 完成后向量库仍是混合空间（部分新模型 / 部分旧模型），此时清告警等于把问题藏起来。
    is_partial = bool(limit) or bool(doc_ids_arg)
    if is_partial:
        print("[rebuild] PARTIAL_SCOPE —— 本次为局部重建，向量库仍处于**混合空间**"
              "（其余文档仍是旧模型向量），已保留漂移告警。请执行全量重建收口：")
        print("[rebuild]   docker exec grid-backend python /app/scripts/rebuild_embeddings.py --yes")
        return 1

    if failed:
        # 全量但有失败 = 混合向量空间，比"全部旧"更糟 → **绝不能**重置签名
        print("[rebuild] REBUILD_PARTIAL —— 向量库处于**混合空间**（部分文档仍是旧模型向量），"
              "已保留漂移告警。请修复后仅重建失败文档：")
        ids = " ".join(d for d, _ in failed)
        print(f"[rebuild]   docker exec grid-backend python /app/scripts/rebuild_embeddings.py "
              f"--yes --doc-ids {ids}")
        return 1

    # 全量且全部成功 → 重置签名，漂移告警归零
    await embedding_service.mark_vectors_rebuilt(settings.EMB_PROVIDER)
    print("[rebuild] REBUILD_OK —— 请接着跑 scripts/eval_retrieval.py 验收 recall≥92%")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Milvus 向量全量重建（H1 检索底座升级）")
    ap.add_argument("--yes", action="store_true", help="跳过确认（CI/容器内自动化用）")
    ap.add_argument("--limit", type=int, default=0, help="只重建前 N 篇（冒烟验证用）")
    ap.add_argument("--doc-ids", nargs="+", default=None,
                    help="只重建指定文档（部分失败后补跑用）")
    args = ap.parse_args()

    if not args.yes:
        ans = input(
            f"将重建全部向量（provider={settings.EMB_PROVIDER}, dim={settings.EMBEDDING_DIM}）。"
            "确认输入 yes："
        )
        if ans.strip().lower() not in ("y", "yes"):
            print("已取消")
            return 1
    return asyncio.run(_run(args.limit or None, args.doc_ids))


if __name__ == "__main__":
    sys.exit(main())
