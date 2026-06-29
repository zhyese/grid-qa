"""结构化日志（loguru）：控制台 + 落文件 data/logs/app.log（按大小轮转）。"""
import sys
from pathlib import Path

from loguru import logger


def setup_logging():
    Path("data/logs").mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="INFO", enqueue=True)
    logger.add(
        "data/logs/app.log",
        rotation="50 MB",
        retention="10 days",
        level="INFO",
        encoding="utf-8",
        enqueue=True,
    )
    logger.info("日志系统初始化完成（输出: 控制台 + data/logs/app.log）")
    return logger
