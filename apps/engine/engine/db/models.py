"""
engine/db/models.py — SQLAlchemy ORM 表定义

职责: 定义 market_parameter_snapshots, analysis_result_snapshots,
      monitor_state_snapshots, alert_events, tracked_positions 五张表。
依赖: sqlalchemy
被依赖: engine.db.session, engine.api, engine.monitor
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class MarketParameterSnapshotRow(Base):
    __tablename__ = "market_parameter_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    data_json: Mapped[str] = mapped_column(Text, nullable=False)  # JSON blob

    __table_args__ = (
        Index("ix_mps_symbol_time", "symbol", "captured_at"),
    )


class AnalysisResultSnapshotRow(Base):
    __tablename__ = "analysis_result_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    baseline_snapshot_id: Mapped[str] = mapped_column(Text, nullable=False)
    scores_json: Mapped[str] = mapped_column(Text, nullable=False)
    scenario: Mapped[str] = mapped_column(Text, nullable=False)
    scenario_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    strategies_json: Mapped[str] = mapped_column(Text, nullable=False)
    meso_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_ars_symbol_time", "symbol", "created_at"),
    )


class MonitorStateSnapshotRow(Base):
    __tablename__ = "monitor_state_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    monitor_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    analysis_id: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_snapshot_id: Mapped[str] = mapped_column(Text, nullable=False)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_mss_symbol_time", "symbol", "captured_at"),
    )


class AlertEventRow(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    indicator: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str] = mapped_column(Text, nullable=False)
    threshold: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_alerts_symbol_time", "symbol", "timestamp", "severity"),
    )


class TrackedPositionRow(Base):
    __tablename__ = "tracked_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    analysis_id: Mapped[str] = mapped_column(Text, nullable=False)
    strategy_index: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    legs_json: Mapped[str] = mapped_column(Text, nullable=False)
    entry_spot: Mapped[float] = mapped_column(Float, nullable=False)
    entry_iv: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        Index("ix_positions_symbol", "symbol", "status"),
    )
