"""
engine/api/routes_analysis.py — 分析引擎 REST 端点

职责: 提供触发分析、查询分析结果、获取 payoff 数据的 HTTP API。
      路由层只做参数校验和调用编排，业务逻辑委托给 pipeline。
依赖: engine.pipeline, engine.db.session, engine.db.models
被依赖: engine.main
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from engine.db.models import AnalysisResultSnapshotRow, MarketParameterSnapshotRow
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

    # 持久化 MarketParameterSnapshot
    mps_row = MarketParameterSnapshotRow(
        snapshot_id=baseline.snapshot_id,
        symbol=baseline.symbol,
        captured_at=baseline.captured_at,
        data_json=baseline.model_dump_json(),
    )
    db.merge(mps_row)

    # 持久化 AnalysisResultSnapshot
    scores_json = json.dumps(
        {
            "gamma_score": result.gamma_score,
            "break_score": result.break_score,
            "direction_score": result.direction_score,
            "iv_score": result.iv_score,
        }
    )
    ars_row = AnalysisResultSnapshotRow(
        analysis_id=result.analysis_id,
        symbol=result.symbol,
        created_at=result.created_at,
        baseline_snapshot_id=result.baseline_snapshot_id,
        scores_json=scores_json,
        scenario=result.scenario,
        scenario_confidence=result.scenario_confidence,
        strategies_json=json.dumps(result.strategies),
        meso_json=json.dumps(
            {"s_dir": result.meso_s_dir, "s_vol": result.meso_s_vol}
        ) if result.meso_s_dir is not None else None,
    )
    db.merge(ars_row)
    db.commit()

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
    payoff_curve = strategy.get("payoff_curve", {})
    return {
        "analysis_id": analysis_id,
        "strategy_index": strategy_index,
        "strategy_type": strategy.get("strategy_type"),
        "payoff_curve": payoff_curve,
    }
