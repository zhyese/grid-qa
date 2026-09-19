"""N5 故障仿真演练沙箱：剧本 CRUD + 孪生故障链生成剧本 + 时间轴推演 + 评分与 AI 复盘。

推演无状态：事件是否发生 = (now - started_at) >= tOffset，GET 即时推导，不落中间态；
演练者操作打卡 POST 记录；finish 计算 checklist 覆盖率 + 响应及时性并生成复盘
（LLM 失败降级模板，degraded 不 crash）。纯只读推演，不触碰任何真实设备/工单。
"""
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.obs import degraded
from app.core.response import BizError
from app.models.drill import DrillRun, DrillScenario

_MAX_EVENTS = 30
_MAX_CHECKLIST = 20


def _scenario_row(s: DrillScenario) -> dict:
    return {"id": s.id, "name": s.name, "description": s.description,
            "stationId": s.station_id, "faultDevice": s.fault_device,
            "faultDesc": s.fault_desc, "propagation": s.propagation or [],
            "checklist": s.checklist or [], "difficulty": s.difficulty,
            "enabled": bool(s.enabled), "source": s.source,
            "createdBy": s.created_by,
            "createdAt": s.created_at.isoformat() if s.created_at else None}


def _run_row(r: DrillRun, *, live: bool = False) -> dict:
    d = {"id": r.id, "scenarioId": r.scenario_id, "scenarioName": r.scenario_name,
         "stationId": r.station_id, "faultDevice": r.fault_device,
         "status": r.status, "actions": r.actions or [], "score": r.score or {},
         "evaluationMd": r.evaluation_md, "error": r.error,
         "startedBy": r.started_by,
         "startedAt": r.started_at.isoformat() if r.started_at else None,
         "finishedAt": r.finished_at.isoformat() if r.finished_at else None,
         "durationSec": r.duration_sec}
    if live:
        elapsed = _elapsed_sec(r)
        d["elapsedSec"] = elapsed
        # 无状态推演：到期事件即时推导（复盘前不剧透 checklist）
        d["dueEvents"] = [e for e in (r.propagation or []) if e.get("tOffset", 0) <= elapsed]
        d["totalEvents"] = len(r.propagation or [])
    return d


def _elapsed_sec(r: DrillRun) -> int:
    end = r.finished_at or datetime.now()
    return max(0, int((end - (r.started_at or end)).total_seconds()))


def _validate(propagation: list, checklist: list) -> None:
    if not propagation:
        raise BizError("传播事件时间轴不能为空", 400)
    if len(propagation) > _MAX_EVENTS:
        raise BizError(f"传播事件不超过 {_MAX_EVENTS} 条", 400)
    for e in propagation:
        if not isinstance(e, dict) or not e.get("event"):
            raise BizError("传播事件需包含 event 字段", 400)
        e["tOffset"] = max(0, int(e.get("tOffset", 0)))
    if not checklist:
        raise BizError("应尽动作清单不能为空", 400)
    if len(checklist) > _MAX_CHECKLIST:
        raise BizError(f"动作清单不超过 {_MAX_CHECKLIST} 条", 400)
    for i, c in enumerate(checklist, 1):
        if not isinstance(c, dict) or not c.get("action"):
            raise BizError("动作清单项需包含 action 字段", 400)
        if not c.get("id"):  # 空串也算未传（setdefault 不覆盖已存在的空键）
            c["id"] = f"c{i}"
        c["keywords"] = [str(k) for k in (c.get("keywords") or []) if str(k).strip()][:8]


# ---------- 剧本 CRUD ----------

async def list_scenarios(db: AsyncSession, tenant_id: str, keyword: str = "",
                         page: int = 1, size: int = 20) -> tuple[int, list[dict]]:
    conds = [DrillScenario.tenant == tenant_id]
    if keyword:
        conds.append(DrillScenario.name.contains(keyword))
    total = (await db.execute(select(func.count(DrillScenario.id)).where(*conds))).scalar() or 0
    rows = (await db.execute(
        select(DrillScenario).where(*conds)
        .order_by(DrillScenario.updated_at.desc())
        .offset((page - 1) * size).limit(size))).scalars().all()
    return total, [_scenario_row(s) for s in rows]


async def get_scenario(db: AsyncSession, scenario_id: str, tenant_id: str) -> DrillScenario:
    s = (await db.execute(select(DrillScenario).where(
        DrillScenario.id == scenario_id,
        DrillScenario.tenant == tenant_id))).scalar_one_or_none()
    if s is None:
        raise BizError("演练剧本不存在", 404)
    return s


