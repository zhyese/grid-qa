"""Agentic RAG 的 SSE/会话适配；不读取或写入共享问答缓存。"""
import asyncio
import time

from sqlalchemy import select

from app.core.obs import degraded
from app.models.conversation import Conversation
from app.services import agentic_rag_service, agentic_history_service, conversation_service


async def stream_agentic_answer(db, query, model_type=None, *, conversation_id=None,
                                username='', tenant='default', user_role='', user_dept='',
                                memory_read=False, memory_write=False):
    started = time.monotonic()
    if memory_read or memory_write:
        yield {'type':'error', 'agenticRag':True,
               'message':'证据检索模式暂不使用长期记忆，请关闭记忆选项后重试', 'confidence':'refused'}
        return
    if not query or len(query) > 4000:
        yield {'type':'error', 'agenticRag':True, 'message':'请提供不超过4000字的问题', 'confidence':'refused'}
        return
    history = []
    if conversation_id:
        conv = (await db.execute(select(Conversation).where(
            Conversation.id == conversation_id, Conversation.username == username,
            Conversation.is_deleted.is_(False)))).scalar_one_or_none()
        if not conv or not await agentic_history_service._accessible(db, conversation_id, username, tenant):
            yield {'type':'error','agenticRag':True,'message':'会话不存在或不可访问','confidence':'refused'}
            return
        history = await agentic_history_service.history(db, conversation_id, username=username,
                                                        tenant=tenant, role=user_role, dept=user_dept, limit=6)
    else:
        conv = await conversation_service.create_conversation(db, username, query)
        conversation_id = conv.id
    yield {'type':'meta','sources':[],'conversationId':conversation_id,'agentMode':True,'agenticRag':True}
    queue = asyncio.Queue()

    async def run():
        try:
            result = await agentic_rag_service.run_agentic_rag(
                db, query, model_type, tenant=tenant, role=user_role or '', dept=user_dept or '',
                username=username, history=history,
                on_step=lambda step: queue.put_nowait({'type':'tool_step','agenticRag':True,'step':step}))
            await queue.put({'type':'_result','result':result})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            degraded('agentic_stream', exc)
            await queue.put({'type':'error','agenticRag':True,'message':'深度查证暂不可用，请稍后重试',
                             'degraded':True,'confidence':'refused'})

    task = asyncio.create_task(run())
    try:
        while True:
            event = await queue.get()
            if event['type'] == 'tool_step':
                yield event
                continue
            if event['type'] == 'error':
                yield event
                return
            result = event['result']
            await agentic_history_service.save_answer(db, conversation_id, query, result,
                                                       username=username, tenant=tenant)
            summary = {k: result[k] for k in ('status','stopReason','missingEvidence','conflicts',
                                              'rounds','toolCalls','verification')}
            yield {'type':'meta','sources':result['sources'],'evidencePack':result['evidencePack'],
                   'conversationId':conversation_id,'agentMode':True,'agenticRag':True}
            yield {'type':'token','content':result['answer'],'agenticRag':True}
            yield {'type':'done','annotatedAnswer':result['answer'],'agenticResult':summary,
                   'sources':result['sources'],'evidencePack':result['evidencePack'],
                   'conversationId':conversation_id,'agentMode':True,'agenticRag':True,
                   'iterations':result['rounds'],'toolsUsed':result['toolsUsed'],
                   'confidence':result['confidence'],'degraded':result['degraded'],
                   'highRisk':result.get('highRisk',[]),
                   'retrievalDegraded':result.get('retrievalDegraded',False),
                   'retrievalDegradedReason':'部分检索组件不可用' if result.get('retrievalDegraded') else '',
                   'llmDegraded':result.get('llmDegraded',False),'modelType':result.get('modelType',model_type),
                   'responseTime':round(time.monotonic()-started,3),'cached':False,'cacheLayer':'agentic_rag'}
            return
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
