"""第二批联动功能容器环境自测（通知中心/排行/转issue/记忆recall/评测矩阵/演练sweep）。"""
import io
import sys
import time

import httpx

BASE = "http://localhost:8001/api"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {str(detail)[:150]}")


def main():
    c = httpx.Client(base_url=BASE, timeout=60)
    r = c.post("/system/login", json={"username": "admin", "password": "admin123"})
    token = r.json().get("data", {}).get("token", "")
    check("登录", bool(token))
    auth = {"Authorization": f"Bearer {token}"}

    # ---- 通知中心（先上传文档供会签/批注用）----
    r = c.get("/notifications/unread-count", headers=auth)
    check("未读数端点", r.json().get("code") == 200)
    files = {"files": ("自测通知文档.txt", io.BytesIO("规程样例。".encode()), "text/plain")}
    c.post("/document/upload", headers=auth, files=files, data={"docType": "运维手册"})
    lr = c.get("/document/list", headers=auth,
               params={"keyword": "自测通知文档", "size": 5}).json().get("data") or {}
    items = lr.get("list") or lr.get("items") or []
    doc_id = (items[0].get("docId") or items[0].get("id")) if items else ""
    check("上传文档反查 docId", bool(doc_id), f"docId={doc_id}")
    r = c.post(f"/doc-collab/documents/{doc_id}/signoffs", headers=auth,
               json={"title": "自测通知会签", "signers": [{"signer": "admin", "role": "admin"}]})
    so = r.json().get("data") or {}
    check("发起会签(draft)", r.json().get("code") == 200 and so.get("status") == "draft")
    c.post(f"/doc-collab/signoffs/{so.get('id')}/submit", headers=auth)
    r = c.get("/notifications", headers=auth)
    notes = r.json().get("data") or []
    check("提交会签→待签通知落库", any("轮到你签批" in n.get("title", "") for n in notes),
          f"n={len(notes)}")
    c.post(f"/doc-collab/signoffs/{so.get('id')}/sign", headers=auth,
           json={"password": "admin123"})
    r = c.get("/notifications", headers=auth)
    notes = r.json().get("data") or []
    check("签批完成→发起人通知", any("会签完成" in n.get("title", "") for n in notes))
    r = c.post("/notifications/read-all", headers=auth)
    check("全部已读", r.json().get("code") == 200)
    r = c.get("/notifications/unread-count", headers=auth)
    check("未读清零", (r.json().get("data") or {}).get("count") == 0)

    # ---- 运维报告 → 完成通知 ----
    r = c.post("/ops-reports", headers=auth, json={"reportType": "shift", "days": 1,
                                                   "title": "自测·通知链路报告"})
    rep_id = (r.json().get("data") or {}).get("id", "")
    done = {}
    for _ in range(30):
        d = c.get(f"/ops-reports/{rep_id}", headers=auth).json().get("data") or {}
        if d.get("status") != "generating":
            done = d
            break
        time.sleep(5)
    check("报告生成完成", done.get("status") == "done")
    r = c.get("/notifications", headers=auth)
    check("报告完成通知", any("生成完成" in n.get("title", "") for n in (r.json().get("data") or [])))
    c.delete(f"/ops-reports/{rep_id}", headers=auth)

    # ---- 演练排行 ----
    r = c.get("/drills/leaderboard", headers=auth)
    check("排行榜端点", r.json().get("code") == 200 and isinstance(r.json().get("data"), list),
          f"n={len(r.json().get('data') or [])}")

    # ---- 批注 → 治理 issue（复用上文 doc_id）----
    r = c.post(f"/doc-collab/documents/{doc_id}/annotations", headers=auth,
               json={"chunkIdx": 0, "quote": "", "content": "自测批注转治理"})
    ann = r.json().get("data") or {}
    check("建批注", bool(ann.get("id")))
    r = c.post(f"/doc-collab/annotations/{ann.get('id')}/to-issue", headers=auth)
    d = r.json().get("data") or {}
    check("批注转治理 issue", r.json().get("code") == 200 and d.get("issueId"))
    r2 = c.post(f"/doc-collab/annotations/{ann.get('id')}/to-issue", headers=auth)
    check("幂等（再转 existing）", (r2.json().get("data") or {}).get("existing") is True)
    c.delete("/document/delete", headers=auth, params={"docId": doc_id})

    # ---- 记忆 recall ----
    r = c.post("/memory/recall", headers=auth, json={"query": "1号主变油温高"})
    check("记忆 recall（空记忆也 200）", r.json().get("code") == 200)
    r = c.post("/memory/recall", headers=auth, json={"query": "  "})
    check("空 query 被拒", r.json().get("code") == 400)

    # ---- eval_matrix API ----
    r = c.get("/system/eval-matrix/reports", headers=auth)
    reports = r.json().get("data") or []
    check("评测报告列表", r.json().get("code") == 200 and len(reports) >= 1, f"n={len(reports)}")
    if reports:
        name = reports[0]["name"]
        r = c.get(f"/system/eval-matrix/reports/{name}", headers=auth)
        check("读取报告内容", r.json().get("code") == 200 and
              len((r.json().get("data") or {}).get("content", "")) > 50)
    r = c.get("/system/eval-matrix/reports/..%2Fdocker-compose.yml", headers=auth)
    check("路径穿越被拒", r.json().get("code") in (400, 404) or r.status_code == 404, f"code={r.json().get('code')} http={r.status_code}")
    r = c.get("/system/eval-matrix/status", headers=auth)
    check("运行状态端点", r.json().get("code") == 200 and
          (r.json().get("data") or {}).get("active") is False)

    # ---- 问答链路无回归（摘要开关默认关）----
    r = c.post("/qa/answer", headers=auth, json={"query": "两票是什么"}, timeout=90)
    d = r.json().get("data") or {}
    check("问答主链路正常", bool(d.get("answer") or d.get("data", {}).get("answer")))

    fails = results.count(False)
    print(f"\n===== 第二批自测: {len(results) - fails}/{len(results)} 通过 =====")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
