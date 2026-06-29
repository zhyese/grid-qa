"""MinIO 客户端封装。

minio SDK 为同步，FastAPI 端为异步，故在 service 层用 asyncio.to_thread 包装调用。
"""
import io
from typing import Optional

from minio import Minio

from app.config import settings

_client: Optional[Minio] = None


def get_minio() -> Minio:
    global _client
    if _client is None:
        _client = Minio(
            settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=settings.MINIO_SECURE,
        )
    return _client


async def init_bucket() -> None:
    client = get_minio()
    if not client.bucket_exists(settings.MINIO_BUCKET):
        client.make_bucket(settings.MINIO_BUCKET)
        print(f"[minio] 已创建 bucket: {settings.MINIO_BUCKET}")


def put_object(object_name: str, data: bytes, length: int, content_type: str = "application/octet-stream") -> None:
    get_minio().put_object(
        settings.MINIO_BUCKET, object_name, io.BytesIO(data), length, content_type=content_type
    )


def get_object_bytes(object_name: str) -> bytes:
    resp = get_minio().get_object(settings.MINIO_BUCKET, object_name)
    try:
        return resp.read()
    finally:
        resp.close()
        resp.release_conn()


def remove_object(object_name: str) -> None:
    get_minio().remove_object(settings.MINIO_BUCKET, object_name)
