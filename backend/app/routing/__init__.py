"""查询智能路由：根据查询特征自动选择最优检索路径。

路径:
  sparse  - 仅 BM25 关键词匹配（短术语/标准引用/数值参数）
  dense   - 仅向量语义检索（故障口语/长自然语言/同义词混用）
  hybrid  - 全链路混合检索（默认，dense+BM25+RRF+rerank+MMR）

用法:
  from app.routing import classify_query
  decision = classify_query(query)
  contexts = await mixed_search(db, query, topk, routing=decision)
"""
