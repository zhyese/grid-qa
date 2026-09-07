"""工作流 DAG 执行引擎（可视化工作流编排 · BRD §5.3.1）。

graph JSON 格式：
  nodes: [{id, type, key, label, params, x, y}]
  edges: [{id, source, target, branch?}]   # branch 仅 condition 出边："true"/"false"

节点 7 种：
  input     入参（query + 可选 vars），整图唯一起点
  retrieval 混合检索（retrieval_service.mixed_search）→ context 文本 + chunks
  kg        设备因果链（kg_service.graph_context）→ context 文本 + rows
  llm       prompt 模板（providers.factory get_llm_provider().chat）→ text
  agent     ReAct Agent（persona_store.get_persona + agent_runtime.run_agent）→ answer/steps
  condition contains/not_contains/is_empty 判定，出边必须标 branch
  output    终点：取上游输出的主字段为最终答案（或 params.template 插值）

执行语义：
  - 变量插值 {{nodeKey.field}}，未知名替换为空串并记 warning
  - Kahn 拓扑序执行（保证所有上游先于下游）；condition 只激活匹配 branch 的出边，
    未激活节点标 skipped
  - 某节点失败：仅其出边不激活（其余父边仍可激活下游）；无任何 output 执行到 → run failed
  - 单节点超时 120s、总预算 240s；异常走 degraded 上报，不裸崩
"""
import asyncio
import json
import re
import time
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.obs import degraded
from app.core.safety import guard_query

NODE_TYPES = ("input", "retrieval", "kg", "llm", "agent", "condition", "output")

NODE_TIMEOUT_S = 120      # 单节点上限（agent 多轮工具调用可能较慢）
RUN_BUDGET_S = 240        # 整次 run 总预算
_STORE_MAX = 4000         # node_states 里单字段落库截断长度

_TMPL = re.compile(r"\{\{\s*([A-Za-z0-9_\-]+)\.([A-Za-z0-9_\-]+)\s*\}\}")

# 各节点输出的主字段（condition 判定 / output 兜底 / 插值字符串化统一走它）
_PRIMARY_FIELD = {"input": "query", "retrieval": "context", "kg": "context",
                  "llm": "text", "agent": "answer", "condition": "value", "output": "text"}


def _var_name(n: dict) -> str:
    """节点插值变量名：input 节点固定为保留名 "input"（{{input.query}} 语义，
    引擎默认模板自身就这么写），其余节点取 key 或 id（key 与 input 撞名由 validate 拦）。"""
    return "input" if n.get("type") == "input" else (n.get("key") or n["id"])


# ---------- 校验（保存时调用；返回错误串列表，空=通过） ----------

def validate_graph(graph: dict) -> list[str]:
    nodes = (graph or {}).get("nodes") or []
    edges = (graph or {}).get("edges") or []
    errs: list[str] = []
    if not nodes:
        return ["图中没有节点"]
    ids = [n.get("id") for n in nodes]
    if any(not i for i in ids):
        return ["存在缺 id 的节点"]
    if len(set(ids)) != len(ids):
        return ["节点 id 重复"]
    keys = [_var_name(n) for n in nodes]
    if len(set(keys)) != len(keys):
        errs.append("节点变量名(key)重复")
    by_id = {n["id"]: n for n in nodes}
    for n in nodes:
        if n.get("type") not in NODE_TYPES:
            errs.append(f"节点 {n['id']} 类型非法：{n.get('type')}")
            continue
        p = n.get("params") or {}
        if n["type"] == "llm" and not str(p.get("prompt") or "").strip():
            errs.append(f"LLM 节点 {n['id']} 缺 prompt")
        if n["type"] == "agent" and not str(p.get("persona") or "").strip():
            errs.append(f"Agent 节点 {n['id']} 缺 persona")
        if n["type"] == "condition" and (p.get("op") or "contains") not in (
                "contains", "not_contains", "is_empty"):
            errs.append(f"条件节点 {n['id']} op 非法")
    input_nodes = [n for n in nodes if n["type"] == "input"]
    if len(input_nodes) != 1:
        errs.append(f"必须有且仅有 1 个输入节点，当前 {len(input_nodes)}")
    if not any(n["type"] == "output" for n in nodes):
        errs.append("缺少输出节点")
    seen_edges: set[tuple[str, str]] = set()
    adj: dict[str, list[str]] = {}
    indeg = {i: 0 for i in ids}
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s not in by_id or t not in by_id:
            errs.append(f"边引用不存在的节点：{s}→{t}")
            continue
        if s == t:
            errs.append(f"自环边：{s}→{t}")
            continue
        if (s, t) in seen_edges:
            errs.append(f"重复边：{s}→{t}")
            continue
        seen_edges.add((s, t))
        src_type = by_id[s]["type"]
        if src_type == "condition":
            if e.get("branch") not in ("true", "false"):
                errs.append(f"条件节点 {s} 的出边必须标注 branch(true/false)")
        elif e.get("branch"):
            errs.append(f"非条件节点 {s} 的出边不应带 branch")
        adj.setdefault(s, []).append(t)
        indeg[t] += 1
    # 可达性（从唯一 input 出发）：不可达 = 环成员或孤儿
    if len(input_nodes) == 1:
        queue, reachable = [input_nodes[0]["id"]], {input_nodes[0]["id"]}
        while queue:
            cur = queue.pop(0)
            for t in adj.get(cur, []):
                if t not in reachable:
                    reachable.add(t)
                    queue.append(t)
        unreachable = [i for i in ids if i not in reachable]
        if unreachable:
            errs.append(f"存在环或不可达节点：{', '.join(unreachable[:5])}")
    # 环检测（可达性抓不到环：环成员从 input 可达）——Kahn 残留即环成员
    order = topo_order(nodes, edges)
    if len(order) != len(ids):
        cyclic = [i for i in ids if i not in set(order)]
        errs.append(f"存在环或不可达节点（环成员）：{', '.join(cyclic[:5])}")
    return errs


