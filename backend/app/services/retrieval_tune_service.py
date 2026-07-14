"""检索参数扫描引擎（只建议模式）：单参数扰动 + 开关 A/B → 报告缓存。

执行时验证结论（已据此收敛扫描空间，避免无效扫描）：
- mixed_search 已 override 化的参数：RRF_K / RRF_DENSE_WEIGHT / MMR_LAMBDA / TOPK +
  RERANK_ENABLE / MULTI_QUERY_ENABLE / SMALL_TO_BIG_ENABLE → 扫描有效，纳入 PARAM_SPACE/SWITCHES
- CRAG_HIGH/LOW 在 qa_service._crag_correct（不在 mixed_search 评测路径）→ 扫描无效，已移除
- HYDE_ENABLE 在 _hyde_or_cache 子函数（未透传 overrides）→ 扫描无效，已移除
"""
import json
import time
from pathlib import Path

from app.config import settings
from app.core.obs import degraded
from app.services import retrieval_eval_service

_REPORT = Path(__file__).resolve().parent.parent.parent / "data" / "tune_report.json"

# 连续参数候选值（当前值在扫描时一并跑，作该参数 baseline 对照）
PARAM_SPACE: dict[str, list] = {
    "RRF_K": [40, 80],
    "MMR_LAMBDA": [0.3, 0.7],
    "RRF_DENSE_WEIGHT": [0.7, 1.3],
    "TOPK": [3, 8],
}
# 开关类 A/B（均为 mixed_search 顶层 _ov 化，扫描有效）
SWITCHES: list[str] = ["RERANK_ENABLE", "MULTI_QUERY_ENABLE", "SMALL_TO_BIG_ENABLE"]


def _current(param: str):
    if param == "TOPK":       # TOPK 非 settings 字段（mixed_search 参数），当前值=扫描 topk
        return settings.TUNE_SCAN_TOPK
    return getattr(settings, param)


def _build_suggestions(baseline: dict, scan: list[dict], min_improve: float) -> list[dict]:
    """对比 baseline，按四道护栏产出建议（margin / 最优候选 / 多指标 confidence）。"""
    by_param: dict[str, list[dict]] = {}
    for row in scan:
        by_param.setdefault(row["param"], []).append(row)
    suggestions = []
    for param, rows in by_param.items():
        best = max(rows, key=lambda r: r["recall"] - baseline["recall"])  # 护栏③：同参数取提升最大候选
        d_recall = best["recall"] - baseline["recall"]
        d_mrr = best["mrr"] - baseline["mrr"]
        if d_recall < min_improve:  # 护栏①：提升低于 margin 不出
            continue
        # 护栏④：多指标同向判 confidence
        if d_recall >= 0.05 and d_mrr >= 0:
            conf = "high"
        elif d_mrr >= 0:
            conf = "medium"
        else:
            conf = "low"
        suggestions.append({
            "param": param, "current": best.get("current"),
            "suggested": best["value"], "metric": "recall",
            "delta": round(d_recall, 4), "confidence": conf,
            "reason": f"recall {baseline['recall']:.3f}→{best['recall']:.3f}, MRR {baseline['mrr']:.3f}→{best['mrr']:.3f}",
        })
    return suggestions


async def run_scan(db) -> dict:
    """跑完整扫描，写报告缓存，返回报告 dict。"""
    if not settings.TUNE_ENABLE:
        return get_tune_report()
    t0 = time.time()
    topk = settings.TUNE_SCAN_TOPK
    try:
        baseline = await retrieval_eval_service.evaluate_over_golden(db, overrides=None, topk=topk)
    except Exception as e:
        degraded("tune_baseline", e)
        return {"error": f"baseline 评测失败: {e}"}

    # 护栏②：有效样本不足 → 中止（防小样本过拟合）
    if baseline["validSample"] < settings.TUNE_MIN_SAMPLE:
        return {"error": f"有效样本不足({baseline['validSample']}<{settings.TUNE_MIN_SAMPLE})，扫描中止"}

    scan_matrix, switches_result = [], []
    incomplete = False
    try:
        for param, candidates in PARAM_SPACE.items():
            cur = _current(param)
            for val in [cur] + [c for c in candidates if c != cur]:
                m = await retrieval_eval_service.evaluate_over_golden(db, overrides={param: val}, topk=topk)
                if m.get("validSample", 0) < baseline["validSample"]:
                    incomplete = True  # embed/rerank 降级致部分 query 无结果
                scan_matrix.append({"param": param, "value": val, "current": cur,
                                    **{k: m[k] for k in ("recall", "mrr", "ndcg")}})
        for sw in SWITCHES:
            ov = {sw: not bool(getattr(settings, sw))}
            m = await retrieval_eval_service.evaluate_over_golden(db, overrides=ov, topk=topk)
            switches_result.append({"switch": sw, "state": ov[sw],
                                    "recall": m["recall"], "mrr": m["mrr"],
                                    "delta": round(m["recall"] - baseline["recall"], 4)})
    except Exception as e:
        degraded("tune_scan", e)
        incomplete = True

    suggestions = _build_suggestions(baseline, scan_matrix, settings.TUNE_MIN_IMPROVE)
    report = {
        "baseline": baseline, "suggestions": suggestions,
        "scanMatrix": scan_matrix, "switches": switches_result,
        "runAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": round(time.time() - t0, 1),
        "evalCount": len(scan_matrix) + len(switches_result) + 1,
        "incomplete": incomplete,
        "note": "评测不完整（部分 query 无结果，可能 embed/rerank 降级）" if incomplete else "",
    }
    try:
        _REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        degraded("tune_report_write", e)
    try:
        from app.core import metrics
        metrics.RETRIEVAL_TUNE_TOTAL.inc()
        metrics.RETRIEVAL_BASELINE.labels("recall").set(baseline["recall"])
        metrics.RETRIEVAL_BASELINE.labels("mrr").set(baseline["mrr"])
        metrics.RETRIEVAL_BASELINE.labels("ndcg").set(baseline["ndcg"])
    except Exception:
        pass
    return report


def get_tune_report() -> dict:
    """读报告缓存（无则 {empty:True}）。"""
    try:
        return json.loads(_REPORT.read_text(encoding="utf-8")) if _REPORT.exists() else {"empty": True}
    except Exception as e:
        degraded("tune_report_read", e)
        return {"empty": True}
