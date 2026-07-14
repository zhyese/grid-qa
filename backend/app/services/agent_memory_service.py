"""N1 Agent 长期记忆服务：extract_facts / consolidate / recall / forget / decay。

三层存储复用：
- Milvus（memory_collection）：向量记忆，recall 时语义检索
- Neo4j（User→PREFERS→Entity）：图记忆，recall 时遍历用户偏好
- Redis（memory:hot:{user_id} ZSet）：热记忆，秒级召回最近命中的事实
- MySQL（agent_memory 表）：审计 + 软删除 + 容量管理

零回归原则：
- recall 返回空字符串 = 无记忆 = 零行为变化
- ctx=None 时调用方应跳过 recall（由 agent_runtime 控制）
- extract_facts 失败时降级跳过（不影响主流程）
"""
import datetime
import json
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.obs import degraded
from app.core.otel_genai import get_trace_id, trace_span
from app.db.session import AsyncSessionLocal
from app.models.agent_memory import AgentMemory
from app.providers.factory import get_llm_provider

# scope 枚举
SCOPE_USER = "user"
SCOPE_DEVICE = "device"

# category 枚举
CATEGORY_PREFERENCE = "preference"
CATEGORY_DIAGNOSIS = "diagnosis"
CATEGORY_PENDING = "pending"

# extract_facts prompt（极简化：只抽原子事实，≤200 token 输出）
_EXTRACT_PROMPT = """你是电网运维对话记忆抽取器。从下面的用户提问和AI回答中，抽取值得长期记住的原子事实。

【只抽这三类】
1. preference: 用户偏好/习惯/负责的变电站/常问的设备类型（如"用户负责110kV城东站"）
2. diagnosis: 诊断结论/已排除的故障/已确认的原因（如"1号主变油温高已排除冷却器故障"）
3. pending: 待确认项/待排查方向（如"待确认1号主变是否负载过高"）

【严格要求】
1. 只抽明确的事实，不编造。
2. 每条事实 ≤30 字，原子化（一句话一个事实）。
3. 输出严格 JSON 数组，每条含 fact/entity/category 三个字段：
   [{{"fact":"用户负责110kV城东站","entity":"110kV城东站","category":"preference"}}]
4. 无值得记住的事实时输出 []。不要解释、不要 markdown。

【用户提问】{user_msg}
【AI回答】{answer}"""


