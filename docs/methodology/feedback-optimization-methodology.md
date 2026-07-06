# 反馈驱动优化方法论

> 基于 `backend/app/services/feedback_service.py` / `feedback_optimizer_service.py` / `rewrite_event_service.py` + `routers/qa.py` + `routers/system.py` 源码整理（codegraph 核实）。

## 一、闭环总览（一个 dislike 牵动整条优化链）

```
用户点 👎 dislike
  → record_feedback（落 feedback 表 + FEEDBACK 指标）
  → 异步 _judge_bg（LLM-judge 幻觉率 + 检索质量 good/partial/poor，回填 feedback 表）
  → 异步 invalidate_cache_on_dislike（失效 Redis L1 + MySQL L2 该 query 缓存）
  → 异步 maybe_blacklist_on_dislike（累计≥阈值 → 自动进 Redis 黑名单）
       ↓
管理员侧：
  → generate_optimization_report（分析 dislike → 5 类优化建议）
  → auto_tune_cache_ttl（批量黑名单 + TTL 延长候选）
  → 前端可视化（优化建议 tab + Query改写 tab）
```

> 一个坏答案被点踩，**立刻失效缓存（别再喂人）+ 沉淀 judge 分（坏 case 入库）+ 攒够自动拉黑**，管理员再看聚合建议动手。这就是「被动收集 → 主动优化」的闭环。

## 二、record_feedback（`feedback_service.py:22`）

落库 + 指标 + dislike 异步 judge：
- 写 `feedback` 表（query/answer/feedback/username/reason/retrieval_sources）
- `metrics.FEEDBACK.labels(feedback).inc()`
- **dislike + ONLINE_FAITHFULNESS_ENABLE** → 异步 `_judge_bg`（不阻塞反馈接口）

## 三、_judge_bg（dislike 的 LLM-judge，`feedback_service.py:49`）

后台对坏答案跑两个 judge，回填 feedback 表字段：
- **judge_halluc / judge_supported**：`judge.judge_hallucination`（幻觉率 + 支撑率）
- **retrieval_quality**：`judge.judge_context_relevance`（query vs 检索来源 相关性）→ `good(≥0.7) / partial(≥0.4) / poor`

> 这俩字段是后续「一致性矩阵」和「编造检测」的数据源。bg task 用独立 `AsyncSessionLocal`。

## 四、dislike 触发的两个异步动作（`qa.py:feedback`）

```python
if feedback == "dislike" and query:
    _bg_tasks.add(asyncio.create_task(invalidate_cache_on_dislike(query)))
    _bg_tasks.add(asyncio.create_task(maybe_blacklist_on_dislike(query)))
```

- **invalidate_cache_on_dislike**：精确匹配 `query_normalized == nq` 软删 MySQL + SCAN `qa:*:{nq}` 删 Redis L1（防坏答案继续命中）
- **maybe_blacklist_on_dislike**：该 query 累计 dislike ≥ `OPTIMIZER_BLACKLIST_THRESHOLD`(2) → `SADD qa:cache:blacklist`（自动进黑名单，QA 层拦截）

## 五、auto_tune_cache_ttl（`feedback_optimizer_service.py:270`）

管理员点「🎚️ 缓存调优」触发，批量分析：
- **高频坏答案**（dislike ≥ 阈值）→ 归一化 query 批量 `SADD` 黑名单
- **高频好答案**（like ≥ 5）→ 标 TTL 延长候选（分析展示）

返回 `{blacklisted, extended, appliedBlacklist}`，前端调优结果卡展示。

## 六、generate_optimization_report（5 类建议）

`POST /system/optimizer/generate` 实时分析 dislike 模式，生成可执行建议：
| 类型 | 触发 | 建议 |
|---|---|---|
| `retrieval` | 单 query dislike ≥ 3 | 高频失分，加 golden 集/查文档覆盖/缓存预热 |
| `knowledge_gap` | 设备词被踩但无文档覆盖 | 上传该设备运维规程 |
| `cache` | 缓存命中率 < 20%（样本≥10）| 预热/扩容/开 semantic |
| `trend` | dislike 周环比 ≥ 1.2 | 排查近期文档变更/新设备 |
| `hallucination` | 检索差(poor/partial) 但 judge_halluc<0.3 | 疑似 LLM 编造（最危险），补文档/强 prompt 引用 |

> 建议是**可执行**的（具体动作），不是空话。命中率/趋势接真实指标（进程内 mirror + 周环比）。

## 七、feedback_stats（趋势看板，`feedback_service.py:142`）

`GET /qa/feedback-stats`（admin），聚合：
- 点赞/点踩分布 + dislikeRate
- 坏 case 设备聚类（术语表标准词匹配 query）+ 文档覆盖交叉（盲区）
- **一致性矩阵**（retrieval_quality × judge_halluc）：
  - `retrieval_good_answer_good` ✅ 正常
  - `retrieval_good_answer_bad` 🔧 生成问题
  - `retrieval_poor_answer_good` ⚠️ **LLM 编造**（检索差却答得好，最危险）
  - `retrieval_poor_answer_bad` ❌ 检索根因

## 八、黑名单手动管理（`system.py`）

- `GET /system/optimizer/blacklist` 列出
- `POST /system/optimizer/blacklist?query=` 手动加
- `DELETE /system/optimizer/blacklist?query=` 移除

前端优化建议 tab 有输入框 + 列表（点✕移除）。

## 九、可视化（Admin）

- **📈 优化建议 tab**：重新分析/缓存调优按钮 + 建议卡片 + 黑名单管理 + 调优结果
- **🔧 Query改写 tab**：改写质量（采纳率/否决率/缓存命中率 折线 + 策略饼图 + 分数散点 + 明细），数据来自 `rewrite_event` 表

## 十、关键配置

```python
OPTIMIZER_BLACKLIST_THRESHOLD = 2     # dislike 累计≥此值自动黑名单
OPTIMIZER_CACHE_HIT_FLOOR = 0.20      # 命中率低于此才出缓存建议
OPTIMIZER_MIN_SAMPLE = 10             # 缓存样本少于此不出建议
OPTIMIZER_TREND_RATIO = 1.2           # dislike 周环比≥此值预警
ONLINE_FAITHFULNESS_ENABLE = True     # dislike 异步 judge 开关
```

## 十一、踩坑

1. **黑名单数据链路**：原 `dislike≥3` 阈值对低频专业问答太苛刻（9 个 dislike 全分散各 1 次，永不达标）→ 降到 2 + 加自动触发（maybe_blacklist_on_dislike）+ 手动入口，三管齐下打通。
2. **bg task session**：invalidate/maybe_blacklist/judge 全用独立 `AsyncSessionLocal`，防请求 db close 时并发 500。
3. **优化建议硬编码噪音**：原 cache 建议无条件必出（没读命中率）→ 接进程内真实命中率（`metrics.cache_hit_rate`），样本足 + 低命中率才建议。
