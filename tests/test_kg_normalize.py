"""知识图谱实体消歧 + 关系 schema 约束单测（A5，纯逻辑）。"""
from app.services.kg_normalize import canonical_entity, canonical_relation


def test_entity_strip_number_prefix():
    """#1主变 / 1号主变 → 与 主变 同一实体（编号前缀剥离，多跳推理不断链）。"""
    assert canonical_entity("#1主变") == canonical_entity("主变")
    assert canonical_entity("1号主变") == canonical_entity("主变")


def test_entity_normalize_alias():
    """主变 → 主变压器（复用术语表别名归一）。"""
    assert "主变压器" in canonical_entity("主变")


def test_relation_whitelist_passthrough():
    """白名单关系原样保留。"""
    assert canonical_relation("发生") == "发生"
    assert canonical_relation("处置方法") == "处置方法"


def test_relation_alias_mapped():
    """别名映射到标准关系。"""
    assert canonical_relation("因为过载") == "原因"      # "因为" → 原因
    assert canonical_relation("处理") == "处置方法"      # "处理" → 处置方法
    assert canonical_relation("导致跳闸") == "原因"      # "导致" → 原因


def test_relation_fallback_to_related():
    """无法归类的关系归为'相关'（保留连通性，不丢边）。"""
    assert canonical_relation("乱七八糟的关系") == "相关"
    assert canonical_relation("") == "相关"


def test_entity_empty_safe():
    assert canonical_entity("") == ""
    assert canonical_entity("   ") == ""


def test_canonical_relation_protect():
    assert canonical_relation("动作于") == "保护"
    assert canonical_relation("保护范围") == "保护"
    assert canonical_relation("跳闸") == "保护"


def test_canonical_relation_test():
    assert canonical_relation("校验") == "试验"
    assert canonical_relation("检验") == "试验"
    assert canonical_relation("测试") == "试验"


def test_canonical_relation_existing_unchanged():
    assert canonical_relation("导致") == "原因"
    assert canonical_relation("处理") == "处置方法"
    assert canonical_relation("无语义关系xyz") == "相关"
