"""golden 问答集格式校验（CI 轻量门禁，不依赖后端服务）。

校验 backend/data/golden_qa.json：必为非空数组，每条含非空 query + expect[] + category。
- source=feedback 的条目允许空 expect（用户反馈回流的 bad case 待人工标注）
- relevant_docs 为可选字段（分级相关性标注，出现时 key 必须为字符串、value 为整数 1-3）
退出码 1 = 校验失败（CI 红灯）。
"""
import json
import sys
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent.parent / "backend" / "data" / "golden_qa.json"


def main():
    errors = []
    if not GOLDEN.exists():
        print(f"[FAIL] golden 文件不存在: {GOLDEN}")
        sys.exit(1)
    try:
        items = json.loads(GOLDEN.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[FAIL] JSON 解析失败: {e}")
        sys.exit(1)
    if not isinstance(items, list) or not items:
        print("[FAIL] golden 必须是非空数组")
        sys.exit(1)
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            errors.append(f"#{i}: 非对象")
            continue
        q = it.get("query", "")
        expect = it.get("expect", [])
        cat = it.get("category", "")
        src = it.get("source", "")
        is_feedback = src == "feedback"
        if not q or not isinstance(q, str):
            errors.append(f"#{i}: query 缺失/非字符串")
        if not isinstance(expect, list) or any(not isinstance(x, str) or not x for x in expect):
            errors.append(f"#{i}: expect 格式错（需字符串数组，元素非空）")
        if not expect and not is_feedback:
            errors.append(f"#{i}: expect 为空且非 feedback 来源（feedback 条目允许空待标注）")
        if not cat:
            errors.append(f"#{i}: category 缺失")
        # 可选字段 relevant_docs 校验
        rd = it.get("relevant_docs")
        if rd is not None:
            if not isinstance(rd, dict):
                errors.append(f"#{i}: relevant_docs 必须为对象")
            elif not all(isinstance(k, str) and k and isinstance(v, int) and 1 <= v <= 3 for k, v in rd.items()):
                errors.append(f"#{i}: relevant_docs 格式错（value 需为整数 1~3）")
    if errors:
        print("[FAIL] golden 校验失败:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    fb_count = sum(1 for it in items if it.get("source") == "feedback")
    rd_count = sum(1 for it in items if it.get("relevant_docs"))
    print(f"[OK] golden 校验通过: {len(items)} 条（feedback 回流传标注 {fb_count}，分级标注 {rd_count}）")
    sys.exit(0)


if __name__ == "__main__":
    main()
