"""
engine/models/strategy.py — 策略数据模型

职责: 定义策略腿、复合希腊值、策略候选的 Pydantic 数据模型。
依赖: pydantic, datetime
被依赖: engine.steps.s06_strategy_calculator, engine.steps.s07_risk_profiler,
        engine.steps.s08_strategy_scorer, engine.models.snapshots
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict


class StrategyLeg(BaseModel):
    """单条策略腿"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    side: Literal["buy", "sell"]
    option_type: Literal["call", "put"]
    strike: float
    expiry: date
    qty: int = 1
    premium: float          # mid price
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float
    oi: int
    bid: float | None = None
    ask: float | None = None


class GreeksComposite(BaseModel):
    """策略组合的净复合希腊值"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    net_delta: float
    net_gamma: float
    net_theta: float
    net_vega: float


class StrategyCandidate(BaseModel):
    """策略候选，包含腿、损益结构和希腊值"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_type: str
    description: str
    legs: list[StrategyLeg]
    net_credit_debit: float         # 正=credit, 负=debit
    max_profit: float
    max_loss: float
    breakevens: list[float]
    pop: float                      # Probability of Profit [0, 1]
    ev: float                       # Expected Value
    greeks_composite: GreeksComposite
    risk_profile: str | None = None     # Step 7 填充
    total_score: float | None = None    # Step 8 填充
