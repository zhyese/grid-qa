"""N1 Agent 长期记忆层测试（最关键模块）。

测试重点（参考架构文档 R5 零回归 / R1 抽取成本 / 遗忘衰减策略）：
- recall 返回空字符串时 agent_runtime 行为零回归（ctx=None 跳过）
- recall 有记忆时 messages 注入正确（第2条 system 消息）
- extract_and_consolidate 是 fire-and-forget（不阻塞主流程，异常不外抛）
- forget 软删除（deleted_at 非空，recall 不返回）
- decay 时间衰减（90天x0.5, 180天x0.2）+ 容量上限500淘汰
- consolidate 去重逻辑
"""
import asyncio
import datetime
import json
import types

import pytest

from app.services import agent_memory_service
from app.services.agent_memory_service import agent_memory


# ===== 辅助：FakeSession 模拟 SQLAlchemy AsyncSession =====
class FakeResult:
    """模拟 db.execute() 返回的结果对象。"""

    def __init__(self, scalars_list=None, scalar_val=None):
        self._scalars = scalars_list or []
        self._scalar = scalar_val

    def scalar(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalars[0] if self._scalars else None

    def scalars(self):
        return self

    def all(self):
        return self._scalars


class FakeSession:
    """模拟 AsyncSession，按调用顺序返回预设结果。"""

    def __init__(self, results=None):
        self._results = results or []
        self._idx = 0
        self.added = []
        self.deleted = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def execute(self, query):
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return FakeResult(scalars_list=[], scalar_val=0)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True


def _make_memory_row(fact_id="f1", user_id="u1", fact_text="测试事实",
                     entity="1号主变", category="preference", weight=1.0,
                     deleted_at=None, last_hit_at=None, hit_count=0):
    """创建一个模拟记忆行对象（用 SimpleNamespace 避免 SQLAlchemy instrumentation）。"""
    now = datetime.datetime.now()
    return types.SimpleNamespace(
        id=1,
        fact_id=fact_id,
        user_id=user_id,
        scope="user",
        fact_text=fact_text,
        entity=entity,
        category=category,
        weight=weight,
        created_at=now,
        last_hit_at=last_hit_at or now,
        hit_count=hit_count,
        deleted_at=deleted_at,
    )


# ===== recall 零回归 =====
def test_recall_empty_query_returns_empty():
    """query 为空时 recall 返回空字符串。"""
    result = asyncio.run(agent_memory.recall("", "user1"))
    assert result == ""


def test_recall_empty_user_id_returns_empty():
    """user_id 为空时 recall 返回空字符串。"""
    result = asyncio.run(agent_memory.recall("1号主变油温高", ""))
    assert result == ""


def test_recall_all_services_fail_returns_empty(monkeypatch):
    """所有外部服务不可用时 recall 返回空字符串（零回归）。"""
    # mock Redis 不可用
    def fake_get_redis():
        raise ConnectionError("redis down")
    monkeypatch.setattr("app.clients.redis_client.get_redis", fake_get_redis)

    # mock embedding 不可用
    async def fake_embed_query(q):
        raise ConnectionError("embedding down")
    monkeypatch.setattr("app.services.embedding_service.embed_query", fake_embed_query)

    # mock Neo4j 不可用
    async def fake_get_prefs(user_id):
        raise ConnectionError("neo4j down")
    monkeypatch.setattr("app.clients.neo4j_client.get_user_preferences", fake_get_prefs)

    result = asyncio.run(agent_memory.recall("1号主变油温高", "user1"))
    assert result == ""


def test_recall_returns_formatted_text_when_memory_exists(monkeypatch):
    """有记忆时 recall 返回格式化的 system 消息文本。"""
    # mock Redis 返回热记忆
    class FakeRedis:
        async def zrevrange(self, key, start, end):
            return ["f1", "f2"]

    monkeypatch.setattr("app.clients.redis_client.get_redis", lambda: FakeRedis())

    # mock AsyncSessionLocal 返回热记忆文本
    fake_row1 = types.SimpleNamespace(category="preference", fact_text="用户负责110kV城东站")
    fake_row2 = types.SimpleNamespace(category="diagnosis", fact_text="1号主变已排除冷却器故障")

    fake_session = FakeSession(results=[
        FakeResult(scalars_list=[fake_row1, fake_row2]),  # Redis 热记忆查询
    ])
    monkeypatch.setattr(agent_memory_service, "AsyncSessionLocal", lambda: fake_session)

    # mock embedding 返回向量（但 search_memory 返回空）
    async def fake_embed_query(q):
        return [0.1] * 1024
    monkeypatch.setattr("app.services.embedding_service.embed_query", fake_embed_query)

    # mock Milvus search 返回空（只测 Redis 热记忆路径）
    monkeypatch.setattr("app.clients.milvus_client.search_memory", lambda vec, uid, topk=5: [])

    # mock Neo4j 返回空
    async def fake_get_prefs(user_id):
        return []
    monkeypatch.setattr("app.clients.neo4j_client.get_user_preferences", fake_get_prefs)

    result = asyncio.run(agent_memory.recall("1号主变", "user1"))
    assert "长期记忆" in result
    assert "用户负责110kV城东站" in result
    assert "1号主变已排除冷却器故障" in result


def test_recall_no_memory_returns_empty_string(monkeypatch):
    """无记忆时 recall 返回空字符串（不是 None，不是带 header 的空消息）。"""
    # 所有服务返回空
    class FakeRedis:
        async def zrevrange(self, key, start, end):
            return []
    monkeypatch.setattr("app.clients.redis_client.get_redis", lambda: FakeRedis())

    async def fake_embed_query(q):
        return [0.1] * 1024
    monkeypatch.setattr("app.services.embedding_service.embed_query", fake_embed_query)

    monkeypatch.setattr("app.clients.milvus_client.search_memory", lambda vec, uid, topk=5: [])

    async def fake_get_prefs(uid):
        return []
    monkeypatch.setattr("app.clients.neo4j_client.get_user_preferences", fake_get_prefs)

    result = asyncio.run(agent_memory.recall("query", "user1"))
    assert result == ""


# ===== agent_runtime 记忆注入集成测试 =====
from app.services import agent_runtime
from app.services.agent_runtime import Persona, Tool, ToolRegistry, run_agent


class CapturingProvider:
    """捕获 messages 的 FakeProvider。"""
    def __init__(self, script):
        self.script = script
        self.i = 0
        self.captured_messages = None

    async def chat_with_tools(self, messages, tools, tool_choice="auto",
                              temperature=0.2, max_tokens=2048, **kw):
        self.captured_messages = list(messages)
        resp = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return {"content": resp.get("content"), "tool_calls": resp.get("tool_calls")}


def _simple_registry():
    reg = ToolRegistry()
    async def h(db, mt, **a):
        return "ok"
    reg.register(Tool("h1", "d", {"type": "object"}, h))
    return reg


def test_agent_runtime_ctx_none_skips_recall(monkeypatch):
    """ctx=None 时不调用 recall（diagnose 老链路零回归）。"""
    recall_called = {"called": False}

    async def mock_recall(*args, **kwargs):
        recall_called["called"] = True
        return ""
    monkeypatch.setattr(agent_memory, "recall", mock_recall)

    persona = Persona(name="qa", system_prompt="s", allowed_tools=["h1"])
    fake = CapturingProvider([{"content": "答案", "tool_calls": None}])
    monkeypatch.setattr(agent_runtime, "get_llm_provider", lambda mt: fake)

    asyncio.run(run_agent(None, persona, "q", registry=_simple_registry()))
    assert recall_called["called"] is False  # ctx=None → recall 未被调用


def test_agent_runtime_ctx_none_messages_has_empty_second_system(monkeypatch):
    """ctx=None 时 messages 第2条是空 system 消息（零行为变化）。"""
    persona = Persona(name="qa", system_prompt="你是电网运维助手", allowed_tools=["h1"])
    fake = CapturingProvider([{"content": "答案", "tool_calls": None}])
    monkeypatch.setattr(agent_runtime, "get_llm_provider", lambda mt: fake)

    asyncio.run(run_agent(None, persona, "用户问题", registry=_simple_registry()))
    msgs = fake.captured_messages
    assert len(msgs) == 3
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "你是电网运维助手"
    assert msgs[1]["role"] == "system"
    assert msgs[1]["content"] == ""  # 空字符串 = 无记忆 = 零回归
    assert msgs[2]["role"] == "user"
    assert msgs[2]["content"] == "用户问题"


def test_agent_runtime_recall_empty_injects_empty_system(monkeypatch):
    """recall 返回空字符串时 messages 第2条是空 system（零行为变化）。"""
    async def mock_recall(*args, **kwargs):
        return ""
    monkeypatch.setattr(agent_memory, "recall", mock_recall)

    persona = Persona(name="qa", system_prompt="s", allowed_tools=["h1"])
    fake = CapturingProvider([{"content": "答案", "tool_calls": None}])
    monkeypatch.setattr(agent_runtime, "get_llm_provider", lambda mt: fake)

    asyncio.run(run_agent(None, persona, "q", registry=_simple_registry(),
                          ctx={"username": "user1"}))
    msgs = fake.captured_messages
    assert msgs[1]["content"] == ""  # recall 返回空 → 空 system 消息


def test_agent_runtime_recall_text_injects_into_messages(monkeypatch):
    """recall 有记忆时注入到 messages 第2条 system 消息。"""
    memory_text = "以下是关于该用户的长期记忆：\n- [preference] 用户负责110kV城东站"
    async def mock_recall(*args, **kwargs):
        return memory_text
    monkeypatch.setattr(agent_memory, "recall", mock_recall)

    persona = Persona(name="qa", system_prompt="你是助手", allowed_tools=["h1"])
    fake = CapturingProvider([{"content": "答案", "tool_calls": None}])
    monkeypatch.setattr(agent_runtime, "get_llm_provider", lambda mt: fake)

    asyncio.run(run_agent(None, persona, "1号主变", registry=_simple_registry(),
                          ctx={"username": "user1"}))
    msgs = fake.captured_messages
    assert msgs[1]["role"] == "system"
    assert msgs[1]["content"] == memory_text  # 记忆注入到第2条 system


def test_agent_runtime_recall_exception_does_not_crash(monkeypatch):
    """recall 抛异常时不崩溃（降级为空字符串）。"""
    async def mock_recall(*args, **kwargs):
        raise RuntimeError("memory service down")
    monkeypatch.setattr(agent_memory, "recall", mock_recall)

    persona = Persona(name="qa", system_prompt="s", allowed_tools=["h1"])
    fake = CapturingProvider([{"content": "答案", "tool_calls": None}])
    monkeypatch.setattr(agent_runtime, "get_llm_provider", lambda mt: fake)

    result = asyncio.run(run_agent(None, persona, "q", registry=_simple_registry(),
                                    ctx={"username": "user1"}))
    assert result.degraded is False  # recall 异常不影响主流程
    assert result.answer == "答案"


# ===== extract_and_consolidate fire-and-forget =====
def test_extract_and_consolidate_exception_does_not_propagate(monkeypatch):
    """extract_and_consolidate 内部异常不外抛（fire-and-forget）。"""
    async def mock_extract_facts(*args, **kwargs):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(agent_memory, "extract_facts", mock_extract_facts)

    # 不应抛异常
    asyncio.run(agent_memory.extract_and_consolidate("用户问题", "AI回答", "user1"))


def test_extract_and_consolidate_consolidate_exception_does_not_propagate(monkeypatch):
    """consolidate 异常不外抛（fire-and-forget）。"""
    async def mock_extract_facts(user_msg, answer, model_type=None):
        return [{"fact": "测试事实", "entity": "1号主变", "category": "preference"}]

    async def mock_consolidate(*args, **kwargs):
        raise RuntimeError("DB down")

    monkeypatch.setattr(agent_memory, "extract_facts", mock_extract_facts)
    monkeypatch.setattr(agent_memory, "consolidate", mock_consolidate)

    # 不应抛异常
    asyncio.run(agent_memory.extract_and_consolidate("用户问题", "AI回答", "user1"))


def test_agent_runtime_extract_and_consolidate_fire_and_forget(monkeypatch):
    """run_agent 结束后 fire-and-forget 触发 extract_and_consolidate（不阻塞响应）。"""
    extract_called = {"called": False}

    async def mock_extract_and_consolidate(*args, **kwargs):
        extract_called["called"] = True

    async def mock_recall(*args, **kwargs):
        return ""

    monkeypatch.setattr(agent_memory, "recall", mock_recall)
    monkeypatch.setattr(agent_memory, "extract_and_consolidate", mock_extract_and_consolidate)

    persona = Persona(name="qa", system_prompt="s", allowed_tools=["h1"])
    fake = CapturingProvider([{"content": "答案", "tool_calls": None}])
    monkeypatch.setattr(agent_runtime, "get_llm_provider", lambda mt: fake)

    result = asyncio.run(run_agent(None, persona, "q", registry=_simple_registry(),
                                    ctx={"username": "user1"}))
    # 主流程正常返回
    assert result.answer == "答案"
    # fire-and-forget 被触发
    assert extract_called["called"] is True


def test_agent_runtime_extract_not_triggered_without_ctx(monkeypatch):
    """ctx=None 时不触发 extract_and_consolidate（零回归）。"""
    extract_called = {"called": False}

    async def mock_extract_and_consolidate(*args, **kwargs):
        extract_called["called"] = True

    monkeypatch.setattr(agent_memory, "extract_and_consolidate", mock_extract_and_consolidate)

    persona = Persona(name="qa", system_prompt="s", allowed_tools=["h1"])
    fake = CapturingProvider([{"content": "答案", "tool_calls": None}])
    monkeypatch.setattr(agent_runtime, "get_llm_provider", lambda mt: fake)

    asyncio.run(run_agent(None, persona, "q", registry=_simple_registry()))  # 无 ctx
    assert extract_called["called"] is False


# ===== forget 软删除 =====
def test_forget_sets_deleted_at(monkeypatch):
    """forget 设置 deleted_at 非空（软删除）。"""
    row = _make_memory_row(fact_id="f1", user_id="u1")
    fake_session = FakeSession(results=[
        FakeResult(scalars_list=[row]),  # select 查到行
    ])
    monkeypatch.setattr(agent_memory_service, "AsyncSessionLocal", lambda: fake_session)

    # mock Redis
    class FakeRedis:
        async def zrem(self, key, member):
            pass
    monkeypatch.setattr("app.clients.redis_client.get_redis", lambda: FakeRedis())

    result = asyncio.run(agent_memory.forget("f1"))
    assert result is True
    assert row.deleted_at is not None  # deleted_at 被设置
    assert fake_session.committed is True


def test_forget_nonexistent_returns_false(monkeypatch):
    """forget 不存在的 memory_id 返回 False。"""
    fake_session = FakeSession(results=[
        FakeResult(scalars_list=[]),  # 查不到行
    ])
    monkeypatch.setattr(agent_memory_service, "AsyncSessionLocal", lambda: fake_session)

    result = asyncio.run(agent_memory.forget("nonexistent"))
    assert result is False


def test_forget_redis_error_does_not_crash(monkeypatch):
    """forget 时 Redis 异常不崩溃。"""
    row = _make_memory_row(fact_id="f1", user_id="u1")
    fake_session = FakeSession(results=[
        FakeResult(scalars_list=[row]),
    ])
    monkeypatch.setattr(agent_memory_service, "AsyncSessionLocal", lambda: fake_session)

    def fake_get_redis():
        raise ConnectionError("redis down")
    monkeypatch.setattr("app.clients.redis_client.get_redis", fake_get_redis)

    result = asyncio.run(agent_memory.forget("f1"))
    assert result is True  # 软删除成功，Redis 异常被吞


# ===== decay 时间衰减 =====
def test_decay_90d_half_weight(monkeypatch):
    """90天未命中 weight x 0.5。"""
    now = datetime.datetime.now()
    d90 = now - datetime.timedelta(days=120)  # 120天前（在90-180天之间）

    row_90 = _make_memory_row(fact_id="f90", weight=1.0, last_hit_at=d90)

    fake_session = FakeSession(results=[
        FakeResult(scalars_list=[]),    # 物理删除查询（无超期软删除）
        FakeResult(scalars_list=[]),    # 180天查询（无）
        FakeResult(scalars_list=[row_90]),  # 90天查询
    ])
    monkeypatch.setattr(agent_memory_service, "AsyncSessionLocal", lambda: fake_session)

    # mock Milvus delete
    monkeypatch.setattr("app.clients.milvus_client.delete_memory", lambda fid: None)

    count = asyncio.run(agent_memory.decay())
    assert count == 1
    assert row_90.weight == pytest.approx(0.5)  # 1.0 * 0.5


def test_decay_180d_weight(monkeypatch):
    """180天未命中 weight x 0.2。"""
    now = datetime.datetime.now()
    d200 = now - datetime.timedelta(days=200)  # 200天前（超过180天）

    row_180 = _make_memory_row(fact_id="f180", weight=1.0, last_hit_at=d200)

    fake_session = FakeSession(results=[
        FakeResult(scalars_list=[]),        # 物理删除查询
        FakeResult(scalars_list=[row_180]), # 180天查询
        FakeResult(scalars_list=[]),        # 90天查询
    ])
    monkeypatch.setattr(agent_memory_service, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr("app.clients.milvus_client.delete_memory", lambda fid: None)

    count = asyncio.run(agent_memory.decay())
    assert count == 1
    assert row_180.weight == pytest.approx(0.2)  # 1.0 * 0.2


def test_decay_both_90d_and_180d(monkeypatch):
    """同时有90天和180天未命中的记忆。"""
    now = datetime.datetime.now()
    d120 = now - datetime.timedelta(days=120)
    d200 = now - datetime.timedelta(days=200)

    row_90 = _make_memory_row(fact_id="f90", weight=2.0, last_hit_at=d120)
    row_180 = _make_memory_row(fact_id="f180", weight=2.0, last_hit_at=d200)

    fake_session = FakeSession(results=[
        FakeResult(scalars_list=[]),            # 物理删除查询
        FakeResult(scalars_list=[row_180]),     # 180天查询
        FakeResult(scalars_list=[row_90]),      # 90天查询
    ])
    monkeypatch.setattr(agent_memory_service, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr("app.clients.milvus_client.delete_memory", lambda fid: None)

    count = asyncio.run(agent_memory.decay())
    assert count == 2
    assert row_90.weight == pytest.approx(1.0)    # 2.0 * 0.5
    assert row_180.weight == pytest.approx(0.4)   # 2.0 * 0.2


def test_decay_physical_deletes_old_soft_deleted(monkeypatch):
    """物理删除软删除超过30天的记忆。"""
    now = datetime.datetime.now()
    cutoff = now - datetime.timedelta(days=35)  # 35天前软删除（超过30天）

    old_row = _make_memory_row(fact_id="fold", deleted_at=cutoff)

    fake_session = FakeSession(results=[
        FakeResult(scalars_list=[old_row]),  # 物理删除查询
        FakeResult(scalars_list=[]),         # 180天查询
        FakeResult(scalars_list=[]),         # 90天查询
    ])
    monkeypatch.setattr(agent_memory_service, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr("app.clients.milvus_client.delete_memory", lambda fid: None)

    count = asyncio.run(agent_memory.decay())
    assert count == 0  # 只衰减，不计数物理删除
    assert len(fake_session.deleted) == 1  # 物理删除了一行


# ===== consolidate 去重 + 容量管理 =====
def test_consolidate_empty_facts_returns_zero():
    """空 facts 列表直接返回 0。"""
    result = asyncio.run(agent_memory.consolidate("u1", []))
    assert result == 0


def test_consolidate_dedup_skips_existing(monkeypatch):
    """consolidate 跳过已存在的 fact_text（去重）。"""
    existing_text = "用户负责110kV城东站"

    fake_session = FakeSession(results=[
        FakeResult(scalar_val=1),                    # 当前记忆数=1
        # select(AgentMemory.fact_text).scalars().all() 返回字符串列表
        FakeResult(scalars_list=[existing_text]),
    ])
    monkeypatch.setattr(agent_memory_service, "AsyncSessionLocal", lambda: fake_session)

    # mock embedding（不应被调用，因为去重后无新事实）
    embed_called = {"called": False}
    async def fake_embed_texts(texts):
        embed_called["called"] = True
        return [[0.1] * 1024 for _ in texts]
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed_texts)

    facts = [{"fact": existing_text, "entity": "110kV城东站", "category": "preference"}]
    result = asyncio.run(agent_memory.consolidate("u1", facts))
    assert result == 0  # 全部去重，写入0条
    assert embed_called["called"] is False


def test_consolidate_capacity_full_evicts_lowest(monkeypatch):
    """容量满时淘汰低权重记忆。"""
    from app.config import settings
    monkeypatch.setattr(settings, "MEMORY_CAPACITY", 2)  # 容量=2

    now = datetime.datetime.now()
    evict_row = _make_memory_row(fact_id="evict1", weight=0.3,
                                  last_hit_at=now - datetime.timedelta(days=100))

    fake_session = FakeSession(results=[
        FakeResult(scalar_val=2),                        # 当前记忆数=2（满）
        FakeResult(scalars_list=[evict_row]),            # 淘汰查询
        FakeResult(scalars_list=[]),                     # 去重查询（无已有）
    ])
    monkeypatch.setattr(agent_memory_service, "AsyncSessionLocal", lambda: fake_session)

    # mock embedding
    async def fake_embed_texts(texts):
        return [[0.1] * 1024 for _ in texts]
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed_texts)

    # mock Milvus/Neo4j/Redis
    monkeypatch.setattr("app.clients.milvus_client.insert_memory", lambda **kw: None)
    async def fake_upsert(uid, entity, cat):
        pass
    monkeypatch.setattr("app.clients.neo4j_client.upsert_user_preference", fake_upsert)

    class FakeRedis:
        async def zadd(self, key, mapping):
            pass
    monkeypatch.setattr("app.clients.redis_client.get_redis", lambda: FakeRedis())

    facts = [{"fact": "新事实", "entity": "1号主变", "category": "diagnosis"}]
    result = asyncio.run(agent_memory.consolidate("u1", facts))
    assert result == 1  # 淘汰1条，写入1条
    assert evict_row.deleted_at is not None  # 被淘汰的记忆 soft-deleted


def test_consolidate_writes_new_fact(monkeypatch):
    """consolidate 写入新事实到 MySQL + Milvus + Neo4j + Redis。"""
    fake_session = FakeSession(results=[
        FakeResult(scalar_val=0),                    # 当前记忆数=0
        FakeResult(scalars_list=[]),                 # 去重查询（无已有）
    ])
    monkeypatch.setattr(agent_memory_service, "AsyncSessionLocal", lambda: fake_session)

    milvus_calls = {"insert": 0}
    neo4j_calls = {"upsert": 0}
    redis_calls = {"zadd": 0}

    async def fake_embed_texts(texts):
        return [[0.1] * 1024 for _ in texts]
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed_texts)

    def fake_insert_memory(**kw):
        milvus_calls["insert"] += 1
    monkeypatch.setattr("app.clients.milvus_client.insert_memory", fake_insert_memory)

    async def fake_upsert(uid, entity, cat):
        neo4j_calls["upsert"] += 1
    monkeypatch.setattr("app.clients.neo4j_client.upsert_user_preference", fake_upsert)

    class FakeRedis:
        async def zadd(self, key, mapping):
            redis_calls["zadd"] += 1
    monkeypatch.setattr("app.clients.redis_client.get_redis", lambda: FakeRedis())

    facts = [{"fact": "新事实", "entity": "1号主变", "category": "diagnosis"}]
    result = asyncio.run(agent_memory.consolidate("u1", facts))
    assert result == 1
    assert milvus_calls["insert"] == 1
    assert neo4j_calls["upsert"] == 1
    assert redis_calls["zadd"] == 1
    assert len(fake_session.added) == 1  # MySQL 写入1条
    assert fake_session.committed is True


def test_consolidate_embedding_error_returns_zero(monkeypatch):
    """embedding 服务异常时 consolidate 返回 0（降级不崩）。"""
    fake_session = FakeSession(results=[
        FakeResult(scalar_val=0),
        FakeResult(scalars_list=[]),  # 去重查询（无已有）
    ])
    monkeypatch.setattr(agent_memory_service, "AsyncSessionLocal", lambda: fake_session)

    async def fake_embed_texts(texts):
        raise RuntimeError("embedding API down")
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed_texts)

    facts = [{"fact": "新事实", "entity": "1号主变", "category": "diagnosis"}]
    result = asyncio.run(agent_memory.consolidate("u1", facts))
    assert result == 0  # embedding 失败，不写入


