"""控制器真实执行循环；仅外部模型/检索边界用脚本替身。"""
import asyncio
import json

import pytest

from app.services import agentic_rag_service as svc


class Reader:
    def __init__(self, evidence=None):
        self.evidence = evidence if evidence is not None else [dict(key='doc:1', kind='document',
            text='手册给出的温度记录为85℃。', docId='d', docName='规程', chunkIdx=0, sourceHash='hash')]
        self.calls = []
        self.revoked = False

    async def execute(self, tool, args):
        self.calls.append((tool, args))
        return {'evidence': self.evidence, 'note': ''}

    async def revalidate(self, evidence):
        return [] if self.revoked else evidence


def call(name='search_documents', args=None):
    return {'id': 'tool1', 'name': name, 'arguments': args or {'query': '温度'}}


class Provider:
    def __init__(self, plans=None, sufficient=True, claims=None, clarification=''):
        self.plans = list(plans or [[call()]])
        self.sufficient = sufficient
        self.claims = claims if claims is not None else [{'text': '温度记录为85℃。', 'evidenceIds': ['E1']}]
        self.clarification = clarification
        self.plan_count = 0
        self.final_count = 0

    async def chat_with_tools(self, messages, tools, **kwargs):
        self.plan_count += 1
        calls = self.plans.pop(0) if self.plans else [call()]
        return {'content': '', 'tool_calls': calls}

    async def chat(self, messages, **kwargs):
        if '[EVIDENCE_ASSESSMENT]' in messages[0]['content']:
            return json.dumps({'sufficient': self.sufficient, 'missing': [] if self.sufficient else ['缺少运行记录'],
                               'conflicts': [], 'clarification': self.clarification})
        self.final_count += 1
        return json.dumps({'claims': self.claims, 'uncertainties': []})


@pytest.mark.asyncio
async def test_returns_traceable_evidence_and_numbered_citations():
    result = await svc.run_agentic_rag(None, '温度是多少', reader=Reader(), provider=Provider())
    assert result['status'] == 'completed'
    assert '[1]' in result['answer']
    assert result['sources'][0]['docId'] == 'd'
    assert result['evidencePack'][0]['evidenceId'] == 'E1'
    assert result['confidence'] != 'high'


@pytest.mark.asyncio
async def test_write_tool_is_rejected_before_dispatch():
    reader = Reader()
    result = await svc.run_agentic_rag(None, '查资料', reader=reader,
                                     provider=Provider(plans=[[call('create_ticket')]]))
    assert reader.calls == []
    assert result['stopReason'] == 'tool_not_allowed'


@pytest.mark.asyncio
async def test_model_cannot_override_tenant_identity():
    reader = Reader()
    await svc.run_agentic_rag(None, '查资料', tenant='one', reader=reader,
                             provider=Provider(plans=[[call(args={'query':'q','tenant':'two','role':'admin'})]]))
    assert all('tenant' not in args and 'role' not in args for _,args in reader.calls)


@pytest.mark.asyncio
async def test_empty_evidence_cannot_be_declared_sufficient():
    provider = Provider(sufficient=True)
    result = await svc.run_agentic_rag(None, '查资料', reader=Reader([]), provider=provider)
    assert result['status'] == 'insufficient'
    assert result['sources'] == []
    assert provider.final_count == 0


@pytest.mark.asyncio
async def test_no_progress_stops_after_two_duplicate_rounds():
    provider = Provider(sufficient=False)
    result = await svc.run_agentic_rag(None, '查资料', reader=Reader(), provider=provider)
    assert provider.plan_count == 3
    assert result['toolCalls'] == 3
    assert result['stopReason'] == 'no_new_evidence'


@pytest.mark.asyncio
async def test_more_than_six_tools_are_never_executed():
    reader = Reader()
    result = await svc.run_agentic_rag(None, '查资料', reader=reader,
                                     provider=Provider(plans=[[call()]*12], sufficient=False))
    assert len(reader.calls) == 6
    assert result['stopReason'] == 'tool_budget'


@pytest.mark.asyncio
async def test_unknown_reference_and_unsupported_number_are_not_output():
    claims = [{'text':'温度999℃。','evidenceIds':['E1']}, {'text':'虚构事实','evidenceIds':['E99']}]
    result = await svc.run_agentic_rag(None, '查资料', reader=Reader(), provider=Provider(claims=claims))
    assert '999' not in result['answer']
    assert '虚构事实' not in result['answer']
    assert result['status'] != 'completed'
    assert result['verification']['rejectedClaims'] == 2


@pytest.mark.asyncio
async def test_revoked_source_is_removed_before_answer_is_emitted():
    reader = Reader()
    class RevokingProvider(Provider):
        async def chat(self, messages, **kwargs):
            answer = await super().chat(messages, **kwargs)
            if '[GROUNDED_ANSWER]' in messages[0]['content']: reader.revoked = True
            return answer
    result = await svc.run_agentic_rag(None, '查资料', reader=reader, provider=RevokingProvider())
    assert result['sources'] == []
    assert '85' not in result['answer']
    assert result['stopReason'] == 'source_changed'


