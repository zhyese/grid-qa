"""RAG 问答编排：热点缓存 / 多轮上下文 / 检索 / prompt / LLM / 后处理 / 相关问题推荐。"""
import json
import re
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import redis_client
from app.config import settings
from app.core.obs import degraded
from app.providers.factory import get_llm_provider
from app.rag import citation, prompt_templates
from app.services import conversation_service, kg_service, retrieval_service, term_service

_HISTORY_LIMIT = 6  # 拼接最近 3 轮（6 条消息）


def _cache_key(model_type: str | None, query: str) -> str:
    return f"qa:{model_type or 'default'}:{query}"


async def answer(
    db: AsyncSession, query: str, model_type: str | None = None,
    topk: int = 5, conversation_id: str | None = None, username: str = "",
) -> dict:
    t0 = time.time()
    nq = term_service.normalize(query)

    # 多轮不走缓存（上下文变化）；单轮命中热点缓存
    if not conversation_id:
        try:
            cached = await redis_client.cache_get_json(_cache_key(model_type, nq))
        except Exception as e:
            degraded("qa_cache_get", e)
            cached = None
        if cached:
            cached["cached"] = True
            cached["responseTime"] = round(time.time() - t0, 3)
            try:
                from app.core import metrics
                metrics.QA_TOTAL.labels(model_type or settings.LLM_PROVIDER, "true").inc()
            except Exception:
                pass
            return cached

    contexts = await retrieval_service.mixed_search(db, nq, topk)
    if not contexts:
        return {
            "answer": "根据现有资料无法确认该问题，请先上传并解析相关运维文档后重试。",
            "retrievalSource": [], "responseTime": round(time.time() - t0, 3),
            "hallucinationRate": 0.0, "cached": False, "conversationId": conversation_id or "",
        }

    # 多轮历史
    history = []
    if conversation_id:
        history = await conversation_service.get_messages(db, conversation_id, _HISTORY_LIMIT)
    # GraphRAG：融合知识图谱结构化上下文（KG_RAG_ENABLE 默认开）
    graph: list[str] = []
    if settings.KG_RAG_ENABLE:
        try:
            graph = await kg_service.graph_context(nq)
        except Exception as e:
            degraded("kg_graph_context", e)
            graph = []
    messages = prompt_templates.build_messages_with_history(nq, contexts, history, graph)
    _llm0 = time.time()
    ans = await get_llm_provider(model_type).chat(messages, temperature=0.2)
    try:
        from app.core import metrics
        _p = model_type or settings.LLM_PROVIDER
        metrics.LLM_CALLS.labels(_p).inc()
        metrics.LLM_LATENCY.labels(_p).observe(time.time() - _llm0)
    except Exception:
        pass

    # 持久化对话
    if not conversation_id:
        conv = await conversation_service.create_conversation(db, username, query)
        conversation_id = conv.id
    await conversation_service.save_message(db, conversation_id, "user", query)
    await conversation_service.save_message(db, conversation_id, "assistant", ans)

    _halluc = citation.estimate_hallucination(ans, len(contexts))
    try:
        from app.core import metrics
        metrics.HALLUC.observe(_halluc)
    except Exception:
        pass
    result = {
        "answer": ans,
        "retrievalSource": [{"docName": c.get("docName", ""), "text": c["chunk"][:200]} for c in contexts],
        "graphCount": len(graph),
        "responseTime": round(time.time() - t0, 3),
        "hallucinationRate": _halluc,
        "cached": False,
        "conversationId": conversation_id,
    }

    # 仅单轮结果写缓存
    try:
        await redis_client.cache_set_json(_cache_key(model_type, nq), result, settings.QA_CACHE_TTL)
    except Exception as e:
        degraded("qa_cache_set", e)
    try:
        from app.core import metrics
        metrics.QA_TOTAL.labels(model_type or settings.LLM_PROVIDER, "false").inc()
    except Exception:
        pass
    return result