# ===== extract_facts LLM 抽取 =====
def test_extract_facts_parses_json_array(monkeypatch):
    """extract_facts 解析 LLM 返回的 JSON 数组。"""
    class FakeProvider:
        async def chat(self, messages, temperature=0.1, max_tokens=300):
            return '[{"fact":"用户负责110kV城东站","entity":"110kV城东站","category":"preference"}]'

    monkeypatch.setattr(agent_memory_service, "get_llm_provider", lambda mt: FakeProvider())

    facts = asyncio.run(agent_memory.extract_facts("用户提问", "AI回答"))
    assert len(facts) == 1
    assert facts[0]["fact"] == "用户负责110kV城东站"
    assert facts[0]["entity"] == "110kV城东站"
    assert facts[0]["category"] == "preference"


def test_extract_facts_llm_error_returns_empty(monkeypatch):
    """LLM 调用异常时 extract_facts 返回空列表。"""
    class FakeProvider:
        async def chat(self, messages, temperature=0.1, max_tokens=300):
            raise RuntimeError("API down")

    monkeypatch.setattr(agent_memory_service, "get_llm_provider", lambda mt: FakeProvider())

    facts = asyncio.run(agent_memory.extract_facts("用户提问", "AI回答"))
    assert facts == []


def test_extract_facts_invalid_json_returns_empty(monkeypatch):
    """LLM 返回非 JSON 时 extract_facts 返回空列表。"""
    class FakeProvider:
        async def chat(self, messages, temperature=0.1, max_tokens=300):
            return "这不是JSON"

    monkeypatch.setattr(agent_memory_service, "get_llm_provider", lambda mt: FakeProvider())

    facts = asyncio.run(agent_memory.extract_facts("用户提问", "AI回答"))
    assert facts == []


