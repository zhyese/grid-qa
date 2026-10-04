"""F1 数据资产与标注工作台单测（sqlite in-memory；发布写到临时文件，绝不碰真 golden_qa.json）。"""
import json

import pytest

from app.core.response import BizError
from app.services import golden_set_service as golden

TENANT = "default"


@pytest.fixture
def tmp_golden(tmp_path, monkeypatch):
    """把发布目标改到临时文件，避免污染仓库里的 golden_qa.json。"""
    p = tmp_path / "golden_qa.json"
    monkeypatch.setattr(golden, "GOLDEN_PATH", p)
    return p


def _case(query="主变压器温度异常如何处置", expect=None, category="变电", **kw):
    return {
        "query": query,
        "expect": expect if expect is not None else ["主变压器"],
        "category": category,
        **kw,
    }


# ---------- 校验 ----------

def test_validate_case_ok():
    assert golden.validate_case(_case()) == []


def test_validate_case_missing_query_and_category():
    errs = golden.validate_case({"query": "", "expect": ["a"], "category": ""})
    assert any("query" in e for e in errs)
    assert any("category" in e for e in errs)


def test_validate_case_empty_expect_allowed_only_for_feedback():
    assert any("expect 为空" in e for e in
               golden.validate_case({"query": "q", "expect": [], "category": "变电"}))
    assert golden.validate_case(
        {"query": "q", "expect": [], "category": "变电", "source": "feedback"}) == []


def test_validate_case_relevant_docs_range():
    bad = {"query": "q", "expect": ["a"], "category": "c", "relevant_docs": {"d": 9}}
    assert any("1~3" in e for e in golden.validate_case(bad))


# ---------- CRUD ----------

@pytest.mark.asyncio
async def test_upsert_and_list_and_stats(test_db):
    await golden.upsert_case(test_db, TENANT, "alice", _case(labelStatus="verified"))
    await golden.upsert_case(test_db, TENANT, "alice",
                             _case(query="隔离开关可以带负荷操作吗", labelStatus="pending"))

    listed = await golden.list_cases(test_db, TENANT)
    assert listed["total"] == 2
    assert {c["query"] for c in listed["list"]} == {
        "主变压器温度异常如何处置", "隔离开关可以带负荷操作吗"}

    st = await golden.stats(test_db, TENANT)
    assert st["total"] == 2
    assert st["publishable"] == 1
    assert st["byStatus"]["verified"] == 1
    assert st["byCategory"]["变电"] == 2


@pytest.mark.asyncio
async def test_upsert_is_idempotent_by_query(test_db):
    await golden.upsert_case(test_db, TENANT, "alice", _case(note="v1"))
    await golden.upsert_case(test_db, TENANT, "bob", _case(note="v2", difficulty="hard"))

    listed = await golden.list_cases(test_db, TENANT)
    assert listed["total"] == 1                     # 同 query 更新而非新增
    assert listed["list"][0]["note"] == "v2"
    assert listed["list"][0]["difficulty"] == "hard"
    assert listed["list"][0]["updatedBy"] == "bob"


@pytest.mark.asyncio
async def test_upsert_rejects_bad_enum(test_db):
    with pytest.raises(BizError):
        await golden.upsert_case(test_db, TENANT, "a", _case(labelStatus="bogus"))
    with pytest.raises(BizError):
        await golden.upsert_case(test_db, TENANT, "a", _case(difficulty="insane"))
    with pytest.raises(BizError):
        await golden.upsert_case(test_db, TENANT, "a", _case(query="   "))


def test_validate_case_rejects_overlong_query():
    """query 必须能放进 VARCHAR(512)：MySQL 不允许 TEXT 列建唯一约束（1170），
    故模型用 VARCHAR(512) + 应用层长度校验（单测 sqlite 不会暴露该问题）。"""
    assert golden.validate_case(_case(query="x" * 512)) == []
    errs = golden.validate_case(_case(query="x" * 513))
    assert any("512" in e for e in errs)


@pytest.mark.asyncio
async def test_upsert_rejects_overlong_query(test_db):
    with pytest.raises(BizError):
        await golden.upsert_case(test_db, TENANT, "a", _case(query="x" * 513))


