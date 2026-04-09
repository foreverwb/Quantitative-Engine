"""
engine/models/context.py — 分析上下文数据模型

职责: 定义事件信息、Regime 上下文、Meso 信号的 Pydantic 数据模型。
依赖: pydantic, datetime
被依赖: engine.steps.s02_regime_gating, engine.pipeline, engine.models.snapshots
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict


class EventInfo(BaseModel):
    """事件信息"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: Literal["earnings", "fomc", "cpi", "none"]
    event_date: date | None
    days_to_event: int | None  # 正=未来, 负=已过, None=无事件


class MesoSignal(BaseModel):
    """Meso 层信号"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    s_dir: float          # [-100, 100]
    s_vol: float          # [-100, 100]
    s_conf: float         # [0, 100]
    s_pers: float         # [0, 100]
    quadrant: str
    signal_label: str
    event_regime: str
    prob_tier: str


class RegimeContext(BaseModel):
    """Regime 上下文，贯穿整个分析流程"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    trade_date: date
    regime_class: Literal["LOW_VOL", "NORMAL", "STRESS"]
    event: EventInfo
    meso_signal: MesoSignal | None  # 来自 Meso API 的信号
