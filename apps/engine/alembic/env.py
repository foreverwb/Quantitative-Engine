import os
import re
import sys
from logging.config import fileConfig
from pathlib import Path

import yaml
from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# ── 将 apps/engine 加入 sys.path，使 engine.* 可导入 ──────────────────────
_engine_root = Path(__file__).parent.parent
if str(_engine_root) not in sys.path:
    sys.path.insert(0, str(_engine_root))

from engine.db.models import Base  # noqa: E402  — 需先修改 sys.path

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── 从 engine.yaml 读取 DB URL（替换环境变量） ──────────────────────────────
def _get_db_url() -> str:
    yaml_path = _engine_root / "engine" / "config" / "engine.yaml"
    raw = yaml_path.read_text(encoding="utf-8")
    expanded = re.sub(
        r"\$\{([^}]+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        raw,
    )
    cfg = yaml.safe_load(expanded)
    return cfg.get("database", {}).get("url", "sqlite:///data/engine.db")


# 始终用 engine.yaml 中的 URL 覆盖 alembic.ini 的占位符
config.set_main_option("sqlalchemy.url", _get_db_url())

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
