"""
engine/models/batch_snapshot.py — Batch Snapshot Models

职责: 定义 fetch-symbols CLI 生成的快照文件数据模型。
依赖: pydantic
被依赖: cli_fetch_symbols, cli_run_micro
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

_SOURCE_MESO_SYMBOLS = "meso_symbols"
_SOURCE_MESO_CHART_POINTS = "meso_chart_points"
_SOURCE_MESO_DATE_GROUPS_FALLBACK = "meso_date_groups_fallback"

VALID_SOURCES = frozenset(
    {_SOURCE_MESO_SYMBOLS, _SOURCE_MESO_CHART_POINTS, _SOURCE_MESO_DATE_GROUPS_FALLBACK}
)


class FetchSymbolsSnapshot(BaseModel):
    """快照：fetch-symbols CLI 的输出，供 run-micro 消费。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    trade_date: date
    fetched_at: datetime
    source: str  # "meso_symbols" | "meso_chart_points" | "meso_date_groups_fallback"
    meso_base_url: str
    symbols: list[str]
    symbol_count: int
    chart_points: list[dict] | None = None  # raw chart point data if fetched
