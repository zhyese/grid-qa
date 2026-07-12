"""Alembic 迁移环境（同步 pymysql，从 app.config 读 DB URL）。

运行（在 backend/ 目录）:
    alembic revision --autogenerate -m "描述"
    alembic upgrade head
    alembic stamp head   # 已用 create_all 建表的现有库，标记到 head
"""
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# 让 env.py 能 import app.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.db.base import Base  # noqa: E402
import app.models.user  # noqa: E402,F401  注册所有模型
import app.models.document  # noqa: E402,F401
import app.models.chunk  # noqa: E402,F401
import app.models.conversation  # noqa: E402,F401
import app.models.operation_log  # noqa: E402,F401
import app.models.feedback  # noqa: E402,F401
import app.models.qa_cache  # noqa: E402,F401
import app.models.ticket  # noqa: E402,F401  两票全生命周期
import app.models.permission  # noqa: E402,F401  RBAC 角色权限

config = context.config
# 从 settings 注入同步 DB URL（aiomysql→pymysql）
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL.replace("aiomysql", "pymysql"))

if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
