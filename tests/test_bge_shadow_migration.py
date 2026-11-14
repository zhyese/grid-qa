"""迁移必须只写新集合，完整保留长中文原文，失败不能标记就绪。"""
import importlib.util
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('bge_shadow', Path(__file__).parents[1] / 'scripts' / 'migrate_bge_shadow.py')
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


def record(**kw):
    return dict(doc_id='doc', doc_name='规程', chunk_idx=0, content='中文原文', context_line='定位语', **kw)


def test_new_target_cannot_be_any_existing_collection():
    with pytest.raises(ValueError):
        m.validate_target('cloud', {'cloud', 'local'})
    with pytest.raises(ValueError):
        m.validate_target('../bad', set())
    m.validate_target('grid_chunks_bge_full_20261113', {'cloud', 'local'})


def test_long_chinese_content_is_not_silently_truncated():
    r = record(); r['content'] = '规程' * 3000
    m.validate_records([r])
    assert m.payload([r], [[1., 0.]])[0]['text'] == '规程' * 3000


def test_duplicate_source_keys_are_rejected():
    with pytest.raises(ValueError, match='duplicate'):
        m.validate_records([record(), record()])


def test_fingerprint_detects_context_and_source_changes():
    first = record(); second = record(); second['context_line'] = '新的定位语'
    assert m.fingerprint([first]) != m.fingerprint([second])


@pytest.mark.asyncio
async def test_failed_embedding_batch_is_never_inserted():
    stored = []
    async def embed(texts):
        return [[float('nan'), 0.]]
    async def insert(rows):
        stored.extend(rows)
    with pytest.raises(ValueError):
        await m.build_batches([record()], embed, insert, dim=2, batch_size=1)
    assert stored == []


@pytest.mark.asyncio
async def test_embed_uses_context_while_index_preserves_original_text():
    stored = []
    async def embed(texts):
        assert texts == ['定位语\n中文原文']
        return [[1., 0.]]
    async def insert(rows):
        stored.extend(rows)
    await m.build_batches([record()], embed, insert, dim=2, batch_size=1)
    assert stored == [dict(pk='doc_0', embedding=[1., 0.], text='中文原文', doc_id='doc', doc_name='规程', chunk_idx=0)]


def test_verification_rejects_equal_count_but_wrong_content():
    indexed = m.payload([record()], [[1., 0.]])
    indexed[0]['text'] = '旧内容'
    with pytest.raises(ValueError, match='payload'):
        m.verify_records([record()], indexed)


@pytest.mark.parametrize('changed', [{'verified': False}, {'signature': 'other-model'}, {'dim': 1024}])
def test_cutover_refuses_unverified_or_mismatched_model(changed):
    report = dict(target='new', sourceFingerprint='hash', signature='bge:v1:512', dim=512, verified=True)
    report.update(changed)
    with pytest.raises(ValueError):
        m.validate_manifest(report, 'new', 'hash', 'bge:v1:512', 512, 512)


def test_cutover_refuses_wrong_physical_vector_dimension():
    report = dict(target='new', sourceFingerprint='hash', signature='bge:v1:512', dim=512, verified=True)
    with pytest.raises(ValueError):
        m.validate_manifest(report, 'new', 'hash', 'bge:v1:512', 512, 1024)
