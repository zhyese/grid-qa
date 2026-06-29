"""评测混合检索召回率 recall@K。

每个 query 期望命中含特定关键词的文档；统计 topK 中是否命中。
运行: python scripts/eval_retrieval.py   (需先跑 seed_demo.py 建库)
"""
import httpx

BASE = "http://127.0.0.1:8001"
TOPK = 5

# (query, 期望命中的文档名关键词)
QUERIES = [
    ("主变压器温度异常如何处置", "主变压器"),
    ("SF6断路器气体压力低怎么办", "SF6断路器"),
    ("隔离开关可以带负荷操作吗", "隔离开关"),
    ("输电线路主保护有哪些", "线路保护"),
    ("电流互感器二次侧为什么不能开路", "互感器"),
    ("配电变压器套管闪络怎么处理", "配电变压器"),
    ("变压器瓦斯保护动作怎么办", "主变压器"),
    ("电压互感器二次侧为什么不能短路", "互感器"),
]


def main():
    with httpx.Client(timeout=60) as c:
        token = c.post(
            f"{BASE}/api/system/login",
            json={"username": "admin", "password": "admin123"},
        ).json()["data"]["token"]
        H = {"Authorization": "Bearer " + token}
        hit = 0
        for q, expect in QUERIES:
            r = c.post(
                f"{BASE}/api/retrieval/mixed", headers=H,
                json={"query": q, "topK": TOPK},
            ).json()
            docs = [x["docName"] for x in r["data"]["retrievalList"]]
            ok = any(expect in d for d in docs)
            hit += ok
            mark = "✓" if ok else "✗"
            print(f'{mark} {q}  -> 期望"{expect}", 命中 {[d[:16] for d in docs]}')
        recall = hit / len(QUERIES)
        print(f"\n召回率 recall@{TOPK} = {hit}/{len(QUERIES)} = {recall * 100:.1f}%")


if __name__ == "__main__":
    main()