def test_extract_facts_filters_long_facts(monkeypatch):
    """超过200字的事实被过滤。"""
    long_fact = "A" * 201
    class FakeProvider:
        async def chat(self, messages, temperature=0.1, max_tokens=300):
            return json.dumps([
                {"fact": long_fact, "entity": "e", "category": "preference"},
                {"fact": "正常事实", "entity": "e", "category": "diagnosis"},
            ])

    monkeypatch.setattr(agent_memory_service, "get_llm_provider", lambda mt: FakeProvider())

    facts = asyncio.run(agent_memory.extract_facts("用户提问", "AI回答"))
    assert len(facts) == 1
    assert facts[0]["fact"] == "正常事实"


def test_extract_facts_invalid_category_defaults_to_preference(monkeypatch):
    """无效 category 默认归为 preference。"""
    class FakeProvider:
        async def chat(self, messages, temperature=0.1, max_tokens=300):
            return json.dumps([
                {"fact": "事实", "entity": "e", "category": "invalid_category"},
            ])

    monkeypatch.setattr(agent_memory_service, "get_llm_provider", lambda mt: FakeProvider())

    facts = asyncio.run(agent_memory.extract_facts("用户提问", "AI回答"))
    assert len(facts) == 1
    assert facts[0]["category"] == "preference"  # 默认归为 preference


