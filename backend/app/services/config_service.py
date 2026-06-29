"""运行时配置（Milvus 索引参数、模型推理参数）。内存单例，MVP 不持久化。

实际生效为 best-effort：检索/问答读取当前值；重建 Milvus 索引需手动触发（避免在线抖动）。
"""
_runtime = {
    "milvus": {"indexType": "IVF_FLAT", "param": {"nlist": 1024, "nprobe": 16}},
    "model": {"modelType": "default", "param": {"temperature": 0.2, "max_tokens": 2048}},
}


def get_milvus_config() -> dict:
    return _runtime["milvus"]


def update_milvus_config(index_type: str, param: dict) -> dict:
    _runtime["milvus"] = {"indexType": index_type, "param": param or {}}
    return _runtime["milvus"]


def get_model_config() -> dict:
    return _runtime["model"]


def update_model_config(model_type: str, param: dict) -> dict:
    _runtime["model"] = {"modelType": model_type, "param": param or {}}
    return _runtime["model"]