def test_golden_case_query_column_is_varchar():
    """回归锁：query 一旦退回 Text，真实 MySQL 建表会因唯一约束直接失败。"""
    from app.models.golden_set import GoldenCase, GoldenSetVersion
    from sqlalchemy import String, Text
    assert isinstance(GoldenCase.__table__.c.query.type, String)
    # 快照须能容纳数百条用例（TEXT 上限 64KB 会截断）
    assert isinstance(GoldenSetVersion.__table__.c.snapshot_json.type, Text)


@pytest.mark.asyncio
async def test_delete_is_soft_and_filters_list(test_db):
    r = await golden.upsert_case(test_db, TENANT, "a", _case())
    await golden.delete_case(test_db, TENANT, r["case"]["id"])
    assert (await golden.list_cases(test_db, TENANT))["total"] == 0
    # 再次 upsert 同 query → 复活（软删不挡回填）
    await golden.upsert_case(test_db, TENANT, "a", _case())
    assert (await golden.list_cases(test_db, TENANT))["total"] == 1


@pytest.mark.asyncio
async def test_filters(test_db):
    await golden.upsert_case(test_db, TENANT, "a", _case(category="变电", difficulty="hard"))
    await golden.upsert_case(test_db, TENANT, "a",
                             _case(query="输电线路主保护有哪些", category="输电", difficulty="easy"))

    assert (await golden.list_cases(test_db, TENANT, category="输电"))["total"] == 1
    assert (await golden.list_cases(test_db, TENANT, difficulty="hard"))["total"] == 1
    assert (await golden.list_cases(test_db, TENANT, keyword="保护"))["total"] == 1


@pytest.mark.asyncio
async def test_tenant_isolation(test_db):
    await golden.upsert_case(test_db, "t1", "a", _case())
    assert (await golden.list_cases(test_db, "t2"))["total"] == 0


# ---------- 批量导入 ----------

def test_norm_row_chinese_headers():
    row = {"问题": "主变油温高怎么办", "期望关键词": "油温|主变", "分类": "变电",
           "难度": "medium", "设备标签": "1号主变,2号主变", "标注状态": "verified"}
    out = golden._norm_row(row)
    assert out["query"] == "主变油温高怎么办"
    assert out["expect"] == ["油温", "主变"]
    assert out["category"] == "变电"
    assert out["difficulty"] == "medium"
    assert out["deviceTags"] == ["1号主变", "2号主变"]
    assert out["labelStatus"] == "verified"


def test_norm_row_english_headers_and_defaults():
    out = golden._norm_row({"query": "q", "expect": "a;b", "category": "c"})
    assert out["expect"] == ["a", "b"]
    assert out["labelStatus"] == "labeled"      # 导入默认视为已标注
    assert out["source"] == "import"


def test_parse_upload_csv():
    content = "query,expect,category\n主变油温高,油温,变电\n".encode("utf-8")
    rows = golden.parse_upload("cases.csv", content)
    assert rows and rows[0]["query"] == "主变油温高"


def test_parse_upload_rejects_unknown_format():
    with pytest.raises(BizError):
        golden.parse_upload("cases.pdf", b"x")


@pytest.mark.asyncio
async def test_batch_import_reports_failures(test_db):
    rows = [
        {"问题": "主变油温高怎么办", "期望关键词": "油温", "分类": "变电"},
        {"问题": "", "期望关键词": "x", "分类": "变电"},          # 缺 query → 失败
    ]
    res = await golden.batch_import(test_db, TENANT, "a", rows)
    assert res["imported"] == 1
    assert res["failed"] == 1
    assert "缺少 query" in res["errors"][0]


# ---------- 发布 / 版本 / 哈希链 ----------

@pytest.mark.asyncio
async def test_publish_writes_file_and_version(tmp_golden, test_db):
    await golden.upsert_case(test_db, TENANT, "a", _case(labelStatus="verified"))
    await golden.upsert_case(test_db, TENANT, "a",
                             _case(query="待标注的问题", labelStatus="pending"))

    res = await golden.publish(test_db, TENANT, "a", note="first")
    assert res["version"] == 1
    assert res["published"] == 1                 # pending 不发布
    items = json.loads(tmp_golden.read_text(encoding="utf-8"))
    assert len(items) == 1
    assert items[0]["query"] == "主变压器温度异常如何处置"
    assert "relevant_docs" not in items[0] or items[0]["relevant_docs"] == {}


