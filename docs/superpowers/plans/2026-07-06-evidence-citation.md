# 证据溯源 L2（句级角标可点击溯源）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 激活证据溯源死代码进主问答链路——答案 `[n]` 角标可点击定位来源卡片 + 后端向量相似度自动补标 + 来源卡片补全 score/docId/chunkIdx/docType/检索来源 + hover 反向高亮。

**Architecture:** 主链路内联（非异步补）。`citation.auto_cite` 对无角标句子用 `embed_texts` 批量向量相似度补标；`retrieval_service._to_item` 扩字段 + 新增纯函数 `_aggregate_srcs` 在 `rrf_fuse` 前/后做 sources 归因回填；`qa_service.answer`/`stream_answer` 把 `evidenceTrace` + 补标后 `answer` 随响应/`done` 段下发；前端改读主链路字段 + 反向高亮。

**Tech Stack:** FastAPI + pydantic-settings（`Settings: FIELD: type = default`）、pytest（根目录 `python -m pytest`，conftest 已把 backend 加进 sys.path）、numpy（raptor.py 已依赖）、Vue3 + MarkdownIt（前端无单测，手动验证）。

## Global Constraints

- 配置项语法严格照抄邻居：`CITATION_AUTO_ENABLE: bool = True`、`CITATION_SIM_THRESHOLD: float = 0.6`（config.py Settings 类内，`case_sensitive=False` 读 .env）。
- pytest 一律根目录跑：`python -m pytest tests/test_xxx.py -v`（conftest.py 已 `sys.path.insert backend`）。
- async 测试用 `asyncio.run(...)` 包同步，不依赖 pytest-asyncio 配置。
- `mixed_search` 的 11 个调用方用 `.get()` 取字段，扩字段向后兼容，不得删改既有字段名。
- `rrf_fuse`（`rrf.py`）只保留首遇 key 的 hit 字段 → sources 归因**必须**独立聚合回填，不能指望 rrf 透传。
- 源码改完必须 Docker rebuild + up -d（源码 bake 进镜像，无 bind mount；改 .env 也要 up -d 重建容器）。
- 端口：后端 8001、MySQL 3307、前端 5173。
- 旧缓存（Redis/MySQL）无 `evidenceTrace`/`annotatedAnswer` 字段 → 前端可选链兜底，零迁移。

---

## File Structure

- **Create** `tests/test_citation.py` — `auto_cite` + settings 配置项单测
- **Create** `tests/test_retrieval_sources.py` — `_aggregate_srcs` + `_to_item` 单测
- **Modify** `backend/app/config.py` — Settings 加 `CITATION_AUTO_ENABLE` / `CITATION_SIM_THRESHOLD`
- **Modify** `backend/app/rag/citation.py` — 新增 `auto_cite` + `_cosine_mat`
- **Modify** `backend/app/services/retrieval_service.py` — `_to_item` 扩字段、`_dense_and_sparse` 打 srcs、新增 `_aggregate_srcs`、`mixed_search` 回填 sources + 补 docType
- **Modify** `backend/app/services/qa_service.py` — `answer` / `stream_answer` 接入 auto_cite + retrievalSource 扩字段 + evidenceTrace
- **Modify** `frontend/src/views/Chat.vue` — 来源卡片字段、反向高亮、annotatedAnswer、句级面板数据源
- **Modify** `.env` — 追加两个配置（注释 + 默认值）

---

### Task 1: 配置项 CITATION_AUTO_ENABLE / CITATION_SIM_THRESHOLD

**Files:**
- Modify: `backend/app/config.py`（Settings 类内，CRAG/RAG 配置区附近）
- Modify: `.env`（追加）
- Test: `tests/test_citation.py`（新建）

**Interfaces:**
- Produces: `settings.CITATION_AUTO_ENABLE: bool`、`settings.CITATION_SIM_THRESHOLD: float`，供 Task 2 `auto_cite` 读取。

- [ ] **Step 1: 写失败测试（新建 test_citation.py）**

```python
# tests/test_citation.py
"""证据溯源 auto_cite + 配置项单测。"""
from app.config import settings


def test_citation_settings_defaults():
    assert settings.CITATION_AUTO_ENABLE is True
    assert settings.CITATION_SIM_THRESHOLD == 0.6
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_citation.py::test_citation_settings_defaults -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'CITATION_AUTO_ENABLE'`

