"""KG 三元组抽取重写单测：解析 / 噪声过滤 / 归一去重 / e2e mock。"""
import asyncio
from app.services import kg_service as svc


# ---------- _parse_triples_v2 ----------
def test_parse_json_array():
    ans = '[{"s":"主变压器","r":"原因","o":"冷却系统故障"},{"s":"断路器","r":"试验","o":"回路电阻测试"}]'
    out = svc._parse_triples_v2(ans)
    assert len(out) == 2 and out[0]["s"] == "主变压器"


def test_parse_array_with_garbage_around():
    ans = '说明文字 [{"s":"A","r":"发生","o":"B"}] 后缀'
    out = svc._parse_triples_v2(ans)
    assert out == [{"s": "A", "r": "发生", "o": "B"}]


def test_parse_bad_json_returns_empty():
    assert svc._parse_triples_v2("not json at all") == []
    assert svc._parse_triples_v2("") == []


def test_parse_drops_invalid_items_keeps_valid():
    ans = '[{"s":"好","r":"原因","o":"的"},{"s":"","r":"x","o":"y"},{"s":"a","r":"b","o":"c"}]'
    out = svc._parse_triples_v2(ans)
    assert out == [{"s": "好", "r": "原因", "o": "的"}, {"s": "a", "r": "b", "o": "c"}]


def test_parse_line_fallback():
    ans = '抽取结果：\n{"s":"主变","r":"原因","o":"过载"}\n{"s":"风扇","r":"表现为","o":"停转"}'
    out = svc._parse_triples_v2(ans)
    assert len(out) == 2 and out[0]["o"] == "过载"


# ---------- _is_trivial ----------
def test_is_trivial_section_and_number():
    assert svc._is_trivial("第二章")
    assert svc._is_trivial("1.2")
    assert svc._is_trivial("3.4.1")
    assert svc._is_trivial("123")
    assert svc._is_trivial("本文")
    assert svc._is_trivial("本章")
    assert svc._is_trivial("图3")
    assert svc._is_trivial("表1")


def test_is_trivial_not_real_entity():
    assert not svc._is_trivial("主变压器")
    assert not svc._is_trivial("冷却系统故障")
    assert not svc._is_trivial("SF6断路器")


# ---------- _normalize_triples ----------
def test_normalize_dedup_selfloop_trivial_canon():
    raw = [
        {"s": "1号主变", "r": "原因", "o": "冷却系统故障"},   # 编号前缀去除 → 主变压器
        {"s": "主变压器", "r": "导致", "o": "冷却系统故障"},   # 导致→原因；与上条归一后重复→去重
        {"s": "主变压器", "r": "原因", "o": "主变压器"},       # 自环→过滤
        {"s": "第二章", "r": "属于", "o": "本文"},             # trivial→过滤
        {"s": "断路器", "r": "动作于", "o": "跳闸"},           # 动作于→保护
    ]
    out = svc._normalize_triples(raw)
    pairs = {(t["s"], t["r"], t["o"]) for t in out}
    assert ("主变压器", "原因", "冷却系统故障") in pairs
    assert ("断路器", "保护", "跳闸") in pairs
    assert len(out) == 2


# ---------- _extract_from_chunks + _normalize_triples e2e ----------
class _ScriptedProvider:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def chat(self, msgs, **kw):
        self.calls += 1
        return self.replies.pop(0)


def test_extract_pipeline_drops_noise_and_canonicalizes(monkeypatch):
    monkeypatch.setattr(svc, "_BATCH", 1)   # 2 chunks → 2 批 → 2 次 chat
    prov = _ScriptedProvider([
        '[{"s":"1号主变","r":"原因","o":"冷却系统故障"},{"s":"第二章","r":"属于","o":"本文"}]',
        '[{"s":"断路器","r":"动作于","o":"跳闸"},{"s":"主变压器","r":"导致","o":"冷却系统故障"}]',
    ])
    raw = asyncio.run(svc._extract_from_chunks(prov, ["batch1 text", "batch2 text"]))
    assert len(raw) == 4                      # 解析阶段不过滤，含噪声
    normed = svc._normalize_triples(raw)
    pairs = {(t["s"], t["r"], t["o"]) for t in normed}
    assert ("主变压器", "原因", "冷却系统故障") in pairs   # 1号主变→主变压器 + 导致→原因 + 去重
    assert ("断路器", "保护", "跳闸") in pairs             # 动作于→保护
    assert all("第" not in t["s"] and "本文" not in t["o"] for t in normed)  # 噪声已滤


def test_extract_from_chunks_degrades_on_llm_failure():
    class _Boom:
        async def chat(self, *a, **k):
            raise RuntimeError("LLM 挂")
    out = asyncio.run(svc._extract_from_chunks(_Boom(), ["text"]))
    assert out == []                          # 单批失败降级返回空，不抛
