"""
engine/api/routes_monitor.py — 监控数据 REST 端点

职责: 提供市场快照、历史数据、监控状态和告警日志的 HTTP API。
依赖: engine.db.session, engine.db.models
被依赖: engine.main
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from engine.db.models import (
    AlertEventRow,
    MarketParameterSnapshotRow,
    MonitorStateSnapshotRow,
)
from engine.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["monitor"])


# ---------------------------------------------------------------------------
# GET /api/v2/market/{symbol}/snapshot
# ---------------------------------------------------------------------------


@router.get("/market/{symbol}/snapshot", summary="最新市场参数快照")
def get_market_snapshot(
    symbol: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """返回指定 symbol 最新的 MarketParameterSnapshot。"""
    row: MarketParameterSnapshotRow | None = (
        db.query(MarketParameterSnapshotRow)
        .filter(MarketParameterSnapshotRow.symbol == symbol.upper())
        .order_by(MarketParameterSnapshotRow.captured_at.desc())
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"No snapshot found for {symbol!r}")

    data = json.loads(row.data_json)
    return {
        "snapshot_id": row.snapshot_id,
        "symbol": row.symbol,
        "captured_at": row.captured_at.isoformat(),
        "data": data,
    }


# ---------------------------------------------------------------------------
# GET /api/v2/market/{symbol}/history?hours=4
# ---------------------------------------------------------------------------


@router.get("/market/{symbol}/history", summary="市场参数历史（迷你趋势图）")
def get_market_history(
    symbol: str,
    hours: Annotated[int, Query(ge=1, le=168, description="回溯小时数")] = 4,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """返回最近 N 小时内的市场快照列表，供前端趋势迷你图使用。"""
    cutoff = datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    rows: list[MarketParameterSnapshotRow] = (
        db.query(MarketParameterSnapshotRow)
        .filter(
            MarketParameterSnapshotRow.symbol == symbol.upper(),
            MarketParameterSnapshotRow.captured_at >= cutoff,
        )
        .order_by(MarketParameterSnapshotRow.captured_at.asc())
        .all()
    )

    snapshots = [
        {
            "snapshot_id": r.snapshot_id,
            "captured_at": r.captured_at.isoformat(),
            "data": json.loads(r.data_json),
        }
        for r in rows
    ]
    return {
        "symbol": symbol.upper(),
        "hours": hours,
        "count": len(snapshots),
        "snapshots": snapshots,
    }


# ---------------------------------------------------------------------------
# GET /api/v2/monitor/{symbol}/state
# ---------------------------------------------------------------------------


@router.get("/monitor/{symbol}/state", summary="最新监控状态（含告警颜色）")
def get_monitor_state(
    symbol: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """返回指定 symbol 最新的 MonitorStateSnapshot，包含各层指标颜色。"""
    row: MonitorStateSnapshotRow | None = (
        db.query(MonitorStateSnapshotRow)
        .filter(MonitorStateSnapshotRow.symbol == symbol.upper())
        .order_by(MonitorStateSnapshotRow.captured_at.desc())
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No monitor state found for {symbol!r}",
        )

    state = json.loads(row.state_json)
    return {
        "monitor_id": row.monitor_id,
        "symbol": row.symbol,
        "captured_at": row.captured_at.isoformat(),
        "analysis_id": row.analysis_id,
        "state": state,
    }


# ---------------------------------------------------------------------------
# GET /api/v2/monitor/{symbol}/alerts?limit=50
# ---------------------------------------------------------------------------


@router.get("/monitor/{symbol}/alerts", summary="告警日志")
def get_alerts(
    symbol: str,
    limit: Annotated[int, Query(ge=1, le=500, description="返回条数上限")] = 50,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """返回指定 symbol 最新的告警事件列表，按时间倒序。"""
    rows: list[AlertEventRow] = (
        db.query(AlertEventRow)
        .filter(AlertEventRow.symbol == symbol.upper())
        .order_by(AlertEventRow.timestamp.desc())
        .limit(limit)
        .all()
    )

    alerts = [
        {
            "alert_id": r.alert_id,
            "symbol": r.symbol,
            "timestamp": r.timestamp.isoformat(),
            "tier": r.tier,
            "indicator": r.indicator,
            "severity": r.severity,
            "old_value": r.old_value,
            "new_value": r.new_value,
            "threshold": r.threshold,
            "action": r.action,
        }
        for r in rows
    ]
    return {
        "symbol": symbol.upper(),
        "count": len(alerts),
        "alerts": alerts,
    }
