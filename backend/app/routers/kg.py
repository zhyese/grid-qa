"""知识图谱接口：抽取三元组 / 关系图谱 / 统计。"""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.limiter import limiter
from app.core.permissions import KG_EDIT
from app.core.response import BizError, success
from app.db.session import get_db
from app.dependencies import get_current_user, require_perm
from app.models.kg_triple import KgTriple
from app.models.user import User
from app.schemas.kg import KgExtractRequest, KgBatchExtractRequest
from app.services import kg_service
from app.services import kg_batch_service
from app.services.log_service import write_log

router = APIRouter(prefix="/kg", tags=["知识图谱"])


@router.post("/extract")
@limiter.limit("5/minute")
async def extract(
    request: Request,
    body: KgExtractRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(KG_EDIT)),
):
    data = await kg_service.extract_triples(db, body.docId, body.modelType, tenant=user.tenant_id, user=user)
    await write_log(db, user.username, "图谱抽取", f"{data['docName']}：{data['tripleCount']}条三元组")
    return success(data, "抽取成功")


@router.get('/extract/documents')
async def extract_documents(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                            keyword: str = Query('', max_length=256),
                            db: AsyncSession = Depends(get_db), user: User = Depends(require_perm(KG_EDIT))):
    return success(await kg_batch_service.candidates(db, user, page, size, keyword))


@router.post('/extract/batch')
@limiter.limit('5/minute')
async def extract_batch(request: Request, body: KgBatchExtractRequest,
                        db: AsyncSession = Depends(get_db), user: User = Depends(require_perm(KG_EDIT))):
    data = await kg_batch_service.submit(db, body.docIds, body.modelType, user)
    await write_log(db, user.username, '批量图谱抽取', f"提交或复用 {len(data['tasks'])} 份文档任务")
    return success(data, '任务已排队，可关闭页面')


@router.get('/extract/tasks')
async def extract_tasks(page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200),
                         db: AsyncSession = Depends(get_db), user: User = Depends(require_perm(KG_EDIT))):
    return success(await kg_batch_service.list_tasks(db, user, page, size))


@router.post('/extract/tasks/{task_id}/retry')
@limiter.limit('5/minute')
async def retry_extract(request: Request, task_id: str, db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_perm(KG_EDIT))):
    return success(await kg_batch_service.retry(db, task_id, user), '任务已重新排队')


@router.get("/graph")
async def graph(
    entity: str = "",
    limit: int = 300,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await kg_service.get_graph(db, entity, limit, tenant=user.tenant_id)
    return success(data, "查询成功")


@router.get("/stats")
async def stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await kg_service.get_stats(db)
    return success(data, "查询成功")


@router.get("/path")
async def kg_path(
    entity: str,
    depth: int = 3,
    limit: int = 20,
    user: User = Depends(get_current_user),
):
    """多跳影响链：从某实体出发的 N 跳因果路径（故障传播/影响分析，Neo4j）。"""
    paths = await kg_service.get_paths(entity, depth, limit)
    return success({"entity": entity, "depth": depth, "paths": paths}, "查询成功")


@router.get("/influence")
async def influence(
    limit: int = 15,
    user: User = Depends(get_current_user),
):
    """枢纽实体：出度最高（影响传播源头，Neo4j）。"""
    hubs = await kg_service.get_hubs(limit)
    return success({"hubs": hubs}, "查询成功")


# ===== 三元组人工修正（编辑/删除，BRD 知识扩展）=====

@router.get("/triples")
async def list_triples(
    page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
    doc_id: str | None = Query(None), kw: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(KG_EDIT)),
):
    """三元组管理列表（分页，可按 doc_id/关键词筛）。"""
    stmt = select(KgTriple).order_by(KgTriple.created_at.desc())
    if doc_id:
        stmt = stmt.where(KgTriple.doc_id == doc_id)
    if kw:
        like = f"%{kw}%"
        from sqlalchemy import or_
        stmt = stmt.where(or_(KgTriple.subject.like(like), KgTriple.object.like(like), KgTriple.relation.like(like)))
    from sqlalchemy import func as _f
    total = (await db.execute(select(_f.count()).select_from(stmt.subquery()))).scalar() or 0
    rows = (await db.execute(stmt.offset((page - 1) * size).limit(size))).scalars().all()
    return success({"total": total, "list": [
        {"id": r.id, "subject": r.subject, "relation": r.relation, "object": r.object,
         "docId": r.doc_id, "docName": r.doc_name,
         "createdAt": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else ""} for r in rows]}, "查询成功")


