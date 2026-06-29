"""电网运维领域 RAG prompt 模板。"""

SYSTEM_PROMPT = """你是电网运维专业问答助手，严格依据"参考资料"作答，覆盖变电、配电、输电场景。
规则：
1) 只能基于参考资料作答；资料中没有的信息，必须回答"根据现有资料无法确认"，禁止编造。
2) 涉及操作步骤、检修、故障处置时，必须按资料原文顺序，不得遗漏关键步骤。
3) 涉及停电、操作、安全距离等高风险操作，必须在答案末尾追加"⚠ 安全提示：操作前核对调度指令与安规"。
4) 答案中引用资料时，在对应句末标注 [1]、[2] 等编号，编号对应参考资料序号。
5) 专业术语首次出现时给出全称（如"主变压器(主变)"）。
6) 输出格式：先给"结论"，再给"依据/步骤"，最后给"引用来源"。
"""


def build_messages(query: str, contexts: list[dict]) -> list[dict]:
    """contexts: [{docName, chunk}, ...]"""
    refs = "\n\n".join(
        f"[{i + 1}] {c.get('docName', '')}：{c.get('chunk', '')}"
        for i, c in enumerate(contexts)
    )
    user = f"【参考资料】\n{refs}\n\n【问题】{query}\n\n请严格依据参考资料按规则作答。"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_messages_with_history(query: str, contexts: list[dict], history: list[dict]) -> list[dict]:
    """多轮：system(规则) + 历史对话 + 当前(资料+问题)。history: [{role, content}]。"""
    refs = "\n\n".join(
        f"[{i + 1}] {c.get('docName', '')}：{c.get('chunk', '')}"
        for i, c in enumerate(contexts)
    )
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in history:
        msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append(
        {"role": "user", "content": f"【参考资料】\n{refs}\n\n【问题】{query}\n\n请严格依据参考资料并结合上文按规则作答。"}
    )
    return msgs

