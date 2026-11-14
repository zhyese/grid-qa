"""Agentic RAG 只读证据边界：检索仅提供候选，来源与权限始终重新读取。"""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.obs import degraded
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.kg_triple import KgTriple
from app.models.knowledge_governance import KnowledgeDocumentMetadata
from app.models.ops_workbench import TelemetryPoint
from app.services import knowledge_governance_service, telemetry_history_service


def _schema(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False},
    }}


TOOL_SCHEMAS = [
    _schema("search_documents", "检索可访问的原始规程/手册分块；保留来源定位。", {
        "query": {"type": "string", "minLength": 1, "maxLength": 1000},
        "doc_type": {"type": "string", "maxLength": 64},
    }, ["query"]),
    _schema("search_cases", "检索已入库的故障案例原文，不生成案例。", {
        "query": {"type": "string", "minLength": 1, "maxLength": 1000},
    }, ["query"]),
    _schema("open_document", "按已知文档ID和分块序号补充原文，最多3600字符。", {
        "doc_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "chunk_idx": {"type": "integer", "minimum": 0},
    }, ["doc_id", "chunk_idx"]),
    _schema("query_graph", "读取有可访问文档来源的图谱关系；关系是抽取结果，不是新事实。", {
        "entity": {"type": "string", "minLength": 1, "maxLength": 256},
    }, ["entity"]),
    _schema("query_telemetry", "只读当前租户已导入遥测；无数据时返回缺口，不推测。", {
        "device_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "metric": {"type": "string", "maxLength": 64},
        "hours": {"type": "number", "minimum": 0.1, "maximum": 2160},
    }, ["device_id"]),
]


