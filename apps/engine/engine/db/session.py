"""
engine/db/session.py — 数据库会话工厂

职责: 提供 SQLAlchemy async engine、SessionLocal 工厂和 get_db FastAPI 依赖。
依赖: sqlalchemy, engine.db.models
被依赖: engine.api, engine.monitor
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from engine.db.models import Base

# 全局 engine/session factory，由 lifespan 初始化
_engine: Any = None
_SessionLocal: Any = None


def init_db(database_url: str) -> None:
    """初始化数据库引擎并创建所有表（alembic 之前的备用路径）。"""
    global _engine, _SessionLocal
    _engine = create_engine(database_url, connect_args={"check_same_thread": False})
    _SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=_engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：提供数据库 Session，自动关闭。"""
    if _SessionLocal is None:
        raise RuntimeError("DB not initialised; call init_db() first")
    db: Session = _SessionLocal()
    try:
        yield db
    finally:
        db.close()
