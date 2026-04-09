"""
engine/models/snapshots.py — 三层快照数据模型

职责: 定义市场参数快照、分析结果快照、监控状态快照的 Pydantic 数据模型。
依赖: pydantic, datetime
被依赖: engine.steps.s11_snapshot_writer, engine.monitor, engine.db, engine.api
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MarketParameterSnapshot(BaseModel):
    """市场参数快照（第一层）"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str                        # UUID
    symbol: str
    captured_at: datetime

    # 价格层
    spot_price: float
    spot_change_pct: float = 0.0            # vs 基线

    # 波动率层
    atm_iv_front: float
    atm_iv_back: float | None = None
    term_spread: float = 0.0                # back - front
    iv30d: float
    hv20d: float | None = None
    vrp: float = 0.0
    vol_of_vol: float = 0.0
    iv_rank: float = 0.0
    iv_pctl: float = 0.0
    iv_consensus: float = 0.0

    # 期权结构层
    net_gex: float = 0.0
    net_dex: float = 0.0
    zero_gamma_strike: float | None = None
    call_wall_strike: float | None = None
    put_wall_strike: float | None = None
    vol_pcr: float | None = None
    oi_pcr: float | None = None

    # 事件层
    regime_class: str = "NORMAL"
    next_event_type: str | None = None
    days_to_event: int | None = None


class AnalysisResultSnapshot(BaseModel):
    """分析结果快照（第二层）"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    analysis_id: str                        # UUID
    symbol: str
    created_at: datetime
    baseline_snapshot_id: str              # 关联基线

    # Scores
    gamma_score: float
    break_score: float
    direction_score: float
    iv_score: float

    # 场景
    scenario: str
    scenario_confidence: float
    scenario_method: str
    invalidate_conditions: list[str]

    # 策略 (JSON 序列化)
    strategies: list[dict]                 # StrategyCandidate.model_dump()

    # Meso 交叉引用
    meso_s_dir: float | None = None
    meso_s_vol: float | None = None


class MonitorStateSnapshot(BaseModel):
    """监控状态快照（第三层）"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    monitor_id: str
    symbol: str
    captured_at: datetime
    analysis_id: str
    baseline_snapshot_id: str

    # 偏移度
    spot_drift_pct: float = 0.0
    iv_drift_pct: float = 0.0
    zero_gamma_drift_pct: float = 0.0
    term_structure_flip: bool = False
    gex_sign_flip: bool = False

    # 策略健康度 (per position)
    positions_health: list[dict] = []

    # 场景有效性
    scenario_still_valid: bool = True
    invalidated_conditions: list[str] = []
    recommended_action: str | None = None  # "hold"/"adjust"/"exit"/"recalc_from_step_N"
