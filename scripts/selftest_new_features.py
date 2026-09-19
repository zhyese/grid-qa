"""三功能容器环境全链路自测（对运行中的 http://localhost:8001 直打）。

退出码非 0 即自测失败。输出每步 PASS/FAIL 摘要。
"""
import io
import json
import sys
import time

import httpx

BASE = "http://localhost:8001/api"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'}  {name}  {detail[:160]}")


def main():
    c = httpx.Client(base_url=BASE, timeout=60)

    # ---- 登录 ----
    r = c.post("/system/login", json={"username": "admin", "password": "admin123"})
    token = r.json().get("data", {}).get("token", "")
    check("登录拿 token", r.status_code == 200 and bool(token))
    auth = {"Authorization": f"Bearer {token}"}

    # ---- 未授权冒烟 ----
    r = c.get("/ops-reports/meta")
    check("未登录访问被拒(401)", r.json().get("code") == 401)

    # ========== N7 运维报告 ==========
    r = c.get("/ops-reports/meta", headers=auth)
    meta = r.json().get("data") or []
    check("报告 meta（4 类型）", r.json().get("code") == 200 and len(meta) == 4,
          f"types={[m['type'] for m in meta]}")

    r = c.post("/ops-reports", headers=auth,
               json={"reportType": "shift", "days": 3, "title": "自测·交接班报告"})
    rep = r.json().get("data") or {}
    check("创建报告(generating)", r.json().get("code") == 200 and rep.get("status") == "generating",
          f"id={rep.get('id')}")
    rep_id = rep.get("id", "")

    final = {}
    for _ in range(30):  # 轮询至多 150s
        rr = c.get(f"/ops-reports/{rep_id}", headers=auth).json().get("data") or {}
        if rr.get("status") != "generating":
            final = rr
            break
        time.sleep(5)
    check("报告生成完成", final.get("status") == "done",
          f"status={final.get('status')} err={final.get('error','')[:80]}")
    check("报告含分节+摘要+LLM元信息",
          bool(final.get("sections")) and bool(final.get("summary")) and bool(final.get("llmMeta")),
          f"fallback={final.get('llmMeta',{}).get('fallback')} sections={len(final.get('sections') or [])}")
    check("数据快照五源齐", set((final.get("dataSnapshot") or {}).get("sources", {})) >=
          {"alarms", "tickets", "proactive", "handovers", "telemetry"})

    r = c.get(f"/ops-reports/{rep_id}/export/docx", headers=auth)
    check("Word 导出", r.status_code == 200 and r.content[:2] == b"PK",
          f"bytes={len(r.content)}")

    # ========== 文档批注 + 电子签批 ==========
    fname = "自测协作文档.txt"
    files = {"files": (fname, io.BytesIO("第1条 运行规程自测样例。\n第2条 安全措施。".encode()), "text/plain")}
    r = c.post("/document/upload", headers=auth, files=files, data={"docType": "运维手册"})
    ok_up = (r.json().get("data") or {}).get("successList") or []
    # upload 只回文件名；docId 从列表接口按名反查（同 Documents.vue 的取法）
    lr = c.get("/document/list", headers=auth, params={"keyword": fname, "size": 5}).json().get("data") or {}
    items = lr.get("list") or lr.get("items") or []
    doc_id = next((d.get("id") or d.get("docId") for d in items
                   if d.get("docName") == fname or d.get("name") == fname), "")
    check("上传自测文档", bool(ok_up) and bool(doc_id),
          f"uploaded={ok_up} docId={doc_id}")

    r = c.post(f"/doc-collab/documents/{doc_id}/annotations", headers=auth,
               json={"chunkIdx": 0, "quote": "第1条 运行规程", "content": "自测批注：建议补充引用编号"})
    ann = r.json().get("data") or {}
    check("创建批注", r.json().get("code") == 200 and ann.get("status") == "open", f"id={ann.get('id')}")
    ann_id = ann.get("id", "")

    r = c.post(f"/doc-collab/annotations/{ann_id}/reply", headers=auth,
               json={"content": "自测回复：已确认"})
    check("批注回复", len((r.json().get("data") or {}).get("replies", [])) == 1)
    c.post(f"/doc-collab/annotations/{ann_id}/resolve", headers=auth, json={"resolved": True})
    r = c.get(f"/doc-collab/documents/{doc_id}/annotation-stats", headers=auth)
    stats = r.json().get("data") or {}
    check("批注解决+统计", stats.get("total") == 1 and stats.get("resolved") == 1, str(stats))

    r = c.get("/doc-collab/signers", headers=auth)
    signers = r.json().get("data") or []
    check("签批人下拉(含admin)", any(s.get("signer") == "admin" for s in signers),
          f"n={len(signers)}")

    r = c.post(f"/doc-collab/documents/{doc_id}/signoffs", headers=auth,
               json={"title": "自测会签", "signers": [{"signer": "admin", "role": "admin"}]})
    so = r.json().get("data") or {}
    check("发起会签(draft)", r.json().get("code") == 200 and so.get("status") == "draft", f"id={so.get('id')}")
    so_id = so.get("id", "")

    r = c.post(f"/doc-collab/signoffs/{so_id}/submit", headers=auth)
    so = r.json().get("data") or {}
    check("提交会签(指纹固化)", so.get("status") == "pending" and bool(so.get("docFingerprint")))

    r = c.post(f"/doc-collab/signoffs/{so_id}/sign", headers=auth,
               json={"password": "wrong-pass", "comment": "x"})
    check("错误口令被拒(403)", r.json().get("code") == 403)

    r = c.post(f"/doc-collab/signoffs/{so_id}/sign", headers=auth,
               json={"password": "admin123", "comment": "自测签批通过"})
    so = r.json().get("data") or {}
    check("口令复核签批完成", so.get("status") == "signed", f"flow={so.get('flow')}")

    r = c.get(f"/doc-collab/signoffs/{so_id}/verify", headers=auth)
    v = r.json().get("data") or {}
    check("哈希链完整性校验", v.get("valid") is True, str(v))

    r = c.get(f"/doc-collab/signoffs/{so_id}", headers=auth)
    evs = (r.json().get("data") or {}).get("events") or []
    check("审计留痕完整", [e["action"] for e in evs] == ["create", "submit", "sign"],
          f"actions={[e['action'] for e in evs]}")

    # ========== N5 演练沙箱 ==========
    r = c.post("/drills/scenarios", headers=auth, json={
        "name": "自测·主变故障演练", "stationId": "110kV-demo", "faultDevice": "mt-1",
        "faultDesc": "自测：1号主变油温骤升",
        "propagation": [
            {"tOffset": 0, "device": "mt-1", "severity": "critical", "event": "主变油温骤升告警"},
            {"tOffset": 5, "device": "cb-1", "severity": "warning", "event": "冷却器异常"},
        ],
        "checklist": [
            {"action": "查看遥测", "keywords": ["遥测"]},
            {"action": "汇报调度", "keywords": ["调度"]},
        ],
    })
    sc = r.json().get("data") or {}
    check("创建演练剧本", r.json().get("code") == 200 and len(sc.get("checklist", [])) == 2,
          f"id={sc.get('id')}")
    sc_id = sc.get("id", "")

    r = c.post("/drills/runs", headers=auth, json={"scenarioId": sc_id})
    run = r.json().get("data") or {}
    check("开始演练", run.get("status") == "running" and run.get("elapsedSec", 99) <= 2,
          f"elapsed={run.get('elapsedSec')}s due={len(run.get('dueEvents', []))}")
    run_id = run.get("id", "")

    time.sleep(6)  # 等 t+5s 事件到期
    r = c.get(f"/drills/runs/{run_id}", headers=auth)
    run = r.json().get("data") or {}
    check("时间轴推演(第二事件到期)", len(run.get("dueEvents", [])) == 2,
          f"due={len(run.get('dueEvents', []))}/{run.get('totalEvents')}")

    c.post(f"/drills/runs/{run_id}/actions", headers=auth, json={"action": "查看1号主变遥测趋势"})
    c.post(f"/drills/runs/{run_id}/actions", headers=auth, json={"action": "汇报调度并通知检修"})
    r = c.post(f"/drills/runs/{run_id}/finish", headers=auth)
    fin = r.json().get("data") or {}
    score = fin.get("score") or {}
    check("完成评分(全覆盖·优秀)", fin.get("status") == "finished" and score.get("coverage") == 1.0
          and score.get("grade") == "优秀", f"score={json.dumps(score, ensure_ascii=False)[:120]}")
    check("AI复盘生成", len(fin.get("evaluationMd", "")) > 20,
          f"len={len(fin.get('evaluationMd',''))} 模板={'LLM 不可用' in fin.get('evaluationMd','')}")

    r = c.get("/drills/stats", headers=auth)
    ds = r.json().get("data") or {}
    check("演练统计", ds.get("finished", 0) >= 1, str(ds))

    # 清理自测数据（不影响其他数据）：删报告/剧本；文档与批注留作演示数据可接受？一并删干净
    c.delete(f"/ops-reports/{rep_id}", headers=auth)
    c.delete(f"/drills/scenarios/{sc_id}", headers=auth)
    c.delete(f"/document/delete", headers=auth, params={"docId": doc_id})
    check("清理自测数据", True)

    fails = [n for n, ok, _ in results if not ok]
    print(f"\n===== 自测汇总: {len(results) - len(fails)}/{len(results)} 通过 =====")
    if fails:
        print("失败项:", fails)
        sys.exit(1)


if __name__ == "__main__":
    main()