- [ ] **Step 3: 加配置项**

在 `backend/app/config.py` Settings 类内（找到 CRAG 或 SELF_RAG 配置那一区，紧随其后插入）：

```python
    # ---------- 证据溯源（P4-⑮ 句级角标）----------
    CITATION_AUTO_ENABLE: bool = True       # 无角标句子是否向量相似度自动补标
    CITATION_SIM_THRESHOLD: float = 0.6     # 自动补标 cosine 阈值（低于则不补，保留"无引用"）
```

在 `.env` 末尾追加：
```env
# 证据溯源
CITATION_AUTO_ENABLE=true
CITATION_SIM_THRESHOLD=0.6
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_citation.py::test_citation_settings_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py .env tests/test_citation.py
git commit -m "feat(citation): 新增证据溯源配置项 CITATION_AUTO_ENABLE/SIM_THRESHOLD"
```

---

### Task 2: citation.auto_cite（向量相似度自动补标）

**Files:**
- Modify: `backend/app/rag/citation.py`
- Test: `tests/test_citation.py`（追加用例）

**Interfaces:**
- Consumes: `embedding_service.embed_texts(texts: list[str]) -> list[list[float]]`（批量，Task 直接用）、`settings.CITATION_SIM_THRESHOLD`（Task 1）。
- Produces: `async auto_cite(answer, contexts, threshold=None) -> tuple[str, dict]`，返回 `(补标后answer, evidence_trace_dict)`。`contexts` 每项含 `chunk` 字段（mixed_search `_to_item` 产物）。

- [ ] **Step 1: 写失败测试（追加到 test_citation.py）**

```python
import asyncio
from unittest.mock import patch

from app.rag import citation


def _run(coro):
    return asyncio.run(coro)


def test_auto_cite_all_already_cited(monkeypatch):
    """答案每句已有角标 → 不再补，trace 全支撑。"""
    async def fake_embed(texts):
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    answer = "主变油温应≤85℃[1]。应申请停运[1]。"
    contexts = [{"chunk": "主变油温限值85", "docName": "A"}]
    annotated, trace = _run(citation.auto_cite(answer, contexts, threshold=0.6))
    assert "[1]" in annotated
    assert trace["totalSupported"] == 2
    assert trace["supportRatio"] == 1.0


def test_auto_cite_bare_sentence_matched(monkeypatch):
    """无角标句子 → 补到最相似 chunk。"""
    calls = []

    async def fake_embed(texts):
        calls.append(texts)
        if len(calls) == 1:           # 第一次：chunks
            return [[1.0, 0.0], [0.0, 1.0]]
        return [[0.9, 0.1]]           # bare 句偏向 chunk0

    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    answer = "油温过高需停运。"        # 无角标
    contexts = [{"chunk": "油温限值", "docName": "A"}, {"chunk": "停运流程", "docName": "B"}]
    annotated, trace = _run(citation.auto_cite(answer, contexts, threshold=0.6))
    assert annotated.strip().endswith("[1]")   # 补到 chunk0 → [1]
    assert trace["supportRatio"] == 1.0


def test_auto_cite_below_threshold_not_annotated(monkeypatch):
    """句子与所有 chunk 相似度都低于阈值 → 不补，保留无引用。"""
    calls = []

    async def fake_embed(texts):
        calls.append(texts)
        if len(calls) == 1:
            return [[1.0, 0.0], [0.9, 0.4359]]   # 两 chunk 近乎同向
        return [[0.0, 1.0]]                       # 句子与两 chunk 都低相关

    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    answer = "完全无关的一句话。"
    contexts = [{"chunk": "x", "docName": "A"}, {"chunk": "y", "docName": "B"}]
    annotated, trace = _run(citation.auto_cite(answer, contexts, threshold=0.6))
    assert "[" not in annotated                   # 没补任何角标
    assert trace["totalSupported"] == 0


def test_auto_cite_embed_failure_degrades(monkeypatch):
    """embed 异常 → 降级，返回原答案 + 仅原有角标 trace，不抛。"""
    async def boom(texts):
        raise RuntimeError("embed down")
    monkeypatch.setattr("app.services.embedding_service.embed_texts", boom)
    answer = "有据[1]。无据句。"
    contexts = [{"chunk": "x", "docName": "A"}]
    annotated, trace = _run(citation.auto_cite(answer, contexts, threshold=0.6))
    assert "[1]" in annotated
    assert trace["totalSupported"] == 1           # 只有原本带角标那句
    assert trace["totalSentences"] == 2


def test_auto_cite_empty_contexts():
    """无 contexts → 原样返回，不调 embed。"""
    annotated, trace = _run(citation.auto_cite("某句。", [], threshold=0.6))
    assert "[" not in annotated
    assert trace["totalSupported"] == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_citation.py -v -k auto_cite`
