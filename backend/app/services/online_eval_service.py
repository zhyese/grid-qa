"""检索质量持续评测（Online RAG Eval）：线上流量采样→自动评分→趋势监控→告警。

核心指标：
- Context Relevance：检索到的文档与 query 的相关性（LLM Judge 评分 0-1）
- Answer Faithfulness：答案被检索资料支撑的比例（LLM Judge）
- Answer Completeness：答案的完整性评分（LLM Judge）
- 综合质量分 = 加权平均

采样策略：对流式/非流式答案按一定比例采样（默认 10%），异步跑 Judge 评分。
评分结果写入 eval_log 表，Grafana 可配置面板展示质量趋势。
"""
import asyncio
import json
import random
import time
from datetime import datetime

from sqlalchemy import desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.obs import degraded

_SAMPLE_RATE = 0.1  # 10% 采样


def should_sample() -> bool:
    """判断当前请求是否应该被采样评估。"""
    return random.random() < _SAMPLE_RATE


async def eval_quality(
    db: AsyncSession, query: str, answer: str,
    contexts: list[dict], model_type: str | None = None,
) -> dict:
    """对单个问答对做质量评估。

    Returns:
        {"contextRelevance": 0-1, "faithfulness": 0-1,
         "completeness": 0-1, "overall": 0-1, "latencyMs": int}
    """
    from app.rag import judge

    t0 = time.time()
    sources = [c.get("chunk", c.get("text", "")) or "" for c in contexts]

    try:
        ctx_res = await judge.judge_context_relevance(query, sources, model_type)
        relevance = ctx_res.get("relevance_score", 0.5)
    except Exception as e:
        degraded("online_eval_relevance", e)
        relevance = 0.5

    try:
        faith_res = await judge.judge_hallucination(answer, sources, model_type)
        faithfulness = 1.0 - (faith_res.get("hallucination", 0.5) or 0.5)
    except Exception as e:
        degraded("online_eval_faithfulness", e)
        faithfulness = 0.5

    try:
        completeness = await _judge_completeness(query, answer, model_type)
    except Exception as e:
        degraded("online_eval_completeness", e)
        completeness = 0.5

    overall = round(relevance * 0.3 + faithfulness * 0.4 + completeness * 0.3, 3)

    result = {
        "contextRelevance": round(relevance, 3),
        "faithfulness": round(faithfulness, 3),
        "completeness": round(completeness, 3),
        "overall": overall,
        "latencyMs": int((time.time() - t0) * 1000),
    }

    # 落库
    try:
        from app.models.operation_log import OperationLog
        log = OperationLog(
            username="system",
            action="online_eval",
            detail=json.dumps({
                "query": query[:100], "result": result,
                "ts": datetime.now().isoformat(),
            }, ensure_ascii=False)[:1000],
        )
        db.add(log)
        await db.commit()
    except Exception as e:
        degraded("online_eval_log", e)

    return result


async def _judge_completeness(query: str, answer: str, model_type: str | None = None) -> float:
    """LLM Judge 评估答案完整性。"""
    from app.providers.factory import get_llm_provider
    provider = get_llm_provider(model_type)
    content = await provider.chat(
        [{"role": "user", "content": (
            f"评估以下答案对问题的完整性（0-1分，只输出数字）：\n"
            f"问题：{query}\n答案：{answer[:500]}\n"
            f"评分标准：1=完全覆盖, 0.7=主要部分覆盖, 0.4=部分覆盖, 0=完全不相关"
        )}],
        temperature=0, max_tokens=10,
    )
    try:
        return max(0.0, min(1.0, float(content.strip())))
    except (ValueError, TypeError):
        return 0.5


async def get_quality_trends(db: AsyncSession, days: int = 7) -> dict:
    """获取质量评分趋势（近 N 天）。"""
    try:
        rows = (await db.execute(
            text("""
                SELECT
                    DATE(operate_time) as dt,
                    AVG(CAST(JSON_EXTRACT(detail, '$.result.overall') AS DECIMAL(5,3))) as avg_overall,
                    AVG(CAST(JSON_EXTRACT(detail, '$.result.contextRelevance') AS DECIMAL(5,3))) as avg_relevance,
                    AVG(CAST(JSON_EXTRACT(detail, '$.result.faithfulness') AS DECIMAL(5,3))) as avg_faithfulness,
                    COUNT(*) as sample_count
                FROM operation_log
                WHERE action='online_eval'
                  AND operate_time >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
                GROUP BY DATE(operate_time)
                ORDER BY dt
            """),
            {"days": days},
        )).all()

        return {
            "days": days,
            "trends": [
                {"date": str(r.dt), "overall": round(float(r.avg_overall or 0), 3),
                 "relevance": round(float(r.avg_relevance or 0), 3),
                 "faithfulness": round(float(r.avg_faithfulness or 0), 3),
                 "samples": int(r.sample_count or 0)}
                for r in rows
            ],
            "latestOverall": round(float(rows[-1].avg_overall or 0), 3) if rows else 0,
        }
    except Exception as e:
        degraded("quality_trends", e)
        return {"trends": [], "latestOverall": 0}