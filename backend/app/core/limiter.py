"""限流（slowapi），按客户端 IP。

生产在 nginx/代理后，应改 key_func 读 X-Forwarded-For 以识别真实客户端。
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, config_filename="")  # 不读 .env（含中文，避免 GBK 解码错误）