def topo_order(nodes: list[dict], edges: list[dict]) -> list[str]:
    """Kahn 拓扑序（所有上游先于下游；图须无环，环成员不返回——由 validate 前置保证）。"""
    adj: dict[str, list[str]] = {}
    indeg: dict[str, int] = {n["id"]: 0 for n in nodes}
    for e in edges:
        if e.get("source") not in indeg or e.get("target") not in indeg:
            continue  # 悬挂边（validate 已单独报错）不参与排序，防 KeyError
        adj.setdefault(e["source"], []).append(e["target"])
        indeg[e["target"]] += 1
    queue = sorted([i for i, d in indeg.items() if d == 0])
    order: list[str] = []
    while queue:
        cur = queue.pop(0)
        order.append(cur)
        for t in adj.get(cur, []):
            indeg[t] -= 1
            if indeg[t] == 0:
                queue.append(t)
    return order


# ---------- 变量插值 ----------

def interpolate(template: str, ctx: dict[str, dict]) -> tuple[str, list[str]]:
    """{{key.field}} 插值。返回 (结果, warnings)。未知引用 → 空串 + warning。"""
    warns: list[str] = []

    def _sub(m: re.Match) -> str:
        key, field = m.group(1), m.group(2)
        rec = ctx.get(key)
        if not rec or field not in rec:
            warns.append(f"变量未找到：{m.group(0)}")
            return ""
        v = rec[field]
        if v is None:
            return ""
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

    return _TMPL.sub(_sub, template or ""), warns


def _stringify(record: dict | None) -> str:
    """取节点输出的主字段字符串化（condition 判定 / output 兜底）。"""
    if not record:
        return ""
    for f in ("text", "answer", "context", "query", "value"):
        v = record.get(f)
        if v:
            return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return ""


def _capped(record: dict) -> dict:
    """node_states 落库前的字段截断（防 chunks/steps 撑爆 JSON 列）。"""
    out = {}
    for k, v in record.items():
        if isinstance(v, str) and len(v) > _STORE_MAX:
            out[k] = v[:_STORE_MAX] + f"…(截断,共{len(v)})"
        elif isinstance(v, list) and len(json.dumps(v, ensure_ascii=False)) > _STORE_MAX:
            out[k] = json.dumps(v, ensure_ascii=False)[:_STORE_MAX]
        else:
            out[k] = v
    return out


# ---------- 节点执行器（签名统一；异常上抛由引擎兜底） ----------
# 均返回 dict（节点输出记录）；upstream_key = 首条入边 source 的变量名

async def _exec_input(db: AsyncSession, params: dict, run_input: dict, ctx, user, upstream_key) -> dict:
    q = str(run_input.get("query") or "")
    guard_query(q)  # 入站 prompt injection 告警（与 qa 主链路同款，不阻断）
    rec = {"query": q}
    for k, v in (params.get("vars") or {}).items():
        rec[str(k)] = v
    return rec


