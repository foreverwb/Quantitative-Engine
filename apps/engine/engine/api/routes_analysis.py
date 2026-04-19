"""
engine/api/routes_analysis.py — 分析引擎 REST 端点

职责: 提供触发分析、查询分析结果、获取 payoff 数据的 HTTP API。
      路由层只做参数校验和调用编排，业务逻辑委托给 pipeline。
依赖: engine.pipeline, engine.db.session, engine.db.models, engine.db.persist
被依赖: engine.main
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from engine.db.models import AnalysisResultSnapshotRow, MarketParameterSnapshotRow
from engine.db.persist import persist_analysis_result
from engine.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["analysis"])

# Pipeline 实例由 main.py lifespan 注入
_pipeline: Any = None


def set_pipeline(pipeline: Any) -> None:
    """由 main.py lifespan 调用，注入 AnalysisPipeline 实例。"""
    global _pipeline
    _pipeline = pipeline


# ---------------------------------------------------------------------------
# POST /api/v2/analysis/{symbol}
# ---------------------------------------------------------------------------


@router.post("/analysis/{symbol}", summary="触发完整分析，返回 analysis_id")
async def run_analysis(
    symbol: str,
    trade_date: Annotated[date | None, Query(description="交易日期，默认今日")] = None,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """触发完整 Step 2-9 分析流水线，将结果持久化并返回 analysis_id。"""
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline not initialised")

    effective_date = trade_date or date.today()
    try:
        baseline, result = await _pipeline.run_full(symbol.upper(), effective_date)
    except Exception as exc:
        logger.error("Pipeline failed for %s: %s", symbol, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    persist_analysis_result(db, baseline, result)

    return {"analysis_id": result.analysis_id}


# ---------------------------------------------------------------------------
# GET /api/v2/analysis/{analysis_id}
# ---------------------------------------------------------------------------


@router.get("/analysis/{analysis_id}", summary="获取分析结果")
def get_analysis(
    analysis_id: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """从数据库查询并返回完整分析结果快照。"""
    row: AnalysisResultSnapshotRow | None = (
        db.query(AnalysisResultSnapshotRow)
        .filter(AnalysisResultSnapshotRow.analysis_id == analysis_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"analysis_id {analysis_id!r} not found")

    scores = json.loads(row.scores_json)
    strategies = json.loads(row.strategies_json)
    meso = json.loads(row.meso_json) if row.meso_json else {}

    # 查关联的市场快照
    mps_row: MarketParameterSnapshotRow | None = (
        db.query(MarketParameterSnapshotRow)
        .filter(MarketParameterSnapshotRow.snapshot_id == row.baseline_snapshot_id)
        .first()
    )
    market_snapshot = json.loads(mps_row.data_json) if mps_row else {}

    return {
        "analysis_id": row.analysis_id,
        "symbol": row.symbol,
        "created_at": row.created_at.isoformat(),
        "scenario": row.scenario,
        "scenario_confidence": row.scenario_confidence,
        "scores": scores,
        "strategies": strategies,
        "market_snapshot": market_snapshot,
        "meso": meso,
    }


# ---------------------------------------------------------------------------
# GET /api/v2/analysis/{analysis_id}/payoff/{strategy_index}
# ---------------------------------------------------------------------------


@router.get(
    "/analysis/{analysis_id}/payoff/{strategy_index}",
    summary="获取策略 payoff 数据",
)
def get_payoff(
    analysis_id: str,
    strategy_index: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """返回指定策略的 payoff_curve 数据。"""
    row: AnalysisResultSnapshotRow | None = (
        db.query(AnalysisResultSnapshotRow)
        .filter(AnalysisResultSnapshotRow.analysis_id == analysis_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"analysis_id {analysis_id!r} not found")

    strategies: list[dict] = json.loads(row.strategies_json)
    if strategy_index < 0 or strategy_index >= len(strategies):
        raise HTTPException(
            status_code=404,
            detail=f"strategy_index {strategy_index} out of range (0-{len(strategies) - 1})",
        )

    strategy = strategies[strategy_index]
    payoff = strategy.get("payoff", {})
    return {
        "analysis_id": analysis_id,
        "strategy_index": strategy_index,
        "strategy_type": strategy.get("strategy_type"),
        "payoff": payoff,
    }


# ---------------------------------------------------------------------------
# POST /api/v2/analysis/{analysis_id}/payoff/{strategy_index}/recalc
# ---------------------------------------------------------------------------


class PayoffRecalcRequest(BaseModel):
    """Slider 重算请求体。"""

    model_config = ConfigDict(extra="forbid")

    slider_dte: int = Field(..., ge=0, description="UI 滑块指定的 DTE")
    slider_iv_multiplier: float = Field(
        ..., gt=0, description="IV 曲面缩放倍数",
    )


@router.post(
    "/analysis/{analysis_id}/payoff/{strategy_index}/recalc",
    summary="Slider 交互式 payoff 重算",
)
def recalc_payoff(
    analysis_id: str,
    strategy_index: int,
    body: PayoffRecalcRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """根据前端 slider 参数重算 payoff 曲线。"""
    from engine.core.payoff_engine import recalc_payoff_with_sliders
    from engine.core.pricing import SMVSurface

    row: AnalysisResultSnapshotRow | None = (
        db.query(AnalysisResultSnapshotRow)
        .filter(AnalysisResultSnapshotRow.analysis_id == analysis_id)
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"analysis_id {analysis_id!r} not found",
        )

    strategies: list[dict] = json.loads(row.strategies_json)
    if strategy_index < 0 or strategy_index >= len(strategies):
        raise HTTPException(
            status_code=404,
            detail=f"strategy_index {strategy_index} out of range",
        )

    strategy = strategies[strategy_index]
    legs_raw = strategy.get("legs", [])

    # 从关联的市场快照获取 spot
    mps_row: MarketParameterSnapshotRow | None = (
        db.query(MarketParameterSnapshotRow)
        .filter(
            MarketParameterSnapshotRow.snapshot_id == row.baseline_snapshot_id,
        )
        .first()
    )
    if mps_row is None:
        raise HTTPException(
            status_code=404,
            detail="baseline market snapshot not found",
        )
    market_data = json.loads(mps_row.data_json)
    spot = float(market_data["spot_price"])

    # 构建 FakeLeg 供 recalc 使用
    from dataclasses import dataclass, field as dc_field

    @dataclass
    class _RecalcLeg:
        side: str
        option_type: str
        strike: float
        expiry: date
        qty: int
        premium: float

    parsed_legs = []
    for lg in legs_raw:
        parsed_legs.append(_RecalcLeg(
            side=lg["side"],
            option_type=lg["option_type"],
            strike=float(lg["strike"]),
            expiry=date.fromisoformat(lg["expiry"]),
            qty=int(lg.get("qty", 1)),
            premium=float(lg["premium"]),
        ))

    # 构建 flat SMV surface 作为 fallback (使用 payoff 中存储的 IV)
    import pandas as pd

    avg_iv = sum(lg.get("iv", 0.25) for lg in legs_raw) / max(len(legs_raw), 1)
    vol_cols = [f"vol{d}" for d in range(0, 101, 5)]
    monies_df = pd.DataFrame([
        {"dte": body.slider_dte or 30, **{c: avg_iv for c in vol_cols}},
        {"dte": max(body.slider_dte + 30, 60), **{c: avg_iv for c in vol_cols}},
    ])
    strikes_df = pd.DataFrame([
        {"strike": lg.strike, "dte": body.slider_dte or 30, "delta": 0.5}
        for lg in parsed_legs
    ])
    smv_surface = SMVSurface(monies_df, strikes_df, spot=spot)

    pnl_curve = recalc_payoff_with_sliders(
        legs=parsed_legs,
        spot=spot,
        smv_surface=smv_surface,
        slider_dte=body.slider_dte,
        slider_iv_multiplier=body.slider_iv_multiplier,
    )

    return {
        "analysis_id": analysis_id,
        "strategy_index": strategy_index,
        "slider_dte": body.slider_dte,
        "slider_iv_multiplier": body.slider_iv_multiplier,
        "pnl_curve": pnl_curve,
    }
