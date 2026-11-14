import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.conversation import Conversation, Message
from app.services import agentic_rag_service, agent_runtime, qa_service


def result():
    return dict(answer='依据资料回答[1]', sources=[{'docId':'d','docName':'规程','chunk':'证据'}],
                evidencePack=[{'evidenceId':'E1','docId':'d','text':'证据'}], status='completed',
                stopReason='sufficient', missingEvidence=[], conflicts=[], rounds=1, toolCalls=1,
                toolsUsed=['search_documents'], confidence='medium', degraded=False,
                verification={'semanticEntailmentVerified':False})


@pytest.mark.asyncio
async def test_deep_switch_uses_new_controller_and_never_shared_cache(test_db, monkeypatch):
    monkeypatch.setattr(settings,'AGENTIC_RETRIEVAL_ENABLE',True)
    reads=[]; received={}
    async def cache(*args, **kw):
        reads.append(args)
        return None
    async def legacy(*args, **kw):
        return SimpleNamespace(answer='old',degraded=True,iterations=1,tools_used=[])
    async def run(db,query,model_type,**kwargs):
        received.update(kwargs)
        kwargs['on_step']({'iter':1,'phase':'retrieve','tool':'search_documents','result':'取得1条证据'})
        return result()
    monkeypatch.setattr(qa_service.redis_client,'cache_get_json',cache)
    monkeypatch.setattr(agent_runtime,'run_agent',legacy)
    monkeypatch.setattr(agentic_rag_service,'run_agentic_rag',run)
    events=[e async for e in qa_service.stream_answer(test_db,'查询规程','deepseek',agent_mode=True,
           username='owner',tenant='one',user_role='editor',user_dept='检修')]
    assert events[-1].get('agenticRag') is True
    assert events[-1]['sources'][0]['docId']=='d'
    assert received['dept']=='检修' and received['tenant']=='one'
    assert reads==[]
    assert len((await test_db.execute(select(Message))).scalars().all())==2
    from app.models.agentic_answer import AgenticAnswer
    snapshots=(await test_db.execute(select(AgenticAnswer))).scalars().all()
    assert len(snapshots)==1
    assert snapshots[0].payload['agenticResult']['stopReason']=='sufficient'


@pytest.mark.asyncio
async def test_foreign_conversation_never_reaches_agent(test_db, monkeypatch):
    from app.services import agentic_stream_service as stream
    test_db.add(Conversation(id='foreign',username='other',title='secret'))
    await test_db.commit()
    called=[]
    async def run(*args,**kwargs):called.append(True);return result()
    monkeypatch.setattr(agentic_rag_service,'run_agentic_rag',run)
    events=[e async for e in stream.stream_agentic_answer(test_db,'q','deepseek',
            conversation_id='foreign',username='owner',tenant='one',user_role='editor',user_dept='')]
    assert events[-1]['type']=='error'
    assert called==[]


@pytest.mark.asyncio
async def test_client_close_cancels_background_controller(test_db, monkeypatch):
    from app.services import agentic_stream_service as stream
    cancelled=asyncio.Event()
    async def run(*args,**kwargs):
        try:
            kwargs['on_step']({'phase':'plan','iter':1})
            await asyncio.Event().wait()
        finally:cancelled.set()
    monkeypatch.setattr(agentic_rag_service,'run_agentic_rag',run)
    response=stream.stream_agentic_answer(test_db,'q','deepseek',username='owner',tenant='one',
                                        user_role='editor',user_dept='')
    assert (await anext(response))['type']=='meta'
    assert (await anext(response))['type']=='tool_step'
    await response.aclose()
    assert cancelled.is_set()
