# Laya 决策模型落地方案——告警分诊 PoC 与数据冷启动（2026-09-21）

> 前置调研：docs/laya-decision-model-调研.md（Laya = 非自回归 System 1 决策模型）。
> 本方案为可执行 spec：数据不足已实测确认，冷启动为第一支柱。

## 0. 数据现状（实测 2026-09-21，决定方案形态）

| 数据源 | 存量 | 可用性 |
|---|---|---|
| realtime_event（告警） | 51 条（带设备名仅 4） | ❌ 量级不足 |
| proactive_ops_run 人工反馈 | confirmed 1 / rejected 1 / 转票 0 | ❌ 几乎为零 |
| alert_disposal / tickets | 153 / 12（完成 2） | ❌ 同上 |

**结论**：有效人工标注 <20 条，无法直接微调。方案 = 三源数据冷启动 + 微调 PoC +
单点集成 + 反馈回流飞轮。

## 1. 目标与验收线

- 业务目标：告警进门毫秒级产出「处置类别 + 严重度 + 是否需立即人工」三路预判，
  为 Agent 深诊与运维人员提供前置参考；省低价值 LLM 调用。
- PoC 硬门禁（不过即止损归档）：
  1. 合成留出集 choice 准确率 ≥ 规则基线（fault_prediction_service 现行规则）+10pp
  2. golden 规程问答集外推：分诊类别与规程处置方向一致率 ≥80%
  3. 校准后 ECE <0.15（温度缩放，官方流程）
  4. CPU 单次 <500ms（backend 容器内实测）

## 2. 总体架构

```
[冷启动数据流水线]                    [训练/校准]              [推理服务]           [集成点]
规程知识库(golden/文档) ─┐
LLM 合成蒸馏(qwen) ─────┼→ 合成训练集 → laya-multilingual 微调 → 温度校准 → checkpoint
规则弱监督(现有规则引擎) ─┘        (scripts/laya_finetune.py) (laya_calibrate.py)   │
                                                                       ▼
                                              app/services/decision_model_service.py
                                              (懒加载+to_thread+degraded 降级,默认关)
                                                                       │
                                              realtime_event_service.ingest_event
                                              → ai_triage_json 列 → 主动运维页徽章
                                                                       │
                                              人工 confirm/reject/处置 ──→ 回流新标注
                                              （数据飞轮：真实数据持续替换合成数据）
```

## 3. 数据冷启动规格（M0）

### 3.1 三问题原语 schema（一次定义，训练与推理共用）
```json
state 模板: "电网告警｜设备:{canonical_device_name}｜站点:{station}｜等级:{severity}｜
             标题:{title}｜摘要:{summary[:200]}"
questions: [
  {"id":"action","kind":"choice",
   "options":["持续观察","通知调度","隔离故障设备","转检修工单","紧急停运"]},
  {"id":"urgency","kind":"noul","question":"是否需要立即人工介入？"},
  {"id":"severity","kind":"score","max":4,"question":"综合严重程度(0-4)？"}
]
```

### 3.2 三源数据
1. **LLM 合成蒸馏（主力，目标 ≥3000 条）**：scripts/laya_synth_data.py——
   以规程文档 chunk + golden 问答为素材，按「设备类型 × 故障模式 × 严重度 × 表述变体」
   笛卡尔采样生成告警 state，由 qwen（System 2）按规程打三路 gold 标签；
   每批抽 5% 人工抽检，不一致率 >10% 则修 prompt 重生成。成本：约 3k 次 LLM 调用。
2. **规则弱监督（≥500 条）**：复用 fault_prediction_service 的
   _severity_weight/_risk_level/_suggestion 对合成 state 打平行标签，
   与 LLM 标签交叉验证——分歧样本进入人工抽检池。
3. **存量真实（<20 条）+ 线上回流（长期）**：全部进验证集（不进训练集）；
   阶段 2 上线后每次人工 confirm/reject/处置选择 emit 质量事件（source=laya-triage）
   落库回流，真实数据占比随时间上升。

### 3.3 数据切分
按「设备类型」分层切 8:1:1（train/val/test），test 另加 golden 规程外推集 50 条。

## 4. 训练与校准（M1）