Expected: FAIL（`AttributeError: module 'app.rag.citation' has no attribute 'auto_cite'`）

- [ ] **Step 3: 实现 auto_cite + _cosine_mat**

在 `backend/app/rag/citation.py` 末尾追加：

```python
def _cosine_mat(sent_vecs: list[list[float]], chunk_vecs: list[list[float]]) -> "list[list[float]]":
    """句子×chunk 的 cosine 相似度矩阵 (n_sent, n_chunk)。不假设向量已归一化。"""
    import numpy as np
    if not sent_vecs or not chunk_vecs:
        return []
    S = np.asarray(sent_vecs, dtype=np.float32)
    C = np.asarray(chunk_vecs, dtype=np.float32)
    sn = S / (np.linalg.norm(S, axis=1, keepdims=True) + 1e-10)
    cn = C / (np.linalg.norm(C, axis=1, keepdims=True) + 1e-10)
    return (sn @ cn.T).tolist()


async def auto_cite(answer: str, contexts: list[dict],
                    threshold: float | None = None) -> tuple[str, dict]:
    """对答案中无 [n] 角标的句子，用向量相似度匹配 chunk 补标。

    返回 (annotated_answer, evidence_trace_dict)。
    contexts: mixed_search _to_item 产物，每项含 "chunk"。
    embed 异常 → degraded，返回 (answer, evidence_trace(answer))。
    """
    from app.config import settings
    from app.services import embedding_service

    if not answer or not contexts:
        return answer, evidence_trace(answer)

    if threshold is None:
        threshold = getattr(settings, "CITATION_SIM_THRESHOLD", 0.6)

    sentences = split_sentences(answer)
    bare_idx = [i for i, s in enumerate(sentences) if not extract_sentence_sources(s)]
    annotated = list(sentences)

    if bare_idx:
        try:
            chunk_texts = [c.get("chunk", "") or c.get("text", "") for c in contexts]
            chunk_embs = await embedding_service.embed_texts(chunk_texts)
            bare_embs = await embedding_service.embed_texts([sentences[i] for i in bare_idx])
            sim = _cosine_mat(bare_embs, chunk_embs)   # (len(bare), len(chunks))
            for row, si in enumerate(bare_idx):
                if not sim[row]:
                    continue
                best_k = max(range(len(sim[row])), key=lambda k: sim[row][k])
                if sim[row][best_k] >= threshold:
                    annotated[si] = sentences[si] + f"[{best_k + 1}]"
        except Exception as e:
            try:
                from app.core.obs import degraded
                degraded("auto_cite_embed", e)
            except Exception:
                pass

    new_answer = "".join(annotated)
    return new_answer, evidence_trace(new_answer)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_citation.py -v`
Expected: 5 passed（含 Task 1 的 settings 测试）

- [ ] **Step 5: Commit**

```bash
git add backend/app/rag/citation.py tests/test_citation.py
git commit -m "feat(citation): auto_cite 向量相似度自动补标 + 单测"
```

---

### Task 3: retrieval _aggregate_srcs + _to_item 扩字段

**Files:**
- Modify: `backend/app/services/retrieval_service.py`
- Test: `tests/test_retrieval_sources.py`（新建）

**Interfaces:**
- Produces: `_aggregate_srcs(dense_hits, sparse_hits) -> dict[key, list[str]]`（纯函数，key→排序后 src 列表）；`_to_item(h)` 扩出 `docType/chunkIdx/sources` 字段。供 Task 4 在 `mixed_search` 接线。

- [ ] **Step 1: 写失败测试（新建 test_retrieval_sources.py）**

