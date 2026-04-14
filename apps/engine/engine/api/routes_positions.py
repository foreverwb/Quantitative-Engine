"""
engine/api/routes_positions.py — 持仓 CRUD 端点

职责: 提供 tracked_positions 表的增删改查 REST API。
依赖: engine.db.session, engine.db.models
被依赖: engine.main
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from engine.db.models import TrackedPositionRow
from engine.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/positions", tags=["positions"])


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------


class PositionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    analysis_id: str
    strategy_index: int
    legs_json: list[dict]       # StrategyLeg.model_dump() 列表
    entry_spot: float
    entry_iv: float


class PositionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str | None = None   # "active" | "closed"


def _row_to_dict(r: TrackedPositionRow) -> dict[str, Any]:
    return {
        "position_id": r.position_id,
        "symbol": r.symbol,
        "analysis_id": r.analysis_id,
        "strategy_index": r.strategy_index,
        "entry_time": r.entry_time.isoformat(),
        "status": r.status,
        "legs": json.loads(r.legs_json),
        "entry_spot": r.entry_spot,
        "entry_iv": r.entry_iv,
    }


# ---------------------------------------------------------------------------
# POST /api/v2/positions
# ---------------------------------------------------------------------------


@router.post("", summary="新建跟踪持仓", status_code=201)
def create_position(
    body: PositionCreate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """创建新的持仓跟踪记录，返回 position_id。"""
    row = TrackedPositionRow(
        position_id=str(uuid.uuid4()),
        symbol=body.symbol.upper(),
        analysis_id=body.analysis_id,
        strategy_index=body.strategy_index,
        entry_time=datetime.utcnow(),
        status="active",
        legs_json=json.dumps(body.legs_json),
        entry_spot=body.entry_spot,
        entry_iv=body.entry_iv,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# GET /api/v2/positions
# ---------------------------------------------------------------------------


@router.get("", summary="查询持仓列表")
def list_positions(
    symbol: Annotated[str | None, Query(description="过滤 symbol")] = None,
    status: Annotated[str | None, Query(description="过滤状态 active/closed")] = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """列出持仓，可按 symbol 和 status 过滤。"""
    q = db.query(TrackedPositionRow)
    if symbol:
        q = q.filter(TrackedPositionRow.symbol == symbol.upper())
    if status:
        q = q.filter(TrackedPositionRow.status == status)
    rows = q.order_by(TrackedPositionRow.entry_time.desc()).all()
    return {"count": len(rows), "positions": [_row_to_dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# GET /api/v2/positions/{position_id}
# ---------------------------------------------------------------------------


@router.get("/{position_id}", summary="获取单个持仓")
def get_position(
    position_id: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row: TrackedPositionRow | None = (
        db.query(TrackedPositionRow)
        .filter(TrackedPositionRow.position_id == position_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"position_id {position_id!r} not found")
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# PATCH /api/v2/positions/{position_id}
# ---------------------------------------------------------------------------


@router.patch("/{position_id}", summary="更新持仓状态")
def update_position(
    position_id: str,
    body: PositionUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """支持更新 status（active → closed）。"""
    row: TrackedPositionRow | None = (
        db.query(TrackedPositionRow)
        .filter(TrackedPositionRow.position_id == position_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"position_id {position_id!r} not found")

    if body.status is not None:
        if body.status not in {"active", "closed"}:
            raise HTTPException(status_code=422, detail="status must be 'active' or 'closed'")
        row.status = body.status  # type: ignore[assignment]

    db.commit()
    db.refresh(row)
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# DELETE /api/v2/positions/{position_id}
# ---------------------------------------------------------------------------


@router.delete("/{position_id}", summary="删除持仓记录", status_code=204)
def delete_position(
    position_id: str,
    db: Session = Depends(get_db),
) -> None:
    row: TrackedPositionRow | None = (
        db.query(TrackedPositionRow)
        .filter(TrackedPositionRow.position_id == position_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"position_id {position_id!r} not found")
    db.delete(row)
    db.commit()
