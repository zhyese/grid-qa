# 实时主动运维、任务事件中心与知识治理

## 目标

本次增量把三条能力合成一条可恢复、可审计的闭环：外部实时事件进入统一模型，规则门禁触发只读 Agent，建议经人工确认后只能创建两票草稿；所有后台工作进入 MySQL 持久任务队列，业务变化写入 Outbox；检索和问答缓存统一执行知识时效硬门禁。

## 主链路

1. `POST /api/realtime/events` 接收 SCADA、OMS、PMS 或 generic 事件。
2. 连接器凭据绑定租户，`eventId + source + tenant` 负责幂等；设备映射把源设备 ID 归一为平台设备 ID。
3. 恢复、心跳和低等级事件仅归档；warning/major/critical 事件原子写入实时事件、Agent run、持久任务和领域事件 Outbox。
4. `proactive_ops.process` 使用 ALERT persona 做只读诊断，结果固定进入 `proposed`，不会执行遥控、拉合闸或停送电。
5. 有权限的人员确认或驳回建议；确认后可创建 `draft` 两票。`source_ref=proactive:<run_id>` 保证重复请求不重复建票。
6. worker 通过行锁领取任务，维护心跳，失败指数退避，耗尽后进入死信；服务重启会回收 stale 任务并接管业务 `running` 状态。

## 知识治理门禁

- 档案字段：责任人、区域、生效时间、失效时间、永久有效、复审时间、版本和生命周期状态。
- 扫描问题：缺失元数据、未生效、已过期、即将过期、到期复审，以及同主题知识中的否定冲突和数值阈值冲突。
- 问题状态：`open → confirmed → resolved/ignored`，每次审核保留操作人、说明和前后状态。
- 明确为 withdrawn、superseded、未生效或已过期的文档不能进入 LLM 上下文；Redis、MySQL 和语义缓存命中也会重新核验来源文档。
- 生产租户的治理存储异常采用 fail-closed，同时记录 `DEGRADED`，防止静默使用失效知识。

## 接入认证

单租户连接器配置：

```env
REALTIME_EVENT_CREDENTIAL_TENANT=default
REALTIME_EVENT_TOKEN=<随机长 token>
REALTIME_EVENT_SIGNING_SECRET=<独立 HMAC 密钥>
```

多租户连接器使用 JSON 映射：

```env
REALTIME_EVENT_TENANT_TOKENS={"tenant-a":"token-a","tenant-b":"token-b"}
REALTIME_EVENT_TENANT_SIGNING_SECRETS={"tenant-a":"secret-a","tenant-b":"secret-b"}
```

Token 模式发送 `X-Tenant-Id` 与 `X-Event-Token`。HMAC 模式发送 `X-Tenant-Id`、`X-Event-Timestamp`、`X-Event-Signature`；签名原文为 `timestamp + "." + tenant + "." + raw_body`，算法为 HMAC-SHA256。平台 JWT 用户只能写入自身租户。

示例事件：

```json
{
  "eventId": "SCADA-20260716-001",
  "source": "scada",
  "eventType": "temperature_alarm",
  "severity": "major",
  "occurredAt": "2026-07-16T09:30:00+08:00",
  "title": "1号主变油温越限",
  "summary": "顶层油温达到 92℃",
  "payload": {"deviceId": "T1_main_transformer"},
  "measurements": {"oilTemperature": 92}
}
```

## 运维入口

- 前端 `/operations`：处置建议、实时事件、设备映射、持久任务和领域事件投递轨迹。
- 前端 `/knowledge-governance`：治理档案、扫描、冲突证据和人工审核。
- `/api/system/task-center/*`：任务/事件查询、统计、死信重试与终止。
- `/api/knowledge-governance/*`：文档档案、扫描、问题列表、统计与审核。

数据库新表由 `create_all` 创建；现有库的 `alert_disposal.tenant_id`、`tickets.source_ref` 及相关索引由启动期幂等迁移补齐。
