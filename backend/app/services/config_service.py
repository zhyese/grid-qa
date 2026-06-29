"""运行时配置：Redis 持久化（重启不丢，替代内存单例）。"""
from app.clients import redis_client

_DEFAULT_MILVUS = {"indexType": "HNSW", "param": {"M": 16, "efConstruction": 200, "ef": 64}}
_DEFAULT_MODEL = {"modelType": "default", "param": {"temperature": 0.2, "max_tokens": 2048}}


async def get_milvus_config() -> dict:
    v = await redis_client.cache_get_json("config:milvus")
    return v or _DEFAULT_MILVUS


async def update_milvus_config(index_type: str, param: dict) -> dict:
    data = {"indexType": index_type, "param": param or {}}
    await redis_client.cache_set_json_persistent("config:milvus", data)
    return data


async def get_model_config() -> dict:
    v = await redis_client.cache_get_json("config:model")
    return v or _DEFAULT_MODEL


async def update_model_config(model_type: str, param: dict) -> dict:
    data = {"modelType": model_type, "param": param or {}}
    await redis_client.cache_set_json_persistent("config:model", data)
    return data
