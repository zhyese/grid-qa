"""把 kb_seed/*.txt 按 docType 上传→解析→向量化进 :8001 知识库。

docType 由文件名前缀推断（故障案例/操作规程/运维手册/安全规程）。
用法: venv/Scripts/python.exe kb_seed/seed_kb.py
"""
import asyncio
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8001/api"
KB_DIR = Path(__file__).resolve().parent
PREFIX_TYPES = ("故障案例", "操作规程", "运维手册", "安全规程", "标准条文", "检修规程", "应急预案")


def doc_type_of(name: str) -> str:
    for p in PREFIX_TYPES:
        if name.startswith(p):
            return p
    return "运维手册"


async def main():
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(f"{BASE}/system/login",
                         json={"username": "admin", "password": "admin123"})
        r.raise_for_status()
        tok = r.json()["data"]["token"]
        H = {"Authorization": f"Bearer {tok}"}

        files = sorted(KB_DIR.glob("*.txt"))
        print(f"待入库 {len(files)} 个文件")
        groups: dict[str, list[Path]] = {}
        for f in files:
            groups.setdefault(doc_type_of(f.name), []).append(f)
        print("分组:", {k: len(v) for k, v in groups.items()})

        all_ids: list[str] = []
        for dt, paths in groups.items():
            mp_files = [("files", (p.name, p.read_bytes(), "text/plain")) for p in paths]
            r = await c.post(f"{BASE}/document/upload", headers=H,
                             files=mp_files, data={"docType": dt})
            d = (r.json() or {}).get("data", {})
            succ = d.get("successList", []) or []
            fail = d.get("failList", []) or []
            print(f"[上传 {dt}] 成功 {len(succ)} / 失败 {len(fail)}")
            for it in succ:
                did = it.get("id") or it.get("docId") or it.get("doc_id")
                if did:
                    all_ids.append(did)
                    print(f"   + {it.get('docName') or it.get('name')} → {did}")
            for it in fail:
                print(f"   x 失败 {it}")

        print(f"\n总 docIds: {len(all_ids)}")
        if not all_ids:
            print("无 docId，终止"); return

        # 解析（批量）
        r = await c.post(f"{BASE}/document/parse", headers=H, json={"docIds": all_ids})
        print("[解析]", (r.json() or {}).get("message"), "返回项数:", len(r.json().get("data") or []))

        # 向量化（逐个）
        for did in all_ids:
            r = await c.post(f"{BASE}/document/vector/generate", headers=H, json={"docId": did})
            rd = (r.json() or {}).get("data") or {}
            print(f"[向量化 {did}] {rd.get('vectorCount')} 向量 / 路由 {rd.get('embeddingRoute')}")

        # 统计
        r = await c.get(f"{BASE}/document/stats", headers=H)
        print("\n[stats]", r.json().get("data"))


if __name__ == "__main__":
    asyncio.run(main())
