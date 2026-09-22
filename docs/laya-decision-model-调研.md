# Laya 决策模型（System 1）引入调研——赋能运维决策建议（2026-09-21）

> 更正：本报告替换此前误题的《laya-3d-调研.md》（那份调研的是 LayaAir 3D 引擎，
> 非用户所指）。Laya 实为 Convai Innovations 2026-09 新发布的开源决策模型。

## 1. Laya 是什么（一手来源：HF 模型卡 convaiinnovations/laya）

- **定位**：System 1 决策引擎——BERT 类判别式（非自回归、不生成文本、零幻觉），
  输入「状态 + 类型化问题」→ 输出**带校准概率的决策**；单次前向 33ms（GPU）/
  193-464ms（CPU 预加载后），100+ 语言，Apache 2.0，可自部署。
- **三种问题原语**（决策 schema 请求时定义，无需重训）：
  - `choice`：选项分类（选项自带；>20 选项能力下降）
  - `score`：有序打分 0-N（官方自述最弱原语）
  - `noul`：yes / no / unsure 三态概率（对无法判定的问题显式输出 unsure）
- **检查点**：`laya`（英文 ModernBERT-large 421M）/ `laya-multilingual`
  （mmBERT-base 322M，含中文）/ `laya-typed-decisions`（决策微调版，硬标签 0.766）。
- **训练法**：RLCD（RL from Calibrated Decisions）+ proper scoring rules。

## 2. 官方诚实限制（决定接入策略，必须正视）

1. **base 检查点零样本决策≈随机**（typed-decisions 上 0.34-0.36，低于多数类基线 0.461）。
   官方原话："a fast base to specialise, not a zero-shot decision engine"——
   必须用自己的数据微调（官方提供微调 notebook）。
2. **出厂过度自信**：需在自己数据上做温度缩放校准（ECE 0.466 → 校准后 0.081）。
3. `laya-multilingual` 在英文决策基准上同样近随机；中文决策能力**未验证**，
   需以本项目数据微调后实测。
4. 发布仅数日：社区检验不足、API 可能变动。

## 3. 与本项目决策场景的映射（三原语 → 运维决策点）

| 项目决策点 | 现状 | Laya 原语 | 增量价值 |
|---|---|---|---|
| 告警分诊（RealtimeEvent 进门） | 规则 rule_decision + LLM 深诊（秒级耗钱） | choice(处置类别)+score(严重度)+noul(是否立即人工) | **毫秒级前置闸门**：省低价值 LLM 调用 |
| 两票审核预筛（ticket_audit） | LLM 全量审（有得分） | choice(通过/驳回/人工)+noul(安措完备?) | 低风险快过、高风险才走 LLM 深审 |
| 主动运维建议确认（confirm/reject） | 纯人工 | noul/choice 概率参考 | 给运维人员第二意见（非替代） |
| 演练动作匹配（checklist） | 关键词 substring | choice(动作↔清单项语义匹配) | 同义表述也能命中（评分更公平） |
| query 路由（query_classifier 决策树） | 规则 | choice(检索路径) | 语义级路由升级 |
| 批注/反馈分类 | 无/规则 | choice(紧急度/是否转治理) | 数据飞轮自动分流 |

**定位原则**：Laya 做高频、低延迟、结构化的 System 1 预判；LLM（qwen/deepseek）
保持 System 2 深推理。两者互补，不替换。

## 4. 接入架构（遵循仓库既有范式）

- `pip install laya`（注意 USE_TF=0）；模型仿 bge 模式：宿主 HF 缓存 + 容器
  `HF_HUB_OFFLINE=1` 只读挂载（新增 laya 模型目录到挂载清单）。
- 新服务 `app/services/decision_model_service.py`：懒加载 Router(preload)（用
  laya-multilingual），`predict(state, questions) -> dict`；模型不可用 **degraded 降级
  返回 None 走原路径，绝不阻断主链路**（仓库铁律）。
- 开关 `LAYA_DECISION_ENABLE`（默认 False=现状）；决策结果只作建议透出，
  不自动执行任何设备/工单动作（与主动运维"人工确认"边界一致）。
- 部署位：backend 容器内 CPU 推理（322M base，预计 <500ms 可接受）。

## 5. 落地路径（关键：先微调 PoC，再接链路）

**阶段 0（PoC，1-2 周）——决定去留**：
1. 下载 laya-multilingual；用存量数据构造训练集（告警→实际处置、两票→审核结论，
   两类数据项目已有历史积累）。
2. 微调 + 温度缩放校准；留出时间切分的验证集，与两条基线对比：
   规则基线（现状 rule_decision）与多数类基线。
3. **验收线：中文分诊准确率显著超规则基线（≥+10pp）且 ECE<0.15、CPU<500ms；
   任一不达 → 放弃引入，写结论文档归档。**

**阶段 1（试点）**：仅接「告警分诊」一个点——Laya 结果作为 `预判` 字段挂在
RealtimeEvent 处置流上供 Agent 与运维人员参考，A/B 观察一周对 confirm 率/
平均处置时长的影响。

**阶段 2（推广）**：达标后再扩两票预筛、演练匹配等；每次扩点都带对照评估。

## 6. 风险清单

| 风险 | 对策 |
|---|---|
| 零样本不可用（官方明示） | 阶段 0 微调 PoC 前置为硬门禁 |
| 中文决策能力未验证 | PoC 用本项目中文数据实测，不达标即止损 |
| 模型发布仅数日，API 不稳 | 服务层封装隔离（decision_model_service），可整体摘除 |
| 微调数据量需求未知 | 先用告警处置史（量大）打样，不足再评估 |
| CPU 推理占用后端资源 | 懒加载+并发护栏；必要时限流/独立 sidecar |

## 7. 结论

Laya 与本项目「决策建议」诉求**方向高度契合**（毫秒级本地决策、概率校准、
noul 三态天然适配运维"无法判定→转人工"），但它**不是即插即用**：base 零样本
不可信，必须走「自有数据微调 → 校准 → 单点试点 → 对照达标 → 推广」的严谨路径。
建议立即启动阶段 0 PoC（数据已有、成本 1-2 周），验收线不过则果断放弃。

## 参考来源

- [HuggingFace 模型卡：convaiinnovations/laya](https://huggingface.co/convaiinnovations/laya)（架构/限制/校准数据均出自一手模型卡）
- [Laya 官网：33ms Multilingual System 1 Decision Engine](https://laya.convaiinnovations.com)
- [GitHub：NandhaKishorM/laya](https://github.com/NandhaKishorM/laya)
- [Hacker News 社区讨论](https://news.ycombinator.com)
- [搜狐：开源模型 Laya 发布报道](https://m.sohu.com)
