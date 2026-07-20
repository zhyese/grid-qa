"""知识时效与冲突治理服务。

设计原则：
- 时效状态由元数据推导，不改写源文档；
- 冲突检测采用保守、可解释的规则，只生成“潜在冲突”；
- 扫描幂等更新问题证据，不自动关闭、忽略或覆盖任何知识；
- 状态变更必须写人工审核流水。
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Any, Iterable, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.response import BizError
from app.db.session import AsyncSessionLocal
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.knowledge_governance import (
    KnowledgeDocumentMetadata,
    KnowledgeGovernanceIssue,
    KnowledgeGovernanceReview,
)
from app.services import quality_event_bus


VERSION_STATUSES = {"draft", "active", "superseded", "withdrawn"}
ISSUE_STATUSES = {"open", "confirmed", "resolved", "ignored"}
ISSUE_TYPES = {
    "metadata_missing",
    "not_yet_effective",
    "expired",
    "expiring",
    "review_due",
    "conflict_negation",
    "conflict_threshold",
}

_STATUS_TRANSITIONS = {
    "open": {"open", "confirmed", "resolved", "ignored"},
    "confirmed": {"open", "confirmed", "resolved", "ignored"},
    "resolved": {"open", "resolved"},
    "ignored": {"open", "ignored"},
}

# 触发 doc_blocked 事件的版本状态（withdrawn/superseded；expired 由扫描发现单独 emit）
_BLOCKED_VERSION_STATUSES = {"withdrawn", "superseded"}


async def _maybe_emit_doc_blocked(doc_id: str, reason: str | None,
                                 tenant_id: str = "default") -> None:
    """A2 数据飞轮：emit governance.doc_blocked（订阅者联动清理 Milvus/Neo4j/qa_cache）。

    QUALITY_BUS_ENABLE=False（默认）→ 不 emit 保现状；reason 入 payload 供订阅者区分
    withdraw/supersede/expire。emit 自身已捕异常，无 throw；caller 需 await。
    """
    if not getattr(settings, "QUALITY_BUS_ENABLE", False):
        return
    if not reason:
        return
    try:
        await quality_event_bus.emit(
            "governance", "doc_blocked",
            {"doc_id": doc_id, "reason": reason},
            tenant=tenant_id or "default",
        )
    except Exception:
        pass

_NEGATIVE_MARKERS = (
    "严禁", "禁止", "不得", "不允许", "不可", "不应", "不需要", "无需", "禁止性",
)
_POSITIVE_MARKERS = ("必须", "应当", "需要", "允许", "可以", "应", "需", "须")
_THRESHOLD_CUES = re.compile(
    r"不得超过|不超过|至少|不低于|不高于|高于|低于|超过|达到|限值|范围|应为|必须|"
    r"允许值|额定值|≤|≥|<|>"
)
_THRESHOLD_RE = re.compile(
    r"(?P<value>-?\d+(?:\.\d+)?)\s*"
    r"(?P<unit>℃|°[cC]|kV|KV|kv|mA|MA|MPa|Mpa|kPa|KPa|Pa|Hz|HZ|%|mm|cm|"
    r"分钟|min|小时|天|[VAmhd])"
)
_EQUIPMENT_TERMS = (
    "主变压器", "变压器", "主变", "断路器", "隔离开关", "接地开关", "开关柜", "母线",
    "电缆", "继电保护", "保护装置", "避雷器", "电压互感器", "电流互感器", "互感器",
    "GIS", "电容器", "电抗器", "蓄电池", "直流系统", "接地线", "输电线路", "线路",
    "油色谱", "SF6", "局部放电", "局放",
)
_METRIC_TERMS = (
    "温度", "油温", "绕组温度", "压力", "电流", "电压", "频率", "湿度", "绝缘电阻",
    "油位", "局放", "局部放电", "浓度", "含量", "时间", "距离",
)
_GENERIC_SECTIONS = {
    "总则", "范围", "概述", "前言", "术语", "定义", "基本要求", "一般要求", "安全要求",
    "运行要求", "注意事项", "附录", "参考文献", "规定", "要求",
}


@dataclass(slots=True, frozen=True)
class ChunkSnapshot:
    chunk_id: str
    content: str
    section: str = ""


@dataclass(slots=True)
class DocumentSnapshot:
    doc_id: str
    doc_name: str
    doc_type: str = ""
    equipment_tags: str = ""
    chunks: list[ChunkSnapshot] = field(default_factory=list)
    metadata: Any | None = None


@dataclass(slots=True)
class IssueFinding:
    issue_type: str
    severity: str
    doc_id: str
    title: str
    summary: str
    evidence: dict[str, Any]
    related_doc_id: str | None = None

    @property
    def fingerprint(self) -> str:
        doc_ids = sorted(filter(None, (self.doc_id, self.related_doc_id)))
        raw = "|".join(("kg-v1", self.issue_type, *doc_ids))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    utc_value = _utc_naive(value)
    return f"{utc_value.isoformat(timespec='seconds')}Z" if utc_value else None


def _json_loads(value: str | None) -> dict[str, Any]:
    try:
        loaded = json.loads(value or "{}")
        return loaded if isinstance(loaded, dict) else {"value": loaded}
    except (TypeError, ValueError):
        return {"raw": value or ""}


def _missing_metadata_fields(meta: Any | None) -> list[str]:
    if meta is None:
        return [
            "owner", "applicableRegion", "effectiveAt", "expiryPolicy",
            "reviewPolicy", "versionLabel", "versionStatus",
        ]
    missing: list[str] = []
    if not (getattr(meta, "owner", "") or "").strip():
        missing.append("owner")
    if not (getattr(meta, "applicable_region", "") or "").strip():
        missing.append("applicableRegion")
    if getattr(meta, "effective_at", None) is None:
        missing.append("effectiveAt")
    if not getattr(meta, "is_permanent", False) and getattr(meta, "expires_at", None) is None:
        missing.append("expiryPolicy")
    if not getattr(meta, "review_interval_days", None) and getattr(meta, "next_review_at", None) is None:
        missing.append("reviewPolicy")
    if not (getattr(meta, "version_label", "") or "").strip():
        missing.append("versionLabel")
    if getattr(meta, "version_status", None) not in VERSION_STATUSES:
        missing.append("versionStatus")
    return missing


def effective_state(meta: Any | None, now: datetime | None = None) -> str:
    """推导当前生效状态，不写回数据库。"""
    now = _utc_naive(now) or _utcnow_naive()
    if meta is None:
        return "metadata_incomplete"
    version_status = getattr(meta, "version_status", None)
    if version_status in {"draft", "superseded", "withdrawn"}:
        return version_status
    effective_at = _utc_naive(getattr(meta, "effective_at", None))
    expires_at = _utc_naive(getattr(meta, "expires_at", None))
    if effective_at is None:
        return "metadata_incomplete"
    if effective_at > now:
        return "not_yet_effective"
    if not getattr(meta, "is_permanent", False) and expires_at and expires_at < now:
        return "expired"
    return "active"


def is_retrievable(meta: Any | None, now: datetime | None = None) -> bool:
    """判断治理元数据是否允许进入 RAG 上下文。

    未补录元数据与 ``draft`` 暂时保持兼容并交给治理问题持续催办；明确撤回、
    被新版本替代、尚未生效或已经过期的知识必须从最终检索结果中剔除。
    """
    return effective_state(meta, now) not in {
        "superseded", "withdrawn", "not_yet_effective", "expired",
    }


async def blocked_document_ids(
    db: AsyncSession,
    doc_ids: Iterable[str],
    *,
    tenant_id: str | None = None,
    now: datetime | None = None,
) -> set[str]:
    """返回明确不可用于检索的文档 ID；没有治理记录的旧文档保持可用。"""
    ids = list(dict.fromkeys(doc_id for doc_id in doc_ids if doc_id))
    if not ids:
        return set()
    stmt = select(KnowledgeDocumentMetadata).where(
        KnowledgeDocumentMetadata.doc_id.in_(ids)
    )
    if tenant_id:
        stmt = stmt.where(KnowledgeDocumentMetadata.tenant_id == tenant_id)
    rows = (await db.execute(stmt)).scalars().all()
    return {row.doc_id for row in rows if not is_retrievable(row, now)}


def build_lifecycle_findings(
    doc: DocumentSnapshot,
    *,
    now: datetime | None = None,
    expiry_warning_days: int = 30,
) -> list[IssueFinding]:
    """纯函数：根据一份文档快照生成时效治理发现。"""
    now = _utc_naive(now) or _utcnow_naive()
    meta = doc.metadata
    findings: list[IssueFinding] = []
    missing = _missing_metadata_fields(meta)
    if missing:
        findings.append(IssueFinding(
            issue_type="metadata_missing",
            severity="warning",
            doc_id=doc.doc_id,
            title=f"文档治理元数据不完整：{doc.doc_name}",
            summary=f"缺少 {len(missing)} 项治理元数据，需责任人补录后复核。",
            evidence={
                "docName": doc.doc_name,
                "missingFields": missing,
                "explanation": "缺失字段会影响生效判断、到期提醒和责任追踪。",
            },
        ))
    if meta is None:
        return findings

    version_status = getattr(meta, "version_status", None)
    effective_at = _utc_naive(getattr(meta, "effective_at", None))
    expires_at = _utc_naive(getattr(meta, "expires_at", None))
    next_review_at = _utc_naive(getattr(meta, "next_review_at", None))
    if version_status in {"superseded", "withdrawn"}:
        return findings

    if effective_at and effective_at > now:
        findings.append(IssueFinding(
            issue_type="not_yet_effective",
            severity="info",
            doc_id=doc.doc_id,
            title=f"文档尚未生效：{doc.doc_name}",
            summary=f"计划于 {_iso(effective_at)} 生效。",
            evidence={
                "docName": doc.doc_name,
                "effectiveAt": _iso(effective_at),
                "scanTime": _iso(now),
                "explanation": "生效时间晚于本次扫描时间。",
            },
        ))

    if not getattr(meta, "is_permanent", False) and expires_at:
        if expires_at < now:
            findings.append(IssueFinding(
                issue_type="expired",
                severity="critical",
                doc_id=doc.doc_id,
                title=f"文档已失效：{doc.doc_name}",
                summary=f"文档已于 {_iso(expires_at)} 失效，引用前必须人工确认。",
                evidence={
                    "docName": doc.doc_name,
                    "expiresAt": _iso(expires_at),
                    "scanTime": _iso(now),
                    "overdueDays": max(0, (now - expires_at).days),
                    "explanation": "失效时间早于本次扫描时间。",
                },
            ))
        elif expires_at <= now + timedelta(days=expiry_warning_days):
            findings.append(IssueFinding(
                issue_type="expiring",
                severity="warning",
                doc_id=doc.doc_id,
                title=f"文档即将失效：{doc.doc_name}",
                summary=f"文档将在 {(expires_at - now).days + 1} 天内失效。",
                evidence={
                    "docName": doc.doc_name,
                    "expiresAt": _iso(expires_at),
                    "scanTime": _iso(now),
                    "warningDays": expiry_warning_days,
                    "explanation": "失效时间进入预警窗口。",
                },
            ))

    if next_review_at and next_review_at <= now:
        findings.append(IssueFinding(
            issue_type="review_due",
            severity="warning",
            doc_id=doc.doc_id,
            title=f"文档到期未复审：{doc.doc_name}",
            summary=f"计划复审时间为 {_iso(next_review_at)}。",
            evidence={
                "docName": doc.doc_name,
                "nextReviewAt": _iso(next_review_at),
                "scanTime": _iso(now),
                "overdueDays": max(0, (now - next_review_at).days),
                "explanation": "下次复审时间不晚于本次扫描时间。",
            },
        ))
    return findings


def _split_sentences(text: str) -> Iterable[str]:
    for sentence in re.split(r"[。！？；;\n\r]+", text or ""):
        sentence = re.sub(r"\s+", " ", sentence).strip()
        if 6 <= len(sentence) <= 500:
            yield sentence


def _normalize_unit(unit: str) -> str:
    key = unit.replace("°", "").lower()
    aliases = {
        "c": "℃", "kv": "kV", "v": "V", "ma": "mA", "a": "A",
        "mpa": "MPa", "kpa": "kPa", "pa": "Pa", "hz": "Hz",
        "分钟": "min", "小时": "h", "天": "d",
    }
    return aliases.get(key, unit)


def _polarity(sentence: str) -> int:
    if any(marker in sentence for marker in _NEGATIVE_MARKERS):
        return -1
    if any(marker in sentence for marker in _POSITIVE_MARKERS):
        return 1
    return 0


def _normal_tokens(sentence: str) -> set[str]:
    normalized = sentence.lower()
    for marker in sorted((*_NEGATIVE_MARKERS, *_POSITIVE_MARKERS), key=len, reverse=True):
        normalized = normalized.replace(marker.lower(), "")
    normalized = re.sub(r"-?\d+(?:\.\d+)?", "#", normalized)
    normalized = re.sub(r"[^a-z0-9#\u4e00-\u9fff]+", "", normalized)
    tokens: set[str] = set()
    for part in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9#]+", normalized):
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            tokens.update(part[i:i + 2] for i in range(max(0, len(part) - 1)))
            if len(part) == 1:
                tokens.add(part)
        else:
            tokens.add(part)
    return tokens


def _text_similarity(left: str, right: str) -> float:
    a, b = _normal_tokens(left), _normal_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _metric_keys(sentence: str) -> set[str]:
    return {term for term in _METRIC_TERMS if term.lower() in sentence.lower()}


def _threshold_values(sentence: str) -> list[tuple[float, str]]:
    if not _THRESHOLD_CUES.search(sentence):
        return []
    values: list[tuple[float, str]] = []
    for match in _THRESHOLD_RE.finditer(sentence):
        try:
            values.append((float(match.group("value")), _normalize_unit(match.group("unit"))))
        except ValueError:
            continue
    return values


def _section_scope(section: str) -> str | None:
    cleaned = re.sub(r"^[\d一二三四五六七八九十]+[.、．)）\s-]*", "", section or "")
    cleaned = re.sub(r"\s+", "", cleaned).strip("#：:。.-")
    if not cleaned or cleaned in _GENERIC_SECTIONS or not (2 <= len(cleaned) <= 64):
        return None
    return f"section:{cleaned.lower()}"


def _scope_keys(doc: DocumentSnapshot) -> set[str]:
    scopes: set[str] = set()
    for tag in re.split(r"[,，;；]", doc.equipment_tags or ""):
        tag = tag.strip().lower()
        if tag:
            scopes.add(f"equipment:{tag}")
    sample_parts: list[str] = []
    for chunk in doc.chunks[:80]:
        sample_parts.append(chunk.content[:1000])
        section = _section_scope(chunk.section)
        if section:
            scopes.add(section)
    sample = "\n".join(sample_parts).lower()
    for term in _EQUIPMENT_TERMS:
        if term.lower() in sample:
            scopes.add(f"topic:{term.lower()}")
    return scopes


def _sentence_records(doc: DocumentSnapshot, limit: int = 300) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for chunk in doc.chunks:
        for sentence in _split_sentences(chunk.content):
            records.append({
                "chunkId": chunk.chunk_id,
                "section": chunk.section,
                "text": sentence,
                "polarity": _polarity(sentence),
                "thresholds": _threshold_values(sentence),
                "metrics": _metric_keys(sentence),
            })
            if len(records) >= limit:
                return records
    return records


def _evidence_side(doc: DocumentSnapshot, sentence: dict[str, Any]) -> dict[str, Any]:
    polarity = sentence["polarity"]
    return {
        "docId": doc.doc_id,
        "docName": doc.doc_name,
        "chunkId": sentence["chunkId"],
        "section": sentence["section"],
        "excerpt": sentence["text"][:300],
        "polarity": "negative" if polarity < 0 else "positive" if polarity > 0 else "neutral",
    }


def detect_potential_conflicts(
    documents: Sequence[DocumentSnapshot],
    *,
    max_pairs: int = 3000,
    max_evidence_per_issue: int = 5,
) -> list[IssueFinding]:
    """检测同设备/主题下的潜在否定冲突和阈值冲突。

    返回的是候选证据，不会选择“正确版本”，也不会修改任何文档。
    """
    eligible = [
        doc for doc in documents
        if getattr(doc.metadata, "version_status", None) not in {"superseded", "withdrawn"}
        and doc.chunks
    ]
    scope_docs: dict[str, list[int]] = defaultdict(list)
    for idx, doc in enumerate(eligible):
        scopes = _scope_keys(doc)
        for scope in scopes:
            scope_docs[scope].append(idx)

    pair_scopes: dict[tuple[int, int], set[str]] = defaultdict(set)
    for scope, indexes in scope_docs.items():
        for left, right in combinations(sorted(set(indexes)), 2):
            if len(pair_scopes) >= max_pairs and (left, right) not in pair_scopes:
                break
            pair_scopes[(left, right)].add(scope)

    records = [_sentence_records(doc) for doc in eligible]
    findings: list[IssueFinding] = []
    for (left_idx, right_idx), shared_scopes in pair_scopes.items():
        left, right = eligible[left_idx], eligible[right_idx]
        if left.doc_id > right.doc_id:
            left, right = right, left
            left_idx, right_idx = right_idx, left_idx
        left_records, right_records = records[left_idx], records[right_idx]
        shared = sorted(shared_scopes)[:10]

        negation_matches: list[dict[str, Any]] = []
        left_normative = [r for r in left_records if r["polarity"] != 0]
        right_normative = [r for r in right_records if r["polarity"] != 0]
        for a in left_normative:
            for b in right_normative:
                if a["polarity"] == b["polarity"]:
                    continue
                similarity = _text_similarity(a["text"], b["text"])
                if similarity < 0.68:
                    continue
                negation_matches.append({
                    "left": _evidence_side(left, a),
                    "right": _evidence_side(right, b),
                    "similarity": round(similarity, 3),
                    "explanation": "同一设备或主题下，两条高度相似的规范性表述具有相反极性。",
                })
                if len(negation_matches) >= max_evidence_per_issue:
                    break
            if len(negation_matches) >= max_evidence_per_issue:
                break
        if negation_matches:
            findings.append(IssueFinding(
                issue_type="conflict_negation",
                severity="critical",
                doc_id=left.doc_id,
                related_doc_id=right.doc_id,
                title=f"潜在相反规定：{left.doc_name} / {right.doc_name}",
                summary=f"发现 {len(negation_matches)} 组肯定/禁止方向相反的候选表述，需人工判定适用版本和范围。",
                evidence={
                    "sharedScope": shared,
                    "matchType": "normative_polarity",
                    "matches": negation_matches,
                    "disclaimer": "规则扫描结果仅为潜在冲突，不代表任一文档错误。",
                },
            ))

        threshold_matches: list[dict[str, Any]] = []
        left_thresholds = [r for r in left_records if r["thresholds"]]
        right_thresholds = [r for r in right_records if r["thresholds"]]
        for a in left_thresholds:
            for b in right_thresholds:
                metrics_a, metrics_b = a["metrics"], b["metrics"]
                if metrics_a and metrics_b and not (metrics_a & metrics_b):
                    continue
                similarity = _text_similarity(a["text"], b["text"])
                if similarity < 0.68:
                    continue
                for value_a, unit_a in a["thresholds"]:
                    for value_b, unit_b in b["thresholds"]:
                        if unit_a != unit_b or abs(value_a - value_b) < 1e-9:
                            continue
                        threshold_matches.append({
                            "left": {
                                **_evidence_side(left, a),
                                "threshold": {"value": value_a, "unit": unit_a},
                            },
                            "right": {
                                **_evidence_side(right, b),
                                "threshold": {"value": value_b, "unit": unit_b},
                            },
                            "similarity": round(similarity, 3),
                            "sharedMetric": sorted(metrics_a & metrics_b),
                            "explanation": f"相似规则中的数值阈值不同（{value_a:g}{unit_a} / {value_b:g}{unit_b}）。",
                        })
                        if len(threshold_matches) >= max_evidence_per_issue:
                            break
                    if len(threshold_matches) >= max_evidence_per_issue:
                        break
                if len(threshold_matches) >= max_evidence_per_issue:
                    break
            if len(threshold_matches) >= max_evidence_per_issue:
                break
        if threshold_matches:
            findings.append(IssueFinding(
                issue_type="conflict_threshold",
                severity="critical",
                doc_id=left.doc_id,
                related_doc_id=right.doc_id,
                title=f"潜在阈值冲突：{left.doc_name} / {right.doc_name}",
                summary=f"发现 {len(threshold_matches)} 组相似规则采用不同数值阈值，需结合版本、区域和设备型号审核。",
                evidence={
                    "sharedScope": shared,
                    "matchType": "numeric_threshold",
                    "matches": threshold_matches,
                    "disclaimer": "规则扫描结果仅为潜在冲突，不自动选择或覆盖阈值。",
                },
            ))
    return findings


def _metadata_dict(meta: KnowledgeDocumentMetadata | None) -> dict[str, Any] | None:
    if meta is None:
        return None
    return {
        "docId": meta.doc_id,
        "owner": meta.owner or "",
        "applicableRegion": meta.applicable_region or "",
        "effectiveAt": _iso(meta.effective_at),
        "expiresAt": _iso(meta.expires_at),
        "isPermanent": bool(meta.is_permanent),
        "reviewIntervalDays": meta.review_interval_days,
        "nextReviewAt": _iso(meta.next_review_at),
        "versionLabel": meta.version_label or "",
        "versionStatus": meta.version_status or "",
        "createdBy": meta.created_by or "",
        "updatedBy": meta.updated_by or "",
        "createdAt": _iso(meta.created_at),
        "updatedAt": _iso(meta.updated_at),
    }


def _issue_dict(issue: KnowledgeGovernanceIssue) -> dict[str, Any]:
    return {
        "id": issue.id,
        "type": issue.issue_type,
        "severity": issue.severity,
        "status": issue.status,
        "docId": issue.doc_id,
        "relatedDocId": issue.related_doc_id,
        "title": issue.title,
        "summary": issue.summary,
        "evidence": _json_loads(issue.evidence_json),
        "occurrenceCount": issue.occurrence_count,
        "detectedAt": _iso(issue.detected_at),
        "lastSeenAt": _iso(issue.last_seen_at),
        "reviewer": issue.reviewer or "",
        "reviewNote": issue.review_note or "",
        "reviewedAt": _iso(issue.reviewed_at),
    }


async def _tenant_document(db: AsyncSession, doc_id: str, tenant_id: str) -> Document:
    doc = (await db.execute(select(Document).where(
        Document.id == doc_id, Document.tenant_id == tenant_id,
    ))).scalar_one_or_none()
    if not doc:
        raise BizError("文档不存在或不属于当前租户", 404)
    return doc


async def get_metadata(db: AsyncSession, doc_id: str, tenant_id: str) -> dict[str, Any]:
    doc = await _tenant_document(db, doc_id, tenant_id)
    meta = (await db.execute(select(KnowledgeDocumentMetadata).where(
        KnowledgeDocumentMetadata.doc_id == doc_id,
        KnowledgeDocumentMetadata.tenant_id == tenant_id,
    ))).scalar_one_or_none()
    return {
        "document": {"docId": doc.id, "docName": doc.doc_name, "docType": doc.doc_type},
        "metadata": _metadata_dict(meta),
        "effectiveState": effective_state(meta),
        "missingFields": _missing_metadata_fields(meta),
    }


async def upsert_metadata(
    db: AsyncSession,
    doc_id: str,
    tenant_id: str,
    values: dict[str, Any],
    operator: str,
) -> dict[str, Any]:
    doc = await _tenant_document(db, doc_id, tenant_id)
    meta = (await db.execute(select(KnowledgeDocumentMetadata).where(
        KnowledgeDocumentMetadata.doc_id == doc_id,
        KnowledgeDocumentMetadata.tenant_id == tenant_id,
    ))).scalar_one_or_none()
    creating = meta is None
    if meta is None:
        meta = KnowledgeDocumentMetadata(
            doc_id=doc_id, tenant_id=tenant_id, created_by=operator, updated_by=operator,
        )
        db.add(meta)

    allowed = {
        "owner", "applicable_region", "effective_at", "expires_at", "is_permanent",
        "review_interval_days", "next_review_at", "version_label", "version_status",
    }
    normalized = {key: value for key, value in values.items() if key in allowed}
    for key in ("effective_at", "expires_at", "next_review_at"):
        if key in normalized:
            normalized[key] = _utc_naive(normalized[key])
    if normalized.get("is_permanent") is True:
        normalized["expires_at"] = None
    for key, value in normalized.items():
        setattr(meta, key, value)

    if meta.version_status and meta.version_status not in VERSION_STATUSES:
        raise BizError("无效的版本状态", 400)
    if meta.is_permanent and meta.expires_at:
        raise BizError("永久有效文档不能同时设置失效时间", 400)
    if meta.effective_at and meta.expires_at and meta.effective_at > meta.expires_at:
        raise BizError("生效时间不能晚于失效时间", 400)
    if meta.review_interval_days and "next_review_at" not in normalized:
        base = meta.effective_at or _utcnow_naive()
        meta.next_review_at = base + timedelta(days=meta.review_interval_days)
    meta.updated_by = operator
    await db.commit()
    await db.refresh(meta)
    # A2 数据飞轮：withdrawn/superseded 状态 → emit governance.doc_blocked
    # （订阅者联动清理 Milvus/Neo4j/qa_cache；QUALITY_BUS_ENABLE=False 时不 emit 保现状）
    if meta.version_status in _BLOCKED_VERSION_STATUSES:
        await _maybe_emit_doc_blocked(meta.doc_id, meta.version_status, tenant_id)
    return {
        "document": {"docId": doc.id, "docName": doc.doc_name},
        "metadata": _metadata_dict(meta),
        "effectiveState": effective_state(meta),
        "missingFields": _missing_metadata_fields(meta),
        "created": creating,
    }


async def list_documents_with_metadata(
    db: AsyncSession,
    tenant_id: str,
    *,
    keyword: str = "",
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    condition = [Document.tenant_id == tenant_id]
    if keyword:
        condition.append(Document.doc_name.like(f"%{keyword}%"))
    total = (await db.execute(
        select(func.count()).select_from(Document).where(*condition)
    )).scalar() or 0
    rows = (await db.execute(
        select(Document, KnowledgeDocumentMetadata)
        .outerjoin(
            KnowledgeDocumentMetadata,
            and_(
                KnowledgeDocumentMetadata.doc_id == Document.id,
                KnowledgeDocumentMetadata.tenant_id == tenant_id,
            ),
        )
        .where(*condition)
        .order_by(Document.created_at.desc())
        .offset((page - 1) * size).limit(size)
    )).all()
    return {
        "total": total,
        "list": [{
            "docId": doc.id,
            "docName": doc.doc_name,
            "docType": doc.doc_type,
            "documentStatus": doc.status,
            "effectiveState": effective_state(meta),
            "missingFields": _missing_metadata_fields(meta),
            "metadata": _metadata_dict(meta),
        } for doc, meta in rows],
    }


async def _load_snapshots(
    db: AsyncSession,
    tenant_id: str,
    *,
    max_documents: int,
    max_chunks_per_document: int,
    document_ids: Sequence[str] | None = None,
) -> list[DocumentSnapshot]:
    stmt = select(Document).where(Document.tenant_id == tenant_id)
    if document_ids:
        stmt = stmt.where(Document.id.in_(list(dict.fromkeys(document_ids))))
    docs = (await db.execute(
        stmt.order_by(Document.created_at.desc()).limit(max_documents)
    )).scalars().all()
    if not docs:
        return []
    doc_ids = [doc.id for doc in docs]
    metas = (await db.execute(select(KnowledgeDocumentMetadata).where(
        KnowledgeDocumentMetadata.tenant_id == tenant_id,
        KnowledgeDocumentMetadata.doc_id.in_(doc_ids),
    ))).scalars().all()
    meta_map = {meta.doc_id: meta for meta in metas}
    chunk_rows = (await db.execute(select(Chunk).where(
        Chunk.doc_id.in_(doc_ids)
    ).order_by(Chunk.doc_id, Chunk.chunk_idx))).scalars().all()
    chunk_map: dict[str, list[ChunkSnapshot]] = defaultdict(list)
    for chunk in chunk_rows:
        if len(chunk_map[chunk.doc_id]) >= max_chunks_per_document:
            continue
        chunk_map[chunk.doc_id].append(ChunkSnapshot(
            chunk_id=chunk.id, content=chunk.content or "", section=chunk.section or "",
        ))
    return [DocumentSnapshot(
        doc_id=doc.id,
        doc_name=doc.doc_name,
        doc_type=doc.doc_type,
        equipment_tags=doc.equipment_tags or "",
        chunks=chunk_map.get(doc.id, []),
        metadata=meta_map.get(doc.id),
    ) for doc in docs]


async def _persist_findings(
    db: AsyncSession,
    tenant_id: str,
    findings: Sequence[IssueFinding],
    now: datetime,
) -> tuple[int, int]:
    if not findings:
        return 0, 0
    # 同一扫描内按稳定指纹合并，避免重复 insert。
    unique = {finding.fingerprint: finding for finding in findings}
    fingerprints = list(unique)
    rows = (await db.execute(select(KnowledgeGovernanceIssue).where(
        KnowledgeGovernanceIssue.tenant_id == tenant_id,
        KnowledgeGovernanceIssue.fingerprint.in_(fingerprints),
    ))).scalars().all()
    existing = {row.fingerprint: row for row in rows}
    created = updated = 0
    for fingerprint, finding in unique.items():
        evidence_json = json.dumps(finding.evidence, ensure_ascii=False, separators=(",", ":"))
        row = existing.get(fingerprint)
        if row is None:
            db.add(KnowledgeGovernanceIssue(
                tenant_id=tenant_id,
                fingerprint=fingerprint,
                issue_type=finding.issue_type,
                severity=finding.severity,
                status="open",
                doc_id=finding.doc_id,
                related_doc_id=finding.related_doc_id,
                title=finding.title,
                summary=finding.summary,
                evidence_json=evidence_json,
                occurrence_count=1,
                detected_at=now,
                last_seen_at=now,
            ))
            created += 1
            continue
        # 只刷新扫描事实；人工设置的 status/reviewer/review_note 不被扫描器覆盖。
        row.severity = finding.severity
        row.title = finding.title
        row.summary = finding.summary
        row.evidence_json = evidence_json
        row.last_seen_at = now
        row.occurrence_count = (row.occurrence_count or 0) + 1
        updated += 1
    await db.commit()
    return created, updated


async def run_scan(
    db: AsyncSession,
    tenant_id: str = "default",
    *,
    expiry_warning_days: int = 30,
    include_conflicts: bool = True,
    max_documents: int = 100,
    max_chunks_per_document: int = 80,
    document_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    now = _utcnow_naive()
    snapshots = await _load_snapshots(
        db,
        tenant_id,
        max_documents=max_documents,
        max_chunks_per_document=max_chunks_per_document,
        document_ids=document_ids,
    )
    lifecycle: list[IssueFinding] = []
    for doc in snapshots:
        lifecycle.extend(build_lifecycle_findings(
            doc, now=now, expiry_warning_days=expiry_warning_days,
        ))
    conflicts = detect_potential_conflicts(snapshots) if include_conflicts else []
    findings = [*lifecycle, *conflicts]
    created, updated = await _persist_findings(db, tenant_id, findings, now)
    # A2 数据飞轮：扫描发现的 expired 文档 → emit governance.doc_blocked（reason=expired）
    for finding in findings:
        if finding.issue_type == "expired":
            await _maybe_emit_doc_blocked(
                finding.doc_id, "expired", tenant_id,
            )
    by_type: dict[str, int] = defaultdict(int)
    for finding in findings:
        by_type[finding.issue_type] += 1
    return {
        "tenantId": tenant_id,
        "scanTime": _iso(now),
        "documentsScanned": len(snapshots),
        "findings": len(findings),
        "created": created,
        "updated": updated,
        "byType": dict(sorted(by_type.items())),
        "conflictDetection": {
            "enabled": include_conflicts,
            "mode": "explainable_rules",
            "autoOverwrite": False,
        },
    }


async def list_issues(
    db: AsyncSession,
    tenant_id: str,
    *,
    status: str = "",
    issue_type: str = "",
    severity: str = "",
    keyword: str = "",
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    conditions = [KnowledgeGovernanceIssue.tenant_id == tenant_id]
    if status:
        conditions.append(KnowledgeGovernanceIssue.status == status)
    if issue_type:
        conditions.append(KnowledgeGovernanceIssue.issue_type == issue_type)
    if severity:
        conditions.append(KnowledgeGovernanceIssue.severity == severity)
    if keyword:
        conditions.append(or_(
            KnowledgeGovernanceIssue.title.like(f"%{keyword}%"),
            KnowledgeGovernanceIssue.summary.like(f"%{keyword}%"),
        ))
    total = (await db.execute(
        select(func.count()).select_from(KnowledgeGovernanceIssue).where(*conditions)
    )).scalar() or 0
    rows = (await db.execute(select(KnowledgeGovernanceIssue).where(*conditions)
        .order_by(KnowledgeGovernanceIssue.last_seen_at.desc())
        .offset((page - 1) * size).limit(size)
    )).scalars().all()
    return {"total": total, "list": [_issue_dict(row) for row in rows]}


async def get_issue(db: AsyncSession, issue_id: str, tenant_id: str) -> dict[str, Any]:
    issue = (await db.execute(select(KnowledgeGovernanceIssue).where(
        KnowledgeGovernanceIssue.id == issue_id,
        KnowledgeGovernanceIssue.tenant_id == tenant_id,
    ))).scalar_one_or_none()
    if not issue:
        raise BizError("治理问题不存在", 404)
    reviews = (await db.execute(select(KnowledgeGovernanceReview).where(
        KnowledgeGovernanceReview.issue_id == issue_id,
    ).order_by(KnowledgeGovernanceReview.created_at.asc()))).scalars().all()
    data = _issue_dict(issue)
    data["reviewHistory"] = [{
        "id": review.id,
        "fromStatus": review.from_status,
        "toStatus": review.to_status,
        "reviewer": review.reviewer,
        "note": review.note or "",
        "createdAt": _iso(review.created_at),
    } for review in reviews]
    return data


def validate_status_transition(current: str, target: str) -> None:
    if current not in ISSUE_STATUSES or target not in ISSUE_STATUSES:
        raise BizError("无效的治理问题状态", 400)
    if target not in _STATUS_TRANSITIONS[current]:
        raise BizError(f"不允许从 {current} 变更为 {target}", 400)


async def review_issue(
    db: AsyncSession,
    issue_id: str,
    tenant_id: str,
    *,
    status: str,
    note: str,
    reviewer: str,
) -> dict[str, Any]:
    issue = (await db.execute(
        select(KnowledgeGovernanceIssue)
        .where(
            KnowledgeGovernanceIssue.id == issue_id,
            KnowledgeGovernanceIssue.tenant_id == tenant_id,
        )
        .with_for_update()
    )).scalar_one_or_none()
    if not issue:
        raise BizError("治理问题不存在", 404)
    validate_status_transition(issue.status, status)
    if status in {"resolved", "ignored"} and not (note or "").strip():
        raise BizError("解决或忽略问题时必须填写审核说明", 400)
    now = _utcnow_naive()
    db.add(KnowledgeGovernanceReview(
        issue_id=issue.id,
        from_status=issue.status,
        to_status=status,
        reviewer=reviewer,
        note=(note or "").strip(),
        created_at=now,
    ))
    issue.status = status
    issue.reviewer = reviewer
    issue.review_note = (note or "").strip()
    issue.reviewed_at = now
    await db.commit()
    await db.refresh(issue)
    return _issue_dict(issue)


async def get_stats(db: AsyncSession, tenant_id: str) -> dict[str, Any]:
    status_rows = (await db.execute(
        select(KnowledgeGovernanceIssue.status, func.count())
        .where(KnowledgeGovernanceIssue.tenant_id == tenant_id)
        .group_by(KnowledgeGovernanceIssue.status)
    )).all()
    type_rows = (await db.execute(
        select(KnowledgeGovernanceIssue.issue_type, func.count())
        .where(KnowledgeGovernanceIssue.tenant_id == tenant_id)
        .group_by(KnowledgeGovernanceIssue.issue_type)
    )).all()
    total_docs = (await db.execute(
        select(func.count()).select_from(Document).where(Document.tenant_id == tenant_id)
    )).scalar() or 0
    governed_docs = (await db.execute(
        select(func.count()).select_from(KnowledgeDocumentMetadata)
        .where(KnowledgeDocumentMetadata.tenant_id == tenant_id)
    )).scalar() or 0
    return {
        "documents": total_docs,
        "governedDocuments": governed_docs,
        "metadataCoverage": round(governed_docs / total_docs, 4) if total_docs else 0.0,
        "byStatus": {key: value for key, value in status_rows},
        "byType": {key: value for key, value in type_rows},
    }


async def enqueue_governance_scan(
    tenant_id: str,
    *,
    expiry_warning_days: int = 30,
    include_conflicts: bool = True,
    max_documents: int = 100,
    max_chunks_per_document: int = 80,
    document_ids: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """优先把扫描投入持久化任务中心；facade 尚未安装时返回 None。"""
    try:
        from app.services.task_center_service import enqueue_task
    except (ImportError, AttributeError):
        return None
    payload = {
        "tenant_id": tenant_id,
        "expiry_warning_days": expiry_warning_days,
        "include_conflicts": include_conflicts,
        "max_documents": max_documents,
        "max_chunks_per_document": max_chunks_per_document,
        "document_ids": list(document_ids or []),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    # 同租户同参数一小时内复用任务，防止按钮连点产生任务风暴。
    idempotency_key = f"knowledge.scan:{tenant_id}:{_utcnow_naive():%Y%m%d%H}:{digest}"
    result = enqueue_task(
        task_type="knowledge.scan",
        payload=payload,
        queue="default",
        idempotency_key=idempotency_key,
        tenant_id=tenant_id,
    )
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, dict):
        return result
    task_id = getattr(result, "id", None) or getattr(result, "task_id", None)
    return {"taskId": task_id or str(result), "idempotencyKey": idempotency_key}


async def handle_knowledge_governance_scan(
    tenant_id: str | dict[str, Any] = "default",
    context: Any | None = None,
    **options: Any,
) -> dict[str, Any]:
    """持久化任务中心 handler：`knowledge.scan` -> 本函数。

    同时兼容 handler 传入 `(tenant_id, **options)` 或直接传入 payload 字典。
    """
    if isinstance(tenant_id, dict):
        payload = dict(tenant_id)
        payload.update(options)
        tenant = payload.pop("tenant_id", payload.pop("tenantId", "default"))
    else:
        tenant = tenant_id
        payload = dict(options)
    # worker 的 TaskContext 是租户边界的权威来源，不能被 payload 伪造覆盖。
    context_tenant = getattr(context, "tenant_id", None)
    if context_tenant:
        tenant = context_tenant
    allowed = {
        "expiry_warning_days", "include_conflicts", "max_documents",
        "max_chunks_per_document", "document_ids",
    }
    scan_options = {key: value for key, value in payload.items() if key in allowed}
    async with AsyncSessionLocal() as db:
        return await run_scan(db, tenant, **scan_options)
