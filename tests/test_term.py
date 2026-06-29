"""术语归一化单测。"""
from app.services.term_service import normalize


def test_alias_normalized():
    assert "主变压器" in normalize("主变是核心设备")


def test_standard_not_harmed():
    # 主变压器 不应被误伤成 "主变压器压器"
    n = normalize("主变压器运行")
    assert "主变压器压器" not in n
    assert "主变压器运行" in n


def test_multi_terms():
    n = normalize("CT 与 SF6断路器 配合")
    assert "电流互感器" in n
    assert "六氟化硫断路器" in n
