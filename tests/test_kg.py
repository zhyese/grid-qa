"""知识图谱三元组解析单测（图谱质量关键：LLM 输出容错抽取）。"""
from app.services.kg_service import _parse_triples


def test_parse_normal():
    ans = '[{"s":"主变压器","r":"发生","o":"温度过高"}]'
    out = _parse_triples(ans)
    assert len(out) == 1
    assert out[0] == {"s": "主变压器", "r": "发生", "o": "温度过高"}


def test_parse_with_codeblock():
    """LLM 常用 ```json 包裹，正则仍能从中间提取数组。"""
    ans = '```json\n[{"s":"断路器","r":"拒动","o":"故障"}]\n```'
    out = _parse_triples(ans)
    assert len(out) == 1
    assert out[0]["s"] == "断路器"


def test_parse_empty_or_invalid():
    assert _parse_triples("") == []
    assert _parse_triples("无法抽取三元组") == []
    assert _parse_triples("not a json array") == []


def test_parse_skip_missing_fields():
    """缺 s/r/o 任一项的条目跳过（防脏数据污染图谱）。"""
    ans = '[{"s":"有主体","r":"","o":"空关系"}, {"s":"完整","r":"正常","o":"客体"}]'
    out = _parse_triples(ans)
    assert len(out) == 1
    assert out[0]["s"] == "完整"


def test_parse_filters_overlong():
    """主体超 20 字的条目过滤（防 LLM 输出整段文字当主体）。"""
    long_s = "超" * 30
    ans = f'[{{"s":"{long_s}","r":"关系","o":"客体"}}]'
    assert _parse_triples(ans) == []


def test_parse_multiple():
    ans = '[{"s":"A","r":"r1","o":"B"},{"s":"C","r":"r2","o":"D"}]'
    out = _parse_triples(ans)
    assert len(out) == 2
