# KG 三元组抽取重写 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重写 `kg_service.extract_triples`——schema 约束 prompt + 健壮解析 + 全局去重/归一/噪声过滤，把三元组质量（噪声/遗漏/不一致）一次治好。自动触发与 MySQL+Neo4j 双写不变。

**Architecture:** 把 schema（实体类型 + 关系白名单）喂进 prompt 让 LLM 直接产出规范三元组；解析 `_parse_triples_v2`（JSON 优先/行式回退/逐条校验）；纯函数 `_normalize_triples` 做归一+去重+`_is_trivial` 过滤；`extract_triples` 退化为薄编排（读 chunks → `_extract_from_chunks` → `_normalize_triples` → 写）。`kg_normalize` 关系白名单 11→13（+保护/试验）。

**Tech Stack:** Python / openai SDK（LLM）/ pytest（异步用 `asyncio.run`，无 pytest-asyncio）

## Global Constraints

- 后端无 pytest-asyncio：异步测用 `asyncio.run(...)`
- 测试落 `tests/`；运行 `venv/Scripts/python.exe -m pytest tests/<file> -v`（conftest 已把 backend 加 sys.path）
- 后端运行**不带 `--reload`**：`venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --app-dir backend`（改后端代码时绝不热重载）
- 复用既有 `degraded(tag,e)` / `get_llm_provider` / `canonical_entity` / `canonical_relation`
- `extract_triples` 函数签名/返回**不变** → `_kg_extract_bg`（自动触发）和 `/kg/extract`（手动）零改动
- 不加置信度字段（避免改 KgTriple 表）；跨文档实体链接不做（term 归一已够）
- 严格特性分支验证通过再合 main

## File Structure

- **Modify:** `backend/app/services/kg_normalize.py` — `_REL_SCHEMA` 关系白名单 11→13（+保护/试验）
- **Modify:** `backend/app/services/kg_service.py` — 替换 `_KG_PROMPT`→`_KG_PROMPT_V2`；新增 `_parse_triples_v2`（保留旧 `_parse_triples`，`tests/test_kg.py` 仍 import 它）、`_is_trivial` / `_normalize_triples` / `_extract_from_chunks`；重写 `extract_triples` 主体
- **Modify:** `tests/test_kg_normalize.py` — 追加 保护/试验 关系映射测试
- **Create:** `tests/test_kg_extract.py` — 解析/过滤/归一/e2e mock 单测

---

### Task 1: kg_normalize 关系白名单 +保护/+试验

**Files:**
- Modify: `backend/app/services/kg_normalize.py`
- Test: `tests/test_kg_normalize.py`（追加）

**Interfaces:**
- Produces: `_REL_SCHEMA` 新增 `"保护"` / `"试验"` 两键；`canonical_relation("动作于")=="保护"`、`canonical_relation("校验")=="试验"`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_kg_normalize.py`：
```python
from app.services import kg_normalize


def test_canonical_relation_protect():
    assert kg_normalize.canonical_relation("动作于") == "保护"
    assert kg_normalize.canonical_relation("保护范围") == "保护"
    assert kg_normalize.canonical_relation("跳闸") == "保护"


def test_canonical_relation_test():
    assert kg_normalize.canonical_relation("校验") == "试验"
    assert kg_normalize.canonical_relation("检验") == "试验"
    assert kg_normalize.canonical_relation("测试") == "试验"


def test_canonical_relation_existing_unchanged():
    assert kg_normalize.canonical_relation("导致") == "原因"
    assert kg_normalize.canonical_relation("处理") == "处置方法"
    assert kg_normalize.canonical_relation("无语义关系xyz") == "相关"
```

- [ ] **Step 2: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_kg_normalize.py -v`
Expected: FAIL（`AssertionError: '相关' != '保护'`——"动作于"未在白名单→落兜底）

- [ ] **Step 3: 扩展白名单**

`backend/app/services/kg_normalize.py`：把 `_REL_SCHEMA` 末尾两行替换为新增 4 行（11→13）。定位现有：
```python
    "额定值": ("额定值", "额定", "参数", "容量"),
    "预警阈值": ("预警阈值", "阈值", "报警值", "限值", "告警"),
}
```
替换为：
```python
    "额定值": ("额定值", "额定", "参数", "容量"),
    "预警阈值": ("预警阈值", "阈值", "报警值", "限值", "告警"),
    "保护": ("保护", "保护范围", "动作于", "跳闸"),
    "试验": ("试验", "检验", "测试", "校验"),
}
```