async def create_scenario(db: AsyncSession, tenant_id: str, username: str, name: str,
                          description: str, station_id: str, fault_device: str,
                          fault_desc: str, propagation: list, checklist: list,
                          difficulty: str = "normal") -> dict:
    if not (name or "").strip():
        raise BizError("剧本名称不能为空", 400)
    _validate(propagation, checklist)
    s = DrillScenario(id=uuid.uuid4().hex, name=name.strip()[:120],
                      description=(description or "")[:500], station_id=station_id or "",
                      fault_device=fault_device or "", fault_desc=(fault_desc or "")[:500],
                      propagation=propagation, checklist=checklist,
                      difficulty=difficulty if difficulty in ("easy", "normal", "hard") else "normal",
                      tenant=tenant_id, created_by=username)
    db.add(s)
    await db.commit()
    await db.refresh(s)  # MySQL 无 RETURNING，server_default 列需 refresh 才可读
    return _scenario_row(s)


async def update_scenario(db: AsyncSession, scenario_id: str, tenant_id: str, **kwargs) -> dict:
    s = await get_scenario(db, scenario_id, tenant_id)
    if (kwargs.get("propagation") is not None
            and kwargs.get("checklist") is not None):
        _validate(kwargs["propagation"], kwargs["checklist"])
    for k, v in kwargs.items():
        if v is None:
            continue
        if k in ("name", "description", "station_id", "fault_device", "fault_desc",
                 "propagation", "checklist", "difficulty"):
            setattr(s, k, v)
        elif k == "enabled":
            s.enabled = 1 if v else 0
    await db.commit()
    await db.refresh(s)  # onupdate 后属性过期，async session 禁属性级懒 IO
    return _scenario_row(s)


async def delete_scenario(db: AsyncSession, scenario_id: str, tenant_id: str) -> dict:
    s = await get_scenario(db, scenario_id, tenant_id)
    running = (await db.execute(select(func.count(DrillRun.id)).where(
        DrillRun.scenario_id == scenario_id, DrillRun.status == "running"))).scalar() or 0
    if running:
        raise BizError("该剧本有进行中的演练，先终止再删除", 400)
    await db.delete(s)
    await db.commit()
    return {"id": scenario_id}


# ---------- 从孪生故障链生成剧本 ----------

_DEFAULT_CHECKLIST = [
    {"action": "查看故障设备详情与遥测", "keywords": ["查看", "设备", "遥测"]},
    {"action": "核对告警与传播影响范围", "keywords": ["告警", "传播", "影响"]},
    {"action": "隔离故障设备（远程/现场）", "keywords": ["隔离", "断开", "停运"]},
    {"action": "汇报调度并通知检修", "keywords": ["汇报", "调度", "通知", "检修"]},
    {"action": "生成两票并执行安全措施", "keywords": ["两票", "工作票", "操作票", "安措"]},
]


def _extract_chain_devices(paths: list) -> list[str]:
    """从 kg 多跳路径提取设备名序列（防御式：路径结构随 kg 版本演化，只取字符串token）。"""
    devices: list[str] = []
    for p in paths or []:
        tokens = []
        if isinstance(p, dict):
            tokens = [v for v in p.values() if isinstance(v, (list, str))]
            seq = []
            for t in tokens:
                if isinstance(t, str):
                    seq.append(t)
                elif isinstance(t, list):
                    seq += [x for x in t if isinstance(x, str)]
            tokens = seq
        elif isinstance(p, list):
            tokens = [x for x in p if isinstance(x, str)]
        for t in tokens:
            t = t.strip()
            if t and t not in devices and len(t) <= 40:
                devices.append(t)
        if len(devices) >= 8:
            break
    return devices


async def scenario_from_fault_chain(db: AsyncSession, tenant_id: str, username: str,
                                    station_id: str, device_id: str,
                                    name: str = "") -> dict:
    """孪生故障链 → 演练剧本（事件按 60s 间隔沿链扩散，checklist 用默认处置清单）。"""
    from app.services.twin_service import get_fault_chain

    try:
        paths = await get_fault_chain(device_id, depth=3, station_id=station_id or None)
    except Exception as e:  # noqa: BLE001 — 孪生/图谱不可用时降级为单设备故障剧本
        degraded("drill_fault_chain", e)
        paths = []
    devices = _extract_chain_devices(paths) or [device_id]
    if device_id not in devices:
        devices.insert(0, device_id)
    propagation = [{
        "tOffset": 0, "device": devices[0], "severity": "critical",
        "event": f"{devices[0]} 发生故障告警，请立即研判处置",
    }]
    for i, dev in enumerate(devices[1:6], 1):
        propagation.append({
            "tOffset": i * 60, "device": dev, "severity": "warning",
            "event": f"故障影响扩散：{dev} 出现关联异常信号",
        })
    propagation.append({
        "tOffset": 360, "device": devices[0], "severity": "info",
        "event": "调度催办：要求 5 分钟内汇报处置进展",
    })
    import copy

    checklist = copy.deepcopy(_DEFAULT_CHECKLIST)
    _validate(propagation, checklist)
    s = DrillScenario(
        id=uuid.uuid4().hex,
        name=(name or f"故障演练·{devices[0]}·{datetime.now().strftime('%m%d%H%M')}").strip()[:120],
        description=f"由孪生故障链自动生成（设备 {device_id}，站点 {station_id or '默认'}），可按需编辑。",
        station_id=station_id or "", fault_device=device_id,
        fault_desc=f"{devices[0]} 疑似故障（孪生故障链 {len(paths)} 条路径推导）",
        propagation=propagation, checklist=checklist,
        difficulty="normal", source="fault_chain", tenant=tenant_id, created_by=username)
    db.add(s)
    await db.commit()
    await db.refresh(s)  # MySQL 无 RETURNING，server_default 列需 refresh 才可读
    return _scenario_row(s)