# ===== list_memories / get_stats 管理端 =====
def test_list_memories_returns_paginated(monkeypatch):
    """list_memories 返回分页结果。"""
    row = _make_memory_row(fact_id="f1", user_id="u1")
    fake_session = FakeSession(results=[
        FakeResult(scalar_val=1),           # total count
        FakeResult(scalars_list=[row]),     # rows
    ])
    monkeypatch.setattr(agent_memory_service, "AsyncSessionLocal", lambda: fake_session)

    result = asyncio.run(agent_memory.list_memories("u1", page=1, size=20))
    assert result["total"] == 1
    assert len(result["list"]) == 1
    assert result["list"][0]["factId"] == "f1"


def test_get_stats_returns_summary(monkeypatch):
    """get_stats 返回记忆统计。"""
    fake_session = FakeSession(results=[
        FakeResult(scalar_val=10),  # total
        FakeResult(scalar_val=8),   # active
        FakeResult(scalar_val=3),   # distinct users
        FakeResult(scalars_list=[("preference", 5), ("diagnosis", 3)]),  # by category
    ])
    monkeypatch.setattr(agent_memory_service, "AsyncSessionLocal", lambda: fake_session)

    result = asyncio.run(agent_memory.get_stats())
    assert result["total"] == 10
    assert result["active"] == 8
    assert result["deleted"] == 2
    assert result["users"] == 3
    assert result["byCategory"]["preference"] == 5
    assert result["byCategory"]["diagnosis"] == 3
