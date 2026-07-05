# 证据溯源 L2（句级角标可点击溯源）— 设计文档

- 日期：2026-07-06
- 状态：已批准（待 spec 审阅）
- 作者：zhyese + Claude

## 1. 背景

证据溯源功能**不是没做，而是半成品**——前后端骨架都搭到了 L2，但关键链路没接通、字段被丢、关键算法是死代码：

| 层 | 现状 | 证据 |
|---|---|---|
| LLM 打 `[1][2]` 角标 | ✅ prompt 强制要求 | `prompt_templates.py:8` SYSTEM_PROMPT 第4条 |
| 后端句级溯源算法 | ✅ 写全了 | `citation.py` 的 `evidence_trace`/`mark_evidence`/`extract_sentence_sources` |
| 后端主链路调用 | ⚠️ **死代码** | `qa_service.answer()` 只调 `estimate_hallucination`，未调 `evidence_trace`/`mark_evidence`；只暴露成独立接口 `/qa/evidence-trace` 靠前端异步拉 |
| 前端 UI | ✅ 全套都在 | `Chat.vue:182` `[n]`→`<sup>` 渲染；`:63` 来源卡片+点击高亮；`:91` 句级溯源面板；`scrollToSource`/`copySource` |
| retrievalSource 字段 | ⚠️ **丢字段** | `retrieval_service._to_item()` 只吐 `{chunk, score, docId, docName}`，`chunkIdx/docType` 被丢、`chunk` 被 `[:200]` 截断 |
| 检索来源归因 | ❌ **主路径没算** | 主路径 `mixed_search` 不做 dense/bm25/rerank 归因，只有 `debug_search` 做 |
| 页码级定位 | ❌ 数据层不支持 | Chunk 模型无 page 字段 |

**用户体感"没做"的根因**：① 句级面板靠流式 done 后异步拉，小模型常不打 `[n]` → 面板挂「支持比 0%」；② 来源卡片只有文档名+前200字，不可验证；③ 后端溯源是死代码，链路绕。

## 2. 目标与范围

**目标**：主问答（Chat）答案每句话可追溯到具体检索片段——`[n]` 角标可点击 → 滚动高亮对应来源卡片；hover 来源卡片 → 反向高亮答案对应句子；来源卡片显示完整字段供验证。

**范围**：
- 覆盖 `/qa/answer`（非流式）+ `/qa/answer/stream`（流式）
- 后端：激活 `citation.py` 死代码进主链路 + 新增向量相似度自动补标 + `retrieval_service` 扩字段与归因
- 前端：来源卡片补全字段渲染 + 句级面板改读主链路下发 + 反向高亮

**非目标（YAGNI）**：
- L3 页码级原文定位（Chunk 无 page 字段，需改解析层，另立项目）
- Diagnose/工单的 `SourcesList` 升级（各有简单版，后续复用本期接口字段）

## 3. 后端设计

### 3.1 `citation.py` 新增自动补标（激活死代码）

新增 `auto_cite`，对答案中没有 `[n]` 角标的句子用向量相似度补标：

```python
async def auto_cite(answer: str, contexts: list[dict],
                    threshold: float | None = None) -> tuple[str, dict]:
    """无角标句子 → 向量相似度匹配 chunk 补 [k]。
    返回 (annotated_answer, evidence_trace_dict)。

    流程：
    1) chunk_embs = await embed_texts([c["chunk"] for c in contexts])   # 1次批量
    2) sentences = split_sentences(answer)
       bare = [s for s in sentences if not extract_sentence_sources(s)]
       bare_embs = await embed_texts(bare)                              # 1次批量
    3) 每个 bare 句 vs chunk_embs 取 cosine top1
       ≥ 阈值 → 在该句末追加对应 [k]；< 阈值 → 不标（保留"无引用来源"）
    4) return annotated, evidence_trace(annotated)

    降级：embed 异常 → degraded("auto_cite_embed", e)，
          返回 (answer, evidence_trace(answer))  # 只标原有角标
    """
```

- 阈值 `settings.CITATION_SIM_THRESHOLD`（默认 0.6），开关 `settings.CITATION_AUTO_ENABLE`（默认 true）。
- 复用 `embedding_service.embed_texts`（批量，走 `EMB_PROVIDER`，与检索 dense 同 provider，语义对齐）。
- cosine 相似度：chunk 用 bge/qwen embedding（已 `normalize_embeddings` 或 API 归一），点积即可。
- 调用成本：每次问答 +2 次批量 embed（chunks≈5 条 + 无角标句子≈N 条），远低于一次 chat。