async def stream_answer(
    db: AsyncSession, query: str, model_type: str | None = None,
    topk: int = 5, conversation_id: str | None = None, username: str = "",
):
    """流式问答：单轮查热点缓存(命中则快流不调LLM) → 否则 meta/token/done 三段。

    补齐 LLM 调用埋点 + 缓存命中埋点（流式路径原本缺失，导致 Grafana LLM/缓存面板 No data）。
    """
    t0 = time.time()
    nq = term_service.normalize(query)
    _p = model_type or settings.LLM_PROVIDER
    is_single = not conversation_id  # 仅单轮查/写缓存（多轮上下文变化不缓存）

    # 0) 单轮查热点缓存 → 命中则不调 LLM，一次性下发完整答案（cached=true）
    if is_single:
        try:
            cached = await redis_client.cache_get_json(_cache_key(model_type, nq))
        except Exception as e:
            degraded("qa_cache_get", e)
            cached = None
        if cached:
            conv = await conversation_service.create_conversation(db, username, query)
            cid = conv.id
            try:
                await conversation_service.save_message(db, cid, "user", query)
                await conversation_service.save_message(db, cid, "assistant", cached.get("answer", ""))
            except Exception as e:
                degraded("conv_save", e)
            yield {"type": "meta", "sources": cached.get("retrievalSource", []), "conversationId": cid}
            yield {"type": "token", "content": cached.get("answer", "")}
            try:
                from app.core import metrics
                metrics.QA_TOTAL.labels(_p, "true").inc()
            except Exception:
                pass
            yield {
                "type": "done", "responseTime": round(time.time() - t0, 3),
                "hallucinationRate": cached.get("hallucinationRate", 0.0),
                "conversationId": cid, "cached": True,
            }
            return

    contexts = await retrieval_service.mixed_search(db, nq, topk)
    if not contexts:
        yield {"type": "done", "content": "根据现有资料无法确认该问题，请先上传并解析相关运维文档后重试。"}
        return

    # 多轮历史
    history = []
    if conversation_id:
        history = await conversation_service.get_messages(db, conversation_id, _HISTORY_LIMIT)
    # GraphRAG：融合知识图谱结构化上下文（KG_RAG_ENABLE 默认开）
    graph: list[str] = []
    if settings.KG_RAG_ENABLE:
        try:
            graph = await kg_service.graph_context(nq)
        except Exception as e:
            degraded("kg_graph_context", e)
            graph = []
    messages = prompt_templates.build_messages_with_history(nq, contexts, history, graph)

    # 流式前先建会话，确保 conversationId 可随 meta 下发
    if is_single:
        conv = await conversation_service.create_conversation(db, username, query)
        conversation_id = conv.id

    # 1) meta：引用来源 + 会话 ID
    yield {
        "type": "meta",
        "sources": [{"docName": c.get("docName", ""), "text": c["chunk"][:200]} for c in contexts],
        "conversationId": conversation_id,
    }

    # 2) 逐 token 流式（打字机）+ LLM 调用埋点
    parts: list[str] = []
    _llm0 = time.time()
    async for token in get_llm_provider(model_type).stream(messages, temperature=0.2):
        parts.append(token)
        yield {"type": "token", "content": token}
    try:
        from app.core import metrics
        metrics.LLM_CALLS.labels(_p).inc()
        metrics.LLM_LATENCY.labels(_p).observe(time.time() - _llm0)
    except Exception:
        pass

    # 3) 持久化完整答案
    full = "".join(parts)
    try:
        await conversation_service.save_message(db, conversation_id, "user", query)
        await conversation_service.save_message(db, conversation_id, "assistant", full)
    except Exception as e:
        degraded("conv_save", e)

    # 4) 单轮写热点缓存
    halluc = citation.estimate_hallucination(full, len(contexts))
    try:
        from app.core import metrics
        metrics.HALLUC.observe(halluc)
    except Exception:
        pass
    if is_single:
        try:
            await redis_client.cache_set_json(
                _cache_key(model_type, nq),
                {
                    "answer": full,
                    "retrievalSource": [{"docName": c.get("docName", ""), "text": c["chunk"][:200]} for c in contexts],
                    "responseTime": round(time.time() - t0, 3),
                    "hallucinationRate": halluc,
                    "cached": False,
                    "conversationId": conversation_id,
                },
                settings.QA_CACHE_TTL,
            )
        except Exception as e:
            degraded("qa_cache_set", e)
    try:
        from app.core import metrics
        metrics.QA_TOTAL.labels(_p, "false").inc()
    except Exception:
        pass
    yield {
        "type": "done",
        "responseTime": round(time.time() - t0, 3),
        "hallucinationRate": halluc,
        "graphCount": len(graph),
        "conversationId": conversation_id,
        "cached": False,
    }


async def generate_related(
    query: str, answer: str = "", model_type: str | None = None
) -> list[str]:
    """基于当前问答，LLM 生成 3 个相关追问问题（引导深挖）。

    独立接口：避免塞进流式 done 拖慢首字延迟，由前端答案渲染后异步拉取。
    """
    provider = get_llm_provider(model_type)
    prompt = (
        "基于以下电网运维问答，生成 3 个用户可能继续追问的相关问题。\n"
        "要求：与原问题相关但换角度或更深一层；简短具体（10-25 字）；聚焦变电/配电/输电运维。\n"
        "只输出 JSON 字符串数组，如 [\"问题1\",\"问题2\",\"问题3\"]，不要任何解释或代码块。\n\n"
        f"【原问题】{query}\n【答案摘要】{(answer or '')[:500]}"
    )
    try:
        ans = await provider.chat(
            [{"role": "user", "content": prompt}], temperature=0.5, max_tokens=400
        )
    except Exception as e:
        degraded("related_gen", e)
        return []
    m = re.search(r"\[.*\]", ans or "", re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    return [str(x).strip()[:60] for x in arr if str(x).strip()][:3]
