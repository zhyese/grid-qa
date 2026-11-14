# 功能链路收口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development. 步骤必须先失败测试，再实现，再验证。

**Goal:** 从已审计断点开始，恢复可信数据基础并修正知识变更联动，不把服务健康等同于业务闭环。
**Architecture:** 保留现有 FastAPI/Vue/MySQL/Milvus/Neo4j/Redis。历史记录白名单增量恢复；应用侧将知识变更后的 BM25、缓存失效和审核门禁显式化。
**Tech Stack:** Python async / SQLAlchemy / pytest / Vue / Node tests / Docker（E盘）。
**Spec:** 本聊天上一轮全链路审计及用户“开始做吧”授权。

## Global Constraints

- 不清空向量/图谱，不覆盖今天新数据；所有业务恢复前保存快照。
- 不恢复用户权限、旧待执行任务、旧消息投递队列；不自动执行工单或现场控制。
- 只有人工审核可以发布 AI 补全知识；保留人工创建/确认入口。
- 不改第三方依赖版本，不向 C 盘安装或迁移数据。
- 现有未提交改动保留。当前分支 codex/kg-batch-extraction 继续工作，不自动提交用户文件。

## Review Focus

- 旧备份字段差异、NULL约束、中文字符集，恢复不能截断丢数据。
- 当前与历史主键冲突、对话用户关联、恢复中断后的幂等性。
- AI草稿不能未经审核进入正式索引/高置信缓存。
- 回流/撤回后 BM25 与答案缓存不能保留旧内容。
- 测试与生产隔离：单元测试内存SQLite，不访问真实数据。

## Task 1：数据基础（主代理）

- [x] 核验 BGE 缓存缺失原因；从已在 E 盘的 blob 补齐 snapshot 文件引用，离线向量测试。
- [x] 当前数据库快照保存到 E 盘；原备份导入独立 staging 库（utf8mb4）。
- [x] 输出白名单业务表字段/冲突差异，只合并兼容记录，保留新数据和审核状态。
- [x] 对账 MySQL/Neo4j/Milvus，不能用少量 MySQL 强制覆盖完整 Neo4j。

## Task 2：审核入口（独立实现代理）

Files: evidence_gap_service.py, main.py, relevant tests。
- [x] 测试自动补全只生成待审核草稿，不调用 confirm_and_sync。
- [x] 修正定时路径与可观测结果；人工确认入口不变。
- [x] 明确注册治理订阅，不依赖用户偶然打开管理端点。

## Task 3：知识更新后处理（独立实现代理）

Files: knowledge_evolution_service.py, knowledge_backbone.py, new knowledge_change_service.py, relevant tests。
- [x] 测试回流/撤回触发 BM25 dirty 与文档缓存失效。
- [x] 解决共享AI文档各草稿parent分组混合及删除后chunk_idx复用。
- [x] 新知识变更辅助函数明确记录失败，不静默吞错；保持架构边界。

## Task 4：页面真实结果与端到端验证（主代理）

- [x] 文档页面拒绝把业务错误当成功；用Node测试证明。
- [x] 全套pytest、前端构建、golden校验、独立审查。
- [x] 仅重建 backend/frontend，验证真实文档检索/图谱消费及安全草稿流程。

## 后续阶段（本轮不以单元测试冒充完成）

统一文档发布状态机及版本淘汰；设备遥测自动形成告警；执行结果提炼为待审核案例并回流。每项需独立真实业务验收。

## 本轮验收与限制

- BGE迁移缺失11个snapshot链接，用E盘现有blob物化；未重新下载，离线512维向量与运行中预热均成功。
- 当前回滚备份：data/backups/pre_chain_commit_20261112.sql（停机后快照）；原始备份与staging恢复库保留。
- 白名单历史增量合并结果见reports/chain-restore-20261112.json；仅default租户和现有用户名关联的对话进入当前库，不恢复旧用户、权限、任务和事件投递队列。
- 旧alert_disposal整数ID偏移1003并同步proactive引用；旧running/queued运行标成failed并注明人工复核；原始状态留在staging。恢复后对话消息、图谱文档、run事件和告警关联均无孤儿。
- 另从Neo4j恢复32条有现存来源文档的关系（候选快照reports/neo4j-recovery-candidates-20261112.json）。MySQL现11176条、去重10825；Neo4j11002条，剩余177条缺来源文档，原样保留待核验。13份缺MySQL元数据的Milvus文档也未猜测归属恢复。
- 独立审查2个P1+1个P2均补测试修复：Redis失败恢复后代际追平；semantic/QA/SSE/Agent/NLI/L2跨代际旧请求不再写新缓存；部分上传只重试失败项。
- 缓存代际当前全局共享，知识变化会导致其他租户精确缓存冷失效。Redis持续不可用直到进程退出时，不宣称持久失效保证；多backend进程代际广播仍为后续工程项。
- 最终pytest：1145 passed, 1 skipped, 37 deselected（5项既有警告）；独立真实Redis Lua额外验证1通过；前端Node8通过；Vite生产构建成功；golden109条通过。
- 真实检索：3条证据同时来自BM25/BGE，图谱上下文8条。
- HTTP问答：code200、4个来源、图谱8条；SSE首答done正常、394字符、图谱8条，重问meta4个来源、Redis命中、无error事件。
- 运行中定时补全实测4份ai_drafted、synced=0，文档总量保持164，没有自动发布AI草稿。
- 千问LLM/embedding欠费或账户异常、豆包端点不可用仍未解决；DeepSeek可用。云重排/云向量会降级，接口确实上报degraded=true，不能标为全链路全绿。
- 本轮只完成上述首批修复，不代表已完成统一文档发布、全部版本淘汰、遥测告警和处置案例回流。
