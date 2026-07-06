# Query 改写方法论（口语版）

> 本文严格基于 `docs/superpowers/specs/2026-07-06-query-rewrite-upgrade-design.md`（设计）和 `docs/superpowers/plans/2026-07-06-query-rewrite-upgrade.md`（实现计划）整理，用大白话讲清楚 query 改写这套东西。代码在 `backend/app/services/rewrite_*.py` + `query_rewrite.py` + `retrieval_service.py`。

---

## 一、这玩意儿是干嘛的

说白了：用户提问常常很**口语**、很**碎**、带**缩写**——比如「主变烧了咋办」「SF6 漏气咋整」「CT 怎么选」。这种问题直接拿去向量检索，召回率很差（query 和文档的语言风格对不上）。

**query 改写**就是先把问题"翻译"成规范、完整、适合检索的样子——「主变烧了咋办」→「主变压器故障的应急处置流程」——再去查。召回立刻就上来了。

---

## 二、以前为啥说它 toy（4 个毛病）

老代码（`rewrite_query`）就是个单 prompt 改写，有 4 个硬伤：

| 毛病 | 大白话 |
|---|---|
| **笼统改写** | 一个 prompt 包打天下，不管你是口语、缩写还是术语缺失，全走同一套话术。没有 few-shot 示例，LLM 凭感觉改。 |
| **盲用** | 改完直接用，**改写后检索是不是更好？不知道**。改坏了也照用。 |
| **不自适应** | 简单规范的问题（如「主变压器温度标准」）也强行改一遍，白白多一次 LLM 调用，增加延迟。 |
| **不缓存** | 同一个问题每次来都重新调 LLM 改写，又慢又费钱。 |

编排层（multi-query / HyDE / RRF / rerank）其实不 toy，**toy 的是改写策略本身**。

---

## 三、怎么治的（4 招，对应 4 个新组件）

### 招 1：先分类，再改写 —— `RewriteStrategyClassifier`

别上来就改，**先看这个问题属于哪一类**（纯规则判断，不调 LLM）：

- **口语化**：太短（<8 字）或含「咋/咋办/啥/嘛」→ 需要规范化
- **缩写**：含 `CT/PT/SF6/GIS/VT/AVR...` → 需要展开成全称
- **术语缺失**：含术语表里的非标准别名（如「主变」→「主变压器」）→ 需要标准化
- **正常**：以上都不是 → **直接跳过改写**（这就是「自适应」，省一次 LLM）

每类有自己的专属 prompt + few-shot 示例（`data/rewrite_fewshot.json`），LLM 改得准多了。

### 招 2：改完评估一下 —— `RewriteEvaluator`

改写不能盲用。改完后**各跑一次轻量检索**（单路 dense，候选 10 条，跳过 rerank/MMR），对比「改写前 vs 改写后」的 top-5 分数和：

- 改写后分数 > 改写前 × 1.05（留 5% margin 防抖动）→ **采纳改写**
- 否则 → **回退原 query**（改写没帮上忙，别用）

这一步是闭环的关键——**改写有没有用，用数据说话**。

### 招 3：缓存 —— `RewriteCache`

改写结果存 Redis（key=`rewrite:{类型}:{md5(query)}`，TTL 7 天）。同一个问题第二次来，**直接拿缓存的改写，不再调 LLM**。

multi-query 分解结果、HyDE 假设文档也都走这套缓存（strategy=multi / hyde）。

### 招 4：自适应调度 —— Classifier 兼任

招 1 的「正常 → 跳过」就是自适应的总开关：**规范问题直接跳过整个改写流程**，省 LLM 调用 + 评估检索的延迟。只有口语/缩写/术语缺失才进改写闭环。

> 配置 `REWRITE_ADAPTIVE_ENABLE=False` 可关掉自适应（所有问题都改写，等价旧行为）。

---

## 四、一次问答里它怎么跑（大白话流程）

```
用户问一个问题
  ↓
Classifier 判类型
  ├─ 正常 → 跳过改写，直接拿原 query 去检索
  └─ 口语/缩写/术语缺失 → 进改写闭环：
       ↓
       查 Redis 缓存
         ├─ 命中 → 直接用缓存的改写（顺便记一笔事件）
         └─ 没命中 → 选对应 prompt + few-shot → LLM 改写
              ↓
              Evaluator 评估：改写后检索分数比原来高吗？
                ├─ 高 → 用改写
                └─ 没高 → 用原 query（改写被否决）
              ↓
              结果写回缓存 + 记一笔事件 + 指标计数
  ↓
拿最终 query 去检索（dense + BM25 + RRF + rerank ...）
```

**CRAG 纠错**场景（检索到 incorrect）仍走老的 `rewrite_query(force=True)`，绕过这套闭环——因为纠错是强制改写，不和 adaptive/评估混。

