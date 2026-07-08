"""Agent persona 定义（S1 纯代码；DB+UI 留 S5）。

DIAGNOSE_PERSONA 由 diagnose_agent_service 迁移而来，system prompt / 工具集 / 输出格式
与原 _AGENT_SYSTEM / TOOLS / _extract_json 完全等价 → 诊断行为零回归。
"""
from app.services import domain_service
from app.services.agent_runtime import Persona

_DIAGNOSE_SYSTEM = """你是电网运维故障诊断专家。基于故障症状，通过调用工具自主收集证据（规程/图谱/历史案例）进行多轮交叉验证后给出诊断。
规则：
1) 每次可调用 0 个或多个工具；证据充分后停止调用工具，直接输出最终诊断。
2) 最终诊断必须输出严格 JSON：{"causes":[{"name":"可能原因","likelihood":"高/中/低","evidence":"资料依据","handling":"处置措施"}],"summary":"总体判断","risks":["风险点"]}
3) 原因按可能性从高到低排序；只基于工具收集的证据，证据不足如实说明；高风险处置（停电/接地/倒闸）须在 risks 标注。"""


async def _diagnose_fallback(db, user_msg, model_type):
    """降级：剥离 '故障症状：' 前缀后调 single-pass diagnose，返回 diagnosis dict。"""
    symptom = (user_msg or "").replace("故障症状：", "").strip()
    data = await domain_service.diagnose(db, symptom, model_type)
    return data.get("diagnosis", {"summary": "", "causes": []})


DIAGNOSE_PERSONA = Persona(
    name="diagnose",
    system_prompt=_DIAGNOSE_SYSTEM,
    allowed_tools=["search_regulation", "query_equipment_graph",
                   "search_similar_case", "draft_ticket"],
    max_iter=6,
    temperature=0.2,
    max_tokens=1500,
    output_format="json",
    fallback=_diagnose_fallback,
    config_source="code",
)


_QA_SYSTEM = """你是电网运维智能问答助手。通过调用工具自主收集证据（规程/图谱/历史案例）后，用自然语言回答用户的运维问题。
规则：
1) 每次可调用 0 个或多个工具；证据充分后停止调用工具，直接给出最终答案。
2) 答案须基于工具收集的证据，客观准确；引用关键规程/案例时简述来源；证据不足如实说明。
3) 涉及高风险操作（停电/接地/倒闸）时，提示风险并建议查阅正式规程或两票。"""


async def _qa_fallback(db, user_msg, model_type):
    """降级：走 qa_service.answer 原链路，返回答案文本。"""
    from app.services import qa_service
    try:
        res = await qa_service.answer(db, user_msg, model_type=model_type)
        return res.get("answer", "")
    except Exception:
        return ""


QA_PERSONA = Persona(
    name="qa",
    system_prompt=_QA_SYSTEM,
    allowed_tools=["search_regulation", "query_equipment_graph", "search_similar_case"],
    max_iter=6,
    temperature=0.2,
    max_tokens=1500,
    output_format="text",
    fallback=_qa_fallback,
    config_source="code",
)