async def _exec_retrieval(db: AsyncSession, params: dict, run_input: dict, ctx, user, upstream_key) -> dict:
    from app.services import retrieval_service
    query, warns = interpolate(str(params.get("query") or "{{input.query}}"), ctx)
    topk = int(params.get("topk") or 5)
    kw: dict[str, Any] = {"tenant": user.get("tenant"), "user_dept": user.get("dept"),
                          "user_role": user.get("role")}
    if params.get("model_type"):
        kw["model_type"] = params["model_type"]
    chunks = await retrieval_service.mixed_search(db, query, topk, **kw)
    context = "\n".join(f"[{i}] {(c.get('docName') or '')}: {(c.get('chunk') or '')[:300]}"
                        for i, c in enumerate(chunks, 1))
    return {"query": query, "count": len(chunks), "chunks": chunks,
            "context": context, "warnings": warns or None}


async def _exec_kg(db: AsyncSession, params: dict, run_input: dict, ctx, user, upstream_key) -> dict:
    from app.services import kg_service
    entity, warns = interpolate(str(params.get("entity") or ""), ctx)
    k = int(params.get("topk") or 8)
    rows = await kg_service.graph_context(entity, k, db=db, tenant=user.get("tenant"))
    return {"entity": entity, "rows": rows or [],
            "context": "\n".join(rows) if rows else "", "warnings": warns or None}


async def _exec_llm(db: AsyncSession, params: dict, run_input: dict, ctx, user, upstream_key) -> dict:
    from app.providers.factory import get_llm_provider
    prompt, warns = interpolate(str(params.get("prompt") or ""), ctx)
    system, w2 = interpolate(str(params.get("system") or ""), ctx)
    provider = get_llm_provider(params.get("model_type") or None)
    messages = [{"role": "system", "content": system or "你是电网运维助手，基于已知信息回答。"},
                {"role": "user", "content": prompt}]
    text = await provider.chat(messages,
                               temperature=float(params.get("temperature") or 0.2),
                               max_tokens=int(params.get("max_tokens") or 1500))
    return {"text": text, "warnings": (warns + w2) or None}


async def _exec_agent(db: AsyncSession, params: dict, run_input: dict, ctx, user, upstream_key) -> dict:
    from app.services.agent_runtime import run_agent
    from app.services.persona_store import get_persona
    persona = await get_persona(str(params.get("persona") or ""))
    if persona is None:
        raise ValueError(f"persona 不存在：{params.get('persona')}")
    message, warns = interpolate(str(params.get("message") or "{{input.query}}"), ctx)
    result = await run_agent(
        db, persona, message,
        model_type=params.get("model_type") or None,
        ctx={"username": user.get("username") or "", "tenant": user.get("tenant") or "default",
             "role": user.get("role") or ""})
    answer = result.answer
    return {"answer": answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False),
            "answer_raw": None if isinstance(answer, str) else answer,
            "iterations": result.iterations, "degraded": result.degraded,
            "tools_used": result.tools_used, "warnings": warns or None}


async def _exec_condition(params: dict, ctx, user, upstream_value: str) -> dict:
    op = params.get("op") or "contains"
    keyword, warns = interpolate(str(params.get("keyword") or ""), ctx)
    target = upstream_value  # 默认判对象：唯一上游的主字段
    if op == "is_empty":
        branch = "true" if not target.strip() else "false"
    elif op == "not_contains":
        branch = "false" if keyword and keyword in target else "true"
    else:  # contains
        branch = "true" if keyword and keyword in target else "false"
    return {"op": op, "keyword": keyword, "value": target, "branch": branch,
            "warnings": warns or None}


async def _exec_output(params: dict, ctx, user, upstream_value: str) -> dict:
    if str(params.get("template") or "").strip():
        text, warns = interpolate(params["template"], ctx)
    else:
        text, warns = upstream_value, []
    return {"text": text, "warnings": warns or None}


# 主字段判定的执行器只需要 upstream_value，不需要 db
_COND_EXECUTORS: dict[str, Callable[..., Awaitable[dict]]] = {
    "condition": _exec_condition,
    "output": _exec_output,
}
_DB_EXECUTORS: dict[str, Callable[..., Awaitable[dict]]] = {
    "input": _exec_input,
    "retrieval": _exec_retrieval,
    "kg": _exec_kg,
    "llm": _exec_llm,
    "agent": _exec_agent,
}


# ---------- 图执行 ----------

