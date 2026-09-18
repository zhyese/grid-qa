"""B6: overconfident 默认开 + 高置信基线持久化（highbase key）。

测试覆盖：
- _write_highbase：high 答案 → 写 qa:highbase:{nq}；非 high 不写；开关关 → 不写
- check_overconfident：dislike 时从 highbase 检出冲突 → OVERCONFIDENT.inc + evidence_gap.collect
- TTL：highbase key ex = OVERCONFIDENT_BASELINE_TTL_DAYS * 86400
- 开关关 → check_overconfident 不检出
- degraded 吞异常：Redis 异常不抛

隔离策略：
- FakeRedis（in-process dict）替代真 Redis，无外部依赖、CI 可跑
- 唯一 nq（uuid）防跨测试残留
- 每个 case 重置 settings 子属性 + FakeRedis 状态
"""
import json
from uuid import uuid4

import pytest

from app.services import qa_service, feedback_optimizer_service as fos


class FakeRedis:
    """最小 aioredis 子集：set/get/scan_iter/delete/ping，decode_responses=True 语义。

    scan_iter 接受 match 参数，对 keys 做简单 fnmatch（覆盖 highbase 单 key 用例）。
    """

    def __init__(self):
        self.store: dict[str, str] = {}
        self.last_ex: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value
        if ex is not None:
            self.last_ex[key] = int(ex)

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
        return n

    async def scan_iter(self, match=None, count=None):
        """异步生成器：兼容 aioredis `async for key in r.scan_iter(...)` 语义。"""
        import fnmatch
        keys = list(self.store.keys())
        if match:
            keys = [k for k in keys if fnmatch.fnmatch(k, match)]
        for k in keys:
            yield k

    async def ping(self):
        return True


@pytest.fixture
def fake_redis(monkeypatch):
    """注入 FakeRedis 到 redis_client.get_redis()；返回实例供断言。"""
    fake = FakeRedis()
    monkeypatch.setattr("app.clients.redis_client.get_redis", lambda: fake)
    return fake


def _conf(**overrides):
    """覆盖 settings 子属性；返回 (orig_dict, new_dict) 便于 restore。"""
    orig = {}
    for k, v in overrides.items():
        orig[k] = getattr(qa_service.settings, k)
        setattr(qa_service.settings, k, v)
    return orig


@pytest.fixture
def restore_settings():
    """记录本测试改动的 settings 属性，teardown 还原。"""
    changed = {}

    def mark(*keys):
        for k in keys:
            if k not in changed:
                changed[k] = getattr(qa_service.settings, k)
        # 同步映射到 fos.settings（同一 Settings 实例，无需重复）

    yield mark
    for k, v in changed.items():
        setattr(qa_service.settings, k, v)


# ===== _write_highbase =====

@pytest.mark.asyncio
async def test_write_highbase_writes_key(fake_redis, restore_settings):
    """high 答案 → 写 qa:highbase:{nq}，confidence=high + answer + ts。"""
    restore_settings("CONFIDENCE_OVERCONFIDENT_ENABLE", "OVERCONFIDENT_BASELINE_TTL_DAYS")
    _conf(CONFIDENCE_OVERCONFIDENT_ENABLE=True, OVERCONFIDENT_BASELINE_TTL_DAYS=30)
    nq = f"主变过载_{uuid4().hex[:8]}"

    await qa_service._write_highbase(nq, "答案是 A", tenant="default")

    key = f"qa:highbase:{nq}"
    assert key in fake_redis.store
    payload = json.loads(fake_redis.store[key])
    assert payload["confidence"] == "high"
    assert payload["answer"] == "答案是 A"
    assert "ts" in payload
    # TTL = 30 天（秒）
    assert fake_redis.last_ex[key] == 30 * 86400


@pytest.mark.asyncio
async def test_write_highbase_truncates_long_answer(fake_redis, restore_settings):
    """answer >500 字 → 截断到 500（防 Redis 大 value）。"""
    restore_settings("CONFIDENCE_OVERCONFIDENT_ENABLE")
    _conf(CONFIDENCE_OVERCONFIDENT_ENABLE=True)
    nq = f"长答_{uuid4().hex[:8]}"
    long_ans = "X" * 1000

    await qa_service._write_highbase(nq, long_ans, tenant="default")

    payload = json.loads(fake_redis.store[f"qa:highbase:{nq}"])
    assert len(payload["answer"]) == 500