class _AgentMemoryService:
    """Agent 记忆服务单例（无状态，方法级独立 session）。"""

    async def extract_facts(self, user_msg: str, answer: str,
                            model_type: str | None = None) -> list[dict]:
        """从一轮对话中抽取结构化事实（用 DeepSeek 最便宜档）。

        Returns:
            [{"fact": str, "entity": str, "category": str}, ...]
        """
        provider = get_llm_provider(model_type or "deepseek")
        prompt = _EXTRACT_PROMPT.format(
            user_msg=(user_msg or "")[:1000],
            answer=(answer or "")[:2000],
        )
        try:
            ans = await provider.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1, max_tokens=300,
            )
        except Exception as e:
            degraded("memory_extract_facts", e)
            return []

        # 解析 JSON 数组
        import re
        m = re.search(r"\[.*\]", ans or "", re.S)
        if not m:
            return []
        try:
            arr = json.loads(m.group(0))
        except Exception:
            return []

        facts: list[dict] = []
        valid_categories = {CATEGORY_PREFERENCE, CATEGORY_DIAGNOSIS, CATEGORY_PENDING}
        for item in arr:
            if not isinstance(item, dict):
                continue
            fact = str(item.get("fact", "")).strip()
            entity = str(item.get("entity", "")).strip()
            category = str(item.get("category", "")).strip()
            if not fact or len(fact) > 200:
                continue
            if category not in valid_categories:
                category = CATEGORY_PREFERENCE
            facts.append({"fact": fact, "entity": entity, "category": category})
        return facts

    async def consolidate(self, user_id: str, facts: list[dict],
                          model_type: str | None = None) -> int:
        """整合新事实：去重/消解/合并 → 写入 Milvus + Neo4j + Redis + MySQL。

        Returns: 实际写入的条数
        """
        if not facts:
            return 0

        # 容量管理：检查当前用户记忆数，超上限时淘汰低权重
        async with AsyncSessionLocal() as db:
            current_count = (await db.execute(
                select(func.count()).select_from(AgentMemory)
                .where(AgentMemory.user_id == user_id, AgentMemory.deleted_at.is_(None))
            )).scalar() or 0

            capacity = settings.MEMORY_CAPACITY
            slots_available = max(0, capacity - current_count)
            if slots_available == 0:
                # 淘汰权重最低的 facts_to_write 条
                to_evict = len(facts)
                evict_rows = (await db.execute(
                    select(AgentMemory)
                    .where(AgentMemory.user_id == user_id, AgentMemory.deleted_at.is_(None))
                    .order_by(AgentMemory.weight.asc(), AgentMemory.last_hit_at.asc())
                    .limit(to_evict)
                )).scalars().all()
                for row in evict_rows:
                    row.deleted_at = datetime.datetime.now()
                await db.commit()
                slots_available = to_evict

            facts_to_write = facts[:slots_available]
            if not facts_to_write:
                return 0

            # 查已有记忆做去重（fact_text 完全相同则跳过）
            existing_texts = set()
            existing_rows = (await db.execute(
                select(AgentMemory.fact_text)
                .where(AgentMemory.user_id == user_id, AgentMemory.deleted_at.is_(None))
            )).scalars().all()
            existing_texts = {t for t in existing_rows if t}

            new_facts = [f for f in facts_to_write if f["fact"] not in existing_texts]
            if not new_facts:
                return 0

            # 生成 embedding 向量（批量）
            from app.services.embedding_service import embed_texts
            try:
                vectors = await embed_texts([f["fact"] for f in new_facts])
            except Exception as e:
                degraded("memory_embed", e)
                return 0

            # 写 MySQL（审计 + 容量管理）
            written = 0
            now = datetime.datetime.now()
            for i, f in enumerate(new_facts):
                fact_id = str(uuid.uuid4())
                mem = AgentMemory(
                    fact_id=fact_id,
                    user_id=user_id,
                    scope=SCOPE_USER,
                    fact_text=f["fact"],
                    entity=f.get("entity", ""),
                    category=f.get("category", CATEGORY_PREFERENCE),
                    weight=1.0,
                    created_at=now,
                    last_hit_at=now,
                    hit_count=0,
                )
                db.add(mem)
                # 写 Milvus memory_collection
                try:
                    from app.clients.milvus_client import insert_memory
                    insert_memory(
                        fact_id=fact_id,
                        embedding=vectors[i],
                        fact_text=f["fact"],
                        user_id=user_id,
                        scope=SCOPE_USER,
                        entity=f.get("entity", ""),
                        category=f.get("category", CATEGORY_PREFERENCE),
                    )
                except Exception as e:
                    degraded("memory_milvus_insert", e)
                # 写 Neo4j 图记忆（User→PREFERS→Entity）
                if f.get("entity"):
                    try:
                        from app.clients.neo4j_client import upsert_user_preference
                        await upsert_user_preference(user_id, f["entity"], f.get("category", CATEGORY_PREFERENCE))
                    except Exception as e:
                        degraded("memory_neo4j_upsert", e)
                # 写 Redis 热记忆
                try:
                    from app.clients.redis_client import get_redis
                    await get_redis().zadd(f"memory:hot:{user_id}", {fact_id: now.timestamp()})
                except Exception:
                    pass
                written += 1
            await db.commit()
            return written

    async def recall(self, query: str, user_id: str, scope: str = "user") -> str:
        """召回与当前查询相关的记忆，返回格式化的 system 消息文本。

        零回归：无记忆时返回空字符串。
        三层召回：Redis 热记忆（秒级）+ Milvus 向量记忆（语义）+ Neo4j 图记忆（偏好）。
        """
        if not query or not user_id:
            return ""

        parts: list[str] = []

        # 1. Redis 热记忆（最近命中的 Top-5）
        try:
            from app.clients.redis_client import get_redis
            r = get_redis()
            hot_ids = await r.zrevrange(f"memory:hot:{user_id}", 0, 4)
            if hot_ids:
                async with AsyncSessionLocal() as db:
                    rows = (await db.execute(
                        select(AgentMemory.fact_text, AgentMemory.category)
                        .where(AgentMemory.fact_id.in_(hot_ids), AgentMemory.deleted_at.is_(None))
                    )).all()
                    for row in rows:
                        parts.append(f"[{row.category}] {row.fact_text}")
        except Exception:
            pass

        # 2. Milvus 向量记忆（语义检索 Top-5）
        try:
            from app.services.embedding_service import embed_query
            from app.clients.milvus_client import search_memory
            query_vec = await embed_query(query)
            hits = search_memory(query_vec, user_id, topk=5)
            if hits:
                # 更新命中计数 + 热记忆
                async with AsyncSessionLocal() as db:
                    for hit in hits:
                        fact_id = hit.get("pk", "")
                        fact_text = hit.get("text", "")
                        category = hit.get("category", "")
                        if fact_text and f"[{category}] {fact_text}" not in parts:
                            parts.append(f"[{category}] {fact_text}")
                        # 更新命中
                        if fact_id:
                            await db.execute(
                                update(AgentMemory)
                                .where(AgentMemory.fact_id == fact_id)
                                .values(
                                    hit_count=AgentMemory.hit_count + 1,
                                    last_hit_at=datetime.datetime.now(),
                                )
                            )
                            try:
                                from app.clients.redis_client import get_redis
                                await get_redis().zadd(
                                    f"memory:hot:{user_id}",
                                    {fact_id: datetime.datetime.now().timestamp()},
                                )
                            except Exception:
                                pass
                    await db.commit()
        except Exception as e:
            degraded("memory_recall_milvus", e)

        # 3. Neo4j 图记忆（用户偏好实体）
        try:
            from app.clients.neo4j_client import get_user_preferences
            prefs = await get_user_preferences(user_id)
            if prefs:
                for p in prefs[:3]:
                    label = f"偏好:{p}" if p not in [x for x in parts] else None
                    if label:
                        parts.append(label)
        except Exception:
            pass

        if not parts:
            return ""

        # 格式化为 system 消息
        trace_id = get_trace_id()
        header = "以下是关于该用户的长期记忆（供参考，不要直接暴露给用户）："
        body = "\n".join(f"- {p}" for p in parts)
        return f"{header}\n{body}"

    async def forget(self, memory_id: str) -> bool:
        """软删除一条记忆（deleted_at = NOW()）。"""
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(AgentMemory).where(AgentMemory.fact_id == memory_id)
            )).scalar_one_or_none()
            if not row:
                return False
            row.deleted_at = datetime.datetime.now()
            await db.commit()
            # 从 Redis 热记忆移除
            try:
                from app.clients.redis_client import get_redis
                await get_redis().zrem(f"memory:hot:{row.user_id}", memory_id)
            except Exception:
                pass
            return True

    async def decay(self) -> int:
        """时间衰减：90 天未命中 weight×0.5，180 天×0.2。物理删除软删除超 30 天的。

        Returns: 衰减的记忆条数
        """
        now = datetime.datetime.now()
        decay_90d = settings.MEMORY_DECAY_90D
        decay_180d = settings.MEMORY_DECAY_180D
        soft_delete_days = settings.MEMORY_SOFT_DELETE_DAYS

        async with AsyncSessionLocal() as db:
            # 1. 物理删除软删除超期的
            cutoff = now - datetime.timedelta(days=soft_delete_days)
            old_rows = (await db.execute(
                select(AgentMemory).where(
                    AgentMemory.deleted_at.is_not(None),
                    AgentMemory.deleted_at < cutoff,
                )
            )).scalars().all()
            for row in old_rows:
                # 同步删除 Milvus 中的向量
                try:
                    from app.clients.milvus_client import delete_memory
                    delete_memory(row.fact_id)
                except Exception:
                    pass
                await db.delete(row)

            # 2. 时间衰减
            d90 = now - datetime.timedelta(days=90)
            d180 = now - datetime.timedelta(days=180)
            # 180 天未命中
            rows_180 = (await db.execute(
                select(AgentMemory).where(
                    AgentMemory.deleted_at.is_(None),
                    AgentMemory.last_hit_at < d180,
                )
            )).scalars().all()
            for row in rows_180:
                row.weight = row.weight * decay_180d

            # 90 天未命中（但不到 180 天）
            rows_90 = (await db.execute(
                select(AgentMemory).where(
                    AgentMemory.deleted_at.is_(None),
                    AgentMemory.last_hit_at < d90,
                    AgentMemory.last_hit_at >= d180,
                )
            )).scalars().all()
            for row in rows_90:
                row.weight = row.weight * decay_90d

            await db.commit()
            return len(rows_180) + len(rows_90)

    async def extract_and_consolidate(self, user_msg: str, answer: str,
                                      user_id: str, model_type: str | None = None) -> None:
        """fire-and-forget：抽取事实 → 整合写入（不阻塞主流程）。

        独立 session，异常不外抛。
        """
        try:
            with trace_span("memory.extract"):
                facts = await self.extract_facts(user_msg, answer, model_type)
                if facts:
                    n = await self.consolidate(user_id, facts, model_type)
                    if n:
                        from app.core.otel_genai import set_attribute
                        set_attribute("memory.facts_extracted", len(facts))
                        set_attribute("memory.facts_written", n)
        except Exception as e:
            degraded("memory_extract_and_consolidate", e)

    async def list_memories(self, user_id: str = "", page: int = 1,
                            size: int = 20) -> dict:
        """管理端：分页查询记忆列表（含已删除）。"""
        async with AsyncSessionLocal() as db:
            stmt = select(AgentMemory)
            cnt_stmt = select(func.count()).select_from(AgentMemory)
            if user_id:
                stmt = stmt.where(AgentMemory.user_id == user_id)
                cnt_stmt = cnt_stmt.where(AgentMemory.user_id == user_id)
            total = (await db.execute(cnt_stmt)).scalar() or 0
            rows = (await db.execute(
                stmt.order_by(AgentMemory.created_at.desc())
                .offset((page - 1) * size).limit(size)
            )).scalars().all()
            return {
                "total": total,
                "list": [{
                    "factId": r.fact_id,
                    "userId": r.user_id,
                    "scope": r.scope,
                    "factText": r.fact_text,
                    "entity": r.entity,
                    "category": r.category,
                    "weight": r.weight,
                    "hitCount": r.hit_count,
                    "createdAt": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
                    "lastHitAt": r.last_hit_at.strftime("%Y-%m-%d %H:%M:%S") if r.last_hit_at else "",
                    "deleted": r.deleted_at is not None,
                } for r in rows],
            }

    async def get_stats(self) -> dict:
        """管理端：记忆统计。"""
        async with AsyncSessionLocal() as db:
            total = (await db.execute(
                select(func.count()).select_from(AgentMemory)
            )).scalar() or 0
            active = (await db.execute(
                select(func.count()).select_from(AgentMemory)
                .where(AgentMemory.deleted_at.is_(None))
            )).scalar() or 0
            deleted = total - active
            users = (await db.execute(
                select(func.count(func.distinct(AgentMemory.user_id)))
            )).scalar() or 0
            by_category = (await db.execute(
                select(AgentMemory.category, func.count())
                .where(AgentMemory.deleted_at.is_(None))
                .group_by(AgentMemory.category)
            )).all()
            return {
                "total": total,
                "active": active,
                "deleted": deleted,
                "users": users,
                "byCategory": {r[0]: r[1] for r in by_category},
            }


# 单例
agent_memory = _AgentMemoryService()