### 3.2 `retrieval_service._to_item()` 扩字段

```python
def _to_item(h: dict) -> dict:
    return {
        "chunk": h.get("text", ""),          # 完整文本，不再截断
        "score": h.get("score", 0.0),
        "docId": h.get("doc_id", ""),
        "docName": h.get("doc_name", ""),
        "docType": h.get("doc_type", ""),     # 见 3.3，mixed_search 元数据过滤时塞入
        "chunkIdx": h.get("chunk_idx"),       # pool 内已有，带出
        "sources": h.get("srcs", []),         # 见 3.4，主路径补归因
    }
```

向后兼容：11 个调用方均用 `.get()` 取需要的字段，新增字段不影响现有消费方。

### 3.3 docType 带出（零额外 SQL）

`mixed_search` 第4步元数据过滤时已查 `Document.doc_type` 进 `dt_map`（仅用于过滤）。改为同时塞进 pool item：
```python
# 元数据过滤阶段（无论是否命中过滤条件，都先把 doc_type/equipment 塞进 item）
for h in pool:
    h["doc_type"] = dt_map.get(h.get("doc_id"), "")
```
（`dt_map` 仅在 `tenant/doc_type/equipment` 任一非空时查；当三者皆空时该过滤块跳过，doc_type 为空串——可接受，或无条件查一次 doc_type 补全。**实现时决策**：建议当过滤块未执行时，按 doc_ids 单查一次 `Document.doc_type` 补全，保证 docType 总有值。）

### 3.4 sources 检索来源归因（主路径补）

主路径 `mixed_search` 给每个 hit 打 src 标记，fused 时聚合：

- `_dense_and_sparse`：dense_cloud 路命中打 `"srcs": ["dense_cloud"]`，dense_bge 路打 `["dense_bge"]`；sparse 打 `["bm25"]`。
- 各 route 分支（sparse/dense/hybrid）：同样打 srcs。
- RRF 融合 `rrf_fuse`：相同 key 合并时 srcs 取并集（需 `rrf_fuse` 透传或在外层聚合——**实现时确认 rrf_fuse 是否合并 dict 字段**，若不合并则在 fused 后按 key 聚合 srcs）。
- rerank 不改变 srcs。
- `_to_item` 带出 `sources`。

前端 badge 映射：`dense_cloud→badge-info`、`dense_bge→badge-primary`、`bm25→badge-warning`（与 `RetrievalDebug.vue:185` 一致）。

### 3.5 `qa_service.answer()` 接入主链路

result 组装处（约 `qa_service.py:243`）：
```python
# 自动补标（激活 citation 死代码）
if getattr(settings, "CITATION_AUTO_ENABLE", True):
    ans, trace = await citation.auto_cite(ans, contexts)
else:
    trace = citation.evidence_trace(ans)

_halluc = citation.estimate_hallucination(ans, len(contexts))
# ... 存对话用补标后的 ans ...

result = {
    "answer": ans,
    "retrievalSource": [{
        "docId": c.get("docId", ""), "docName": c.get("docName", ""),
        "docType": c.get("docType", ""), "chunkIdx": c.get("chunkIdx"),
        "chunk": c["chunk"], "score": c.get("score", 0.0),
        "sources": c.get("sources", []),
    } for c in contexts],
    "evidenceTrace": trace,           # ← 新增
    # ... 其余字段不变 ...
}
```

### 3.6 `qa_service.stream_answer()` 接入

- `meta` 段 `sources` 同步补全完整字段（不再 `chunk[:200]`）。
- 流完拼接 `full` → `annotated, trace = await citation.auto_cite(full, contexts)`。
- `done` 段新增：
```python
yield {
    "type": "done",
    "annotatedAnswer": annotated,    # ← 补标后全文，前端用它替换渲染出角标上标
    "evidenceTrace": trace,          # ← 句级溯源
    # ... 其余不变 ...
}
```
- 流式 token 照原样吐（打字机动画），前端 done 后用 `annotatedAnswer` 覆盖 `m.content` 触发 `renderMd` 重渲染出 `[n]` 上标。视觉变化仅为追加角标，可接受。

## 4. 前端设计（`Chat.vue`）

