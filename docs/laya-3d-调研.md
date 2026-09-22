# LayaAir 引擎引入调研——3D 态势赋能决策建议（2026-09-21）

## 0. 澄清结论

"laya" 经全网检索确认为 **LAYABOX 的 LayaAir 引擎**（不存在名为 laya 的 AI/决策类新技术）。
LayaAir：国产开源全平台 3D 引擎（2D/3D/VR/AR），3.x 起带完整可视化编辑器工具链（场景/材质/
物理/蓝图），WebGPU 优先渲染，TS 开发，一次开发发布 Web/小游戏/原生 APP。

## 1. 与本项目的关系：引擎不产生决策，引擎呈现决策

本项目的决策建议能力已存在于后端链路（RAG 问答 / 主动运维 Agent 建议 / 故障诊断 /
演练评分复盘 / 运维报告），缺口在**决策的 3D 态势呈现与沉浸式演练交互**——这正是
LayaAir 能切入的位置。现有 3D 资产：Three.js 自研 1644 行（deviceFactory + sceneEnvironment）
+ DigitalTwin.vue 597 行（布局 JSON 驱动、WS 告警定位、故障链高亮）。

## 2. 适配性分析

| 维度 | LayaAir | 现状 Three.js | 对本项目影响 |
|---|---|---|---|
| 定位 | 完整游戏引擎+IDE | 3D 图形库 | 本项目是数据驱动轻量孪生，非重度游戏 |
| 编辑器 | ✅ 可视化搭场景/材质/蓝图（最大优势） | ❌ 纯代码 | 复杂场景搭建立效率高数倍 |
| Vue 集成 | 脚手架级（laya-webpack-ts），非组件生态 | 原生契合（TresJS 声明式可选） | LayaAir 页面需独立工程/iframe 嵌入 |
| 渲染 | WebGPU 优先，宣称数倍于 WebGL | WebGL | 当前场景规模（单站 ~百设备）无瓶颈 |
| 多端发布 | ✅ 原生 APP/小游戏 | 仅 Web | **填补本项目移动端空白（PWA 未做）** |
| 生态 | 国内为主，国际社区弱 | 全球最流行 | 长期招人/求助 Three.js 更容易 |
| 构建链 | 需引入 IDE 产物与引擎运行时 | 现有 Vite 链 | 镜像构建与离线部署需评估 |

## 3. 推荐路线：试点切入，不替换存量

### 路线 A（推荐）：演练沙箱 3D 态势页（LayaAir 试点 PoC）
- **场景**：演练进行中，时间轴事件在 3D 变电站里播放——故障设备红闪、传播链沿连线蔓延、
  演练者的处置动作（隔离/汇报）在 3D 场景里落位标注；复盘时回放整场推演。
- **为什么是它**：①独立新页面，不动现有孪生（零回归风险）；②数据全现成
  （DrillRun.propagation 时间轴 + station_layout JSON + checklist）；③"决策赋能"闭环最直观
  （演练=决策培训，3D 态势=决策上下文）；④可充分验证 LayaAir 编辑器搭场景的效率红利。
- **集成形态**：LayaAir 独立子工程（TS）构建产物挂 `/drill3d` 路由下 iframe 嵌入，
  通过 postMessage 与 DrillSandbox.vue 互通（事件推送/操作回传），与主工程解耦。
- **工作量估算**：PoC 约 5-8 人天（含场景搭建、事件驱动动画、iframe 桥）。

### 路线 B（后续）：LayaAir 原生 APP 发布补移动端
- 官方"一次开发多端发布"可把 3D 态势页（甚至问答壳）打成 APP/平板端，
  对应增量报告里一直未做的"移动端/PWA"空白。待路线 A 验证后再决策。

### 路线 C（否决）：替换现有 Three.js 孪生
- 1644 行自研模块已稳定支撑告警定位/故障链，替换收益（编辑器/性能）不抵
  迁移成本与回归风险。现有孪生维持 Three.js，两者长期并存各司其职。

## 4. 风险与对策

1. **构建/部署复杂化**：LayaAir 产物进 frontend/dist 或独立静态目录，Dockerfile 增加产物拷贝；
   离线环境需确认引擎运行时可本地化（不依赖 CDN）。
2. **学习成本**：TS + 引擎组件/ECS 心智与 Vue 不同；限定 1-2 人维护 3D 子工程，其余成员无感。
3. **生态锁定**：引擎 API 演进由 Layabox 主导；事件桥（postMessage）保持 3D 层可替换
   （数据契约 JSON 化，未来可换 Babylon/Unity WebGL 而不动后端）。
4. **性能**：WebGPU 在低配内网机可能回退 WebGL，PoC 需在目标机器实测帧率。

## 5. 验收标准（PoC 达标线）

- 演练事件（tOffset 时间轴）驱动 3D 场景动画：故障闪红 ≤1s 延迟
- 传播链沿 connection 连线蔓延动画 ≥2 级设备
- 处置打卡动作在 3D 场景标注落位
- 复盘回放：整场推演可重放
- iframe 桥双向事件 ≤200ms；目标内网机帧率 ≥30fps

## 6. 结论

引入 LayaAir **可行且有明确增量**（编辑器效率 + 多端发布 + 沉浸式演练），但必须以
**试点页面（路线 A）**方式切入、与现有 Three.js 孪生并存，禁止直接替换。决策赋能的
本体仍是既有的 RAG/Agent/演练链路，LayaAir 负责"让决策看得见、让演练像现场"。

## 参考

- [LayaAir 3.x 官方文档](https://layaair.com) · [引擎介绍](http://ldc2.layabox.com)
- [laya-webpack-typescript（Vue 工程化集成脚手架）](https://github.com)
- [TresJS（Vue×Three.js 声明式方案，路线 C 备选）](https://tresjs.org)
- [Three.js 与前端框架集成指南](https://discoverthreejs.com/zh/book/introduction/threejs-with-frameworks)
- 行业参照：能源电力行业数字孪生 18 案例（中国电力企业管理网）；变电站孪生"可视化→可计算"趋势（中国日报科技）
