"""开关对照评测矩阵纯核心：variants 注册表 + env 覆盖 + delta/verdict + 报告渲染。

spec: docs/superpowers/specs/2026-09-01-eval-matrix-design.md
- VARIANTS 全部引用 config.py 实有开关（关=现状）；baseline=全关（现状）。
- 采集走"子进程 + env 覆盖"（pydantic-settings case_sensitive=False 读 env），业务代码零改动。
- verdict 只出建议（评测先行、人拍板）；本模块是评测工具，无运行时开关、
  不影响问答语义、不进 citation_cache_version()。
"""
from __future__ import annotations

import time

# 变体注册表：name 对应 .env 键的 snake_case；env 值恒为 "true"。
# dims: retrieval=子进程直调 evaluate_over_golden（需 Milvus+embedding，无需 LLM/后端）
#       generation=每变体起独立后端子进程 + run_generation_eval（需 LLM key）
VARIANTS: list[dict] = [
    {"name": "baseline", "title": "现状（对照基线）", "env": {},
     "dims": ("retrieval", "generation")},
    # ---- 检索维（retrieval_service 直读 settings，子进程 env 覆盖生效）----
    {"name": "rrf_route_aware", "title": "路由感知调参（A2/A3/A5/B6）",
     "env": {"RRF_ROUTE_AWARE_ENABLE": "true"}, "dims": ("retrieval",)},
    {"name": "crag_neighbor", "title": "CRAG ambiguous 邻域扩展",
     "env": {"CRAG_NEIGHBOR_EXPAND_ENABLE": "true"}, "dims": ("retrieval",)},
    {"name": "raptor", "title": "RAPTOR 层次化摘要检索",
     "env": {"RAPTOR_ENABLE": "true"}, "dims": ("retrieval",)},
    {"name": "hyde", "title": "HyDE 假设答案检索",
     "env": {"HYDE_ENABLE": "true"}, "dims": ("retrieval",)},
    {"name": "multi_query", "title": "多查询分解",
     "env": {"MULTI_QUERY_ENABLE": "true"}, "dims": ("retrieval",)},
    {"name": "self_rag", "title": "Self-RAG 检索自判",
     "env": {"SELF_RAG_ENABLE": "true"}, "dims": ("retrieval",)},
    # ---- 生成维（qa_service 直读 settings，需变体后端）----
    {"name": "crag_v3", "title": "CRAG v3 连续置信度+归因",
     "env": {"CRAG_V3_ENABLE": "true"}, "dims": ("generation",)},
    {"name": "crag_perdoc", "title": "CRAG v2 LLM 逐条证据评估（token 成本注意）",
     "env": {"CRAG_PERDOC_ENABLE": "true"}, "dims": ("generation",)},
    {"name": "citation_verifier", "title": "引用校验引擎（格式+向量+结构化输出）",
     "env": {"CITATION_VERIFIER_ENABLE": "true", "CITATION_STRUCTURED_OUTPUT": "true"},
     "dims": ("generation",)},
    {"name": "citation_nli", "title": "引用校验3 NLI 精准核验（最重档）",
     "env": {"CITATION_VERIFIER_ENABLE": "true", "CITATION_STRUCTURED_OUTPUT": "true",
             "CITATION_NLI_ENABLE": "true"}, "dims": ("generation",)},
    {"name": "debate", "title": "低置信 debate 三专家裁决（预期增延迟）",
     "env": {"DEBATE_ON_LOW_CONFIDENCE_ENABLE": "true"}, "dims": ("generation",)},
    {"name": "sufficiency_gate", "title": "证据不足置信降档",
     "env": {"SUFFICIENCY_GATE_ENABLE": "true"}, "dims": ("generation",)},
    {"name": "query_rewrite", "title": "LLM query 改写",
     "env": {"QUERY_REWRITE_ENABLE": "true"}, "dims": ("generation",)},
    {"name": "semantic_cache", "title": "语义缓存（首跑不命中，收益看时延列）",
     "env": {"SEMANTIC_CACHE_ENABLE": "true"}, "dims": ("generation",)},
]