### 4.1 来源卡片（`:63` 模板区）
- score 相关度进度条（`barWidth(score)`，复用 `RetrievalDebug.vue` 样式）。
- 检索来源 badge：`v-for` over `s.sources`，按 src 映射彩色 badge。
- docType badge。
- chunk 完整文本：默认截断显示 +「展开/收起」切换（避免长文本撑爆卡片）。
- docId 挂 `data-doc-id`（为将来跳原文预览留口，本期不用）。

### 4.2 句级溯源面板（`:91`）
- 数据源从异步 `getEvidenceTrace` 改为读 `m.evidenceTrace`（主链路下发 / done 段）。
- `getEvidenceTrace` 调用降级为可选的「重新分析」按钮（或直接移除前端调用，接口保留做降级）。

### 4.3 annotatedAnswer 处理
- 流式 `done` 收到 `annotatedAnswer` → `m.content = annotatedAnswer` → 触发 `renderMd` 重渲染（角标 `[n]` → `<sup class="cite-ref">`，已有逻辑）。
- 非流式：result.answer 已是补标后文本，直接渲染。

### 4.4 反向高亮
- 答案渲染时按 `evidenceTrace.sentences` 包 `<span class="ev-sent" data-sidx="i">`，便于定位。
- hover 来源卡片 k → 查 `evidenceTrace.sentences` 中 `sources` 含 k 的 sidx → 给对应 `.ev-sent` 加 `.highlight` class（鼠标移出移除）。
- 点 `[n]` 上标 → `scrollToSource(m, n)`（已有）滚动+高亮第 n 张卡片。

## 5. 接口契约

### 非流式 `/qa/answer` result 新增/变更
```
evidenceTrace: {
  sentences: [{ text, sources: [int], supported: bool, sourceCount: int }],
  totalSupported, totalSentences, supportRatio
}
retrievalSource[] 每项: { docId, docName, docType, chunkIdx, chunk(完整), score, sources[] }
```

### 流式 `/qa/answer/stream`
- `meta.sources[]`：同上完整字段。
- `done` 新增：`annotatedAnswer`、`evidenceTrace`。

### `/qa/evidence-trace`（保留）
内部改调 `citation.auto_cite`（手动重新分析 / 降级兜底）。

## 6. 缓存与降级

- `evidenceTrace` + 补标后 `answer` 随 result 进三级缓存（Redis L1 / MySQL L2 / 语义 L1.5），命中即展示。trace 体量小（几句+编号数组），缓存开销可忽略。
- **旧缓存兼容（零迁移）**：旧缓存无 `evidenceTrace` 字段 → 前端 `m.evidenceTrace?.supportRatio` 已用可选链，自动隐藏面板；无 `annotatedAnswer` → 用原 answer 渲染（无角标上标，不报错）。
- `auto_cite` embed 失败 → `degraded("auto_cite_embed", e)`，回退 `evidence_trace(answer)`（仅原有角标），不阻塞主流程。
- 阈值过高导致全无角标 → `supportRatio=0`，前端显示「无引用来源」，不硬凑。

## 7. 配置项

新增（`settings` + `.env`）：
- `CITATION_SIM_THRESHOLD=0.6`（自动补标 cosine 阈值）
- `CITATION_AUTO_ENABLE=true`（自动补标开关，关则只跑 `evidence_trace` 不补标）

## 8. 测试

- `citation.auto_cite` 单测：全有角标 / 全无角标 / 混合 / 阈值边界（刚好=阈值）/ embed 失败降级。
- `evidence_trace` / `extract_sentence_sources` 回归（已有逻辑不变）。
- `retrieval_service._to_item`：字段齐全；`mixed_search` 集成：sources 归因正确（hybrid 路命中并集）。
- `qa_service.answer` 集成：result 含 `evidenceTrace`、`retrievalSource` 字段齐全、缓存命中字段一致。
- 前端：来源卡片新字段渲染、反向高亮、annotatedAnswer 替换重渲染。

## 9. 实施顺序（供 writing-plans 细化）

1. `citation.auto_cite` + 单测（独立可测）
2. `_to_item` 扩字段 + `mixed_search` docType 带出 + sources 归因 + 集成测试
3. `qa_service.answer` 接入 + result 契约
4. `qa_service.stream_answer` 接入 + done 段契约
5. 配置项 + .env
6. 前端来源卡片字段 + 反向高亮 + annotatedAnswer
7. 端到端验证（Docker rebuild + 真实问答看角标/溯源面板/反向高亮）