# ---------- 演练运行 ----------

async def start_run(db: AsyncSession, tenant_id: str, username: str,
                    scenario_id: str) -> dict:
    s = await get_scenario(db, scenario_id, tenant_id)
    if not s.enabled:
        raise BizError("剧本已停用", 400)
    r = DrillRun(id=uuid.uuid4().hex, scenario_id=s.id, scenario_name=s.name,
                 station_id=s.station_id, fault_device=s.fault_device,
                 status="running", propagation=s.propagation or [],
                 checklist=s.checklist or [], tenant=tenant_id,
                 started_by=username, started_at=datetime.now())
    db.add(r)
    await db.commit()
    return _run_row(r, live=True)


async def get_run(db: AsyncSession, run_id: str, tenant_id: str) -> DrillRun:
    r = (await db.execute(select(DrillRun).where(
        DrillRun.id == run_id, DrillRun.tenant == tenant_id))).scalar_one_or_none()
    if r is None:
        raise BizError("演练记录不存在", 404)
    return r


async def run_state(db: AsyncSession, run_id: str, tenant_id: str) -> dict:
    """轮询端点：到期事件 + 已打卡动作；超时自动判 finished（模板评分，无 LLM）。"""
    r = await get_run(db, run_id, tenant_id)
    if r.status == "running":
        limit = int(getattr(settings, "DRILL_MAX_DURATION_SECONDS", 3600))
        if _elapsed_sec(r) > limit:
            await finish_run(db, run_id, tenant_id, username=r.started_by,
                             auto=True)
            await db.refresh(r)
    return _run_row(r, live=True)


async def record_action(db: AsyncSession, run_id: str, tenant_id: str,
                        username: str, action: str) -> dict:
    r = await get_run(db, run_id, tenant_id)
    if r.status != "running":
        raise BizError(f"演练已{r.status}，无法继续打卡", 400)
    if not (action or "").strip():
        raise BizError("操作内容不能为空", 400)
    actions = list(r.actions or [])
    actions.append({"t": _elapsed_sec(r), "user": username,
                    "action": action.strip()[:300]})
    r.actions = actions
    await db.commit()
    return _run_row(r, live=True)


def _score_run(r: DrillRun) -> dict:
    """评分：checklist 覆盖率（操作文本含任一关键词即命中）+ 命中项响应时延。"""
    actions = r.actions or []
    matched, missed, resp_secs = [], [], []
    used: set[int] = set()  # 一条操作只算一次命中（防一条"查看两票"刷两项）
    for c in (r.checklist or []):
        hit_idx, hit_t = None, None
        for i, a in enumerate(actions):
            text = a.get("action", "")
            if i in used:
                continue
            if any(k and k in text for k in c.get("keywords", [])):
                hit_idx, hit_t = i, a.get("t", 0)
                used.add(i)
                break
        if hit_idx is not None:
            # 响应时延 = 命中操作时刻 − 注入故障时刻（t=0），即从故障发生到完成该动作的秒数
            resp = max(0, hit_t)
            resp_secs.append(resp)
            matched.append({"checklistId": c.get("id"), "actionIdx": hit_idx,
                            "responseSec": resp})
        else:
            missed.append(c.get("id"))
    coverage = round(len(matched) / len(r.checklist or ["x"]), 3) if r.checklist else 0
    avg = round(sum(resp_secs) / len(resp_secs), 1) if resp_secs else None
    grade = ("优秀" if coverage >= 0.9 else
             "良好" if coverage >= 0.7 else
             "合格" if coverage >= 0.5 else "需复训")
    return {"matched": matched, "missed": missed, "coverage": coverage,
            "avgResponseSec": avg, "grade": grade}


