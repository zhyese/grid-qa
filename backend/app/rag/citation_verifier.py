# backend/app/rag/citation_verifier.py
"""第四层 · 三层校验引擎（核心防幻觉）。

校验1 格式合法（零算力）：ref_id ∈ index，剔除越界/重复。
校验2 向量粗筛：事实句 vs 候选 chunk cosine ≥ CITATION_VERIFY_SIM_THRESHOLD（复用 citation._cosine_mat；
        独立阈值，松于 auto_cite 补标的 CITATION_SIM_THRESHOLD=0.6——答案句是 LLM 重组表述，与原文 cosine 天然偏低）。
校验3 NLI 精准核验（CITATION_NLI_ENABLE 开时）：judge._verify_claims 三分类，contradict → drop。
核心事实全 drop → rewrite_needed=True（联动 CRAG）。
全异常/超时 → degraded=True，仅走校验1+2，不阻塞主链路。

label 规整（Task 8 follow-up）：_verify_claims 的 label 域未 whitelist，
LLM 可能返回非标准值（"yes"/"partial" 等）。本模块消费时**仅 "contradict" 精确匹配才 drop**，
其他一律 neutral/keep（保守放行，不误 drop）。
"""
import asyncio

from app.config import settings
from app.rag import citation as cite
from app.rag.citation_index import chunk_id_of
from app.schemas.citation import CitationItem, VerifyItem, VerifyResult

# 高风险要素（数字/否定/时限/金额/免责）必须绑定引用，否则移入警示
_HIGH_RISK = ("不", "禁", "无", "超过", "不超过", "限", "元", "天", "小时", "免责", "除外")


async def verify(
    answer_text: str,
    citation_map: list[CitationItem],
    index: dict[int, str],
    contexts: list[dict],
    model_type: str | None = None,
    *,
    nli_enable: bool | None = None,
) -> VerifyResult:
    """三层校验。返回 VerifyResult（drop/keep/rewrite 决策 + 降级标记）。"""
    if nli_enable is None:
        nli_enable = settings.CITATION_NLI_ENABLE
    threshold = settings.CITATION_VERIFY_SIM_THRESHOLD  # 校验2专用，独立于 auto_cite 的 CITATION_SIM_THRESHOLD

    result = VerifyResult()
    valid_items: list[CitationItem] = []

    # 校验1：格式合法（ref_id ∈ index）
    for item in citation_map:
        if chunk_id_of(item.ref_id, index):
            valid_items.append(item)
        else:
            result.dropped_refs.append(item.ref_id)
            result.items.append(VerifyItem(ref_id=item.ref_id, chunk_id=item.chunk_id,
                                           valid=False, action="drop"))

    if not valid_items:
        # 无任何合法引用：高风险句全标警示，触发 rewrite
        result.unverified_additions.extend(_high_risk_unverified(answer_text))
        result.rewrite_needed = bool(result.unverified_additions) or bool(answer_text.strip())
        return result

    # 校验2：向量粗筛
    ctx_by_id = {c.get("chunkId"): c for c in contexts}
    try:
        from app.services import embedding_service
        sents = [it.sentence for it in valid_items]
        # chunk_id 在 contexts 缺失时用空串(cosine≈0→自然 drop)；不回退到 it.sentence
        # (回退会使 sentence-vs-self cosine=1 自动放行，隐藏 dangling 引用)
        chunk_texts = [ctx_by_id.get(it.chunk_id, {}).get("chunk", "") for it in valid_items]
        s_embs = await embedding_service.embed_texts(sents)
        c_embs = await embedding_service.embed_texts(chunk_texts)
        sim = cite._cosine_mat(s_embs, c_embs)
        passed_idx = {i for i in range(len(valid_items))
                      if i < len(sim) and sim[i] and sim[i][i] >= threshold}
        for i, it in enumerate(valid_items):
            if i not in passed_idx:
                result.dropped_refs.append(it.ref_id)
                result.items.append(VerifyItem(ref_id=it.ref_id, chunk_id=it.chunk_id, valid=False,
                                               nli_label="low_sim", action="drop"))
        valid_items = [valid_items[i] for i in sorted(passed_idx)]
    except Exception as e:
        try:
            from app.core.obs import degraded
            degraded("citation_verify_sim", e)
        except Exception:
            pass
        result.degraded = True  # 向量层失败，仅靠校验1，保守放行 valid_items

    # 校验3：NLI（可选，最重）
    if nli_enable and valid_items and not result.degraded:
        try:
            from app.rag import judge
            claims = [it.sentence for it in valid_items]
            sources = [ctx_by_id.get(it.chunk_id, {}).get("chunk", "") for it in valid_items]
            verdicts = await asyncio.wait_for(
                judge._verify_claims(claims, sources, model_type),
                timeout=settings.CITATION_NLI_TIMEOUT,
            )
            still_valid = []
            for it, v in zip(valid_items, verdicts):
                label = v.get("label", "neutral")
                # label 规整：仅 "contradict" 精确匹配才 drop；非标准 label（yes/partial/None）一律 keep
                if label == "contradict":
                    result.dropped_refs.append(it.ref_id)
                    result.items.append(VerifyItem(ref_id=it.ref_id, chunk_id=it.chunk_id, valid=False,
                                                   nli_label="contradict", action="drop"))
                else:
                    result.items.append(VerifyItem(ref_id=it.ref_id, chunk_id=it.chunk_id, valid=True,
                                                   nli_label=label, action="keep"))
                    still_valid.append(it)
            valid_items = still_valid
        except asyncio.TimeoutError:
            result.degraded = True
            try:
                from app.core.obs import degraded
                degraded("citation_nli_timeout", TimeoutError("nli"))
            except Exception:
                pass
            for it in valid_items:
                result.items.append(VerifyItem(ref_id=it.ref_id, chunk_id=it.chunk_id, valid=True,
                                               nli_label="unknown", action="keep"))
        except Exception as e:
            result.degraded = True
            try:
                from app.core.obs import degraded
                degraded("citation_nli", e)
            except Exception:
                pass
            # NLI 异常：保守放行（valid_items 已通过校验1+2），统一标 unknown
            for it in valid_items:
                result.items.append(VerifyItem(ref_id=it.ref_id, chunk_id=it.chunk_id, valid=True,
                                               nli_label="unknown", action="keep"))
    else:
        # NLI 未开 / 无可核验项 / 已降级：直接放行，标 unknown
        for it in valid_items:
            result.items.append(VerifyItem(ref_id=it.ref_id, chunk_id=it.chunk_id, valid=True,
                                           nli_label="unknown", action="keep"))

    # 核心事实无任何支撑（全 drop）→ rewrite
    if not any(i.action == "keep" for i in result.items):
        result.unverified_additions.extend(_high_risk_unverified(answer_text))
        result.rewrite_needed = True

    return result


def _high_risk_unverified(answer_text: str) -> list[str]:
    """答案中含高风险要素却无引用的句子（校验1 全 drop 时标警示）。"""
    out = []
    for s in cite.split_sentences(answer_text):
        if any(k in s for k in _HIGH_RISK) and not cite.extract_sentence_sources(s):
            out.append(s)
    return out
