"""批量抽取的用户可见结果、持久化与授权边界。"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.core.response import BizError
from app.models.document import Document
from app.models.chunk import Chunk
from app.models.kg_triple import KgTriple
from app.models.user import User
from app.models.persistent_task import PersistentTask
from app.services import kg_service


@pytest_asyncio.fixture
async def kg_db():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        for table in (User.__table__, Document.__table__, Chunk.__table__, KgTriple.__table__, PersistentTask.__table__):
            await conn.run_sync(table.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add(User(id='u', username='test', password_hash='unused', role='admin', tenant_id='one'))
        db.add(Document(id='d', doc_name='手册', minio_object='d.txt', tenant_id='one', chunk_count=1))
        db.add(Document(id='foreign', doc_name='secret', minio_object='s.txt', tenant_id='two', chunk_count=1))
        await db.flush()
        db.add(Chunk(doc_id='d', content='主变压器发生过热', chunk_idx=0))
        await db.commit()
        yield db
    await engine.dispose()


class Replies:
    def __init__(self, replies):
        self.replies = iter(replies)

    async def chat(self, *args, **kwargs):
        value = next(self.replies)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.asyncio
async def test_all_provider_failures_are_not_successful_empty_extraction(kg_db, monkeypatch):
    monkeypatch.setattr(kg_service, 'get_llm_provider', lambda _: Replies([RuntimeError('secret-key-do-not-show')]))
    with pytest.raises(BizError) as exc:
        await kg_service.extract_triples(kg_db, 'd')
    assert 'secret-key-do-not-show' not in str(exc.value)
    assert '失败' in str(exc.value)
    assert not (await kg_db.execute(select(KgTriple))).scalars().all()


@pytest.mark.asyncio
async def test_retry_rechecks_task_after_waiting_for_document_lock(kg_db, monkeypatch):
    from sqlalchemy import update
    from app.services import kg_batch_service as batch
    user = await kg_db.get(User, 'u')
    result = await batch.submit(kg_db, ['d'], None, user)
    task_id = result['tasks'][0]['id']
    task = await kg_db.get(PersistentTask, task_id)
    task.status = 'dead'
    await kg_db.commit()
    original_execute = kg_db.execute
    raced = False
    async def execute(statement, *args, **kwargs):
        nonlocal raced
        # 模拟读到 dead 后等待文档锁期间，另一请求已重试且 worker 已领取。
        if not raced and 'FROM documents' in str(statement):
            raced = True
            async with async_sessionmaker(kg_db.bind, expire_on_commit=False)() as other:
                await other.execute(update(PersistentTask).where(PersistentTask.id == task_id)
                                    .values(status='running', locked_by='other-worker'))
                await other.commit()
        return await original_execute(statement, *args, **kwargs)
    monkeypatch.setattr(kg_db, 'execute', execute)
    with pytest.raises(BizError):
        await batch.retry(kg_db, task_id, user)
    await kg_db.refresh(task)
    assert task.status == 'running'
    assert task.locked_by == 'other-worker'


@pytest.mark.asyncio
async def test_valid_empty_result_is_distinct_from_provider_failure(kg_db, monkeypatch):
    monkeypatch.setattr(kg_service, 'get_llm_provider', lambda _: Replies(['[]']))
    result = await kg_service.extract_triples(kg_db, 'd')
    assert result['status'] == 'empty'
    assert result['failedBatches'] == 0


@pytest.mark.asyncio
async def test_partial_failure_preserves_triples_and_exposes_warning(kg_db, monkeypatch):
    kg_db.add(Chunk(doc_id='d', content='另一分块', chunk_idx=1))
    await kg_db.commit()
    monkeypatch.setattr(kg_service, '_BATCH', 1)
    monkeypatch.setattr(kg_service, 'get_llm_provider', lambda _: Replies([
        '[{"s":"主变压器","r":"发生","o":"过热故障"}]', RuntimeError('offline')]))
    async def upsert(*args, **kwargs):
        return None
    monkeypatch.setattr(kg_service.neo4j_client, 'upsert_triples', upsert)
    result = await kg_service.extract_triples(kg_db, 'd')
    assert result['status'] == 'partial'
    assert result['failedBatches'] == 1
    assert result['newCount'] == 1
    assert len((await kg_db.execute(select(KgTriple))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_submission_deduplicates_and_persists_independent_tasks(kg_db):
    from app.services import kg_batch_service as batch
    user = await kg_db.get(User, 'u')
    first = await batch.submit(kg_db, ['d', 'd'], None, user)
    second = await batch.submit(kg_db, ['d'], None, user)
    assert len(first['tasks']) == 1
    assert first['tasks'][0]['id'] == second['tasks'][0]['id']
    tasks = (await kg_db.execute(select(PersistentTask))).scalars().all()
    assert len(tasks) == 1
    assert tasks[0].queue_name == 'low'
    assert tasks[0].tenant_id == 'one'
    assert tasks[0].payload['userId'] == 'u'


@pytest.mark.asyncio
async def test_submission_rejects_foreign_document_without_partial_queue(kg_db):
    from app.services import kg_batch_service as batch
    with pytest.raises(BizError):
        await batch.submit(kg_db, ['d', 'foreign'], None, await kg_db.get(User, 'u'))
    assert not (await kg_db.execute(select(PersistentTask))).scalars().all()


@pytest.mark.asyncio
async def test_missing_department_cannot_read_restricted_document(kg_db):
    from app.services import kg_batch_service as batch
    doc = await kg_db.get(Document, 'd')
    doc.dept = 'protected'
    user = await kg_db.get(User, 'u')
    user.role = 'editor'
    user.dept = ''
    await kg_db.commit()
    with pytest.raises(BizError):
        await batch.submit(kg_db, ['d'], None, user)


@pytest.mark.asyncio
async def test_task_history_does_not_leak_other_users(kg_db):
    from app.services import kg_batch_service as batch
    user = await kg_db.get(User, 'u')
    await batch.submit(kg_db, ['d'], None, user)
    other = User(id='other', username='other', role='admin', tenant_id='one')
    assert (await batch.list_tasks(kg_db, other))['tasks'] == []


@pytest.mark.asyncio
async def test_retry_only_terminal_owned_failure(kg_db):
    from app.services import kg_batch_service as batch
    user = await kg_db.get(User, 'u')
    result = await batch.submit(kg_db, ['d'], None, user)
    task_id = result['tasks'][0]['id']
    with pytest.raises(BizError):
        await batch.retry(kg_db, task_id, user)
    task = await kg_db.get(PersistentTask, task_id)
    task.status = 'dead'
    task.last_error = 'private traceback'
    await kg_db.commit()
    await batch.retry(kg_db, task_id, user)
    await kg_db.refresh(task)
    assert task.status == 'queued'
    assert task.last_error == ''


@pytest.mark.asyncio
async def test_candidate_pagination_filters_tenant_and_reports_real_counts(kg_db):
    from app.services import kg_batch_service as batch
    kg_db.add(KgTriple(doc_id='d', subject='设备', relation='发生', object='故障'))
    await kg_db.commit()
    page = await batch.candidates(kg_db, await kg_db.get(User, 'u'), page=1, size=1)
    assert page['total'] == 1
    assert page['list'][0]['tripleCount'] == 1
    assert page['list'][0]['chunkCount'] == 1
    assert (await batch.candidates(kg_db, await kg_db.get(User, 'u'), keyword='secret'))['total'] == 0


@pytest.mark.asyncio
async def test_worker_executes_persisted_task_without_request_session(kg_db, monkeypatch):
    from app.services import kg_batch_service as batch
    from app.tasks.worker import run_task_once
    factory = async_sessionmaker(kg_db.bind, expire_on_commit=False)
    monkeypatch.setattr(batch, 'AsyncSessionLocal', factory)
    monkeypatch.setattr(kg_service, 'get_llm_provider', lambda _: Replies(['[]']))
    result = await batch.submit(kg_db, ['d'], None, await kg_db.get(User, 'u'))
    assert await run_task_once(queue='low', session_factory=factory)
    async with factory() as fresh:
        task = await fresh.get(PersistentTask, result['tasks'][0]['id'])
        assert task.status == 'succeeded'
        assert task.result['status'] == 'empty'


@pytest.mark.asyncio
async def test_worker_rechecks_disabled_account_before_model_call(kg_db, monkeypatch):
    from app.services import kg_batch_service as batch
    from app.tasks.worker import run_task_once
    factory = async_sessionmaker(kg_db.bind, expire_on_commit=False)
    monkeypatch.setattr(batch, 'AsyncSessionLocal', factory)
    user = await kg_db.get(User, 'u')
    result = await batch.submit(kg_db, ['d'], None, user)
    user.status = 'inactive'
    await kg_db.commit()
    assert await run_task_once(queue='low', session_factory=factory)
    async with factory() as fresh:
        task = await fresh.get(PersistentTask, result['tasks'][0]['id'])
        assert task.status == 'failed'
        assert '账号' in task.last_error


@pytest.mark.asyncio
async def test_batch_api_requires_permission_and_validates_ids(kg_db):
    import httpx
    from fastapi import FastAPI
    from app.routers.kg import router
    from app.dependencies import get_current_user
    from app.db.session import get_db
    from app.core.limiter import limiter
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(router)
    user = await kg_db.get(User, 'u')
    async def current():
        return user
    async def db_dep():
        yield kg_db
    app.dependency_overrides[get_current_user] = current
    app.dependency_overrides[get_db] = db_dep
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://kg-test') as client:
        for ids in ([], [''], ['d'] * 201):
            assert (await client.post('/kg/extract/batch', json={'docIds': ids})).status_code == 422
        assert (await client.get('/kg/extract/documents')).json()['data']['total'] == 1


@pytest.mark.asyncio
async def test_neo4j_retry_reconciles_already_persisted_triples(kg_db, monkeypatch):
    kg_db.add(KgTriple(doc_id='d', subject='主变压器', relation='发生', object='过热故障'))
    await kg_db.commit()
    monkeypatch.setattr(kg_service, 'get_llm_provider', lambda _: Replies(['[{"s":"主变压器","r":"发生","o":"过热故障"}]']))
    graph = []
    async def upsert(triples, *args):
        graph.extend(triples)
    monkeypatch.setattr(kg_service.neo4j_client, 'upsert_triples', upsert)
    result = await kg_service.extract_triples(kg_db, 'd')
    assert result['newCount'] == 0
    assert len(graph) == 1


@pytest.mark.asyncio
async def test_document_access_is_rechecked_after_long_model_call(kg_db, monkeypatch):
    user = await kg_db.get(User, 'u')
    class ChangedAccess:
        async def chat(self, *args, **kwargs):
            doc = await kg_db.get(Document, 'd')
            doc.tenant_id = 'two'
            await kg_db.commit()
            return '[{"s":"主变压器","r":"发生","o":"过热故障"}]'
    monkeypatch.setattr(kg_service, 'get_llm_provider', lambda _: ChangedAccess())
    with pytest.raises(BizError):
        await kg_service.extract_triples(kg_db, 'd', tenant='one', user=user)
    assert not (await kg_db.execute(select(KgTriple))).scalars().all()
