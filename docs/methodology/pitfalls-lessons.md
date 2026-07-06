# 踩坑集（项目经验）

> 项目开发过程中踩过的坑 + 根因 + 解法。避免重复踩。

## 一、监控链路三坑（Grafana 面板 No data / 死锁 / 空值）

1. **prometheus_client 事件驱动指标未触碰前在 /metrics 隐身**
   - 现象：FEEDBACK/CRAG/ROUTING 等指标在 Grafana 显示 No data，像"后端没打通"
   - 根因：prometheus_client 只在被 `.inc()/.observe()/.set()` 触碰后才输出该 label 序列
   - 解法：`init_metric_series()` 启动时把已知 label 预置 `.inc(0)`（`metrics.py`）

2. **Grafana provisioning `deprecatedInternalID` 冲突死锁**
   - 现象：Grafana 13 provisioning 卡死/面板不加载
   - 根因：dashboard JSON 的 deprecatedInternalID 冲突
   - 解法：清 `rag_grafana-data` 卷重建（`docker volume rm` + 重启 grafana）

3. **`component_health` 原只在 /health 刷新**
   - 现象：基础组件健康看板常驻空值（没人调 /health 就不刷）
   - 根因：健康检查只在 /health 请求时更新 gauge
   - 解法：后台 30s 周期探活任务 `_refresh_component_health_loop`（`main.py` lifespan）

## 二、SQLAlchemy session 并发（bg task 500）

- **现象**：dislike 触发的后台失效缓存任务导致 `/qa/feedback` 接口 500
- **报错**：`IllegalStateChangeError: Method 'close()' can't be called here; _connection_for_bind already in progress`
- **根因**：`invalidate_cache_on_dislike(db, query)` 用的是**请求 db session**，bg task 异步跑时请求结束 close session，bg task 还在用同一 session commit → 状态冲突
- **解法**：所有 bg task（失效/记事件/maybe_blacklist/judge）改用独立 `AsyncSessionLocal()`，不共享请求 db

## 三、认证响应封装（HTTP 恒 200）

- **现象**：测试断言 `r.status_code == 401` 失败（实际 200）
- **根因**：项目统一响应封装（`core/response.py`）——**HTTP 恒 200**，认证/业务失败体现在 body 的 `code` 字段（401/400/500）
- **解法**：测试断言 `r.json()["code"] == 401`（不是 `status_code`）。curl 排查要看 body 不是 HTTP code
- **注意**：backend 启动过程中认证中间件未就绪时会临时放行（200 + code 200），完全启动后才 401——测试要等 backend ready

## 四、ticket-audit 分支引入全局 500（已回滚）

- **现象**：`feat/ticket-audit` 分支加载后 `/qa/answer/stream` 全局 500，后端挂
- **根因**：未定位（疑 metrics 副作用）
- **处理**：2026-07-01 main 回滚到 cb48d8c
- **教训**：重启该分支前必须先复现 500 读 traceback 再合并。分支功能再好，引入 500 就是 3.25

## 五、子 agent 派不出（glm-5 网关）

- **现象**：本机 Claude Code 跑在 glm-5 网关，Agent 工具派子 agent 一律报"模型不存在"
- **影响**：superpowers SDD（subagent-driven-development）/ 多 agent workflow 不可用
- **解法**：只能 **inline 执行**（不派子 agent）。项目 deepseek/百炼云 provider 是另一条管道，不能驱动子 agent

## 六、缓存失效前缀匹配误删（已修）

- **现象**：dislike 失效缓存用 `query.like(f"{query[:20]}%")` 前缀匹配，可能误删（前 20 字相同但问题不同）或漏删（raw query 与缓存存的 normalized_query 不对齐）
- **根因**：前缀匹配与 MySQL 缓存的 `query_hash`(MD5) 精确匹配机制不对齐
- **解法**：改 `query_normalized == nq` 精确匹配 + SCAN `qa:*:{nq}` 删 Redis L1

## 七、改代码忘 rebuild（最常见）

- **现象**：改了后端代码，重启容器后"没生效"
- **根因**：源码 bake 进镜像（无 bind mount），`docker compose restart` 只重启不重建镜像
- **解法**：`docker compose up -d --build backend`（必须 `--build`）。详见 `rebuild-after-change` skill

## 八、优化建议硬编码噪音（已修）

- **现象**：优化建议每次必出一条"缓存策略建议"，与真实命中率无关
- **根因**：原 `try: from app.core import metrics; suggestions.append(...)` 的 try 块只 import 不读指标，无 if 守卫
- **解法**：接进程内真实命中率（`metrics.cache_hit_rate()`），样本≥10 + 命中率<20% 才建议

## 九、modelType="default" 致 LLM 500（已修）

- **现象**：modelType="default" 的问答缓存命中 OK，但 miss 重算时 500
- **报错**：`ValueError: 未知 LLM_PROVIDER: default`
- **根因**：seed 数据用 modelType="default" 存了缓存，但 `get_llm_provider` 的 `provider or settings.LLM_PROVIDER` 短路——"default" 非空直接撞 raise
- **解法**：`get_llm_provider` 把占位值（default/auto）也回落到 `settings.LLM_PROVIDER`

## 十、Git Bash MSYS 路径转换（docker exec 容器路径）

- **现象**：`docker exec grid-backend ls /app/tests` → `ls: cannot access 'C:/Program Files/Git/app/tests'`
- **根因**：Git Bash MSYS 把 `/app/tests` 当 Windows 路径转换
- **解法**：`export MSYS_NO_PATHCONV=1` 禁用转换；容器内 pytest 用 `python -m pytest`（加 cwd 到 path，否则 `No module named app`）

## 十一、Alembic 不在容器 PATH

- **现象**：`docker exec grid-backend alembic ...` → `executable not found`
- **解法**：开发环境用 `Base.metadata.create_all` 兜底建表；或 `docker exec grid-backend python -m alembic`（待确认）

## 十二、改写评估/事件 Python 环境差异

- **现象**：本机 pytest 缺依赖（pymilvus/sqlalchemy 等），跑不了
- **解法**：容器跑 pytest（`docker cp tests grid-backend:/app/tests` + `python -m pytest`）。注意 `MSYS_NO_PATHCONV=1` + 每次清 `/app/tests` 再 cp（防嵌套 `/app/tests/tests`）
