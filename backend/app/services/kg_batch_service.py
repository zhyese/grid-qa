"""KG 多文档抽取：复用持久化任务队列，逐文档隔离失败与权限。"""
from sqlalchemy import select, func, or_, literal

from app.config import settings
from app.core.response import BizError
from app.core.permissions import KG_EDIT, has_perm
from app.db.session import AsyncSessionLocal
from app.models.document import Document
from app.models.chunk import Chunk
from app.models.kg_triple import KgTriple
from app.models.persistent_task import PersistentTask
from app.models.user import User
from app.services import task_queue_service as queue

TASK_TYPE = 'kg.extract'
ACTIVE = ('queued', 'running', 'failed')


def assert_document_access(doc, user):
    if doc.tenant_id != user.tenant_id:
        raise BizError('文档不存在或无权访问', 404)
    if user.role != 'admin':
        if doc.dept and doc.dept != user.dept:
            raise BizError('无权限访问该部门文档', 403)
        roles = [r.strip() for r in (doc.allowed_roles or '').split(',') if r.strip()]
        if roles and user.role not in roles:
            raise BizError('无权限访问该文档', 403)


def _scope(user):
    filters = [Document.tenant_id == user.tenant_id]
    if user.role != 'admin':
        filters.append(or_(Document.dept == '', Document.dept == (user.dept or '')))
        roles = literal(',') + func.replace(Document.allowed_roles, ' ', '') + literal(',')
        filters.append(or_(Document.allowed_roles == '', roles.contains(',' + user.role + ',')))
    return filters


async def candidates(db, user, page=1, size=20, keyword=''):
    filters = _scope(user)
    if keyword:
        filters.append(Document.doc_name.contains(keyword, autoescape=True))
    total = (await db.execute(select(func.count()).select_from(Document).where(*filters))).scalar() or 0
    rows = (await db.execute(select(Document).where(*filters).order_by(Document.created_at.desc(), Document.id)
                             .offset((page-1)*size).limit(size))).scalars().all()
    ids = [d.id for d in rows]
    counts = dict((await db.execute(select(KgTriple.doc_id, func.count()).where(KgTriple.doc_id.in_(ids))
                                   .group_by(KgTriple.doc_id))).all()) if ids else {}
    chunks = dict((await db.execute(select(Chunk.doc_id, func.count()).where(Chunk.doc_id.in_(ids))
                                   .group_by(Chunk.doc_id))).all()) if ids else {}
    return {'total': total, 'list': [dict(docId=d.id, docName=d.doc_name, status=d.status,
            chunkCount=chunks.get(d.id, 0), tripleCount=counts.get(d.id, 0)) for d in rows]}


def public_task(task):
    payload = task.payload or {}
    # 任务系统保留完整堆栈用于审计，但 KG 用户接口绝不回传密钥/原始供应商错误。
    reason = ''
    if task.status in ('failed', 'dead'):
        reason = '抽取失败，请检查模型可用性或文档权限后重试'
        if '所有分块抽取失败' in (task.last_error or ''):
            reason = '模型调用或输出格式失败，请检查账户、网络后重试'
        elif '文档尚未解析' in (task.last_error or ''):
            reason = '文档尚未解析，请先解析文档'
        elif '权限' in (task.last_error or '') or '账号' in (task.last_error or ''):
            reason = '提交账号或文档权限已变化，请检查授权'
    result = task.result or {}
    # 不在轮询中暴露分块内容/三元组样本，也避免超大响应。
    result = {key: result[key] for key in ('status', 'tripleCount', 'newCount', 'failedBatches', 'totalBatches', 'neo4jSynced') if key in result}
    return dict(id=task.id, docId=payload.get('docId'), docName=payload.get('docName', ''),
                status=task.status, result=result, error=reason, attempts=task.attempts,
                createdAt=str(task.created_at or ''), modelType=payload.get('modelType'))