- [ ] **Step 4: 运行确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_kg_normalize.py -v`
Expected: PASS（含原有 + 新 3 个测试全过）

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/kg_normalize.py tests/test_kg_normalize.py
git commit -m "feat(kg): 关系白名单 +保护/+试验（11→13）"
```

---

### Task 2: 解析 `_parse_triples_v2` + 噪声过滤 `_is_trivial` + 归一 `_normalize_triples`

**Files:**
- Modify: `backend/app/services/kg_service.py`（追加 3 个纯函数）
- Test: `tests/test_kg_extract.py`（新建）

**Interfaces:**
- Consumes: `kg_normalize.canonical_entity` / `canonical_relation`
- Produces:
  - `_parse_triples_v2(ans: str) -> list[dict]` — JSON 优先/行式回退/逐条校验
  - `_is_trivial(s: str) -> bool` — 章节/纯数字/黑名单/过短
  - `_normalize_triples(triples: list[dict]) -> list[dict]` — 归一+去重+过滤，每项 `{s,r,o}`（已截断 256/128/256）

- [ ] **Step 1: 写失败测试**

`tests/test_kg_extract.py`：
```python
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
        {"s": "主变压器", "r": "导致", "o": "冷却系统故障"},   # 关系"导致"→"原因"；与上条归一后重复→去重
        {"s": "主变压器", "r": "原因", "o": "主变压器"},       # 自环→过滤
        {"s": "第二章", "r": "属于", "o": "本文"},             # trivial→过滤
        {"s": "断路器", "r": "动作于", "o": "跳闸"},           # 关系"动作于"→"保护"
    ]
    out = svc._normalize_triples(raw)
    pairs = {(t["s"], t["r"], t["o"]) for t in out}
    assert ("主变压器", "原因", "冷却系统故障") in pairs
    assert ("断路器", "保护", "跳闸") in pairs
    assert len(out) == 2
```

- [ ] **Step 2: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_kg_extract.py -v`
Expected: FAIL（`AttributeError: module 'kg_service' has no attribute '_parse_triples_v2'`）

- [ ] **Step 3: 实现 3 个纯函数**

在 `backend/app/services/kg_service.py` 的 `_parse_triples` 函数**之后**追加（保留旧 `_parse_triples` 暂不删，Task 3 替换调用点后再删，避免破坏当前 extract_triples）：
```python
def _validate_triples(arr) -> list[dict]:
    """逐条校验：dict、s/r/o 非空、长度 ≤30。"""
    out = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        s = str(it.get("s", "")).strip()
        r = str(it.get("r", "")).strip()
        o = str(it.get("o", "")).strip()
        if s and r and o and len(s) <= 30 and len(r) <= 30 and len(o) <= 30:
            out.append({"s": s, "r": r, "o": o})
    return out


