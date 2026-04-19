"""
engine/db/persist.py — 分析结果持久化工具函数

职责: 将 pipeline 输出的 MarketParameterSnapshot 和 AnalysisResultSnapshot
      持久化到 SQLAlchemy 数据库。由 API 路由和 CLI 共用。
依赖: engine.db.models, engine.models.snapshots
被依赖: engine.api.routes_analysis, cli_run_micro
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from engine.db.models import AnalysisResultSnapshotRow, MarketParameterSnapshotRow
from engine.models.snapshots import AnalysisResultSnapshot, MarketParameterSnapshot


def persist_analysis_result(
    db: Session,
    baseline: MarketParameterSnapshot,
    result: AnalysisResultSnapshot,
) -> None:
    """Persist pipeline results to database. Reused by API route and CLI."""
    mps_row = MarketParameterSnapshotRow(
        snapshot_id=baseline.snapshot_id,
        symbol=baseline.symbol,
        captured_at=baseline.captured_at,
        data_json=baseline.model_dump_json(),
    )
    db.merge(mps_row)

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
        strategies_json=json.dumps(result.strategies, default=str),
        meso_json=(
            json.dumps({"s_dir": result.meso_s_dir, "s_vol": result.meso_s_vol})
            if result.meso_s_dir is not None
            else None
        ),
    )
    db.merge(ars_row)
    db.commit()