# 越高越好的指标；其余（noResultRate/hallucination）越低越好；avgLatencyMs 仅列示不判
_HIGHER_BETTER = frozenset({"recall", "mrr", "ndcg", "faithfulness"})
_PRIMARY = {"retrieval": "recall", "generation": "faithfulness"}
_ADOPT_GAIN = {"retrieval": 0.01, "generation": 0.02}
_REGRESS_EPS = 0.005
_COST_KEYS = frozenset({"avgLatencyMs"})
_SKIP_DELTA = frozenset({"sampleSize"})

_COLS = {
    "retrieval": ("recall", "mrr", "ndcg", "noResultRate"),
    "generation": ("faithfulness", "hallucination", "avgLatencyMs"),
}
_LABEL = {"recall": "召回", "mrr": "MRR", "ndcg": "nDCG", "noResultRate": "空结果率",
          "faithfulness": "支撑率", "hallucination": "幻觉率", "avgLatencyMs": "平均时延ms"}


def get_variant(name: str) -> dict:
    for v in VARIANTS:
        if v["name"] == name:
            return v
    raise ValueError(f"未知变体: {name}（可选: {','.join(v['name'] for v in VARIANTS)}）")


def select_variants(names: str, dims: set[str]) -> list[dict]:
    """按逗号名单挑变体（all=全部）；baseline 恒含且居首；每变体 dims 与所选维度求交。"""
    if names.strip() == "all":
        picked = list(VARIANTS)
    else:
        picked = [get_variant(n.strip()) for n in names.split(",") if n.strip()]
    if not any(v["name"] == "baseline" for v in picked):
        picked.insert(0, get_variant("baseline"))
    out = []
    for v in picked:
        dims_v = tuple(d for d in v["dims"] if d in dims)
        if dims_v:
            out.append({**v, "dims": dims_v})
    return out


def build_env_overlay(variant: dict, base_env: dict | None = None) -> dict:
    """子进程 env = 父 env + 变体覆盖；编码强制 utf-8 兜 Windows GBK（同 eval_suite.run_dim）。

    矩阵是观测仪器：强制关闭数据飞轮触发器（EVAL_EMIT/EVAL_TO_TUNE），否则 baseline
    探针 recall 低于门禁会 emit eval_low → 订阅者在探针进程内再跑一遍全量 tune 扫描，
    单探针耗时翻数倍（2026-09-03 首跑实测主因之一）。置于变体覆盖之后（不可被变体翻回）。
    """
    env = {**(base_env or {}), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    env.update(variant.get("env") or {})
    env["EVAL_EMIT_ENABLE"] = "false"
    env["EVAL_TO_TUNE_ENABLE"] = "false"
    return env


def compute_delta(base: dict, cur: dict) -> dict:
    """逐指标 Δ=cur-base；非数值与 _SKIP_DELTA 跳过；direction 标注好坏方向。"""
    out = {}
    for key, cur_v in cur.items():
        if key in _SKIP_DELTA or not isinstance(cur_v, (int, float)) or isinstance(cur_v, bool):
            continue
        base_v = base.get(key)
        if not isinstance(base_v, (int, float)) or isinstance(base_v, bool):
            continue
        out[key] = {"base": base_v, "cur": cur_v, "delta": round(cur_v - base_v, 4),
                    "direction": "higher" if key in _HIGHER_BETTER else "lower"}
    return out


def build_verdict(dim: str, delta: dict) -> str:
    """verdict 只建议：恶化>eps→建议回收；主指标升≥阈值且无退化→建议常开候选；否则维持关闭。"""
    if not delta:
        return "—"
    regressed = sorted(
        k for k, d in delta.items()
        if k not in _COST_KEYS and (
            (d["direction"] == "higher" and d["delta"] < -_REGRESS_EPS)
            or (d["direction"] == "lower" and d["delta"] > _REGRESS_EPS)
        )
    )
    if regressed:
        return f"建议回收（存在退化: {','.join(regressed)}）"
    primary = delta.get(_PRIMARY[dim])
    if primary and primary["delta"] >= _ADOPT_GAIN[dim]:
        return "建议常开候选"
    return "维持关闭（收益不足）"


def aggregate(probe_results: list[dict]) -> dict:
    """probe_results=[{variant,dim,metrics}] → 按维度分组、对 baseline 算 Δ 与 verdict。

    行序按 VARIANTS 注册表（baseline 恒首）；缺探针的变体不出行。
    """
    by_dim: dict[str, dict] = {}
    for r in probe_results:
        by_dim.setdefault(r["dim"], {}).setdefault("raw", {})[r["variant"]] = r["metrics"]
    out = {}
    for dim, info in by_dim.items():
        base = info["raw"].get("baseline", {})
        rows = []
        for v in VARIANTS:
            metrics = info["raw"].get(v["name"])
            if metrics is None:
                continue
            is_base = v["name"] == "baseline"
            delta = {} if is_base else compute_delta(base, metrics)
            rows.append({
                "variant": v["name"], "title": v["title"], "metrics": metrics,
                "delta": delta, "verdict": "—" if is_base else build_verdict(dim, delta),
            })
        out[dim] = {"baseline": base, "rows": rows, "sampleSize": base.get("sampleSize")}
    return out


def _fmt(v) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v)
    if 0 <= v <= 1.5:
        return f"{v * 100:.1f}%"
    return f"{v:,.0f}"