@pytest.mark.asyncio
async def test_publish_builds_hash_chain(tmp_golden, test_db):
    await golden.upsert_case(test_db, TENANT, "a", _case(labelStatus="verified"))
    v1 = await golden.publish(test_db, TENANT, "a")

    await golden.upsert_case(test_db, TENANT, "a",
                             _case(query="新增问题", category="输电", labelStatus="labeled"))
    v2 = await golden.publish(test_db, TENANT, "a")

    assert v2["version"] == 2
    assert v2["prevHash"] == v1["contentHash"]        # 链式衔接

    versions = (await golden.list_versions(test_db, TENANT))["versions"]
    asc = sorted(versions, key=lambda x: x["version"])
    assert golden.verify_hash_chain(asc)["ok"] is True


@pytest.mark.asyncio
async def test_publish_without_publishable_cases_raises(tmp_golden, test_db):
    await golden.upsert_case(test_db, TENANT, "a", _case(labelStatus="pending"))
    with pytest.raises(BizError):
        await golden.publish(test_db, TENANT, "a")


@pytest.mark.asyncio
async def test_publish_skips_invalid_cases(tmp_golden, test_db):
    """expect 为空且非 feedback 的条目不允许进 golden（validate_golden 门禁口径）。"""
    await golden.upsert_case(test_db, TENANT, "a",
                             _case(expect=[], category="变电", labelStatus="verified"))
    await golden.upsert_case(test_db, TENANT, "a",
                             _case(query="合规问题", category="变电", labelStatus="verified"))
    res = await golden.publish(test_db, TENANT, "a")
    assert res["published"] == 1
    assert res["invalid"] == 1


@pytest.mark.asyncio
async def test_diff_versions(tmp_golden, test_db):
    await golden.upsert_case(test_db, TENANT, "a", _case(labelStatus="verified"))
    await golden.publish(test_db, TENANT, "a")

    await golden.upsert_case(test_db, TENANT, "a", _case(
        query="新增问题", category="输电", labelStatus="verified"))
    await golden.publish(test_db, TENANT, "a")

    d = await golden.diff_versions(test_db, TENANT, 1, 2)
    assert d["v1Count"] == 1 and d["v2Count"] == 2
    assert d["added"] == ["新增问题"]
    assert d["removed"] == []


@pytest.mark.asyncio
async def test_diff_versions_missing_raises(tmp_golden, test_db):
    with pytest.raises(BizError):
        await golden.diff_versions(test_db, TENANT, 1, 2)


# ---------- 质量事件回流 ----------

@pytest.mark.asyncio
async def test_ingest_quality_events_creates_pending_drafts(test_db):
    from app.models.quality_event import QualityEvent

    # 直接写 test_db（emit 走独立真库 session，单测不可用）
    for src, typ, q in [
        ("feedback", "dislike", "主变油温高怎么办"),
        ("online_eval", "low_faith", "主变油温高怎么办"),
        ("online_eval", "low_faith", "另一条坏case"),
    ]:
        test_db.add(QualityEvent(source=src, type=typ, payload={"query": q}, tenant=TENANT))
    await test_db.commit()

    res = await golden.ingest_quality_events(test_db, TENANT, "a")
    assert res["created"] == 2                 # 同 query 去重
    assert res["skipped"] >= 1

    listed = await golden.list_cases(test_db, TENANT, label_status="pending")
    assert listed["total"] == 2
    assert all(c["source"] == "quality_event" for c in listed["list"])

    # 二次拉取不重复建
    again = await golden.ingest_quality_events(test_db, TENANT, "a")
    assert again["created"] == 0


# ---------- Laya 导出 ----------

@pytest.mark.asyncio
async def test_export_laya_shape(test_db):
    await golden.upsert_case(test_db, TENANT, "a", _case(
        deviceTags=["1号主变"], expectedSources=["主变压器运行规程"]))
    res = await golden.export_laya(test_db, TENANT)

    assert res["count"] == 1
    s = res["samples"][0]
    assert s["state"].startswith("电网运维问答｜")
    ids = [q["id"] for q in s["questions"]]
    assert ids == ["action", "urgency", "severity"]
    kinds = {q["id"]: q["kind"] for q in s["questions"]}
    assert kinds == {"action": "choice", "urgency": "noul", "severity": "score"}
    assert s["questions"][0]["options"] == golden.LAYA_ACTION_OPTIONS
    assert all(q["gold"] is None for q in s["questions"])   # 冷启动外壳：gold 留空
    assert s["meta"]["needsLabel"] is True
    assert s["meta"]["deviceTags"] == ["1号主变"]