def _parse_triples_v2(ans: str) -> list[dict]:
    """解析 LLM 三元组输出：JSON 数组优先，行式回退，逐条校验丢弃坏条目。"""
    if not ans:
        return []
    m = re.search(r"\[.*\]", ans, re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                return _validate_triples(arr)
        except Exception:
            pass
    # 行式回退：逐个 {...} 解析
    line_objs = []
    for frag in re.findall(r"\{[^{}]*\}", ans):
        try:
            line_objs.append(json.loads(frag))
        except Exception:
            pass
    return _validate_triples(line_objs)


_TRIVIAL_BLACK = ("本文", "本节", "本章", "章节", "附录", "摘要", "目录", "前言")
_TRIVIAL_PAT = re.compile(r"(^第[一二三四五六七八九十百0-9]+[章节条])|^[0-9]+(\.[0-9]+)+$|^[0-9]+$|^[图表][0-9一二三四五六七八九十]")


def _is_trivial(s: str) -> bool:
    """噪声判断：空/过短/章节标题/纯数字标点/黑名单词。"""
    if not s:
        return True
    s = s.strip()
    if len(s) < 2:
        return True
    if s in _TRIVIAL_BLACK:
        return True
    if _TRIVIAL_PAT.match(s):
        return True
    if re.fullmatch(r"[\d\.\s\-/,，。：:;；、]+", s):
        return True
    return False


def _normalize_triples(triples: list[dict]) -> list[dict]:
    """全局后处理：实体归一 + 关系白名单 + 去重(s,r,o) + 噪声过滤。"""
    from app.services.kg_normalize import canonical_entity, canonical_relation
    seen: set = set()
    out: list[dict] = []
    for tp in triples:
        if not isinstance(tp, dict):
            continue
        s = canonical_entity(str(tp.get("s", "")))
        r = canonical_relation(str(tp.get("r", "")))
        o = canonical_entity(str(tp.get("o", "")))
        if not (s and r and o):
            continue
        if s == o or _is_trivial(s) or _is_trivial(o):
            continue
        key = (s, r, o)
        if key in seen:
            continue
        seen.add(key)
        out.append({"s": s[:256], "r": r[:128], "o": o[:256]})
    return out
```

- [ ] **Step 4: 运行确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_kg_extract.py -v`
Expected: PASS（解析 5 + trivial 2 + normalize 1 = 8 个全过）

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/kg_service.py tests/test_kg_extract.py
git commit -m "feat(kg): _parse_triples_v2 + _is_trivial + _normalize_triples（解析/过滤/归一去重）"
```

---

### Task 3: `_KG_PROMPT_V2` + `_extract_from_chunks` + 重写 `extract_triples`

**Files:**
- Modify: `backend/app/services/kg_service.py`（替换 prompt、新增 `_extract_from_chunks`、重写 `extract_triples` 主体、删旧 `_parse_triples`/`_KG_PROMPT`）
- Test: `tests/test_kg_extract.py`（追加 e2e mock）

**Interfaces:**
- Consumes: Task 1 `canonical_relation`（保护/试验）；Task 2 `_parse_triples_v2` / `_normalize_triples`
- Produces:
  - `_KG_PROMPT_V2`（schema 约束：实体类型 + 13 关系白名单 + 好坏例）
  - `async _extract_from_chunks(provider, chunks_text: list[str]) -> list[dict]`（批量调 LLM + 解析，单批失败降级）
  - 重写后 `extract_triples(db, doc_id, model_type=None)`（签名/返回不变）

- [ ] **Step 1: 写失败测试（e2e mock：schema 约束 + 噪声 → 干净三元组）**

追加到 `tests/test_kg_extract.py`：
```python
# ---------- _extract_from_chunks + _normalize_triples e2e ----------
class _ScriptedProvider:
    def __init__(self, replies): self.replies = list(replies); self.calls = 0
    async def chat(self, msgs, **kw):
        self.calls += 1
        return self.replies.pop(0)


def test_extract_pipeline_drops_noise_and_canonicalizes():
    # 两批 chunks，每批 LLM 返回带噪声 + 有效
    prov = _ScriptedProvider([
        '[{"s":"1号主变","r":"原因","o":"冷却系统故障"},{"s":"第二章","r":"属于","o":"本文"}]',
        '[{"s":"断路器","r":"动作于","o":"跳闸"},{"s":"主变压器","r":"导致","o":"冷却系统故障"}]',
    ])
    raw = asyncio.run(svc._extract_from_chunks(prov, ["batch1 text", "batch2 text"]))
    # _extract_from_chunks 只解析不过滤 → raw 含噪声
    assert len(raw) == 4
    normed = svc._normalize_triples(raw)
    pairs = {(t["s"], t["r"], t["o"]) for t in normed}
    assert ("主变压器", "原因", "冷却系统故障") in pairs   # 1号主变→主变压器 + 导致→原因 + 与另一条去重
    assert ("断路器", "保护", "跳闸") in pairs             # 动作于→保护
    assert all("第" not in t["s"] and "本文" not in t["o"] for t in normed)  # 噪声已滤


def test_extract_from_chunks_degrades_on_llm_failure(monkeypatch):
    class _Boom:
        async def chat(self, *a, **k): raise RuntimeError("LLM 挂")
    out = asyncio.run(svc._extract_from_chunks(_Boom(), ["text"]))
    assert out == []   # 单批失败降级返回空，不抛
```

- [ ] **Step 2: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_kg_extract.py -v`
Expected: FAIL（`AttributeError: module 'kg_service' has no attribute '_extract_from_chunks'`）

- [ ] **Step 3: 替换 prompt + 新增 _extract_from_chunks + 重写 extract_triples**

`backend/app/services/kg_service.py`：

① 把现有 `_KG_PROMPT = """..."""` 整段**替换**为：
```python
_KG_PROMPT_V2 = """你是电网运维知识图谱抽取器。从下面运维文本中抽取结构化三元组 (主体, 关系, 客体)。

【实体类型】只抽这些类型的名词短语作为主体/客体：
设备(主变压器/断路器/隔离开关/互感器/避雷器/电缆/母线/GIS等)、部件、故障现象、异常、处置措施、检修步骤、运行参数、危险点、保护装置、标准、系统。

【关系白名单】关系 r 只能从以下选一个；文本中不属于这些语义的【不要抽】：
发生 / 表现为 / 处置方法 / 检修步骤 / 原因 / 影响 / 预防 / 属于 / 位于 / 额定值 / 预警阈值 / 保护 / 试验

【严格要求】
1) 只抽文本中明确出现的事实，绝不编造。
2) 主体 s 与客体 o 必须是具体名词短语（设备名/现象/措施/参数），不得是章节号、"本文""本章"等泛指词。
3) 关系 r 必须在白名单内。
4) 输出严格 JSON 数组：[{{"s":"主体","r":"关系","o":"客体"}}, ...]；无合适三元组输出 []。不要解释、不要 markdown 代码块。

【好例】
文本：主变压器上层油温超过 95℃，应立即减负荷运行并检查冷却系统。
输出：[{{"s":"主变压器","r":"预警阈值","o":"上层油温95℃"}},{{"s":"主变压器","r":"处置方法","o":"减负荷运行"}}]

【坏例（禁止）】
{{"s":"第二章","r":"属于","o":"本文"}}  ← 章节号/泛指词，禁止

【文本】
{text}"""
```

② 删除旧 `_KG_PROMPT` 字符串（已无引用——`extract_triples` 改用 V2；`tests/test_kg.py` 不引用 prompt）。**保留 `_parse_triples`**（`tests/test_kg.py` 仍 import 它，纯函数零风险）。③ 在 `_normalize_triples` 之后追加 `_extract_from_chunks`：
```python
async def _extract_from_chunks(provider, chunks_text: list[str]) -> list[dict]:
    """分批调 LLM 抽取（schema 约束 prompt）→ 解析为原始三元组。单批失败降级。"""
    all_triples: list[dict] = []
    for i in range(0, len(chunks_text), _BATCH):
        batch = chunks_text[i:i + _BATCH]
        text = "\n\n".join(batch)
        try:
            ans = await provider.chat(
                [{"role": "user", "content": _KG_PROMPT_V2.format(text=text)}],
                temperature=0.1, max_tokens=3000,
            )
            all_triples.extend(_parse_triples_v2(ans))
        except Exception as e:
            degraded("kg_extract_batch", e)
    return all_triples
```

④ 把现有 `extract_triples` 函数体（从 `doc = ...` 到 `return {...}`）**替换**为（保留函数签名 `async def extract_triples(db, doc_id, model_type=None)`）：
```python
    doc = (await db.execute(select(Document).where(Document.id == doc_id))).scalar_one_or_none()
    if not doc:
        raise BizError("文档不存在", 404)
    rows = (
        await db.execute(select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_idx))
    ).scalars().all()
    if not rows:
        raise BizError("文档尚未解析，请先解析", 400)

    provider = get_llm_provider(model_type)
    raw = await _extract_from_chunks(provider, [c.content for c in rows])
    triples = _normalize_triples(raw)

    # 清旧：MySQL + Neo4j
    await db.execute(delete(KgTriple).where(KgTriple.doc_id == doc_id))
    try:
        await neo4j_client.delete_by_doc(doc_id)
    except Exception as e:
        degraded("kg_neo4j_delete", e)
    # 写 MySQL（统计/审计来源）
    for tp in triples:
        db.add(KgTriple(subject=tp["s"], relation=tp["r"], object=tp["o"],
                        doc_id=doc_id, doc_name=doc.doc_name))
    await db.commit()
    # 写 Neo4j（图查询/多跳推理）
    try:
        await neo4j_client.upsert_triples(triples, doc_id, doc.doc_name)
    except Exception as e:
        degraded("kg_neo4j_write", e)

    try:
        from app.core import metrics
        total = (await db.execute(select(func.count()).select_from(KgTriple))).scalar() or 0
        metrics.KG_EXTRACT.inc()
        metrics.KB_TRIPLES.set(total)
    except Exception:
        pass
    return {"tripleCount": len(triples), "docName": doc.doc_name, "sample": triples[:30]}
```

- [ ] **Step 4: 运行确认通过（全量 KG 单测）**

Run: `venv/Scripts/python.exe -m pytest tests/test_kg_normalize.py tests/test_kg_extract.py tests/test_kg.py -v`
Expected: PASS（`test_kg.py` 仍 import 保留的 `_parse_triples`，不受影响，全过）

- [ ] **Step 5: 语法检查 + 真实端到端（重抽一篇已有文档验证三元组产出）**

```bash
venv/Scripts/python.exe -m py_compile backend/app/services/kg_service.py backend/app/services/kg_normalize.py
# 后端需在不带 --reload 重启加载新代码后：
TOK=$(curl -s -X POST http://127.0.0.1:8001/api/system/login -H "Content-Type: application/json" -d '{"username":"admin","password":"admin123"}' | python -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")
DID=$(curl -s "http://127.0.0.1:8001/api/document/list?keyword=" -H "Authorization: Bearer $TOK" | python -c "import sys,json;d=json.load(sys.stdin)['data'];l=d if isinstance(d,list) else d.get('list',[]);print(next((x['docId'] for x in l if x.get('status')=='vectorized'),''))")
curl -s -X POST http://127.0.0.1:8001/api/kg/extract -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" -d "{\"docId\":\"$DID\",\"modelType\":\"deepseek\"}" | python -c "import sys,json;d=json.load(sys.stdin)['data'];print('tripleCount=',d.get('tripleCount'));[print('  ',t) for t in (d.get('sample') or [])[:8]]"
# 预期：tripleCount > 0，sample 三元组关系均为白名单内（发生/原因/处置方法/保护/试验...），无"第二章/本文"类噪声
```

- [ ] **Step 6: Commit**
```bash
git add backend/app/services/kg_service.py tests/test_kg_extract.py
git commit -m "feat(kg): schema 约束 prompt + _extract_from_chunks + extract_triples 重写"
```

---

## Self-Review（已自检）

- **Spec 覆盖**：
  - schema 约束 prompt（实体类型 + 13 关系白名单 + 好坏例）→ Task 3 `_KG_PROMPT_V2` ✅
  - 健壮解析（JSON 优先/行式回退/逐条校验）→ Task 2 `_parse_triples_v2` ✅
  - 全局归一 + 去重 + 噪声过滤 → Task 2 `_normalize_triples` + `_is_trivial` ✅
  - 关系白名单 11→13（+保护/试验）→ Task 1 ✅
  - extract_triples 重写（签名不变）→ Task 3 ✅
  - 自动触发 `_kg_extract_bg` 不动 + 手动 `/kg/extract` 受益 → 签名不变保证 ✅
  - 测试（解析/过滤/归一/e2e mock/golden）→ Task 1/2/3 ✅（golden 融在 e2e mock 的"应抽出关键三元组"断言）

- **类型一致**：`_parse_triples_v2 -> list[{s,r,o}]`（Task 2 定义）= `_extract_from_chunks` 消费 + `_normalize_triples` 消费（Task 3）；`_normalize_triples -> list[{s,r,o}]` = `extract_triples` 写 KgTriple 用 `tp["s"]/["r"]/["o"]`（Task 3）一致 ✅；`canonical_relation` 返回白名单键 = prompt 列给 LLM 的关系一致 ✅

- **无占位符**：每步含实代码/实命令/实预期 ✅

- **DRY/删旧**：Task 3 替换 `_KG_PROMPT`→V2（删旧 prompt，无引用）；**保留 `_parse_triples`**（`tests/test_kg.py` import 它，纯函数零风险，不强删以避免破坏既有测试）✅