async def submit(db, doc_ids, model_type, user):
    if not getattr(settings, 'TASK_WORKERS_ENABLE', True):
        raise BizError('后台任务服务未启用，请联系管理员', 503)
    ids = sorted(set(doc_ids))
    if not ids or len(ids) > 200:
        raise BizError('每批请选择 1–200 份文档', 400)
    # 统一顺序锁文档，跨用户/跨模型也不重复排同一文档，避免并发提交死锁。
    docs = (await db.execute(select(Document).where(Document.id.in_(ids), Document.tenant_id == user.tenant_id)
                             .order_by(Document.id).with_for_update())).scalars().all()
    if len(docs) != len(ids):
        raise BizError('文档不存在或无权访问', 404)
    for doc in docs:
        assert_document_access(doc, user)
    tasks = []
    for doc in docs:
        key = 'kg:' + doc.id
        old = (await db.execute(select(PersistentTask).where(PersistentTask.tenant_id == user.tenant_id,
                   PersistentTask.task_type == TASK_TYPE, PersistentTask.idempotency_key == key).with_for_update())).scalar_one_or_none()
        if old and old.status in ACTIVE:
            if old.payload.get('userId') != user.id:
                raise BizError('所选文档已有其他用户的抽取任务，请稍后重试', 409)
            tasks.append(public_task(old))
            continue
        if old:
            old.idempotency_key = None
            await db.flush()
        task = await queue.enqueue_task_record(db, TASK_TYPE,
                dict(docId=doc.id, docName=doc.doc_name, userId=user.id, modelType=model_type),
                tenant_id=user.tenant_id, queue='low', idempotency_key=key,
                max_attempts=3, commit=False)
        tasks.append(public_task(task))
    await db.commit()
    return {'tasks': tasks}


async def list_tasks(db, user, page=1, size=50):
    filters = [PersistentTask.tenant_id == user.tenant_id, PersistentTask.task_type == TASK_TYPE,
               PersistentTask.payload['userId'].as_string() == user.id]
    total = (await db.execute(select(func.count()).select_from(PersistentTask).where(*filters))).scalar() or 0
    rows = (await db.execute(select(PersistentTask).where(*filters)
                            .order_by(PersistentTask.created_at.desc(), PersistentTask.id)
                            .offset((page-1)*size).limit(size))).scalars().all()
    return {'total': total, 'tasks': [public_task(t) for t in rows]}


async def retry(db, task_id, user):
    task = await queue.get_task(db, task_id, tenant_id=user.tenant_id)
    if not task or task.task_type != TASK_TYPE or task.payload.get('userId') != user.id:
        raise BizError('任务不存在', 404)
    if task.status != 'dead':
        raise BizError('仅可重试已结束的失败任务；排队/自动重试中的任务无需重复提交', 409)
    if not getattr(settings, 'TASK_WORKERS_ENABLE', True):
        raise BizError('后台任务服务未启用，请联系管理员', 503)
    doc = (await db.execute(select(Document).where(Document.id == task.payload['docId'],
                            Document.tenant_id == user.tenant_id).with_for_update())).scalar_one_or_none()
    if not doc:
        raise BizError('文档不存在或无权访问', 404)
    assert_document_access(doc, user)
    task = (await db.execute(select(PersistentTask).where(PersistentTask.id == task_id)
                            .with_for_update().execution_options(populate_existing=True))).scalar_one()
    if task.status != 'dead':
        raise BizError('任务状态已变化，请刷新', 409)
    key = 'kg:' + doc.id
    current = (await db.execute(select(PersistentTask).where(PersistentTask.tenant_id == user.tenant_id,
                PersistentTask.task_type == TASK_TYPE, PersistentTask.idempotency_key == key)
                .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if current and current.id != task.id:
        if current.status in ACTIVE:
            raise BizError('该文档已有抽取任务，请等待完成', 409)
        current.idempotency_key = None
        await db.flush()
    task.idempotency_key = key
    retried = await queue.retry_task(db, task_id, tenant_id=user.tenant_id)
    if not retried:
        raise BizError('任务状态已变化，请刷新', 409)
    return {'tasks': [public_task(retried)]}


async def run(payload, context, *, session_factory=None):
    from app.services import kg_service, role_permission_service
    async with (session_factory or AsyncSessionLocal)() as db:
        user = await db.get(User, payload.get('userId', ''))
        if not user or user.status != 'active' or user.tenant_id != context.tenant_id:
            raise BizError('提交账号不可用或租户已变化', 403)
        permitted = has_perm(user.role, KG_EDIT)
        if user.role != 'admin':
            override = await role_permission_service.effective_override(user.role, KG_EDIT)
            if override is not None:
                permitted = override
        if not permitted:
            raise BizError('已无图谱编辑权限', 403)
        return await kg_service.extract_triples(db, payload['docId'], payload.get('modelType'),
                                                tenant=context.tenant_id, user=user)