def _digest(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                     separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _arguments(tool: str, args: dict) -> dict:
    """拒绝所有未声明字段，包括租户、角色、部门；不允许模型覆盖服务端上下文。"""
    schema = next((s["function"]["parameters"] for s in TOOL_SCHEMAS
                   if s["function"]["name"] == tool), None)
    if schema is None or not isinstance(args, dict):
        raise ValueError("unknown tool or invalid arguments")
    if set(args) - set(schema["properties"]) or set(schema["required"]) - set(args):
        raise ValueError("unexpected or missing arguments")
    out = {}
    for name, value in args.items():
        rule = schema["properties"][name]
        kind = rule["type"]
        if kind == "string":
            if not isinstance(value, str):
                raise ValueError("expected string")
            value = value.strip()
            if not rule.get("minLength", 0) <= len(value) <= rule["maxLength"]:
                raise ValueError("invalid string length")
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("expected number")
            if kind == "integer" and not isinstance(value, int):
                raise ValueError("expected integer")
            if not math.isfinite(value) or value < rule["minimum"] or value > rule.get("maximum", 10**9):
                raise ValueError("invalid numeric range")
        out[name] = value
    return out


class EvidenceReader:
    schemas = TOOL_SCHEMAS

    def __init__(self, db, *, tenant: str, role: str, dept: str):
        self.db = db
        self.tenant = tenant
        self.role = role or ""
        self.dept = dept or ""
        # 遥测统计由现有 series 生成；仅保留本 reader 已签发的原始统计包供复核。
        self._telemetry = {}

    async def execute(self, tool: str, args: dict) -> dict:
        try:
            clean = _arguments(tool, args)
        except (ValueError, TypeError):
            return {"evidence": [], "note": "工具不在只读白名单内，或参数不符合声明；额外参数不接受。",
                    "error": True}
        if not isinstance(self.tenant, str) or not self.tenant.strip():
            return {"evidence": [], "note": "缺少服务端租户上下文，已拒绝读取。", "degraded": True}
        try:
            # 独立读事务：不提交/回滚调用方工作，也不继承 MySQL 长事务的旧快照。
            async with AsyncSession(bind=self.db.bind, expire_on_commit=False,
                                    autoflush=False) as db:
                if tool in {"search_documents", "search_cases"}:
                    items = await self._search(db, clean["query"],
                                               "故障案例" if tool == "search_cases" else clean.get("doc_type"))
                elif tool == "open_document":
                    item = await self._document(db, clean["doc_id"], clean["chunk_idx"], 3600)
                    items = [item] if item else []
                elif tool == "query_graph":
                    items = await self._graph(db, clean["entity"])
                else:
                    return await self._read_telemetry(db, **clean)
                return {"evidence": items, "note": "" if items else "未找到可访问、可追溯且当前有效的来源。"}
        except Exception as exc:
            degraded("agentic_evidence_read", exc)
            return {"evidence": [], "note": "来源读取或权限/治理校验不可用；已拒绝提供未经确认的证据。",
                    "degraded": True}

    async def _source(self, db, doc_id):
        doc = (await db.execute(select(Document).where(
            Document.id == doc_id, Document.tenant_id == self.tenant,
        ))).scalar_one_or_none()
        if not doc:
            return None
        if self.role != "admin":
            if doc.dept and doc.dept != self.dept:
                return None
            roles = {role.strip() for role in (doc.allowed_roles or "").split(",") if role.strip()}
            if roles and self.role not in roles:
                return None
        # 查询失败直接上抛，execute/revalidate 统一 fail-closed，不使用全局 fail-open 开关。
        if doc.id in await knowledge_governance_service.blocked_document_ids(
                db, [doc.id], tenant_id=self.tenant):
            return None
        meta = (await db.execute(select(KnowledgeDocumentMetadata).where(
            KnowledgeDocumentMetadata.doc_id == doc.id,
        ))).scalar_one_or_none()
        if meta and meta.tenant_id != self.tenant:
            return None
        fingerprint = {
            "id": doc.id, "tenant": doc.tenant_id, "name": doc.doc_name,
            "type": doc.doc_type, "object": doc.minio_object, "status": doc.status,
            "dept": doc.dept, "roles": doc.allowed_roles,
            "version": meta.version_label if meta else "",
            "versionStatus": meta.version_status if meta else "",
            "effectiveAt": meta.effective_at if meta else None,
            "expiresAt": meta.expires_at if meta else None,
            "permanent": meta.is_permanent if meta else None,
        }
        return doc, fingerprint

    async def _document(self, db, doc_id, chunk_idx, limit, chunk_id=None, doc_type=None):
        source = await self._source(db, doc_id)
        if source is None:
            return None
        doc, metadata = source
        if doc_type and doc.doc_type != doc_type:
            return None
        stmt = select(Chunk).where(Chunk.doc_id == doc.id, Chunk.chunk_idx == chunk_idx)
        if chunk_id:
            stmt = stmt.where(Chunk.id == chunk_id)
        chunk = (await db.execute(stmt)).scalar_one_or_none()
        if chunk is None or not chunk.content.strip():
            return None
        full = {
            "key": f"document:{doc.id}:{chunk.id}", "kind": "document",
            "text": chunk.content, "docId": doc.id, "docName": doc.doc_name,
            "docType": doc.doc_type, "chunkId": chunk.id, "chunkIdx": chunk.chunk_idx,
            "pageNum": chunk.page_num, "sectionPath": chunk.section_path or chunk.section or "",
            "version": metadata["version"], "tableHeader": chunk.table_header,
        }
        full["sourceHash"] = _digest({"source": metadata, "chunk": full})
        full["text"] = full["text"][:limit]
        return full

    async def _search(self, db, query, doc_type):
        from app.services import retrieval_service
        hits = await retrieval_service.mixed_search(
            db, query, 8, doc_type=doc_type, tenant=self.tenant,
            user_role=self.role, user_dept=self.dept,
        )
        out, seen = [], set()
        for hit in hits[:24]:
            if not isinstance(hit, dict):
                continue
            doc_id, chunk_idx = hit.get("docId"), hit.get("chunkIdx")
            if not isinstance(doc_id, str) or type(chunk_idx) is not int or chunk_idx < 0:
                continue
            item = await self._document(db, doc_id, chunk_idx, 1800,
                                        chunk_id=hit.get("chunkId"), doc_type=doc_type)
            if item and item["key"] not in seen:
                seen.add(item["key"])
                out.append(item)
            if len(out) >= 8:
                break
        return out

    async def _triple(self, db, triple):
        source = await self._source(db, triple.doc_id)
        if source is None:
            return None
        doc, metadata = source
        # 关系的来源是整份文档（现有 KgTriple 无 chunk_id）；原文重切/改写也使旧关系失效。
        source_chunks = (await db.execute(select(
            Chunk.id, Chunk.chunk_idx, Chunk.content, Chunk.page_num, Chunk.section_path,
        ).where(Chunk.doc_id == doc.id).order_by(Chunk.chunk_idx, Chunk.id))).all()
        full = {
            "key": f"graph:{triple.id}", "kind": "graph", "tripleId": triple.id,
            "text": f"{triple.subject} — {triple.relation} → {triple.object}",
            "docId": doc.id, "docName": doc.doc_name, "docType": doc.doc_type,
            "chunkId": "", "chunkIdx": None, "pageNum": None, "sectionPath": "",
            "version": metadata["version"],
        }
        full["sourceHash"] = _digest({"source": metadata, "triple": full,
                                      "documentChunks": [list(row) for row in source_chunks]})
        full["text"] = full["text"][:1800]
        return full

    async def _graph(self, db, entity):
        # 不访问全局 Neo4j；孤立/其他租户的三元组不会成为候选。
        triples = (await db.execute(select(KgTriple).join(
            Document, Document.id == KgTriple.doc_id,
        ).where(Document.tenant_id == self.tenant, or_(
            KgTriple.subject.contains(entity, autoescape=True),
            KgTriple.object.contains(entity, autoescape=True),
        )).order_by(KgTriple.id).limit(32))).scalars().all()
        out = []
        for triple in triples:
            item = await self._triple(db, triple)
            if item:
                out.append(item)
            if len(out) == 8:
                break
        return out

    async def _telemetry_hash(self, db, item):
        stmt = select(TelemetryPoint).where(
            TelemetryPoint.tenant_id == self.tenant,
            TelemetryPoint.device_id == item["deviceId"],
            TelemetryPoint.observed_at >= datetime.fromisoformat(item["start"]),
            TelemetryPoint.observed_at < datetime.fromisoformat(item["end"]),
        )
        if item["metric"]:
            stmt = stmt.where(TelemetryPoint.metric == item["metric"])
        if item.get("source"):
            stmt = stmt.where(TelemetryPoint.source == item["source"])
        points = (await db.execute(stmt.order_by(TelemetryPoint.observed_at, TelemetryPoint.id)
                                  .limit(telemetry_history_service.AGENT_MAX_POINTS + 1))).scalars().all()
        if not points or len(points) > telemetry_history_service.AGENT_MAX_POINTS:
            return None
        good = [point for point in points if point.quality == "good"]
        if len(good) != item["stats"]["count"] or {p.unit for p in good} != {item["unit"]}:
            return None
        return _digest({"tenant": self.tenant, "device": item["deviceId"],
                        "metric": item["metric"], "start": item["start"], "end": item["end"],
                        "points": [{"id": p.id, "source": p.source, "metric": p.metric,
                                    "at": p.observed_at, "value": p.value, "unit": p.unit,
                                    "quality": p.quality} for p in points]})

    async def _read_telemetry(self, db, device_id, metric="", hours=24):
        result = await telemetry_history_service.series(
            db, self.tenant, device_id, metric=metric, hours=hours,
        )
        if not result.get("stats"):
            return {"evidence": [], "note": result.get("note") or "当前时间窗无可信遥测数据。"}
        item = {
            "kind": "telemetry", "deviceId": result["deviceId"],
            "metric": result["metric"], "source": result["source"],
            "start": result["start"], "end": result["end"], "unit": result["stats"]["unit"],
            "stats": result["stats"], "trend": result.get("trend"),
        }
        item["key"] = "telemetry:" + _digest({key: item[key] for key in
            ("deviceId", "metric", "source", "start", "end")})
        item["sourceHash"] = await self._telemetry_hash(db, item)
        if item["sourceHash"] is None:
            return {"evidence": [], "note": "遥测来源范围或样本数量无法完整复核，请缩小时间窗。",
                    "degraded": True}
        # 确定性原始统计展开，保证数值与单位相邻，供最终引用/单位一致性校验。
        stats, unit = item["stats"], item["unit"]
        values = "，".join(f"{label}{str(stats[key]).removesuffix('.0')}{unit}" for label, key in (
            ("最小", "min"), ("最大", "max"), ("均值", "mean"),
            ("首值", "first"), ("末值", "last"), ("变化", "delta"),
        ))
        summary = f"有效样本{stats['count']}个；{values}。\n"
        metadata = json.dumps({**{k: item[k] for k in
            ("deviceId", "metric", "source", "start", "end", "unit", "stats", "trend")},
            "note": result.get("note", "")}, ensure_ascii=False, allow_nan=False)
        item["text"] = (summary + metadata)[:1800]
        self._telemetry[item["key"]] = deepcopy(item)
        return {"evidence": [item], "note": result.get("note", "")}

    async def revalidate(self, evidence: list[dict]) -> list[dict]:
        """以新读事务复核完整源，不以截片做hash；保留编排字段，覆盖来源字段。"""
        valid = []
        try:
            async with AsyncSession(bind=self.db.bind, expire_on_commit=False,
                                    autoflush=False) as db:
                for item in evidence:
                    if not isinstance(item, dict):
                        continue
                    kind, fresh = item.get("kind"), None
                    if kind == "document":
                        fresh = await self._document(db, item.get("docId"), item.get("chunkIdx"),
                                                     min(3600, len(item.get("text", ""))),
                                                     chunk_id=item.get("chunkId"))
                    elif kind == "graph":
                        triple = (await db.execute(select(KgTriple).where(
                            KgTriple.id == item.get("tripleId"),
                        ))).scalar_one_or_none()
                        if triple:
                            fresh = await self._triple(db, triple)
                    elif kind == "telemetry":
                        issued = self._telemetry.get(item.get("key"))
                        if issued and await self._telemetry_hash(db, issued) == issued["sourceHash"]:
                            fresh = deepcopy(issued)
                    if fresh and fresh["sourceHash"] == item.get("sourceHash") and fresh["key"] == item.get("key"):
                        valid.append({**item, **fresh})
        except Exception as exc:
            degraded("agentic_evidence_revalidate", exc)
            return []
        return valid
