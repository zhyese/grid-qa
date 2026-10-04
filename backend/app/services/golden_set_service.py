"""F1 数据资产与标注工作台：golden 集全生命周期服务。

职责（对齐调研报告 v3 F1）：
- 资产化：用例带 category/difficulty/device_tags/expected_sources/label_status 落 MySQL；
- 版本化：publish 生成版本快照 + sha256 哈希链（防篡改可追溯，借鉴 doc_signoff 思路）；
- 批量导入：CSV / Excel（openpyxl）；
- 闭环：从质量事件总线（dislike / low_faith / eval_low / refused）把坏 case 拉成待标注草稿；
- 发布：把「已标注/已复核」的条目写回 `backend/data/golden_qa.json`，
  运行时评测（eval_retrieval / eval_matrix / validate_golden）零改动；
- 导出：一键导出 Laya 三问题原语训练格式（choice/noul/score 冷启动外壳）。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from sqlalchemy import func, or_, select

from app.core.obs import degraded
from app.core.response import BizError
from app.models.golden_set import GoldenCase, GoldenSetVersion

# 与 scripts/validate_golden.py、retrieval_eval_service 保持同一路径
GOLDEN_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "golden_qa.json"

LABEL_STATUSES = ("pending", "labeled", "verified", "rejected")
PUBLISHABLE_STATUSES = ("labeled", "verified")
DIFFICULTIES = ("easy", "medium", "hard")
MAX_IMPORT_ROWS = 2000
MAX_QUERY_LEN = 512   # 与 GoldenCase.query 的 VARCHAR(512) 对齐（MySQL 唯一约束要求 VARCHAR）

# Laya 三问题原语（spec 2026-09-21 §3.1）
LAYA_ACTION_OPTIONS = ["持续观察", "通知调度", "隔离故障设备", "转检修工单", "紧急停运"]


# ---------- 序列化 / 校验 ----------

def _loads(raw: str, fallback):
    try:
        v = json.loads(raw or "")
        return v if isinstance(v, type(fallback)) else fallback
    except Exception:
        return fallback


def _split_tags(raw: str) -> list[str]:
    return [t.strip() for t in (raw or "").replace("，", ",").split(",") if t.strip()]


def validate_case(case: dict) -> list[str]:
    """校验规则与 scripts/validate_golden.py 完全一致（发布前门禁）。"""
    errors: list[str] = []
    q = case.get("query")
    if not q or not isinstance(q, str) or not q.strip():
        errors.append("query 缺失或非字符串")
    elif len(q) > MAX_QUERY_LEN:
        errors.append(f"query 超过 {MAX_QUERY_LEN} 字符上限（当前 {len(q)}）")
    expect = case.get("expect", [])
    if not isinstance(expect, list) or any(not isinstance(x, str) or not x.strip() for x in expect):
        errors.append("expect 必须是字符串数组且元素非空")
    if not expect and case.get("source") != "feedback":
        errors.append("expect 为空且 source 非 feedback")
    if not case.get("category"):
        errors.append("category 缺失")
    rd = case.get("relevant_docs")
    if rd is not None:
        if not isinstance(rd, dict):
            errors.append("relevant_docs 必须是对象")
        elif not all(isinstance(k, str) and k and isinstance(v, int) and 1 <= v <= 3
                     for k, v in rd.items()):
            errors.append("relevant_docs value 必须是 1~3 的整数")
    return errors


def serialize_case(c: GoldenCase) -> dict:
    return {
        "id": c.id,
        "query": c.query,
        "expect": _loads(c.expect_json, []),
        "category": c.category or "",
        "difficulty": c.difficulty or "",
        "deviceTags": _split_tags(c.device_tags),
        "expectedSources": _loads(c.expected_sources_json, []),
        "relevantDocs": _loads(c.relevant_docs_json, {}),
        "labelStatus": c.label_status or "pending",
        "source": c.source or "manual",
        "note": c.note or "",
        "createdBy": c.created_by or "",
        "updatedBy": c.updated_by or "",
        "updatedAt": c.updated_at.strftime("%Y-%m-%d %H:%M:%S") if c.updated_at else "",
    }


def _to_golden_item(c: GoldenCase) -> dict:
    """资产条目 → golden_qa.json 条目（评测链路消费的字段）。"""
    item = {
        "query": c.query,
        "expect": _loads(c.expect_json, []),
        "category": c.category or "",
        "source": c.source or "manual",
    }
    rd = _loads(c.relevant_docs_json, {})
    if rd:
        item["relevant_docs"] = rd
    if c.difficulty:
        item["difficulty"] = c.difficulty
    tags = _split_tags(c.device_tags)
    if tags:
        item["device_tags"] = tags
    srcs = _loads(c.expected_sources_json, [])
    if srcs:
        item["expected_sources"] = srcs
    return item


# ---------- 查询 ----------

async def list_cases(db, tenant: str, keyword: str = "", category: str = "",
                     difficulty: str = "", label_status: str = "",
                     page: int = 1, size: int = 20) -> dict:
    conds = [GoldenCase.tenant_id == tenant, GoldenCase.is_deleted == 0]
    if keyword:
        conds.append(or_(GoldenCase.query.like(f"%{keyword}%"),
                         GoldenCase.device_tags.like(f"%{keyword}%")))
    if category:
        conds.append(GoldenCase.category == category)
    if difficulty:
        conds.append(GoldenCase.difficulty == difficulty)
    if label_status:
        conds.append(GoldenCase.label_status == label_status)

    total = (await db.execute(
        select(func.count()).select_from(GoldenCase).where(*conds)
    )).scalar() or 0
    rows = (await db.execute(
        select(GoldenCase).where(*conds)
        .order_by(GoldenCase.updated_at.desc())
        .offset((max(1, page) - 1) * size).limit(size)
    )).scalars().all()
    return {"total": int(total), "list": [serialize_case(r) for r in rows]}


async def stats(db, tenant: str) -> dict:
    base = [GoldenCase.tenant_id == tenant, GoldenCase.is_deleted == 0]

    async def _group(col):
        rows = (await db.execute(
            select(col, func.count(GoldenCase.id)).where(*base).group_by(col)
        )).all()
        return {str(k or "未标注"): int(v) for k, v in rows}

    total = (await db.execute(
        select(func.count()).select_from(GoldenCase).where(*base)
    )).scalar() or 0
    published = (await db.execute(
        select(func.count()).select_from(GoldenCase).where(
            *base, GoldenCase.label_status.in_(PUBLISHABLE_STATUSES))
    )).scalar() or 0
    latest = (await db.execute(
        select(GoldenSetVersion).where(GoldenSetVersion.tenant_id == tenant)
        .order_by(GoldenSetVersion.version.desc()).limit(1)
    )).scalar_one_or_none()
    file_count = 0
    try:
        if GOLDEN_PATH.exists():
            file_count = len(json.loads(GOLDEN_PATH.read_text(encoding="utf-8")))
    except Exception as e:
        degraded("golden_file_count", e)
    return {
        "total": int(total),
        "publishable": int(published),
        "goldenFileCount": file_count,
        "byStatus": await _group(GoldenCase.label_status),
        "byCategory": await _group(GoldenCase.category),
        "byDifficulty": await _group(GoldenCase.difficulty),
        "latestVersion": latest.version if latest else 0,
        "latestPublishedAt": (latest.created_at.strftime("%Y-%m-%d %H:%M:%S")
                              if latest and latest.created_at else ""),
    }


# ---------- 增删改 ----------

async def upsert_case(db, tenant: str, username: str, payload: dict,
                      case_id: str = "") -> dict:
    """新增/更新用例（按 (tenant, query) 唯一；改 query 视为新用例）。"""
    query = (payload.get("query") or "").strip()
    if not query:
        raise BizError("query 不能为空", 400)
    if len(query) > MAX_QUERY_LEN:
        raise BizError(f"query 超过 {MAX_QUERY_LEN} 字符上限（当前 {len(query)}）", 400)
    difficulty = (payload.get("difficulty") or "").strip()
    if difficulty and difficulty not in DIFFICULTIES:
        raise BizError(f"difficulty 必须是 {DIFFICULTIES} 之一", 400)
    status = (payload.get("labelStatus") or payload.get("label_status") or "pending").strip()
    if status not in LABEL_STATUSES:
        raise BizError(f"labelStatus 必须是 {LABEL_STATUSES} 之一", 400)

    row = None
    if case_id:
        row = (await db.execute(
            select(GoldenCase).where(GoldenCase.id == case_id, GoldenCase.tenant_id == tenant)
        )).scalar_one_or_none()
        if not row:
            raise BizError("用例不存在", 404)
    else:
        row = (await db.execute(
            select(GoldenCase).where(GoldenCase.tenant_id == tenant,
                                     GoldenCase.query == query)
        )).scalar_one_or_none()
        if row and row.is_deleted:
            row.is_deleted = 0
    if row is None:
        row = GoldenCase(tenant_id=tenant, query=query, created_by=username)
        db.add(row)

    row.query = query
    row.expect_json = json.dumps(payload.get("expect") or [], ensure_ascii=False)
    row.relevant_docs_json = json.dumps(payload.get("relevantDocs") or {}, ensure_ascii=False)
    row.category = (payload.get("category") or "").strip()
    row.difficulty = difficulty
    tags = payload.get("deviceTags") or []
    row.device_tags = ",".join(tags) if isinstance(tags, list) else str(tags or "")
    row.expected_sources_json = json.dumps(payload.get("expectedSources") or [],
                                           ensure_ascii=False)
    row.label_status = status
    row.source = (payload.get("source") or "manual").strip()
    row.note = (payload.get("note") or "")[:500]
    row.updated_by = username
    await db.commit()
    await db.refresh(row)
    errors = validate_case(_to_golden_item(row))
    return {"case": serialize_case(row), "validationErrors": errors}


async def delete_case(db, tenant: str, case_id: str) -> dict:
    row = (await db.execute(
        select(GoldenCase).where(GoldenCase.id == case_id, GoldenCase.tenant_id == tenant)
    )).scalar_one_or_none()
    if not row:
        raise BizError("用例不存在", 404)
    row.is_deleted = 1
    await db.commit()
    return {"id": case_id, "deleted": True}


# ---------- 批量导入 ----------

def _rows_from_csv(content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig", errors="ignore")
    reader = csv.DictReader(io.StringIO(text))
    return [dict(r) for r in reader]


def _rows_from_xlsx(content: bytes) -> list[dict]:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(h or "").strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        if r is None or all(c is None or str(c).strip() == "" for c in r):
            continue
        out.append({header[i]: r[i] for i in range(min(len(header), len(r)))})
    return out


def parse_upload(filename: str, content: bytes) -> list[dict]:
    """解析 CSV/Excel 为行字典（列名对齐用例字段，支持中英文别名）。"""
    name = (filename or "").lower()
    if name.endswith(".csv"):
        rows = _rows_from_csv(content)
    elif name.endswith((".xlsx", ".xlsm")):
        rows = _rows_from_xlsx(content)
    else:
        raise BizError("仅支持 CSV / XLSX 导入", 400)
    if len(rows) > MAX_IMPORT_ROWS:
        raise BizError(f"单次导入不超过 {MAX_IMPORT_ROWS} 行（当前 {len(rows)}）", 400)
    return rows


def _norm_row(row: dict) -> dict:
    """列名归一：兼容中文表头与常见别名。"""
    def pick(*keys):
        for k in keys:
            for rk, rv in row.items():
                if rk and str(rk).strip().lower() == k:
                    return rv
        return None

    def as_list(v):
        if v is None:
            return []
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if str(x).strip()]
        s = str(v).strip()
        if not s:
            return []
        sep = "|" if "|" in s else (";" if ";" in s else ("," if "," in s else "，"))
        return [p.strip() for p in s.split(sep) if p.strip()]

    query = pick("query", "问题", "问句", "query_text")
    expect = pick("expect", "期望", "期望关键词", "关键词")
    category = pick("category", "分类", "场景")
    difficulty = pick("difficulty", "难度")
    tags = pick("devicetags", "device_tags", "设备标签", "设备")
    sources = pick("expectedsources", "expected_sources", "期望来源")
    status = pick("labelstatus", "label_status", "标注状态")
    return {
        "query": (str(query).strip() if query is not None else ""),
        "expect": as_list(expect),
        "category": (str(category).strip() if category is not None else ""),
        "difficulty": (str(difficulty).strip() if difficulty is not None else ""),
        "deviceTags": as_list(tags),
        "expectedSources": as_list(sources),
        "labelStatus": (str(status).strip() if status is not None else "labeled"),
        "source": "import",
    }


async def batch_import(db, tenant: str, username: str, rows: list[dict]) -> dict:
    """批量导入：逐行 upsert（按 query 去重），返回成功/失败明细。"""
    ok, failed = 0, []
    for i, raw in enumerate(rows, 1):
        try:
            data = _norm_row(raw)
            if not data["query"]:
                failed.append(f"第{i}行：缺少 query")
                continue
            if data["difficulty"] and data["difficulty"] not in DIFFICULTIES:
                data["difficulty"] = ""
            if data["labelStatus"] not in LABEL_STATUSES:
                data["labelStatus"] = "labeled"
            await upsert_case(db, tenant, username, data)
            ok += 1
        except Exception as e:
            failed.append(f"第{i}行：{e}")
    return {"imported": ok, "failed": len(failed), "errors": failed[:20]}


# ---------- 质量事件回流（坏 case → 待标注草稿）----------

_BAD_EVENT_TYPES = ("dislike", "low_faith", "eval_low", "refused")


async def ingest_quality_events(db, tenant: str, username: str, limit: int = 200) -> dict:
    """把质量事件里的坏 case 拉成 label_status=pending 的待标注草稿（按 query 去重）。"""
    from app.models.quality_event import QualityEvent

    rows = (await db.execute(
        select(QualityEvent).where(
            QualityEvent.tenant == tenant,
            QualityEvent.type.in_(_BAD_EVENT_TYPES),
        ).order_by(QualityEvent.created_at.desc()).limit(max(1, min(limit, 1000)))
    )).scalars().all()

    created, skipped = 0, 0
    seen: set[str] = set()
    for ev in rows:
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        query = str(payload.get("query") or payload.get("question") or "").strip()
        if not query or query in seen:
            skipped += 1
            continue
        seen.add(query)
        exists = (await db.execute(
            select(GoldenCase.id).where(GoldenCase.tenant_id == tenant,
                                        GoldenCase.query == query).limit(1)
        )).scalar_one_or_none()
        if exists:
            skipped += 1
            continue
        db.add(GoldenCase(
            tenant_id=tenant, query=query, category="待分类",
            label_status="pending", source="quality_event",
            note=f"来自质量事件 {ev.type}（{ev.created_at}）",
            created_by=username, updated_by=username,
        ))
        created += 1
    if created:
        await db.commit()
    return {"created": created, "skipped": skipped, "scanned": len(rows)}


# ---------- 发布（版本 + 哈希链 + 写回 golden_qa.json）----------

def _content_hash(prev_hash: str, snapshot_json: str) -> str:
    raw = f"{prev_hash}|{snapshot_json}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def publish(db, tenant: str, username: str, note: str = "") -> dict:
    """把「已标注/已复核」且校验通过的用例发布为 golden_qa.json + 记录版本。"""
    rows = (await db.execute(
        select(GoldenCase).where(
            GoldenCase.tenant_id == tenant,
            GoldenCase.is_deleted == 0,
            GoldenCase.label_status.in_(PUBLISHABLE_STATUSES),
        ).order_by(GoldenCase.category, GoldenCase.query)
    )).scalars().all()
    if not rows:
        raise BizError("没有可发布的用例（需 label_status 为 labeled/verified）", 400)

    items, invalid = [], []
    for c in rows:
        item = _to_golden_item(c)
        errs = validate_case(item)
        if errs:
            invalid.append({"query": c.query[:60], "errors": errs})
            continue
        items.append(item)
    if not items:
        raise BizError(f"全部 {len(invalid)} 条用例校验未通过，未发布", 400)

    snapshot_json = json.dumps(items, ensure_ascii=False, indent=2)
    last = (await db.execute(
        select(GoldenSetVersion).where(GoldenSetVersion.tenant_id == tenant)
        .order_by(GoldenSetVersion.version.desc()).limit(1)
    )).scalar_one_or_none()
    version = (last.version + 1) if last else 1
    prev_hash = last.content_hash if last else hashlib.sha256(
        f"golden:{tenant}:genesis".encode("utf-8")).hexdigest()
    content_hash = _content_hash(prev_hash, snapshot_json)

    # 写回文件（评测链路读取点）；先写临时文件再替换，避免半截文件被 CI 读到
    try:
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = GOLDEN_PATH.with_suffix(".json.tmp")
        tmp.write_text(snapshot_json, encoding="utf-8")
        tmp.replace(GOLDEN_PATH)
    except Exception as e:
        degraded("golden_publish_write", e)
        raise BizError(f"写回 golden_qa.json 失败：{e}", 500) from e

    db.add(GoldenSetVersion(
        tenant_id=tenant, version=version, case_count=len(items),
        snapshot_json=snapshot_json, content_hash=content_hash, prev_hash=prev_hash,
        note=note[:500], published=1, created_by=username,
    ))
    await db.commit()
    # 评测缓存失效：golden 变了 → 关联缓存无必要保留（评测脚本每次读文件，无需额外处理）
    return {
        "version": version, "published": len(items), "invalid": len(invalid),
        "invalidDetail": invalid[:20],
        "contentHash": content_hash, "prevHash": prev_hash,
        "path": str(GOLDEN_PATH),
        "nextStep": "python scripts/validate_golden.py && python scripts/eval_retrieval.py --topk 5 --threshold 0.92",
    }


async def list_versions(db, tenant: str, limit: int = 50) -> dict:
    rows = (await db.execute(
        select(GoldenSetVersion).where(GoldenSetVersion.tenant_id == tenant)
        .order_by(GoldenSetVersion.version.desc()).limit(limit)
    )).scalars().all()
    return {"versions": [{
        "version": r.version, "caseCount": r.case_count,
        "contentHash": r.content_hash, "prevHash": r.prev_hash,
        "note": r.note or "", "createdBy": r.created_by or "",
        "createdAt": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
    } for r in rows]}


async def diff_versions(db, tenant: str, v1: int, v2: int) -> dict:
    rows = (await db.execute(
        select(GoldenSetVersion).where(
            GoldenSetVersion.tenant_id == tenant,
            GoldenSetVersion.version.in_([v1, v2]),
        )
    )).scalars().all()
    vmap = {r.version: r for r in rows}
    if v1 not in vmap or v2 not in vmap:
        raise BizError("版本不存在", 404)

    def _idx(raw: str) -> dict:
        return {it.get("query", ""): it for it in _loads(raw, [])}

    a, b = _idx(vmap[v1].snapshot_json), _idx(vmap[v2].snapshot_json)
    added = [q for q in b if q not in a]
    removed = [q for q in a if q not in b]
    changed = [q for q in b if q in a and json.dumps(a[q], sort_keys=True, ensure_ascii=False)
               != json.dumps(b[q], sort_keys=True, ensure_ascii=False)]
    return {"v1": v1, "v2": v2, "v1Count": len(a), "v2Count": len(b),
            "added": added[:100], "removed": removed[:100], "changed": changed[:100],
            "addedCount": len(added), "removedCount": len(removed), "changedCount": len(changed)}


# ---------- Laya 冷启动导出 ----------

async def export_laya(db, tenant: str, label_status: str = "") -> dict:
    """导出 Laya 三问题原语格式（choice/noul/score）——**冷启动外壳**。

    说明（诚实标注）：golden 用例是"检索问答"，不是告警分诊样本，因此这里只填
    `state` 与设备/分类元信息，三个 gold 标签一律留空并标 needsLabel=true，
    由 Laya M0 的 LLM 蒸馏 / 人工标注补齐。这正是"数据冷启动底座"的定位。
    """
    conds = [GoldenCase.tenant_id == tenant, GoldenCase.is_deleted == 0]
    if label_status:
        conds.append(GoldenCase.label_status == label_status)
    rows = (await db.execute(
        select(GoldenCase).where(*conds).order_by(GoldenCase.category, GoldenCase.query)
    )).scalars().all()
    samples = []
    for c in rows:
        tags = _split_tags(c.device_tags)
        samples.append({
            "state": (f"电网运维问答｜分类:{c.category or '未分类'}"
                      f"｜设备:{'/'.join(tags) if tags else '未标注'}｜问题:{c.query}"),
            "questions": [
                {"id": "action", "kind": "choice", "options": LAYA_ACTION_OPTIONS, "gold": None},
                {"id": "urgency", "kind": "noul",
                 "question": "是否需要立即人工介入？", "gold": None},
                {"id": "severity", "kind": "score", "max": 4,
                 "question": "综合严重程度(0-4)？", "gold": None},
            ],
            "meta": {
                "goldenCaseId": c.id,
                "needsLabel": True,
                "category": c.category or "",
                "difficulty": c.difficulty or "",
                "deviceTags": tags,
                "expectedDocs": _loads(c.expected_sources_json, []),
                "labelStatus": c.label_status or "pending",
            },
        })
    return {
        "count": len(samples),
        "samples": samples,
        "note": ("冷启动外壳：gold 标签留空待标注（needsLabel=true）。"
                 "可直接作为 scripts/laya_synth_data.py 的素材输入。"),
    }


def verify_hash_chain(versions: list[dict]) -> dict:
    """校验版本哈希链完整性（供审计/前端展示；版本需按 version 升序传入）。"""
    prev = None
    for v in versions:
        if prev is None:
            prev = v
            continue
        if v.get("prevHash") != prev.get("contentHash"):
            return {"ok": False, "brokenAt": v.get("version")}
        prev = v
    return {"ok": True, "checked": len(versions)}