def _fmt_delta(key: str, d: dict) -> str:
    v = d["delta"]
    if key == "avgLatencyMs":
        return f"{v:+,.0f}ms"
    return f"{v * 100:+.1f}pp"


def render_markdown(agg: dict, meta: dict) -> str:
    """矩阵 md：逐维度 Δ 表 + 建议列 + 噪声/语义缓存/只建议三条警示。"""
    lines = [
        f"# 开关对照评测矩阵（{time.strftime('%Y-%m-%d %H:%M:%S')}）", "",
        f"- 环境：{meta.get('envSummary', '')}",
        f"- 参数：topk={meta.get('topk')} limit={meta.get('limit')} "
        f"评测集条数={meta.get('goldenSize', '?')}", "",
    ]
    for dim in ("retrieval", "generation"):
        block = agg.get(dim)
        if not block:
            continue
        cols = _COLS[dim]
        header = (["变体", "说明"]
                  + [c for k in cols for c in (_LABEL[k], f"Δ{_LABEL[k]}")] + ["建议"])
        lines += [f"## {dim} 维", "",
                  "| " + " | ".join(header) + " |",
                  "|" + "---|" * len(header)]
        for row in block["rows"]:
            cells = [row["variant"], row["title"]]
            for k in cols:
                m = row["metrics"].get(k)
                cells.append(_fmt(m) if m is not None else "—")
                d = row["delta"].get(k)
                cells.append(_fmt_delta(k, d) if d else "—")
            cells.append(row["verdict"])
            lines.append("| " + " | ".join(str(c) for c in cells) + " |")
        lines.append("")
    n = int(meta.get("goldenSize") or 0)
    if 0 < n < 50:
        lines.append(f"> ⚠️ 噪声警告：评测集仅 {n} 条，Δ<2pp 不具备统计意义；先扩集再下常开/回收决策。")
    lines += [
        "> ⚠️ semantic_cache 首跑不命中：收益看时延列，需二次运行验证。",
        "> verdict 仅为建议：开关变更需人工评审后改 .env（关=现状语义见 config.py 注释）。", "",
    ]
    return "\n".join(lines) + "\n"


# ===== 产品化：API 触发（子进程）+ 报告浏览 + 定时 cron =====
# 容器内运行依赖 compose 挂载 ./scripts(只读) 与 ./reports(读写)——工具/数据目录，
# 与"后端源码烘焙进镜像"的铁律无冲突（不挂 app 源码）。

