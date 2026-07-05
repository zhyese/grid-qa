"""LLM 成本追踪 & 配额管理：按用户/租户/模型维度追踪 token 消耗 + Admin 看板 + 配额限制。

数据流：qa_service 调 LLM → 记录 token 用量到 cost_log 表 → 定时刷新到 Redis 计数器
       → 配额阈值检查（超限拒绝请求）

指标：COST_TOKEN_TOTAL Counter(user, tenant, model, type)
      COST_QUOTA_EXCEEDED Counter(user, tenant)
"""
import json
import time
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.obs import degraded

# token → 人民币 近似换算（各模型单价）
_MODEL_PRICES = {
    "deepseek": {"input": 0.0005 / 1000, "output": 0.002 / 1000},    # ¥/token
    "qwen": {"input": 0.001 / 1000, "output": 0.002 / 1000},
    "doubao": {"input": 0.0008 / 1000, "output": 0.0015 / 1000},
}

# 默认配额（月 token 上限）
_DEFAULT_QUOTA = {
    "user": 1_000_000,       # 每个用户每月
    "tenant": 10_000_000,    # 每个租户每月
}


async def record_token_usage(
    db: AsyncSession, username: str, tenant: str,
    model: str, input_tokens: int, output_tokens: int,
    query_type: str = "qa",  # qa / diagnose / ticket / debate / rewrite
) -> None:
    """记录一次 LLM 调用的 token 消耗。"""
    total_tokens = input_tokens + output_tokens
    # 估算费用
    prices = _MODEL_PRICES.get(model, {"input": 0.001 / 1000, "output": 0.002 / 1000})
    cost = (input_tokens * prices["input"] + output_tokens * prices["output"])
    try:
        from app.db.session import AsyncSessionLocal
        from app.models.operation_log import OperationLog
        async with AsyncSessionLocal() as cost_db:
            cost_db.add(OperationLog(
                operate_user=username or "unknown",
                operate_type="cost_track",
                content=json.dumps({
                    "tenant": tenant, "model": model,
                    "inputTokens": input_tokens, "outputTokens": output_tokens,
                    "totalTokens": total_tokens, "cost": round(float(cost), 6),
                    "queryType": query_type, "ts": datetime.now().isoformat(),
                }, ensure_ascii=False)[:1000],
            ))
            await cost_db.commit()
    except Exception as e:
        degraded("cost_record", e)

    # 更新 Redis 计数器（get_redis() 拿异步连接做 pipeline）
    try:
        from app.clients import redis_client
        today = date.today().isoformat()
        keys = [
            f"cost:user:{username}:{today}",
            f"cost:tenant:{tenant}:{today}",
            f"cost:user:{username}:monthly",
            f"cost:tenant:{tenant}:monthly",
        ]
        pipe = redis_client.get_redis().pipeline()
        for k in keys:
            pipe.incrby(k, total_tokens)
            pipe.expire(k, 86400 * 31)
        await pipe.execute()
    except Exception as e:
        degraded("cost_redis_incr", e)


async def check_quota(username: str, tenant: str) -> dict:
    """检查是否超配额。返回 {"allowed": bool, "reason": str}。"""
    try:
        from app.clients import redis_client
        r = redis_client.get_redis()
        user_key = f"cost:user:{username}:monthly"
        tenant_key = f"cost:tenant:{tenant}:monthly"
        user_usage = int(await r.get(user_key) or 0)
        tenant_usage = int(await r.get(tenant_key) or 0)
        user_quota = _DEFAULT_QUOTA["user"]
        tenant_quota = _DEFAULT_QUOTA["tenant"]

        if user_usage >= user_quota:
            return {"allowed": False, "reason": f"用户 {username} 月配额已超（{user_usage}/{user_quota}）"}
        if tenant_usage >= tenant_quota:
            return {"allowed": False, "reason": f"租户 {tenant} 月配额已超（{tenant_usage}/{tenant_quota}）"}
        return {"allowed": True, "reason": ""}
    except Exception as e:
        degraded("cost_quota_check", e)
        return {"allowed": True, "reason": "配额检查异常（已放行）"}


async def get_cost_report(db: AsyncSession, period: str = "today") -> dict:
    """成本报告（Admin 看板）。"""
    today = date.today().isoformat()
    try:
        # token 数据由 record_token_usage 落 operation_logs（operate_type=cost_track）
        from sqlalchemy import text

        # 今日总 token
        today_total = (await db.execute(
            text("SELECT SUM(CAST(JSON_EXTRACT(content, '$.totalTokens') AS SIGNED)) "
                 "FROM operation_logs WHERE operate_type='cost_track' "
                 "AND DATE(operate_time) = CURDATE()")
        )).scalar() or 0

        # 按模型汇总
        rows = (await db.execute(
            text("SELECT JSON_EXTRACT(content, '$.model') as model, "
                 "SUM(CAST(JSON_EXTRACT(content, '$.totalTokens') AS SIGNED)) as tokens, "
                 "SUM(CAST(JSON_EXTRACT(content, '$.cost') AS DECIMAL(12,6))) as cost "
                 "FROM operation_logs WHERE operate_type='cost_track' "
                 "AND DATE(operate_time) = CURDATE() "
                 "GROUP BY JSON_EXTRACT(content, '$.model')")
        )).all()

        by_model = [{"model": r.model.strip('"') if r.model else "unknown",
                     "tokens": int(r.tokens or 0), "cost": round(float(r.cost or 0), 4)} for r in rows]

        # 本月总
        month_total = (await db.execute(
            text("SELECT SUM(CAST(JSON_EXTRACT(content, '$.totalTokens') AS SIGNED)) "
                 "FROM operation_logs WHERE operate_type='cost_track' "
                 "AND operate_time >= DATE_FORMAT(CURDATE(), '%Y-%m-01')")
        )).scalar() or 0

        month_rows = (await db.execute(
            text("SELECT JSON_EXTRACT(content, '$.model') as model, "
                 "SUM(CAST(JSON_EXTRACT(content, '$.totalTokens') AS SIGNED)) as tokens "
                 "FROM operation_logs WHERE operate_type='cost_track' "
                 "AND operate_time >= DATE_FORMAT(CURDATE(), '%Y-%m-01') "
                 "GROUP BY JSON_EXTRACT(content, '$.model')")
        )).all()

        month_by_model = [{"model": r.model.strip('"') if r.model else "unknown",
                           "tokens": int(r.tokens or 0)} for r in month_rows]

        # 用户排行（本月 Top-10，operate_user 即调用方 username）
        user_rows = (await db.execute(
            text("SELECT operate_user, "
                 "SUM(CAST(JSON_EXTRACT(content, '$.totalTokens') AS SIGNED)) as tokens "
                 "FROM operation_logs WHERE operate_type='cost_track' "
                 "AND operate_time >= DATE_FORMAT(CURDATE(), '%Y-%m-01') "
                 "GROUP BY operate_user ORDER BY tokens DESC LIMIT 10")
        )).all()

        return {
            "period": period,
            "todayTokens": int(today_total),
            "todayByModel": by_model,
            "monthTokens": int(month_total),
            "monthByModel": month_by_model,
            "topUsers": [{"username": r.operate_user, "tokens": int(r.tokens or 0)} for r in user_rows],
            "userQuota": _DEFAULT_QUOTA["user"],
            "tenantQuota": _DEFAULT_QUOTA["tenant"],
        }
    except Exception as e:
        degraded("cost_report", e)
        return {"error": str(e), "todayTokens": 0, "monthTokens": 0}