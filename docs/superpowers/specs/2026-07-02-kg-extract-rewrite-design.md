# KG 三元组抽取重写 — 设计 spec

> 日期：2026-07-02 ｜ 选型：Schema 约束单轮抽取 + 强后处理（用户确认，方案 A）
> 状态：待用户审阅

## 背景与目标

三元组抽取**已自动触发**（`document_service.vectorize_document` 向量化后后台 `_kg_extract_bg` → `kg_service.extract_triples`，写 MySQL+Neo4j；手动 `/kg/extract` 复用同函数）。问题不在"没自动"，而在**抽取质量差**：噪声多（琐碎/无意义三元组）、遗漏多（关键故障-原因-处置关系丢失）、不一致（实体/关系不收敛）。

**根因**：当前 prompt 让 LLM **自由抽取**，产生任意动词关系 + 无类型约束的实体，post-hoc 的 `kg_normalize`（关系白名单 + 实体归一）兜不住——大量关系落到兜底"相关"造成噪声，或被误映射。

**目标**：重写 `kg_service.extract_triples` 内部——把 schema（实体类型 + 关系白名单）**喂进 prompt**，让 LLM 直接产出规范三元组；配健壮解析 + 全局去重 + 噪声过滤。**自动触发与双写不变**。

## 架构（只重写 extract_triples 内部）

```
文档 chunks 分批(_BATCH=6) → provider.chat(_KG_PROMPT_V2)  ← schema 约束
   → _parse_triples_v2（JSON 数组优先 / 行式回退 / 逐条校验，坏条目丢弃不崩批）
   → 全局后处理：canonical_entity + canonical_relation + 去重(s,r,o) + _is_trivial 过滤
   → 写 MySQL + Neo4j（不变）
```
`document_service.vectorize_document` 的 `_kg_extract_bg` 调度**完全不动**；手动 `/kg/extract` 自动受益于新逻辑。

## 组件

### 新 prompt `_KG_PROMPT_V2`（最大质量杠杆）
- **实体类型指引**：设备 / 部件 / 故障现象 / 异常 / 处置措施 / 检修步骤 / 运行参数 / 危险点 / 保护装置 / 标准 / 系统。
- **关系白名单直接列给 LLM**（§白名单 13 种），要求**只能用这些关系**；文本中不属于这些语义的**不要抽**（从源头去噪，而非抽完兜底）。
- 严格要求：只抽文本明确事实、绝不编造；s/o 为名词短语（设备/现象/措施名），r 必须在白名单内；输出严格 JSON 数组 `[{"s":"...","r":"...","o":"..."}]`；无则 `[]`；不输出解释/markdown。
- 给 **2 个好例 + 1 个坏例**（反噪声）：好例"主变压器 温度异常 → 处置方法 → 减负荷运行"；坏例"第二章 → 属于 → 本文"（禁止）。

### 解析 `_parse_triples_v2(ans) -> list[dict]`
- JSON 数组优先（`json.loads`）。
- 失败 → 行式正则回退（逐行匹配 `{...}` 或 `s|r|o` 分隔）。
- 逐条校验：dict、s/r/o 非空 strip、长度 ≤30；非法条目**丢弃不崩**。
- 取代原 `_parse_triples` 的 20 字硬截断（靠过滤替代）。

### 后处理（跨批全局）
- `canonical_entity`（复用 kg_normalize：去编号前缀 + term 归一）。
- `canonical_relation`（复用 kg_normalize：白名单映射 + 兜底"相关"）。
- **去重**：按归一后 `(s, r, o)` 去重（跨批）。
- **`_is_trivial(s_or_o) -> bool`** 噪声过滤：自环 `s==o`、纯数字/纯标点、长度<2、章节标题模式（`第X章`/`X.Y`/纯编号）、黑名单词（本文/本节/章节/图X/表X）。

### 关系白名单微扩（`kg_normalize._REL_SCHEMA`：11 → 13）
保留现有 11 种，新增 2 个电网高频关系（含别名）：
- **保护**（保护装置 → 被保护设备）：`("保护", "保护范围", "动作于", "跳闸")`
- **试验**（设备 → 试验项目）：`("试验", "检验", "测试", "校验")`

## 数据流（单文档）

```
extract_triples(db, doc_id, model_type):
  chunks = SELECT chunk WHERE doc_id ORDER BY chunk_idx
  all = []
  for batch(6):
    raw = chat(_KG_PROMPT_V2.format(text, schema))
    all += _parse_triples_v2(raw)              # 单批失败 degraded，continue
  seen=set(); normed=[]
  for tp in all:
    s=canonical_entity(tp.s); r=canonical_relation(tp.r); o=canonical_entity(tp.o)
    if not(s and r and o) or s==o: continue
    if _is_trivial(s) or _is_trivial(o): continue
    if (s,r,o) in seen: continue
    seen.add((s,r,o)); normed.append({s,r,o})
  # 清旧 + 写 MySQL + Neo4j（不变）
  return {tripleCount, docName, sample}
```

## 错误处理
- 单批 LLM 失败：`degraded("kg_extract_batch", e)` + continue（不中断整体）。
- 解析失败/空：该批返回 `[]`，不崩。
- Neo4j 写失败：`degraded("kg_neo4j_write", e)`（MySQL 仍写，图谱降级可见）。
- 整体 try/except 已在 `_kg_extract_bg` 外层（后台任务不崩主流程）。

## 测试（`tests/test_kg_extract.py` 新建）
- `_parse_triples_v2`：JSON 数组 / 行式回退 / 非法 JSON→[] / 空→[] / 含坏条目只丢坏的留好的。
- `_is_trivial`：纯数字、章节标题、黑名单词、正常实体（不误杀）。
- 后处理（集成）：噪声+有效混合输入 → 验证去重、自环过滤、trivial 过滤、归一（"1号主变"→"主变压器"）、关系白名单映射 + 兜底。
- 端到端 mock provider：脚本化 LLM 返回 → 验证最终 normed 三元组集合。
- golden：已知短文本应抽出关键三元组（如"主变压器 原因 冷却系统故障"）。
- 用 `asyncio.run`（项目无 pytest-asyncio）；mock `get_llm_provider` + 下游 db/neo4j。

## 范围（YAGNI）
- **不做**：两轮抽取（草抽→精炼）/ LLM+正则混合 / 跨文档实体链接（term 归一已够）/ Neo4j schema 改动 / 触发时机改动 / 置信度评分（v1 用 schema+过滤去噪，先不加置信度字段，避免改 KgTriple 表）。

## 与现有的衔接
- `kg_normalize.canonical_entity/canonical_relation`：复用 + 关系白名单微扩。
- `kg_service._KG_PROMPT` → 替换为 `_KG_PROMPT_V2`；`_parse_triples` → `_parse_triples_v2`。
- `extract_triples` 主体重写（schema prompt + 新解析 + 后处理），函数签名/返回不变 → `_kg_extract_bg` 和 `/kg/extract` 零改动。