import asyncio
import os
import re
import subprocess
import sys

_running = {"active": False, "dims": "", "pid": None, "startedAt": "", "lastError": ""}
_NAME_RE = re.compile(r"^eval_matrix_[A-Za-z0-9_\-]+\.md$")


def _reports_dir() -> str:
    for cand in (os.path.join(os.getcwd(), "reports"), "/app/reports"):
        if os.path.isdir(cand):
            return cand
    return os.path.join(os.getcwd(), "reports")


def _script_path() -> str:
    for cand in (os.path.join(os.getcwd(), "scripts", "eval_matrix.py"),
                 "/app/scripts/eval_matrix.py"):
        if os.path.isfile(cand):
            return cand
    return ""


async def run_matrix_async(dims: list[str]) -> dict:
    """后台子进程跑评测矩阵（防重入：active 时拒绝）。返回触发结果。"""
    from app.core.response import BizError

    if _running["active"]:
        raise BizError("评测矩阵正在运行中，请等本轮完成", 400)
    dims = [d for d in (dims or []) if d in ("retrieval", "generation")] or ["retrieval"]
    script = _script_path()
    if not script:
        raise BizError("scripts/eval_matrix.py 不可达（容器需挂载 ./scripts）", 500)
    _running.update(active=True, dims=",".join(dims), pid=None,
                    startedAt=time.strftime("%Y-%m-%d %H:%M:%S"), lastError="")

    async def _bg() -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, script, "--dims", ",".join(dims),
                cwd=os.path.dirname(os.path.dirname(script)),  # 仓库根（reports/ 相对它落地）
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            _running["pid"] = proc.pid
            _, err = await proc.communicate()
            if proc.returncode != 0:
                _running["lastError"] = (err or b"").decode("utf-8", "ignore")[:500]
        except Exception as e:  # noqa: BLE001 — 子进程异常落状态可见
            from app.core.obs import degraded
            degraded("eval_matrix_run", e)
            _running["lastError"] = str(e)[:500]
        finally:
            _running["active"] = False
            await _notify_done()

    asyncio.create_task(_bg())
    return {"started": True, "dims": dims}


async def _notify_done() -> None:
    from app.db.session import AsyncSessionLocal
    from app.services import notification_service

    err = _running["lastError"]
    async with AsyncSessionLocal() as db:
        await notification_service.notify_role(
            db, ["admin"], "eval_matrix_done" if not err else "system",
            f"评测矩阵{'完成' if not err else '运行失败'}（{_running['dims'] or 'retrieval'}）",
            body=err or "报告已落 reports/eval_matrix_*.md，可在系统管理-评测矩阵查看",
            link="/admin", tenant="default")


def run_status() -> dict:
    return {**_running, "pid": _running["pid"]}


def list_reports(limit: int = 30) -> list[dict]:
    d = _reports_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for name in os.listdir(d):
        if not _NAME_RE.match(name):
            continue
        p = os.path.join(d, name)
        st = os.stat(p)
        out.append({"name": name, "size": st.st_size,
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))})
    return sorted(out, key=lambda x: x["mtime"], reverse=True)[:limit]


def read_report(name: str) -> str:
    """读报告内容（文件名白名单校验防路径穿越）。"""
    from app.core.response import BizError

    if not _NAME_RE.match(name):
        raise BizError("非法报告名", 400)
    p = os.path.join(_reports_dir(), name)
    if not os.path.isfile(p):
        raise BizError("报告不存在", 404)
    with open(p, encoding="utf-8", errors="ignore") as f:
        return f.read(200_000)


async def eval_matrix_cron_loop(hours: float) -> None:
    """夜间定时评测（EVAL_MATRIX_CRON_HOURS>0 时由 main.py 挂载）。"""
    while True:
        await asyncio.sleep(hours * 3600)
        try:
            await run_matrix_async(["retrieval"])  # cron 默认只跑检索维（生成维耗 LLM 额度）
        except Exception:  # noqa: BLE001 — 重入拒绝等不终止 loop
            pass