async def execute_graph(db: AsyncSession, graph: dict, run_input: dict,
                        user: dict) -> dict:
    """执行一张已校验的图。返回 {status, output, node_states, error, warnings}。"""
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    by_id = {n["id"]: n for n in nodes}
    incoming: dict[str, list[dict]] = {}
    outgoing: dict[str, list[dict]] = {}
    for e in edges:
        incoming.setdefault(e["target"], []).append(e)
        outgoing.setdefault(e["source"], []).append(e)

    order = topo_order(nodes, edges)
    if len(order) != len(nodes):  # 防御：validate 漏掉的环
        return {"status": "failed", "output": "", "node_states": [],
                "error": "图中存在环（引擎防御拦截）", "warnings": []}

    input_id = next(n["id"] for n in nodes if n["type"] == "input")
    ctx: dict[str, dict] = {}
    states: dict[str, dict] = {n["id"]: {"nodeId": n["id"], "type": n["type"],
                                         "key": _var_name(n),
                                         "status": "pending", "output": None,
                                         "error": "", "ms": 0}
                               for n in nodes}
    active = {input_id}
    warnings: list[str] = []
    output_reached = False
    run_error = ""
    t0 = time.perf_counter()

    for nid in order:
        node = by_id[nid]
        if nid not in active:
            states[nid]["status"] = "skipped"
            continue
        if time.perf_counter() - t0 > RUN_BUDGET_S:
            states[nid]["status"] = "error"
            states[nid]["error"] = "超出整次执行总预算"
            run_error = run_error or f"节点 {nid} 执行超出总预算 {RUN_BUDGET_S}s"
            continue

        first_in = (incoming.get(nid) or [{}])[0].get("source")
        upstream_key = _var_name(by_id[first_in]) if first_in else None
        upstream_value = _stringify(ctx.get(upstream_key)) if upstream_key else ""
        params = node.get("params") or {}
        key = _var_name(node)
        ntype = node["type"]
        states[nid]["status"] = "running"
        nt0 = time.perf_counter()
        try:
            if ntype in _COND_EXECUTORS:
                rec = await asyncio.wait_for(
                    _COND_EXECUTORS[ntype](params, ctx, user, upstream_value),
                    NODE_TIMEOUT_S)
            else:
                rec = await asyncio.wait_for(
                    _DB_EXECUTORS[ntype](db, params, run_input, ctx, user, upstream_key),
                    NODE_TIMEOUT_S)
            rec.pop("answer_raw", None)  # json answer 不重复落库
            states[nid].update(status="done", output=_capped(rec),
                               ms=int((time.perf_counter() - nt0) * 1000))
            ctx[key] = rec
            warnings += [f"{key}: {w}" for w in (rec.get("warnings") or [])]
            if ntype == "output":
                output_reached = True
            for e in outgoing.get(nid, []):
                if ntype == "condition" and e.get("branch") != rec.get("branch"):
                    continue  # 分支未激活
                active.add(e["target"])
        except Exception as e:  # noqa: BLE001 — 节点失败不中断其余分支
            degraded(f"workflow_node_{ntype}", e)
            states[nid].update(status="error", error=str(e)[:500],
                               ms=int((time.perf_counter() - nt0) * 1000))
            run_error = run_error or f"节点 {key}({ntype}) 失败：{e}"

    output_text = ""
    for nid in order:  # 多 output 取最后执行成功者（拓扑序稳定）
        if states[nid]["type"] == "output" and states[nid]["status"] == "done":
            output_text = (states[nid]["output"] or {}).get("text") or ""
    return {"status": "done" if output_reached else "failed",
            "output": output_text,
            "node_states": [states[n["id"]] for n in nodes],
            "error": run_error, "warnings": warnings}


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def execute_run(run_id: str) -> None:
    """后台任务入口：自建会话加载 run + workflow 快照执行并回写（前端轮询 GET run）。"""
    from app.db.session import AsyncSessionLocal
    from app.models.workflow import Workflow, WorkflowRun

    async with AsyncSessionLocal() as db:
        run = (await db.execute(
            select(WorkflowRun).where(WorkflowRun.id == run_id))).scalar_one_or_none()
        if run is None:
            return
        wf = (await db.execute(
            select(Workflow).where(Workflow.id == run.workflow_id))).scalar_one_or_none()
        if wf is None:  # 运行期间工作流被删：优雅失败，不留僵尸 running 行
            run.status = "failed"
            run.error = "工作流已删除，无法执行"
            run.finished_at = utcnow()
            await db.commit()
            return
        user = {"username": run.created_by, "tenant": run.tenant, "role": "", "dept": ""}
        try:
            result = await execute_graph(db, wf.graph or {}, run.input or {}, user)
        except Exception as e:  # noqa: BLE001 — 后台任务最后一道兜底
            degraded("workflow_execute_run", e)
            result = {"status": "failed", "output": "", "node_states": [],
                      "error": f"引擎异常：{e}", "warnings": []}
        run.status = result["status"]
        run.output = result["output"] or ""
        run.node_states = result["node_states"]
        run.error = result["error"] or ""
        run.finished_at = utcnow()
        run.duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000) \
            if run.started_at else 0
        try:
            from app.core import metrics
            metrics.WORKFLOW_RUNS.labels(result["status"]).inc()
        except Exception:  # noqa: BLE001
            pass
        await db.commit()