```python
# tests/test_retrieval_sources.py
"""检索来源归因 + _to_item 扩字段单测。"""
from app.services import retrieval_service


def test_aggregate_srcs_merges_dense_and_bm25():
    dense = [
        {"key": ("d1", 0), "srcs": ["dense_cloud"]},
        {"key": ("d1", 0), "srcs": ["dense_bge"]},
    ]
    sparse = [{"key": ("d1", 0)}, {"key": ("d2", 1)}]
    m = retrieval_service._aggregate_srcs(dense, sparse)
    assert m[("d1", 0)] == ["bm25", "dense_bge", "dense_cloud"]   # 并集去重排序
    assert m[("d2", 1)] == ["bm25"]


def test_aggregate_srcs_empty():
    assert retrieval_service._aggregate_srcs([], []) == {}


def test_to_item_full_fields():
    h = {
        "text": "abc", "score": 0.9, "doc_id": "d1", "doc_name": "规程A",
        "chunk_idx": 3, "doc_type": "运维手册", "srcs": ["dense_cloud", "bm25"],
    }
    item = retrieval_service._to_item(h)
    assert item["chunk"] == "abc"
    assert item["score"] == 0.9
    assert item["docId"] == "d1"
    assert item["docName"] == "规程A"
    assert item["chunkIdx"] == 3
    assert item["docType"] == "运维手册"
    assert item["sources"] == ["dense_cloud", "bm25"]


def test_to_item_missing_fields_default_empty():
    """旧格式 hit（无 doc_type/srcs/chunk_idx）→ 字段安全缺省，不报错。"""
    item = retrieval_service._to_item({"text": "x", "doc_id": "d", "doc_name": "n"})
    assert item["docType"] == ""
    assert item["chunkIdx"] is None
    assert item["sources"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_retrieval_sources.py -v`
Expected: FAIL（`_aggregate_srcs` 不存在；`_to_item` 缺新字段断言失败）

- [ ] **Step 3: 改 _to_item + 新增 _aggregate_srcs**

`backend/app/services/retrieval_service.py` 现有 `_to_item`（约 line 25-31）替换为：

```python
def _to_item(h: dict) -> dict:
    return {
        "chunk": h.get("text", ""),
        "score": h.get("score", 0.0),
        "docId": h.get("doc_id", ""),
        "docName": h.get("doc_name", ""),
        "docType": h.get("doc_type", ""),
        "chunkIdx": h.get("chunk_idx"),
        "sources": h.get("srcs", []),
    }


def _aggregate_srcs(dense_hits: list[dict], sparse_hits: list[dict]) -> dict:
    """key → 排序后的检索来源列表（dense_cloud/dense_bge/bm25 并集）。

    rrf_fuse 只保留首遇 key 的 hit 字段，sources 必须独立聚合、fuse 后回填。
    """
    src_map: dict = {}
    for h in dense_hits or []:
        k = h.get("key")
        if k is None:
            continue
        src_map.setdefault(k, set()).update(h.get("srcs", ["dense"]))
    for h in sparse_hits or []:
        k = h.get("key")
        if k is None:
            continue
        src_map.setdefault(k, set()).add("bm25")
    return {k: sorted(v) for k, v in src_map.items()}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_retrieval_sources.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/retrieval_service.py tests/test_retrieval_sources.py
git commit -m "feat(retrieval): _to_item 扩字段 + _aggregate_srcs 归因纯函数 + 单测"
```

---

### Task 4: mixed_search 接入 sources 归因 + docType 补全

**Files:**
- Modify: `backend/app/services/retrieval_service.py`（`_dense_and_sparse` + `mixed_search`）

**Interfaces:**
- Consumes: Task 3 的 `_aggregate_srcs`。
- Produces: `mixed_search` 返回的每个 item 含 `docType/chunkIdx/sources` 真实值（非空默认），供 Task 5/6 的 retrievalSource 用。

> 说明：`mixed_search` 是重函数（依赖 db/milvus/embedding），集成测试归到 Task 8 端到端。本任务核心逻辑（聚合 + 字段映射）已被 Task 3 纯函数覆盖，此处只做接线 + import 冒烟。

- [ ] **Step 1: `_dense_and_sparse` 给 dense 命中打 srcs**

`_dense_and_sparse`（约 line 66-105）现有 dense_hits 构造改为分别打 src：

