"""重排：调阿里百炼 gte-rerank（DashScope 原生 text-rerank API）。

百炼 rerank 非 OpenAI 兼容，走 DashScope 原生 HTTP 接口。
失败时由调用方兜底（直接用 RRF 排序），不影响主流程。
"""
from typing import Optional

import httpx

from app.config import settings

_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"


class Reranker:
    def __init__(self):
        self.model = settings.RERANK_MODEL

    async def rerank(self, query: str, documents: list[str], top_n: int = 5) -> list[tuple[int, float]]:
        """返回 [(原始索引, 相关性分数), ...]，按相关性降序，长度 top_n。"""
        if not documents or not settings.DASHSCOPE_API_KEY:
            return [(i, 0.0) for i in range(min(top_n, len(documents)))]
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(
                _URL,
                headers={
                    "Authorization": f"Bearer {settings.DASHSCOPE_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "input": {"query": query, "documents": documents},
                    "parameters": {"return_documents": False, "top_n": top_n},
                },
            )
            resp.raise_for_status()
            results = resp.json().get("output", {}).get("results", [])
        return [(item["index"], float(item["relevance_score"])) for item in results]


_reranker: Optional[Reranker] = None


def get_reranker() -> Reranker:
    global _reranker
    if _reranker is None:
        _reranker = Reranker()
    return _reranker