@pytest.mark.asyncio
async def test_write_highbase_switch_off_skips(fake_redis, restore_settings):
    """CONFIDENCE_OVERCONFIDENT_ENABLE=False → 不写 key。"""
    restore_settings("CONFIDENCE_OVERCONFIDENT_ENABLE")
    _conf(CONFIDENCE_OVERCONFIDENT_ENABLE=False)
    nq = f"关_{uuid4().hex[:8]}"

    await qa_service._write_highbase(nq, "A", tenant="default")

    assert f"qa:highbase:{nq}" not in fake_redis.store


@pytest.mark.asyncio
async def test_write_highbase_redis_exception_swallowed(monkeypatch, restore_settings):
    """Redis 异常 → degraded 吞掉，不抛（fire-and-forget 安全）。"""
    restore_settings("CONFIDENCE_OVERCONFIDENT_ENABLE")

    class BoomRedis:
        async def set(self, *a, **kw):
            raise RuntimeError("redis down")

    monkeypatch.setattr("app.clients.redis_client.get_redis", lambda: BoomRedis())
    _conf(CONFIDENCE_OVERCONFIDENT_ENABLE=True)

    # 不应抛
    await qa_service._write_highbase("任意nq", "A", tenant="default")


# ===== check_overconfident =====

@pytest.mark.asyncio
async def test_check_overconfident_detects_from_highbase(fake_redis, restore_settings, monkeypatch):
    """highbase 存在 + confidence=high → OVERCONFIDENT.inc + collect 调用。"""
    restore_settings("CONFIDENCE_OVERCONFIDENT_ENABLE", "OVERCONFIDENT_BASELINE_TTL_DAYS")
    _conf(CONFIDENCE_OVERCONFIDENT_ENABLE=True, OVERCONFIDENT_BASELINE_TTL_DAYS=30)
    nq = f"检出_{uuid4().hex[:8]}"

    # 预置 highbase key（模拟 answer() 写入）
    await qa_service._write_highbase(nq, "机器判 high 的答案", tenant="default")

    # mock OVERCONFIDENT.inc 计数 + collect
    inc_called = {"n": 0}
    monkeypatch.setattr("app.core.metrics.OVERCONFIDENT.inc",
                        lambda: inc_called.__setitem__("n", inc_called["n"] + 1))

    collect_called = {}

    async def fake_collect(query, answer, confidence, grade, action,
                           source="auto", tenant="default"):
        collect_called["args"] = dict(query=query, answer=answer, confidence=confidence,
                                      grade=grade, action=action, source=source, tenant=tenant)
        return 1

    monkeypatch.setattr("app.services.evidence_gap_service.collect", fake_collect, raising=False)

    hit = await fos.check_overconfident(nq, tenant="default")

    assert hit is True
    assert inc_called["n"] == 1
    assert collect_called["args"]["source"] == "overconfident"
    assert collect_called["args"]["confidence"] == "high"
    assert collect_called["args"]["answer"] == "机器判 high 的答案"


@pytest.mark.asyncio
async def test_check_overconfident_no_highbase_returns_false(fake_redis, restore_settings, monkeypatch):
    """无 highbase key → 返回 False，不调 collect。"""
    restore_settings("CONFIDENCE_OVERCONFIDENT_ENABLE")
    _conf(CONFIDENCE_OVERCONFIDENT_ENABLE=True)
    nq = f"无基线_{uuid4().hex[:8]}"

    called = []

    async def fake_collect(*a, **kw):
        called.append(1)
        return 1

    monkeypatch.setattr("app.services.evidence_gap_service.collect", fake_collect, raising=False)
    monkeypatch.setattr("app.core.metrics.OVERCONFIDENT.inc", lambda: None)

    hit = await fos.check_overconfident(nq, tenant="default")

    assert hit is False
    assert called == []


