"""F2 时序遥测问答单测：确定性统计、趋势方向、消歧与如实拒答（sqlite in-memory，不连真实库）。"""
from datetime import datetime, timedelta

import pytest

from app.models.ops_workbench import TelemetryPoint
from app.services import telemetry_history_service as ths

TENANT = "default"


async def _seed(db, device="1号主变", metric="油温", unit="℃", values=None,
                quality="good", source="user_import", hours_ago_start=6):
    """按小时倒序写入遥测点；values 为按时间正序的数值列表。"""
    values = values if values is not None else [60.0, 62.0, 64.0, 66.0]
    now = datetime.now()
    for i, v in enumerate(values):
        db.add(TelemetryPoint(
            tenant_id=TENANT, source=source, device_id=device, metric=metric,
            unit=unit, value=v, quality=quality,
            observed_at=now - timedelta(hours=hours_ago_start - i),
            imported_by="tester",
        ))
    await db.commit()


@pytest.mark.asyncio
async def test_series_returns_stats_and_trend(test_db):
    await _seed(test_db, values=[60.0, 62.0, 64.0, 66.0])
    res = await ths.series(test_db, TENANT, "1号主变", metric="油温", hours=24)

    assert res["stats"]["count"] == 4
    assert res["stats"]["min"] == 60.0
    assert res["stats"]["max"] == 66.0
    assert res["stats"]["last"] == 66.0
    assert res["stats"]["unit"] == "℃"
    assert res["trend"]["direction"] == "上升"
    assert res["trend"]["slopePerHour"] > 0
    assert len(res["points"]) == 4


@pytest.mark.asyncio
async def test_series_detects_downtrend(test_db):
    await _seed(test_db, values=[80.0, 76.0, 72.0, 68.0])
    res = await ths.series(test_db, TENANT, "1号主变", metric="油温", hours=24)
    assert res["trend"]["direction"] == "下降"
    assert res["trend"]["slopePerHour"] < 0


@pytest.mark.asyncio
async def test_series_flat_is_stable(test_db):
    await _seed(test_db, values=[70.0, 70.01, 69.99, 70.0])
    res = await ths.series(test_db, TENANT, "1号主变", metric="油温", hours=24)
    assert res["trend"]["direction"] == "平稳"


@pytest.mark.asyncio
async def test_series_excludes_non_good_quality(test_db):
    await _seed(test_db, values=[60.0, 61.0, 62.0], quality="good")
    await _seed(test_db, values=[999.0], quality="bad", hours_ago_start=3)
    res = await ths.series(test_db, TENANT, "1号主变", metric="油温", hours=24)

    assert res["stats"]["count"] == 3
    assert res["stats"]["max"] == 62.0          # 坏点不参与统计
    assert res["excluded"] == 1


@pytest.mark.asyncio
async def test_series_unknown_device_lists_available(test_db):
    """设备名对不上 → 不编造，给出可选设备让 Agent 改问。"""
    await _seed(test_db, device="2号主变")
    res = await ths.series(test_db, TENANT, "不存在的设备", hours=24)

    assert res["stats"] is None
    assert res["availableDevices"] == ["2号主变"]
    assert "不存在" in res["note"]


@pytest.mark.asyncio
async def test_series_fuzzy_match_single_device(test_db):
    await _seed(test_db, device="1号主变")
    res = await ths.series(test_db, TENANT, "主变", metric="油温", hours=24)
    assert res["deviceId"] == "1号主变"
    assert res["stats"] is not None


@pytest.mark.asyncio
async def test_series_ambiguous_device_asks_disambiguation(test_db):
    await _seed(test_db, device="1号主变")
    await _seed(test_db, device="2号主变")
    res = await ths.series(test_db, TENANT, "号主变", hours=24)

    assert res["stats"] is None
    assert set(res["availableDevices"]) == {"1号主变", "2号主变"}
    assert "多个设备" in res["note"]


@pytest.mark.asyncio
async def test_series_unknown_metric_lists_available(test_db):
    await _seed(test_db, metric="油温")
    await _seed(test_db, metric="负荷", unit="MW")
    res = await ths.series(test_db, TENANT, "1号主变", metric="振动", hours=24)

    assert res["stats"] is None
    assert set(res["availableMetrics"]) == {"油温", "负荷"}


@pytest.mark.asyncio
async def test_series_empty_window_does_not_fabricate(test_db):
    """时间窗内无数据 → 明确说无数据，绝不推测数值。"""
    await _seed(test_db, hours_ago_start=100)
    res = await ths.series(test_db, TENANT, "1号主变", metric="油温", hours=1)

    assert res["stats"] is None
    assert "无遥测数据" in res["note"]
    assert "不得推测" in res["note"]


@pytest.mark.asyncio
async def test_series_tenant_isolation(test_db):
    """跨租户不可见（租户隔离）。"""
    await _seed(test_db, device="1号主变")
    res = await ths.series(test_db, "other-tenant", "1号主变", hours=24)
    assert res["stats"] is None
    assert res["availableDevices"] == []


@pytest.mark.asyncio
async def test_series_no_device_lists_available(test_db):
    await _seed(test_db)
    res = await ths.series(test_db, TENANT, "", hours=24)
    assert res["stats"] is None
    assert res["availableDevices"] == ["1号主变"]


def test_downsample_keeps_last_point():
    now = datetime.now()
    pts = [TelemetryPoint(tenant_id=TENANT, source="s", device_id="d", metric="m",
                          unit="u", value=float(i), quality="good",
                          observed_at=now + timedelta(minutes=i), imported_by="t")
           for i in range(1000)]
    out = ths._downsample(pts, target=50)
    assert len(out) <= 51
    assert out[-1] is pts[-1]
    assert out[0] is pts[0]


def test_fmt_telemetry_no_data_shows_options():
    from app.services.agent_tools import _fmt_telemetry
    text = _fmt_telemetry({
        "deviceId": "1号主变", "metric": "", "start": "s", "end": "e",
        "stats": None, "availableDevices": ["1号主变"], "availableMetrics": ["油温"],
        "note": "无遥测数据",
    })
    assert "可选设备" in text
    assert "可选指标" in text
    assert "无遥测数据" in text


def test_fmt_telemetry_renders_stats():
    from app.services.agent_tools import _fmt_telemetry
    text = _fmt_telemetry({
        "deviceId": "1号主变", "metric": "油温", "start": "s", "end": "e", "excluded": 1,
        "stats": {"min": 60.0, "max": 66.0, "mean": 63.0, "first": 60.0, "last": 66.0,
                  "delta": 6.0, "unit": "℃", "count": 4},
        "trend": {"direction": "上升", "slopePerHour": 2.0, "maxJump": 2.0, "jumpAt": "t"},
        "points": [{"at": "2026-10-03T10:00:00+00:00", "value": 60.0, "unit": "℃"}],
        "note": "仅 good 参与统计",
    })
    assert "趋势:上升" in text
    assert "有效样本:4" in text
    assert "℃" in text