```python
    dense_hits = []
    for d in dense_cloud:
        dense_hits.append({**d, "key": (d.get("doc_id"), d.get("chunk_idx")), "srcs": ["dense_cloud"]})
    for d in dense_bge:
        dense_hits.append({**d, "key": (d.get("doc_id"), d.get("chunk_idx")), "srcs": ["dense_bge"]})
```

sparse_hits 构造（现有 `sparse_hits.append({...})`）追加 `"srcs": ["bm25"]`：

```python
        sparse_hits.append({
            "key": (c["doc_id"], c["chunk_idx"]), "text": c["text"],
            "doc_id": c["doc_id"], "doc_name": c["doc_name"], "chunk_idx": c["chunk_idx"],
            "srcs": ["bm25"],
        })
```

- [ ] **Step 2: dense 单路分支（route == "dense"）同样打 srcs**

该分支（约 line 157-181）现有 `for src, results in (("dense_cloud", dense_cloud), ("dense_bge", dense_bge)):` 循环里：

```python
            for d in results:
                all_dense.append({
                    **d, "key": (d.get("doc_id"), d.get("chunk_idx")),
                    "srcs": [src],
                })
```

sparse 单路分支（route == "sparse"，约 line 138-149）每个 append 追加 `"srcs": ["bm25"]`。

- [ ] **Step 3: hybrid 分支用 _aggregate_srcs 回填**

hybrid 分支（约 line 183-201，`else:` 块）RRF 融合后，回填 srcs。把：
```python
        fused = rrf.rrf_fuse([all_dense, all_sparse], key_fn=lambda h: h["key"])
        pool = fused[: topk * 2]
```
改为：
```python
        fused = rrf.rrf_fuse([all_dense, all_sparse], key_fn=lambda h: h["key"])
        src_map = _aggregate_srcs(all_dense, all_sparse)
        for h in fused:
            h["srcs"] = src_map.get(h.get("key"), [])
        pool = fused[: topk * 2]
```

dense/sparse 单路分支同样回填（它们不经 rrf_fuse，hit 自带 srcs，但同 key 合并去重）：
```python
        # dense 单路
        src_map = _aggregate_srcs(all_dense, [])
        for h in pool:
            h["srcs"] = src_map.get(h.get("key"), list(h.get("srcs", [])))
        # sparse 单路
        src_map = _aggregate_srcs([], all_sparse)
        for h in pool:
            h["srcs"] = src_map.get(h.get("key"), list(h.get("srcs", [])))
```

- [ ] **Step 4: docType 补全（保证总有值）**

`mixed_search` 第4步元数据过滤（约 line 214-229）现有 `if doc_ids and (tenant or doc_type or equipment):` 块。在该块**之后**（无论是否执行过滤）补一次无条件 doc_type 查询塞进 pool：

```python
    # docType 补全：保证每个 item 都带 doc_type（来源卡片要用）
    _need_type = [h for h in pool if not h.get("doc_type")]
    if _need_type:
        _ids = {h.get("doc_id") for h in _need_type if h.get("doc_id")}
        if _ids:
            _rows = (await db.execute(
                select(Document.id, Document.doc_type).where(Document.id.in_(_ids))
            )).all()
            _dt = {r[0]: (r[1] or "") for r in _rows}
            for h in pool:
                if not h.get("doc_type"):
                    h["doc_type"] = _dt.get(h.get("doc_id"), "")
```

（`Document` 已在文件顶部 import，复用即可。）

- [ ] **Step 5: import 冒烟 + 回归现有检索测试**

Run: `python -c "from app.services import retrieval_service; print('ok')"`
Expected: 打印 `ok`（无语法/import 错误）

