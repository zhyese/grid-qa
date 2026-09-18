"""知识回流共享 backbone：自适应 prompt 草稿生成 + Chunk/Milvus 持久化。

evidence_gap 和 knowledge_evolution 两套服务共用的尾部逻辑：
- classify_query：问题分类（rule-based，零 LLM 成本）
- generate_adaptive_draft：自适应 prompt + LLM 生成（按问题类型选模板）
- ensure_ai_doc：虚拟 AI 文档管理（doc_type=ai_evolution）
- persist_chunk：Chunk 创建 + embed + Milvus 插入

消除两套服务重复实现草稿生成/向量写入的问题。
"""
import asyncio
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession



# ===== 问题分类 =====

_QUERY_TYPE_RULES: dict[str, list[str]] = {
    "fault": ["故障", "异常", "跳闸", "告警", "处置", "排查", "事故", "短路", "接地",
              "过载", "拒动", "误动", "闪络", "击穿", "放电", "渗漏", "发热",
              "漏气", "漏油", "烧损", "爆炸", "裂纹", "变形", "腐蚀", "老化"],
    "procedure": ["操作", "步骤", "检修", "巡视", "投运", "退出", "送电", "停电",
                  "倒闸", "验收", "检测", "试验", "维护", "更换", "安装"],
    "safety": ["安全距离", "限值", "温度", "电压", "电流", "标准", "规范", "规程",
               "保护定值", "整定", "额定", "允许", "最高", "最低", "上限", "下限"],
}

_GAP_PROMPT_EXTRA: dict[str, str] = {
    "fault": (
        "\n\n【知识补全·特别要求】本次为证据补全草稿（原始置信度：{confidence}）。"
        "请严格按以下结构作答：\n"
        "1. 【现象描述】：故障的典型表现和判据\n"
        "2. 【可能原因】：列出常见原因（按可能性排序）\n"
        "3. 【处置措施】：具体操作步骤（按顺序，标注安全注意事项）\n"
        "4. 【依据规程】：引用相关规程条款或标准编号\n"
        "若参考资料不足以覆盖某个环节，该环节必须写明'现有资料无法确认，需补充XX规程'。"
    ),
    "procedure": (
        "\n\n【知识补全·特别要求】本次为证据补全草稿（原始置信度：{confidence}）。"
        "请严格按操作步骤顺序编写，每步标注：\n"
        "1. 操作内容和顺序编号\n"
        "2. 安全注意事项（停电/验电/挂地线等）\n"
        "3. 依据的安规条款或操作票编号\n"
        "缺少依据的步骤必须标注'需补充操作票'。"
    ),
    "safety": (
        "\n\n【知识补全·特别要求】本次为证据补全草稿（原始置信度：{confidence}）。"
        "请给出：\n"
        "1. 明确的数值和单位\n"
        "2. 适用条件（电压等级、设备类型等）\n"
        "3. 依据的标准/规程条款编号\n"
        "4. 例外情况和特殊说明\n"
        "数值必须绑定引用来源，无来源的数值不得给出。"
    ),
    "general": (
        "\n\n【知识补全·特别要求】本次为证据补全草稿（原始置信度：{confidence}）。"
        "请先给结论，再给依据/步骤，最后引用来源。"
        "标注不确定的部分，避免绝对化结论。"
    ),
}


def classify_query(query: str) -> str:
    """轻量问题分类（rule-based，零 LLM 成本）。返回 fault/procedure/safety/general。"""
    for qtype, keywords in _QUERY_TYPE_RULES.items():
        if any(kw in query for kw in keywords):
            return qtype
    return "general"


# ===== 自适应草稿生成 =====

async def generate_adaptive_draft(
    query: str,
    contexts: list[dict],
    *,
    original_answer: str = "",
    confidence: str = "medium",
    model_type: str | None = None,
    temperature: float = 0.3,
) -> str:
    """自适应 prompt + LLM 草稿生成。按问题类型选模板，注入 gap 上下文。

    供 evidence_gap.ai_draft() 和 knowledge_evolution._generate_draft() 共用。
    返回草稿文本（自由格式）。
    """
    from app.providers.factory import get_llm_provider
    from app.rag import prompt_templates

    qtype = classify_query(query)
    extra = _GAP_PROMPT_EXTRA[qtype].format(confidence=confidence or "medium")

    refs = "\n\n".join(
        f"[{i + 1}] {c.get('docName', '')}：{c.get('chunk', '')}"
        for i, c in enumerate(contexts)
    )
    gap_ctx = ""
    if original_answer:
        gap_ctx += f"\n【之前答案（需补强）】\n{original_answer[:500]}"
    if confidence == "refused":
        gap_ctx += "\n【补强方向】强相关证据缺失，需从规程/案例中找支撑"
    elif confidence == "medium":
        gap_ctx += "\n【补强方向】证据有限，需补充更具体的依据和数值"

    system = prompt_templates.get_system_prompt() + extra
    user = (f"【参考资料】\n{refs}{gap_ctx}\n\n"
            f"【问题】{query}\n\n请严格依据参考资料按规则作答，重点补强上述不足。")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    return (await get_llm_provider(model_type).chat(messages, temperature=temperature)).strip()


# ===== 虚拟 AI 文档 =====

AI_DOC_NAME = "AI自进化草稿集"


async def ensure_ai_doc(db: AsyncSession, tenant: str) -> str:
    """建/复用虚拟 AI 文档（doc_type=ai_evolution，供降权识别）。返回 doc_id。"""
    from app.models.document import Document
    doc_id = f"ai-evo-{tenant}"
    doc = (await db.execute(select(Document).where(Document.id == doc_id))).scalar_one_or_none()
    if not doc:
        db.add(Document(
            id=doc_id, doc_name=AI_DOC_NAME, doc_type="ai_evolution",
            minio_object=f"ai-evolution/{tenant}", status="vectorized",
            tenant_id=tenant, upload_user="system",
        ))
        await db.flush()
    return doc_id


# ===== Chunk + Milvus 持久化 =====

async def persist_chunk(
    db: AsyncSession,
    content: str,
    title: str,
    tenant: str,
    doc_id: str | None = None,
    doc_name: str | None = None,
) -> str:
    """写 Chunk 行 + embed + Milvus insert。返回 chunk_id。

    doc_id=None 时使用虚拟 AI 文档（ai-evo-{tenant}）。
    doc_name 默认为 AI_DOC_NAME。
    """
    from app.models.chunk import Chunk
    from app.clients import milvus_client
    from app.providers.factory import get_embedding_provider
    from app.config import settings

    if doc_id is None:
        doc_id = await ensure_ai_doc(db, tenant)
    if doc_name is None:
        doc_name = AI_DOC_NAME

    cnt = (await db.execute(
        select(func.count()).select_from(Chunk).where(Chunk.doc_id == doc_id)
    )).scalar() or 0

    chunk_id = uuid.uuid4().hex
    chunk = Chunk(
        id=chunk_id, doc_id=doc_id, chunk_idx=cnt,
        content=content, char_count=len(content),
        section=f"[AI]{title}"[:256], chunk_type="child",
    )
    db.add(chunk)
    await db.flush()

    vec = (await get_embedding_provider(settings.EMB_PROVIDER).embed([content]))[0]
    await asyncio.to_thread(
        milvus_client.insert_chunks, milvus_client.primary_document_collection(), [vec], [content],
        [doc_id], [doc_name], [cnt],
    )
    return chunk_id