---

## 五、怎么看效果（可视化面板）

光做不观测等于白做。Admin 页面新增「🔧 Query改写」tab，4 个视图：

1. **概览卡**：总改写数 / 采纳率 / 否决率 / 缓存命中率
2. **策略分布饼图**：rewrite/multi/hyde 各占多少、各自采纳率
3. **分数散点图**：每次改写「原分数(x) vs 新分数(y)」，对角线上方=改进（绿）、下方=否决（红）——一眼看出改写是普遍改进还是少数抖动
4. **趋势折线图**：采纳率/否决率/缓存命中率按日变化——驱动调 margin、调 prompt
5. **明细表**：每条改写事件的原 query → 改写、是否采纳、分数对比

数据来自 `rewrite_event` 表（每次改写异步记一笔，采样率可配）。接口 `GET /system/optimizer/rewrite-stats` + `/rewrite-events`。

---

## 六、几个关键开关（`config.py`）

```python
REWRITE_CACHE_TTL       = 604800    # 缓存 7 天
REWRITE_EVAL_ENABLE     = True      # 评估闭环（关掉=改写盲用，省 50ms 延迟）
REWRITE_ADAPTIVE_ENABLE = True      # 自适应（关掉=所有问题都改写）
REWRITE_EVAL_MARGIN     = 0.05      # 评估阈值：改写要高出 5% 才算更优（防分数抖动）
REWRITE_EVAL_CAND       = 10        # 评估检索候选数
REWRITE_EVAL_TOPK       = 5         # 评估取 top-K 算分数和
REWRITE_EVENT_SAMPLE_RATE = 1.0     # 事件采样率（高流量调低避免写放大）
```

---

## 七、踩过的坑（教训）

1. **认证响应**：项目统一封装——**HTTP 恒 200，认证失败体现在 body 的 `code:401`**。测试断言别写 `status_code==401`，要写 `r.json()["code"]==401`。
2. **评估必须轻量**：不能跑全链路检索（双倍 rerank/MMR 太重）。只跑单路 dense（候选 10），算 top-K 分数和——信号粗但够判改写好坏，延迟可控（~50ms × 2 并发）。
3. **缓存/session 并发**：bg task（记事件、失效缓存）**必须用独立 `AsyncSessionLocal`**，不能共享请求 db session——否则请求结束 close session 时 bg task 还在用，触发 `IllegalStateChangeError` 500。
4. **多轮缓存不能无脑放宽**：指代问题（"它呢"）答案依赖上下文，跨对话缓存会脏。所以多轮**写**放宽（高置信就写），但**读**保持 `search_q==nq` 过滤（只完整 query 命中，指代不跨对话读）。
5. **Alembic 迁移链**：手动写迁移要确认 `down_revision` 指向当前 head；开发环境用 `Base.metadata.create_all` 兜底建表。

---

## 八、和业界的关系

这套不是闭门造车，对标的是 2024-2025 RAG query transformation 主流：

- **Multi-Query + RRF（RAG-Fusion）**：项目早有雏形（multi_query + RRF），本次加了缓存
- **HyDE**：项目有，本次加了缓存
- **Adaptive（低置信才 fallback）**：本次用 Classifier 的「正常跳过」实现
- **评估闭环（retrieval feedback）**：本次新增（Evaluator），业界叫 CRAG 式反馈

> 业界共识：**没有单一技术统治，hybrid + adaptive + 评估闭环**是共性最佳实践。本方案就是按这个思路用已有设施补齐的「轻量全套」。

---

## 九、一图总结

```
                 ┌─────────────────────────────────────┐
                 │  用户 query                          │
                 └────────────────┬────────────────────┘
                                  ↓
                         ┌────────────────┐
                         │  Classifier     │  ← 规则判类型（口语/缩写/术语/正常）
                         └────────┬───────┘
                                  ├─ 正常 → 跳过（自适应）
                                  ↓
                         ┌────────────────┐
              命中 ──────│  Cache (Redis)  │────── 没命中
              ↓          └────────────────┘       ↓
        用缓存改写                         ┌──────────────┐
                                         │  LLM 改写     │  ← few-shot prompt
                                         └──────┬───────┘
                                                ↓
                                         ┌──────────────┐
                                         │  Evaluator    │  ← 改写前后分数对比
                                         └──────┬───────┘
                                    采纳 ←──────┴──────→ 否决（用原 query）
                                                ↓
                                         写缓存 + 记事件
                                                ↓
                                         去检索
```

**一句话**：先看是不是规范问题（是就跳过）→ 查缓存（命中就用）→ LLM 改写 → 评估好坏（好才用）→ 存缓存记事件。改写从「盲改盲用」升级到「分类改、评估用、缓存省、自适应跳」。
