"""故障预测建议（BRD §5.3.1 功能扩展）。

基于历史数据做趋势/频次聚合，给出「哪些设备/告警类型近期在升温、历史同类怎么处理、建议关注什么」
的主动建议。不依赖额外模型，纯统计 + 规则，可解释、零额外成本。

数据源：
- operation_logs(operate_type='告警') 近 30 天 —— 提取 severity/title，按标题聚合 + 趋势对比
- tickets / feedbacks —— 关联历史故障与坏 case 计数，反哺风险语境

输出：风险条目列表，按 riskScore 降序。
"""
import datetime
import re
from collections import Counter

from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.operation_log import OperationLog
from app.models.feedback import Feedback
from app.models.ticket import Ticket


_ALERT_RE = re.compile(r"\[(?P<sev>[a-z]+)\]\s*(?P<title>[^：:(（]+)")


def _parse_alert(content: str) -> tuple[str, str]:
    """'[critical] 主变油温高：...' → ('critical', '主变油温高')。"""
    if not content:
        return ("warning", "未知告警")
    m = _ALERT_RE.search(content)
    if m:
        return (m.group("sev").strip(), m.group("title").strip())
    return ("warning", content[:30])


def _severity_weight(sev: str) -> int:
    return {"critical": 3, "error": 3, "warning": 2, "info": 1}.get((sev or "").lower(), 1)


def _risk_level(score: float) -> str:
    if score >= 10:
        return "高"
    if score >= 5:
        return "中"
    return "低"


def _suggestion(title: str, sev: str, trend: str, cnt: int) -> str:
    pref = "建议重点关注" if trend == "上升" else "建议定期巡视"
    if sev == "critical":
        pref = "建议立即排查"
    return f"「{title}」近30天告警 {cnt} 次（趋势{trend}），{pref}，结合历史故障案例核对处置预案。"


async def predict(days: int = 30) -> dict:
    """生成故障预测建议。"""
    now = datetime.datetime.now()
    window_start = now - datetime.timedelta(days=days)
    # 近 7 天 vs 上一个 7 天，判趋势
    recent7 = now - datetime.timedelta(days=7)
    prev7 = now - datetime.timedelta(days=14)

    async with AsyncSessionLocal() as db:
        alerts = (await db.execute(
            select(OperationLog.operate_type, OperationLog.content, OperationLog.operate_time)
            .where(OperationLog.operate_type == "告警", OperationLog.operate_time >= window_start)
        )).all()
        ticket_cnt = (await db.execute(
            select(func.count()).select_from(Ticket)
        )).scalar() or 0
        bad_cnt = (await db.execute(
            select(func.count()).select_from(Feedback).where(Feedback.feedback == "dislike")
        )).scalar() or 0

    # 按标题聚合（全窗口）
    bucket: dict[str, dict] = {}
    sev_seen: dict[str, str] = {}
    rec7_cnt: Counter = Counter()
    prev7_cnt: Counter = Counter()
    for _typ, content, t in alerts:
        sev, title = _parse_alert(content)
        b = bucket.setdefault(title, {"title": title, "count": 0, "sev": sev})
        b["count"] += 1
        # 保留最高严重度
        if _severity_weight(sev) > _severity_weight(b["sev"]):
            b["sev"] = sev
        if t >= recent7:
            rec7_cnt[title] += 1
        elif t >= prev7:
            prev7_cnt[title] += 1

    items = []
    for title, b in bucket.items():
        r7, p7 = rec7_cnt.get(title, 0), prev7_cnt.get(title, 0)
        if r7 > p7:
            trend = "上升"
        elif r7 < p7 or (r7 == 0 and p7 == 0 and b["count"] == 0):
            trend = "下降" if r7 < p7 else "平稳"
        else:
            trend = "平稳"
        score = b["count"] * _severity_weight(b["sev"]) + (2 if trend == "上升" else 0)
        items.append({
            "title": title,
            "severity": b["sev"],
            "count": b["count"],
            "recent7": r7,
            "prev7": p7,
            "trend": trend,
            "riskScore": score,
            "riskLevel": _risk_level(score),
            "suggestion": _suggestion(title, b["sev"], trend, b["count"]),
        })
    items.sort(key=lambda x: x["riskScore"], reverse=True)

    high = sum(1 for i in items if i["riskLevel"] == "高")
    return {
        "windowDays": days,
        "totalAlerts": len(alerts),
        "distinctTitles": len(bucket),
        "ticketCount": ticket_cnt,
        "badCaseCount": bad_cnt,
        "highRiskCount": high,
        "items": items[:20],  # Top 20 风险条目
        "generatedAt": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