@pytest.mark.asyncio
async def test_check_overconfident_switch_off_skips(fake_redis, restore_settings, monkeypatch):
    """CONFIDENCE_OVERCONFIDENT_ENABLE=False → 不扫不检出。"""
    restore_settings("CONFIDENCE_OVERCONFIDENT_ENABLE")
    _conf(CONFIDENCE_OVERCONFIDENT_ENABLE=False)
    nq = f"开关关_{uuid4().hex[:8]}"

    # 即便有 highbase 也不应检出
    await qa_service._write_highbase(nq, "A", tenant="default")  # 开关关时也不写
    # 手动塞一个确保隔离
    fake_redis.store[f"qa:highbase:{nq}"] = json.dumps({"confidence": "high", "answer": "A"})

    called = []

    async def fake_collect(*a, **kw):
        called.append(1)
        return 1

    monkeypatch.setattr("app.services.evidence_gap_service.collect", fake_collect, raising=False)

    hit = await fos.check_overconfident(nq, tenant="default")

    assert hit is False
    assert called == []


@pytest.mark.asyncio
async def test_check_overconfident_redis_exception_returns_false(monkeypatch, restore_settings):
    """Redis 异常 → degraded 吞掉，返回 False（安全侧：不检出）。"""
    restore_settings("CONFIDENCE_OVERCONFIDENT_ENABLE")
    _conf(CONFIDENCE_OVERCONFIDENT_ENABLE=True)

    class BoomRedis:
        def scan_iter(self, **kw):
            raise RuntimeError("redis down")

    monkeypatch.setattr("app.clients.redis_client.get_redis", lambda: BoomRedis())

    hit = await fos.check_overconfident("任意nq", tenant="default")
    assert hit is False


# ===== 端到端：high → 写 highbase → 问答缓存过期 → dislike 仍检出 =====

@pytest.mark.asyncio
async def test_e2e_qa_cache_expired_but_highbase_survives(fake_redis, restore_settings, monkeypatch):
    """B6 核心场景：问答缓存过期被删，highbase 仍存活 → dislike 仍检出冲突。

    这正是 spec §5.2 列出的痛点：老实现扫 qa:*:{nq}，缓存 TTL 短过期就丢；
    新 highbase TTL=30 天独立持久化。
    """
    restore_settings("CONFIDENCE_OVERCONFIDENT_ENABLE", "OVERCONFIDENT_BASELINE_TTL_DAYS")
    _conf(CONFIDENCE_OVERCONFIDENT_ENABLE=True, OVERCONFIDENT_BASELINE_TTL_DAYS=30)
    nq = f"过期_{uuid4().hex[:8]}"

    # 1) answer 时同时写问答缓存 qa:*:{nq} + highbase（这里只直接调 _write_highbase）
    await qa_service._write_highbase(nq, "high 答案", tenant="default")
    # 假装问答缓存也存在
    qa_cache_key = f"qa:default:default:{nq}:v1"
    fake_redis.store[qa_cache_key] = json.dumps({"confidence": "high", "answer": "high 答案"})

    # 2) 问答缓存 TTL 到期被失效（如 invalidate_cache_on_dislike 或自然过期）
    await fake_redis.delete(qa_cache_key)
    assert qa_cache_key not in fake_redis.store
    # highbase 仍在
    assert f"qa:highbase:{nq}" in fake_redis.store

    # 3) dislike → check_overconfident 从 highbase 检出（老逻辑会因 qa:* 没了而漏检）
    monkeypatch.setattr("app.core.metrics.OVERCONFIDENT.inc", lambda: None)
    collect_args = {}

    async def fake_collect(query, answer, confidence, grade, action,
                           source="auto", tenant="default"):
        collect_args["source"] = source
        collect_args["answer"] = answer
        return 1

    monkeypatch.setattr("app.services.evidence_gap_service.collect", fake_collect, raising=False)

    hit = await fos.check_overconfident(nq, tenant="default")

    assert hit is True
    assert collect_args["source"] == "overconfident"
    assert collect_args["answer"] == "high 答案"


# ===== 默认配置断言 =====

def test_default_overconfident_enable_is_true():
    """spec §8: CONFIDENCE_OVERCONFIDENT_ENABLE 默认改 True（原 False）。"""
    # 重新加载 Settings 取默认值（不读 .env）——直接查类属性
    from app.config import Settings
    assert Settings.model_fields["CONFIDENCE_OVERCONFIDENT_ENABLE"].default is True


def test_default_overconfident_baseline_ttl_is_30():
    """spec §8: 新增 OVERCONFIDENT_BASELINE_TTL_DAYS=30。"""
    from app.config import Settings
    assert Settings.model_fields["OVERCONFIDENT_BASELINE_TTL_DAYS"].default == 30
