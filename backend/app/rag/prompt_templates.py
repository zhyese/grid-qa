"""电网运维领域 RAG prompt 模板。"""

SYSTEM_PROMPT = """你是电网运维专业问答助手，严格依据"参考资料"作答，覆盖变电、配电、输电场景。
规则：
1) 只能基于参考资料作答；资料中没有的信息，必须回答"根据现有资料无法确认"，禁止编造。
2) 涉及操作步骤、检修、故障处置时，必须按资料原文顺序，不得遗漏关键步骤。
3) 涉及停电、操作、安全距离等高风险操作，必须在答案末尾追加"⚠ 安全提示：操作前核对调度指令与安规"。
4) 答案中引用资料时，在对应句末标注 [1]、[2] 等编号，编号对应参考资料序号。
5) 专业术语首次出现时给出全称（如"主变压器(主变)"）。
6) 输出格式：先给"结论"，再给"依据/步骤"，最后给"引用来源"。
7) 若提供了"知识图谱(结构化关系)"，可作为结构化依据补充作答（与资料不矛盾时优先采纳，仍需标注来源）。
可核验引用强约束（必须遵守）：
8) 只能使用参考资料中已编号的 [1][2][3]，严禁编造映射表以外的编号。
9) 每条独立事实结论后标注对应编号；过渡/修饰句无需引用。
10) 单句含多个独立事实（限值/材料/时限/费用）时，分别标注对应编号。
11) 无资料支撑的观点不得标注任何编号，直接写明"现有资料无法确认"。
12) 数字、否定描述、时限、金额、免责条款等高风险表述，必须绑定引用编号。
"""


def get_system_prompt() -> str:
    """生效的 system prompt：管理员后台覆盖优先（config_service 热读缓存），否则 code 默认。"""
    try:
        from app.services.config_service import rt_system_prompt
        v = rt_system_prompt()
        return v if v else SYSTEM_PROMPT
    except Exception:
        return SYSTEM_PROMPT


def build_messages(query: str, contexts: list[dict]) -> list[dict]:
    """contexts: [{docName, chunk}, ...]"""
    refs = "\n\n".join(
        f"[{i + 1}] {c.get('docName', '')}：{c.get('chunk', '')}"
        for i, c in enumerate(contexts)
    )
    user = f"【参考资料】\n{refs}\n\n【问题】{query}\n\n请严格依据参考资料按规则作答。"
    return [
        {"role": "system", "content": get_system_prompt()},
        {"role": "user", "content": user},
    ]


def build_messages_with_history(query: str, contexts: list[dict], history: list[dict],
                                graph: list[str] | None = None, confidence: str = "high") -> list[dict]:
    """多轮：system(规则) + 历史对话 + 当前(资料+图谱+问题)。

    history: [{role, content}]；graph: 知识图谱结构化三元组文本列表（GraphRAG 增强）。
    confidence: CRAG 置信度 high/medium/low/refused，低置信时追加保守作答指令（实时护栏）。
    """
    refs = "\n\n".join(
        f"[{i + 1}] {c.get('docName', '')}：{c.get('chunk', '')}"
        for i, c in enumerate(contexts)
    )
    graph_block = ""
    if graph:
        graph_block = "\n\n【知识图谱(结构化关系)】\n" + "\n".join(f"- {g}" for g in graph)
    extra = ""
    if confidence == "medium":
        extra = "\n8) 注意：本次检索证据可能不充分，作答时明确标注不确定的部分，避免绝对化结论。"
    elif confidence == "refused":
        extra = "\n8) 注意：检索未找到强相关资料，优先回答\"根据现有资料无法确认该问题\"，可给通用方向但必须标注非资料依据。"
    msgs = [{"role": "system", "content": get_system_prompt() + extra}]
    for h in history:
        msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append(
        {"role": "user",
         "content": f"【参考资料】\n{refs}{graph_block}\n\n【问题】{query}\n\n请严格依据参考资料(及知识图谱)并结合上文按规则作答。"}
    )
    return msgs