Run: `python -m pytest tests/ -v -k "rrf or chunk or crag" --ignore=tests/test_api.py`
Expected: 现有相关单测全 PASS（接线不破坏既有逻辑）

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/retrieval_service.py
git commit -m "feat(retrieval): mixed_search 接入 sources 归因回填 + docType 无条件补全"
```

---

### Task 5: qa_service.answer 接入 auto_cite + retrievalSource 扩字段

**Files:**
- Modify: `backend/app/services/qa_service.py`（`answer` 函数 result 组装处，约 line 220-258）

**Interfaces:**
- Consumes: `citation.auto_cite`（Task 2）、`_to_item` 扩字段（Task 3/4）。
- Produces: `/qa/answer` 响应 result 新增 `evidenceTrace` 字段；`retrievalSource[]` 每项含完整字段。

> 说明：`qa_service.answer` 依赖 redis/db/conversation/LLM，单测过重。验证 = Task 8 端到端 + 本任务改完后跑 `test_api.py` 回归不破。

- [ ] **Step 1: 改 answer —— 答案补标**

`answer` 函数现有（约 line 220-221）：
```python
    ans = await get_llm_provider(model_type).chat(messages, temperature=config_service.rt_temperature())
    ans = safety.safe_answer(ans)  # 答案脱敏（PII_MASK_ENABLE 开启时，D4）
```
在脱敏**之后**、存对话**之前**插入补标：
```python
    # 证据溯源：无角标句子向量相似度自动补标（激活 citation 死代码进主链路）
    if getattr(settings, "CITATION_AUTO_ENABLE", True):
        ans, _trace = await citation.auto_cite(ans, contexts)
    else:
        _trace = citation.evidence_trace(ans)