- 环境：宿主机 venv 或免费 Colab（mmBERT-base 322M，CPU 可微调，batch 16 × 3-5 epoch）。
- scripts/laya_finetune.py：官方微调流程封装（HF Trainer），输入 3.1 训练集 JSONL，
  产出微调检查点 → 上传宿主 HF cache（`~/.cache/huggingface/hub/`，容器已只读挂载，
  **无需改 compose**）。
- scripts/laya_calibrate.py：val 集温度缩放扫参，温度值与 ECE 存检查点同目录
  `calibration.json`，推理时加载。
- scripts/laya_eval.py：三基线对照（多数类 / 现行规则 / 微调后）+ ECE + 混淆矩阵，
  报告落 reports/laya_eval_*.md（评测矩阵页可浏览）。

## 5. 推理服务（M2，遵循仓库铁律）

```python
# app/services/decision_model_service.py（新增）
- LAYA_DECISION_ENABLE: bool = False      # config.py，默认关=现状
- LAYA_MODEL_DIR / LAYA_TIMEOUT(1.0s) / LAYA_MAX_CONCURRENCY(2)
- 懒加载：首次调用 to_thread 加载 Router(preload)；HF_HUB_OFFLINE 兼容
- async predict(state, questions) -> dict | None
    开关关 → None（零开销）；超载/超时/异常 → degraded("laya_decision", e) + None
    （调用方 None 即走原路径，主链路绝不阻断）
- 校准温度在输出层应用；ECE 指标随 degraded 体系可观测
```

## 6. 集成设计（M2 单点：告警分诊）

- 挂点：`realtime_event_service.ingest_event` 内 rule_decision 判定后、
  主动运维任务派发前——`asyncio.gather` 并行预测不增加入库延迟。
- 存储：realtime_event 幂等补列 `ai_triage_json TEXT NULL`
  （init_db._COLUMN_MIGRATIONS 现成机制，免迁移）：
  `{"action":"隔离故障设备","actionProb":0.71,"urgency":"yes","urgencyProb":0.83,
     "severity":3,"model":"laya-ft-v1","latencyMs":412}`。
- 透出：OperationsCenter 事件卡片/建议详情加「AI 预判」徽章（类别+概率+
  unsure 态灰色展示）；Agent 诊断 prompt 可选注入（开关二级门禁）。
- **红线**：预判仅作参考展示，不自动执行任何设备/工单动作（与主动运维
  人工确认边界一致）；预判错误不拦截告警原有流转。

## 7. 评估与推广（M3）

- 离线：laya_eval 报告（M1 产出）+ 每季度重训重评。
- 在线 AB：开关按 tenant/事件哈希分流，观察 4 周——人工 confirm 率、
  平均处置时长、预判-人工决策一致率；回流标注量/一致性趋势。
- 推广次序（各带独立验收）：两票审核预筛 → 演练动作语义匹配 → query 路由。

## 8. 里程碑与工作量

| 里程碑 | 内容 | 工作量 | Go/No-Go |
|---|---|---|---|
| M0 数据流水线 | 合成脚本+3.5k 数据+抽检达标 | 3-4 人天 | 抽检不一致率 ≤10% |
| M1 微调+校准 | 训练+温度校准+三基线报告 | 2-3 人天 | 验收线 1/2/3 |
| M2 服务+单点集成 | 服务/补列/挂点/徽章/单测/自测 | 3-4 人天 | 验收线 4 + 全量回归 |
| M3 试点 AB | 4 周观察+回流分析 | 1 人天+观察期 | confirm 率不降 |

## 9. 风险与回退

- 合成分布偏移 → golden 外推集把关；线上真实回流持续纠偏。
- laya 包/模型迭代（发布仅数日）→ 服务层隔离，checkpoint 目录化版本管理，
  可整体摘除（开关关即回现状，ai_triage_json 列留存无害）。
- CPU 资源挤占 → 并发护栏 2 + 1s 超时；超限直接跳过（可观测）。
- 微调后仍不达标（中文能力天花板未知）→ M1 门禁止损，结论归档
  docs/laya-decision-model-调研.md 附记，零沉没成本（仅数据脚本可复用）。

## 10. 与既有体系的一致性

- 开关默认 False、degraded 不 crash、幂等补列、质量事件回流、评测报告落
  reports/ 供评测矩阵页浏览——全部对齐仓库现行范式。
