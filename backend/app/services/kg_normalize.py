"""知识图谱实体消歧 + 关系 schema 约束（A5，图谱质量保障）。

LLM 自由抽取会产生两类噪声：
- 同义实体分裂："主变/主变压器/#1主变/1号主变" 成 4 个节点，多跳推理断链
- 关系发散："是/有/关于/涉及" 等无语义关系，图谱 schema 不收敛

收敛策略：
- canonical_entity：去编号前缀（#1/1号/一、）+ 复用 term_service 别名归一（主变→主变压器）
- canonical_relation：关系映射到白名单，无法映射归为"相关"（保留连通性，不丢边）
"""
import re

from app.services.term_service import normalize as term_normalize

# 关系 schema 白名单（电网运维核心语义）—— 标准: (别名...)
_REL_SCHEMA = {
    "发生": ("发生", "出现", "产生"),
    "表现为": ("表现为", "症状", "现象是", "表现为"),
    "处置方法": ("处置方法", "处理方法", "处置", "处理", "应对", "消除"),
    "检修步骤": ("检修步骤", "检修", "维修", "检修方法", "处理步骤"),
    "原因": ("原因", "因为", "导致", "引发", "由"),
    "影响": ("影响", "波及", "后果", "危害"),
    "预防": ("预防", "预防措施", "防范", "防止"),
    "属于": ("属于", "归属", "是"),
    "位于": ("位于", "安装在", "位置", "装设"),
    "额定值": ("额定值", "额定", "参数", "容量"),
    "预警阈值": ("预警阈值", "阈值", "报警值", "限值", "告警"),
}
_REL_ALIAS = {alias: std for std, aliases in _REL_SCHEMA.items() for alias in aliases}
_FALLBACK_REL = "相关"  # 兜底关系：无法归类但保留连通性

_NO_PREFIX1 = re.compile(r"^[#＃]?\d+\s*号?")
_NO_PREFIX2 = re.compile(r"^[一二三四五六七八九十0-9]+[、.\s]+")


def canonical_entity(name: str) -> str:
    """实体消歧：去编号前缀 + 术语别名归一（复用 term_service）。"""
    if not name:
        return ""
    s = name.strip()
    s = _NO_PREFIX1.sub("", s)
    s = _NO_PREFIX2.sub("", s)
    s = term_normalize(s)  # 主变→主变压器 / SF6断路器 等领域归一
    return s.strip()


def canonical_relation(rel: str) -> str:
    """关系 schema 约束：映射到白名单标准关系，无法映射归为'相关'。"""
    if not rel:
        return _FALLBACK_REL
    r = rel.strip()
    if r in _REL_SCHEMA:
        return r
    for alias, std in _REL_ALIAS.items():
        if alias and alias in r:
            return std
    return _FALLBACK_REL