```

- [ ] **Step 2: 改 answer —— result 组装**

现有 result（约 line 243-258）的 `retrievalSource` 与新增 `evidenceTrace`：
```python
    result = {
        "answer": ans,
        "retrievalSource": [{
            "docId": c.get("docId", ""), "docName": c.get("docName", ""),
            "docType": c.get("docType", ""), "chunkIdx": c.get("chunkIdx"),
            "chunk": c.get("chunk", ""), "score": c.get("score", 0.0),
            "sources": c.get("sources", []),
        } for c in contexts],
        "evidenceTrace": _trace,
        "graphCount": len(graph),
        # ... 其余字段保持不变（highRisk/confidence/cragAction/...）...
```
（`_halluc = citation.estimate_hallucination(ans, len(contexts))` 保持原位，用补标后的 ans。）

- [ ] **Step 3: 回归测试**

Run: `python -m pytest tests/test_api.py -v`
Expected: 全 PASS（若 test_api 依赖运行中的服务则跳过，记为集成；核心是不引入 import/语法错）

Run: `python -c "from app.services import qa_service; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/qa_service.py
git commit -m "feat(qa): answer 接入 auto_cite + retrievalSource 扩字段 + evidenceTrace"
```

---

### Task 6: qa_service.stream_answer 接入（meta 扩字段 + done 下发 annotatedAnswer/evidenceTrace）

**Files:**
- Modify: `backend/app/services/qa_service.py`（`stream_answer` 函数）

**Interfaces:**
- Produces: 流式 `meta.sources[]` 含完整字段；`done` 段新增 `annotatedAnswer` + `evidenceTrace`。

- [ ] **Step 1: meta 段 sources 扩字段**

`stream_answer` 现有 meta 段（约 line 469-473）：
```python
    yield {
        "type": "meta",
        "sources": [{"docName": c.get("docName", ""), "text": c["chunk"][:200]} for c in contexts],
        "conversationId": conversation_id,
    }
```
改为：
```python
    yield {
        "type": "meta",
        "sources": [{
            "docId": c.get("docId", ""), "docName": c.get("docName", ""),
            "docType": c.get("docType", ""), "chunkIdx": c.get("chunkIdx"),
            "chunk": c.get("chunk", ""), "score": c.get("score", 0.0),
            "sources": c.get("sources", []),
        } for c in contexts],
        "conversationId": conversation_id,
    }
```

- [ ] **Step 2: done 段前补标 + 下发 annotatedAnswer/evidenceTrace**

`stream_answer` 持久化完整答案后（约 line 488-509，`full = "".join(parts)` 之后、`done` yield 之前）插入：
```python
    # 证据溯源：补标 + trace（done 段下发，前端替换渲染出角标上标）
    if getattr(settings, "CITATION_AUTO_ENABLE", True):
        annotated, _trace = await citation.auto_cite(full, contexts)
    else:
        annotated, _trace = full, citation.evidence_trace(full)
```
并在最终 `done` yield 里追加两个字段（找到现有 `yield {"type": "done", ...}`）：
```python
    yield {
        "type": "done",
        "annotatedAnswer": annotated,        # ← 新增：补标后全文
        "evidenceTrace": _trace,             # ← 新增：句级溯源
        "responseTime": round(time.time() - t0, 3),
        "hallucinationRate": halluc,
        # ... 其余保持 ...
    }
```

- [ ] **Step 3: import 冒烟 + 回归**

Run: `python -c "from app.services import qa_service; print('ok')"`
Expected: `ok`

Run: `python -m pytest tests/test_api.py -v`
Expected: PASS（或不引入错误）

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/qa_service.py
git commit -m "feat(qa): stream_answer meta 扩字段 + done 下发 annotatedAnswer/evidenceTrace"
```

---

### Task 7: 前端 Chat.vue（来源卡片字段 + 反向高亮 + annotatedAnswer + 句级面板数据源）

**Files:**
- Modify: `frontend/src/views/Chat.vue`

> 前端无单测框架（package.json 无 vitest），本任务靠手动验证（Task 8）。

- [ ] **Step 1: 来源卡片模板扩字段（约 line 63-65 区域）**

现有：
```vue
<div class="sources" v-if="m.sources && m.sources.length">
  <div class="src-head">📎 引用来源 <span class="hint">点 [n] 定位 · 点卡片复制</span></div>
  <div v-for="(s, j) in m.sources" :key="j" :id="'src-' + (j + 1)" class="src-item" :class="{ hi: m.hiIdx === j + 1 }" @click="copySource(s)">
```
在 `src-item` 内部追加字段渲染（score 进度条 + sources/docType badge + chunk 可展开）：
```vue
    <div v-if="typeof s.score === 'number'" class="src-score">
      <div class="bar"><div class="bar-fill" :style="{ width: Math.min(100, Math.max(2, s.score * 100)) + '%' }"></div></div>
      <span class="muted">{{ (s.score * 100).toFixed(0) }}%</span>
    </div>
    <div class="src-badges">
      <span v-if="s.docType" class="badge badge-neutral">{{ s.docType }}</span>
      <span v-for="src in (s.sources || [])" :key="src" class="badge" :class="srcBadge(src)">{{ srcLabel(src) }}</span>
    </div>
    <div class="src-text" :class="{ clamp: !s._expanded }" @click="s._expanded = !s._expanded">
      📄 {{ s.docName }}：{{ s.chunk || s.text || '' }}
      <span class="hint">{{ s._expanded ? '收起' : '展开' }}</span>
    </div>
```

- [ ] **Step 2: script 加 srcLabel/srcBadge + 反向高亮**

在 `<script setup>` 内（`copySource` 附近）追加：
```javascript
function srcLabel(s) { return { dense_cloud: '云稠密', dense_bge: 'bge稠密', bm25: 'BM25' }[s] || s }
function srcBadge(s) { return { dense_cloud: 'badge-info', dense_bge: 'badge-primary', bm25: 'badge-warning' }[s] || 'badge-neutral' }
function hoverSource(m, k) {            // hover 来源卡片 k → 高亮答案中引用 [k] 的句子
  m.hiIdx = k
}
function leaveSource(m) { m.hiIdx = null }
```
来源卡片 `<div class="src-item" ... @mouseenter="hoverSource(m, j+1)" @mouseleave="leaveSource(m)">` 加鼠标事件。

- [ ] **Step 3: 流式 done 收 annotatedAnswer 替换渲染**

找到流式事件处理（`streamAnswer` 的 onEvent，处理 `ev.type === 'done'` 处），追加：
```javascript
if (ev.annotatedAnswer) { m.content = ev.annotatedAnswer }   // 补标后全文替换，触发 renderMd 出 [n] 上标
if (ev.evidenceTrace) { m._evTrace = ev.evidenceTrace }      // 句级溯源面板数据源
```
非流式（`answer()` 接口）：result 直接有 `evidenceTrace`，在赋值 `m.content = r.data.answer` 处一并 `m._evTrace = r.data.evidenceTrace`。

- [ ] **Step 4: 句级面板改读主链路字段**

现有 `loadEvidence`（约 line 357，调 `getEvidenceTrace`）改为优先读 `m._evTrace`，缺失才异步拉（降级）：
```javascript
async function loadEvidence(m) {
  if (m._evTrace) { return }                       // 主链路已下发，直接用
  const sources = (m.sources || []).map(s => typeof s === 'string' ? s : (s.chunk || s.text || ''))
  try { const r = await getEvidenceTrace(m.content, sources, m.model_type || null); m._evTrace = r.data } catch (e) {}
}
```

- [ ] **Step 5: 样式补全（`<style scoped>` 内，.sources 附近）**

```css
.src-score { display:flex; align-items:center; gap:6px; margin:2px 0; }
.src-score .bar { flex:1; height:4px; background:var(--border); border-radius:2px; overflow:hidden; }
.src-score .bar-fill { height:100%; background:var(--primary, #3b82f6); }
.src-badges { margin:2px 0; }
.src-badges .badge { margin-right:4px; font-size:11px; }
.src-text.clamp { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; cursor:pointer; }
.src-text { cursor:pointer; }
```

- [ ] **Step 6: Commit**

```bash
git add frontend/src/views/Chat.vue
git commit -m "feat(chat): 来源卡片扩字段 + 反向高亮 + annotatedAnswer + 句级面板读主链路"
```

---

### Task 8: 端到端验证（Docker rebuild + 真实问答）

**Files:** 无（验证任务）

- [ ] **Step 1: rebuild 镜像并启动**

Run:
```bash
docker compose build backend frontend
docker compose up -d
```
Expected: 两镜像 rebuild 成功，容器 Up

- [ ] **Step 2: 冒烟——非流式 /qa/answer 字段齐全**

Run（admin/admin123 登录取 token 后）：
```bash
curl -s -X POST http://127.0.0.1:8001/api/qa/answer \
  -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" \
  -d '{"query":"主变压器温度异常如何处置"}' | python -m json.tool
```
Expected: `data.retrievalSource[0]` 含 `docId/docType/chunkIdx/chunk/score/sources`；`data.evidenceTrace.sentences` 非空、`supportRatio > 0`。

- [ ] **Step 3: 冒烟——流式 done 段带 annotatedAnswer/evidenceTrace**

浏览器打开 `http://localhost:5173` → 智能问答 → 提问「SF6断路器漏气该如何处理」，DevTools Network 看 `stream` 响应：
Expected: `meta` 段 sources 含完整字段；`done` 段含 `annotatedAnswer`（带 `[n]`）+ `evidenceTrace`。

- [ ] **Step 4: UI 交互验证**

- 答案内 `[1]` 渲染为上标，点击 → 滚动并高亮第 1 张来源卡片。
- hover 来源卡片 → 答案中引用了该资料的句子高亮。
- 来源卡片显示 score 进度条、检索来源 badge（云稠密/bge稠密/BM25）、docType、chunk 可展开。
- 「证据溯源·句级引用」面板显示「支持比 N%」，每句 `[n]` 可点定位。
- 句级面板数据来自主链路（Network 无 `/qa/evidence-trace` 异步请求，或仅在旧缓存命中时才降级拉取）。

- [ ] **Step 5: 缓存兼容验证**

重复同一问题（触发缓存命中）：
Expected: 来源卡片 + 句级面板仍正常展示（旧缓存无字段时面板自动隐藏，不报错）。

- [ ] **Step 6: 全量回归**

Run: `python -m pytest tests/ -v --ignore=tests/test_api.py`
Expected: 全 PASS（test_citation + test_retrieval_sources 新增用例 + 既有单测）

---

## Self-Review 结果

- **Spec 覆盖**：spec 第2节（auto_cite）→ Task 2；第3节（_to_item/归因/docType）→ Task 3+4；第3.5/3.6（qa_service）→ Task 5+6；第4节（前端）→ Task 7；第8节（测试）→ Task 2/3/8；第6节（配置）→ Task 1。✅ 全覆盖。
- **占位符**：无 TBD/TODO；每步含完整代码或精确命令。✅
- **类型一致**：`auto_cite` 签名 `(answer, contexts, threshold) -> (str, dict)` 在 Task 2 定义、Task 5/6 消费一致；`_aggregate_srcs`/`_to_item` Task 3 定义、Task 4 消费一致；`contexts` 字段 `chunk` 在 retrieval 产出、auto_cite 消费一致。✅
- **已知降级**：Task 4/5/6 的 mixed_search/qa_service 集成测试归到 Task 8 端到端（重依赖 db/LLM），核心逻辑由 Task 2/3 纯函数单测覆盖，已在相应任务内注明。✅