@pytest.mark.asyncio
async def test_timeout_cancels_model_and_returns_explicit_reason():
    cancelled = asyncio.Event()
    class Slow(Provider):
        async def chat_with_tools(self, *args, **kwargs):
            try: await asyncio.sleep(10)
            finally: cancelled.set()
    result = await svc.run_agentic_rag(None, '查资料', reader=Reader(), provider=Slow(),
                                     limits=svc.Limits(total_seconds=.05, model_seconds=.02))
    assert cancelled.is_set()
    assert result['stopReason'] == 'timeout'
    assert result['degraded'] is True
    assert result['verification']['citationIdsChecked'] is False
    assert result['verification']['numbersChecked'] is False


@pytest.mark.asyncio
async def test_required_parameters_produce_clarification_not_answer():
    provider = Provider(sufficient=False, clarification='请指定设备和时间范围')
    result = await svc.run_agentic_rag(None, '最近温度如何', reader=Reader(), provider=provider)
    assert result['status'] == 'clarification'
    assert '请指定设备和时间范围' in result['answer']
    assert provider.final_count == 0


def test_same_number_with_wrong_unit_is_not_grounded():
    evidence = [dict(evidenceId='E1', text='实测85℃，电压110kV')]
    lines, rejected = svc.grounded_claims({'claims': [{'text':'电压85kV','evidenceIds':['E1']}]}, evidence)
    assert lines == []
    assert rejected == 1


@pytest.mark.asyncio
async def test_planner_context_does_not_accumulate_full_evidence_per_tool():
    evidence = [dict(key=f'd:{i}',kind='document',text='检索内容'*400,docId=f'd{i}',docName='手册') for i in range(12)]
    class Measured(Provider):
        async def chat_with_tools(self, messages, tools, **kwargs):
            assert sum(len(str(m.get('content',''))) for m in messages) < 26000
            return await super().chat_with_tools(messages, tools, **kwargs)
    result = await svc.run_agentic_rag(None,'查资料',reader=Reader(evidence),
                                     provider=Measured(plans=[[call()]*3,[call()]],sufficient=False))
    assert result['stopReason'] != 'provider_error'


@pytest.mark.asyncio
async def test_assessment_cannot_claim_sufficient_while_missing_required_evidence():
    class Contradictory(Provider):
        async def chat(self,messages,**kwargs):
            if '[EVIDENCE_ASSESSMENT]' in messages[0]['content']:
                return json.dumps({'sufficient':True,'missing':['缺少实时测量'],'conflicts':[]})
            return await super().chat(messages,**kwargs)
    result=await svc.run_agentic_rag(None,'实时情况',reader=Reader(),provider=Contradictory())
    assert result['status']!='completed'


def test_scientific_notation_is_checked_as_full_numeric_value():
    lines,rejected=svc.grounded_claims({'claims':[{'text':'电压1e3kV','evidenceIds':['E1']}]},
                                     [{'evidenceId':'E1','text':'电压1kV'}])
    assert lines==[] and rejected==1


@pytest.mark.asyncio
async def test_tool_degradation_is_visible_without_raw_exception():
    class BrokenReader(Reader):
        async def execute(self,*args):
            return {'evidence':[],'degraded':True,'note':'来源服务暂不可用'}
    result=await svc.run_agentic_rag(None,'查资料',reader=BrokenReader(),provider=Provider())
    assert result['degraded'] is True


@pytest.mark.asyncio
async def test_live_measurement_question_cannot_be_answered_from_manual_only():
    provider=Provider(sufficient=True)
    result=await svc.run_agentic_rag(None,'1号主变最近两小时油温是多少',reader=Reader(),provider=provider)
    assert result['status']!='completed'
    assert provider.final_count==0
    assert any('遥测' in gap for gap in result['missingEvidence'])


@pytest.mark.parametrize('source,claim', [('电压1mV','电压1MV'),('电流85MA','电流85mA'),
                                         ('电压5kV','电压.5kV'),('温度−5℃','温度5℃')])
def test_numeric_and_si_prefix_boundaries_do_not_change_meaning(source,claim):
    lines,rejected=svc.grounded_claims({'claims':[{'text':claim,'evidenceIds':['E1']}]},
                                     [{'evidenceId':'E1','text':source}])
    assert lines==[] and rejected==1


@pytest.mark.parametrize('source,claim',[('温度−5℃','温度-5°C'),('电压0.5kV','电压.5kV')])
def test_equivalent_numeric_spelling_preserves_value(source,claim):
    lines,rejected=svc.grounded_claims({'claims':[{'text':claim,'evidenceIds':['E1']}]},
                                     [{'evidenceId':'E1','text':source}])
    assert lines and rejected==0


@pytest.mark.asyncio
async def test_inner_retrieval_degradation_is_not_hidden_by_successful_answer():
    from app.core import qa_trace
    previous=qa_trace._current_collector.get()
    collector=qa_trace.new_collector('test')
    collector.mark('dense_cloud_failed',True)
    try:
        result=await svc.run_agentic_rag(None,'资料说明',reader=Reader(),provider=Provider())
        assert result['degraded'] is True
        assert result['retrievalDegraded'] is True
    finally:qa_trace._current_collector.set(previous)


@pytest.mark.asyncio
async def test_high_risk_terms_carry_explicit_human_workflow_warning():
    reader=Reader([dict(key='doc:1',kind='document',text='倒闸操作须经批准。',docId='d',docName='规程')])
    provider=Provider(claims=[{'text':'倒闸操作须经批准。','evidenceIds':['E1']}])
    result=await svc.run_agentic_rag(None,'查询倒闸要求',reader=reader,provider=provider)
    assert result['highRisk']
    assert '人工' in result['answer'] and '执行' in result['answer']