@router.put("/triples/{triple_id}")
async def update_triple(
    triple_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(KG_EDIT)),
):
    """人工编辑三元组（subject/relation/object）。MySQL + Neo4j 同步改。"""
    t = (await db.execute(select(KgTriple).where(KgTriple.id == triple_id))).scalar_one_or_none()
    if not t:
        raise BizError("三元组不存在", 404)
    old = (t.subject, t.relation, t.object)
    if "subject" in body:
        t.subject = body["subject"]
    if "relation" in body:
        t.relation = body["relation"]
    if "object" in body:
        t.object = body["object"]
    await db.commit()
    try:
        from app.clients import neo4j_client
        await neo4j_client.update_triple(old, (t.subject, t.relation, t.object), t.doc_id)
    except Exception as e:
        from app.core.obs import degraded
        degraded("kg_neo4j_update", e)
    await write_log(db, user.username, "图谱修正", f"{triple_id}: {old[0]}-{old[1]}->{old[2]} 改为 {t.subject}-{t.relation}->{t.object}")
    return success({"id": t.id, "subject": t.subject, "relation": t.relation, "object": t.object}, "已更新")


@router.delete("/triples/{triple_id}")
async def delete_triple(
    triple_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(KG_EDIT)),
):
    """删除错误三元组（MySQL + Neo4j 联动删）。"""
    from sqlalchemy import delete as _del
    t = (await db.execute(select(KgTriple).where(KgTriple.id == triple_id))).scalar_one_or_none()
    if not t:
        raise BizError("三元组不存在", 404)
    triple_tuple = (t.subject, t.relation, t.object, t.doc_id)
    await db.execute(_del(KgTriple).where(KgTriple.id == triple_id))
    await db.commit()
    try:
        from app.clients import neo4j_client
        await neo4j_client.delete_triple(*triple_tuple)
    except Exception as e:
        from app.core.obs import degraded
        degraded("kg_neo4j_delete", e)
    await write_log(db, user.username, "图谱修正", f"删除三元组 {triple_id}")
    return success({"id": triple_id, "deleted": True}, "已删除")


# ===== Sprint3a：CIM(IEC61970) 拓扑导入（电气连通图入 KG）=====

@router.post("/cim-import")
async def cim_import(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(KG_EDIT)),
):
    """导入 CIM RDF/XML → 解析电气连通图 → 存 KgTriple 三元组。

    Body: {"xml": "<rdf:RDF>...</rdf:RDF>", "docId"?: "cim", "docName"?: "站点名"}
    连通边 (设备A, 电气连接, 设备B) 进 KG 后，GraphRAG 多跳推理可用——
    故障传播链从 kgEntity 模糊匹配升级为真实电气连通。真实 CIM 文件接入零改动（只换输入）。
    """
    from app.services.cim_service import import_cim

    body = await request.json()
    xml_text = body.get("xml", "")
    if not xml_text.strip():
        raise BizError("缺少 xml 字段", 400)
    try:
        data = await import_cim(
            db, xml_text,
            doc_id=body.get("docId") or "cim",
            doc_name=body.get("docName", ""),
        )
    except ValueError as e:
        raise BizError(str(e), 400)
    await write_log(db, user.username, "CIM导入",
                    f"{data['station']}: {data['equipmentCount']}设备/{data['edgeCount']}连通")
    return success(data, "导入成功")