_EVAL_PROMPT = """你是电网运维演练教官。基于演练数据写一份简短复盘（Markdown）：
## 表现
## 亮点与不足
## 改进建议
要求 200 字内，只依据数据不编造。

演练数据（JSON）：
{data}"""


def _template_evaluation(r: DrillRun, score: dict) -> str:
    missed_actions = [c.get("action", "") for c in (r.checklist or [])
                      if c.get("id") in (score.get("missed") or [])]
    return (
        "## 表现\n"
        f"动作覆盖率 {int((score.get('coverage') or 0) * 100)}%"
        f"（评级：{score.get('grade')}），"
        f"平均响应 {score.get('avgResponseSec') or '—'} 秒。\n\n"
        "## 亮点与不足\n"
        + ("遗漏动作：" + "；".join(missed_actions) + "\n" if missed_actions
           else "全部关键动作已覆盖。\n")
        + "\n## 改进建议\n（LLM 不可用，模板复盘）针对遗漏动作强化培训，"
        "压缩首步响应时间，演练后对照规程复盘。")


async def finish_run(db: AsyncSession, run_id: str, tenant_id: str, username: str,
                     auto: bool = False, with_llm: bool = True) -> dict:
    r = await get_run(db, run_id, tenant_id)
    if r.status != "running":
        return _run_row(r, live=False)
    r.status = "finished"
    r.finished_at = datetime.now()
    r.duration_sec = _elapsed_sec(r)
    score = _score_run(r)
    r.score = score
    if with_llm and not auto:
        data = {"scenario": r.scenario_name, "faultDevice": r.fault_device,
                "events": r.propagation, "actions": r.actions,
                "score": score, "durationSec": r.duration_sec, "auto": auto}
        try:
            import json

            from app.providers.factory import get_llm_provider
            raw = await get_llm_provider(None).chat(
                [{"role": "user", "content": _EVAL_PROMPT.format(
                    data=json.dumps(data, ensure_ascii=False)[:8000])}],
                temperature=0.3, max_tokens=600)
            text = (raw or "").strip()
            if len(text) > 20:
                r.evaluation_md = text
            else:
                raise ValueError("LLM 空复盘")
        except Exception as e:  # noqa: BLE001 — LLM 失败降级模板复盘
            degraded("drill_eval_llm", e)
            r.evaluation_md = _template_evaluation(r, score)
    else:
        r.evaluation_md = _template_evaluation(r, score)
    await db.commit()
    return _run_row(r, live=False)


async def abort_run(db: AsyncSession, run_id: str, tenant_id: str,
                    username: str) -> dict:
    r = await get_run(db, run_id, tenant_id)
    if r.status != "running":
        raise BizError("演练已结束", 400)
    r.status = "aborted"
    r.finished_at = datetime.now()
    r.duration_sec = _elapsed_sec(r)
    r.score = _score_run(r)  # 中止也给当前进度评分（覆盖不全自然低分）
    await db.commit()
    return _run_row(r, live=False)


async def list_runs(db: AsyncSession, tenant_id: str, scenario_id: str = "",
                    page: int = 1, size: int = 20) -> tuple[int, list[dict]]:
    conds = [DrillRun.tenant == tenant_id]
    if scenario_id:
        conds.append(DrillRun.scenario_id == scenario_id)
    total = (await db.execute(select(func.count(DrillRun.id)).where(*conds))).scalar() or 0
    rows = (await db.execute(
        select(DrillRun).where(*conds)
        .order_by(DrillRun.started_at.desc())
        .offset((page - 1) * size).limit(size))).scalars().all()
    return total, [_run_row(r, live=False) for r in rows]


async def drill_stats(db: AsyncSession, tenant_id: str) -> dict:
    """演练统计：次数/覆盖率均值/评级分布。"""
    rows = (await db.execute(
        select(DrillRun).where(DrillRun.tenant == tenant_id,
                               DrillRun.status == "finished")
        .order_by(DrillRun.started_at.desc()).limit(500))).scalars().all()
    grades: dict[str, int] = {}
    covs: list[float] = []
    for r in rows:
        sc = r.score or {}
        g = sc.get("grade", "")
        if g:
            grades[g] = grades.get(g, 0) + 1
        if sc.get("coverage") is not None:
            covs.append(float(sc["coverage"]))
    running = (await db.execute(select(func.count(DrillRun.id)).where(
        DrillRun.tenant == tenant_id, DrillRun.status == "running"))).scalar() or 0
    return {"finished": len(rows), "running": running,
            "avgCoverage": round(sum(covs) / len(covs), 3) if covs else None,
            "byGrade": grades}
