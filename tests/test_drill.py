"""N5 故障演练沙箱单测：剧本校验、无状态推演、评分去重、复盘降级。"""
import pytest

from app.core.response import BizError
from app.services import drill_service as svc

pytestmark = pytest.mark.asyncio


def _propagation():
    return [
        {"tOffset": 0, "device": "mt-1", "severity": "critical", "event": "主变故障告警"},
        {"tOffset": 60, "device": "cb-1", "severity": "warning", "event": "断路器异常"},
    ]


def _checklist():
    return [
        {"id": "c1", "action": "查看遥测", "keywords": ["遥测"]},
        {"id": "c2", "action": "隔离设备", "keywords": ["隔离", "停运"]},
    ]


async def _scenario(test_db, **over):
    kw = dict(tenant_id="default", username="zhang", name="主变故障演练",
              description="", station_id="s1", fault_device="mt-1", fault_desc="油温骤升",
              propagation=_propagation(), checklist=_checklist())
    kw.update(over)
    return await svc.create_scenario(test_db, **kw)


async def test_scenario_validation(test_db):
    with pytest.raises(BizError):
        await _scenario(test_db, propagation=[])
    with pytest.raises(BizError):
        await _scenario(test_db, checklist=[])
    with pytest.raises(BizError):
        await _scenario(test_db, name="")


async def test_run_due_events_derived_from_time(test_db):
    """无状态推演：elapsed=0 只有 tOffset=0 的事件到期。"""
    s = await _scenario(test_db)
    r = await svc.start_run(test_db, "default", "zhang", s["id"])
    assert r["status"] == "running"
    assert len(r["dueEvents"]) == 1 and r["dueEvents"][0]["tOffset"] == 0
    assert r["totalEvents"] == 2
    # 剧本快照：后续改剧本不影响 run
    await svc.update_scenario(test_db, s["id"], "default",
                              propagation=_propagation() + [
                                  {"tOffset": 120, "device": "x", "severity": "info",
                                   "event": "新增"}])
    state = await svc.run_state(test_db, r["id"], "default")
    assert state["totalEvents"] == 2


async def test_finish_score_full_coverage(test_db):
    s = await _scenario(test_db)
    r = await svc.start_run(test_db, "default", "zhang", s["id"])
    await svc.record_action(test_db, r["id"], "default", "zhang", "查看1号主变遥测")
    await svc.record_action(test_db, r["id"], "default", "zhang", "隔离故障设备并汇报调度")
    out = await svc.finish_run(test_db, r["id"], "default", "zhang",
                               with_llm=False)  # 模板复盘（LLM 缺席降级路径）
    assert out["status"] == "finished"
    assert out["score"]["coverage"] == 1.0
    assert out["score"]["grade"] == "优秀"
    assert "复盘" in out["evaluationMd"]
    assert out["score"]["avgResponseSec"] is not None


async def test_score_dedup_single_action_counts_once(test_db):
    """一条操作只命中一个清单项（防一条文本刷分）。"""
    s = await _scenario(test_db, checklist=[
        {"id": "c1", "action": "查看遥测", "keywords": ["查看"]},
        {"id": "c2", "action": "核对告警", "keywords": ["查看"]},  # 关键词与 c1 相同
    ])
    r = await svc.start_run(test_db, "default", "zhang", s["id"])
    await svc.record_action(test_db, r["id"], "default", "zhang", "查看遥测数据")
    out = await svc.finish_run(test_db, r["id"], "default", "zhang", with_llm=False)
    assert out["score"]["coverage"] == 0.5
    assert out["score"]["missed"] == ["c2"]


async def test_abort_scores_partial(test_db):
    s = await _scenario(test_db)
    r = await svc.start_run(test_db, "default", "zhang", s["id"])
    await svc.record_action(test_db, r["id"], "default", "zhang", "查看遥测")
    out = await svc.abort_run(test_db, r["id"], "default", "zhang")
    assert out["status"] == "aborted"
    assert out["score"]["coverage"] == 0.5
    # 终止后不能再打卡
    with pytest.raises(BizError):
        await svc.record_action(test_db, r["id"], "default", "zhang", "补打卡")


async def test_delete_scenario_blocked_while_running(test_db):
    s = await _scenario(test_db)
    r = await svc.start_run(test_db, "default", "zhang", s["id"])
    with pytest.raises(BizError):
        await svc.delete_scenario(test_db, s["id"], "default")
    await svc.abort_run(test_db, r["id"], "default", "zhang")
    assert (await svc.delete_scenario(test_db, s["id"], "default"))["id"] == s["id"]


async def test_stats(test_db):
    s = await _scenario(test_db)
    r = await svc.start_run(test_db, "default", "zhang", s["id"])
    await svc.record_action(test_db, r["id"], "default", "zhang", "查看遥测")
    await svc.finish_run(test_db, r["id"], "default", "zhang", with_llm=False)
    stats = await svc.drill_stats(test_db, "default")
    assert stats["finished"] == 1 and stats["running"] == 0
    assert stats["avgCoverage"] == 0.5
    assert stats["byGrade"].get("合格") == 1


async def test_extract_chain_devices_defensive():
    """kg 路径结构演化不破：dict/list/str 混合均可提取，且数量截断。"""
    paths = [
        {"nodes": ["1号主变", "冷却器"], "relation": "导致"},
        ["断路器", "保护动作", "母线失压"],
        {"weird": 42},
    ]
    devs = svc._extract_chain_devices(paths)
    assert "1号主变" in devs and "断路器" in devs
    assert len(devs) <= 8
