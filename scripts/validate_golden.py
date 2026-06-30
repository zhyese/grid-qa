"""golden 问答集格式校验（CI 轻量门禁，不依赖后端服务）。

校验 backend/data/golden_qa.json：必为非空数组，每条含非空 query + 非空 expect[] + category。
退出码 1 = 校验失败（CI 红灯）。
"""
import json
import sys
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent.parent / "backend" / "data" / "golden_qa.json"


def main():
    errors = []
    if not GOLDEN.exists():
        print(f"✗ golden 文件不存在: {GOLDEN}")
        sys.exit(1)
    try:
        items = json.loads(GOLDEN.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"✗ JSON 解析失败: {e}")
        sys.exit(1)
    if not isinstance(items, list) or not items:
        print("✗ golden 必须是非空数组")
        sys.exit(1)
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            errors.append(f"#{i}: 非对象")
            continue
        q = it.get("query", "")
        expect = it.get("expect", [])
        cat = it.get("category", "")
        if not q or not isinstance(q, str):
            errors.append(f"#{i}: query 缺失/非字符串")
        if not expect or not isinstance(expect, list) or not all(isinstance(x, str) and x for x in expect):
            errors.append(f"#{i}: expect 缺失/非字符串数组")
        if not cat:
            errors.append(f"#{i}: category 缺失")
    if errors:
        print("✗ golden 校验失败:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    print(f"✓ golden 校验通过: {len(items)} 条问答集")
    sys.exit(0)


if __name__ == "__main__":
    main()
