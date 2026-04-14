"""
engine/steps/_s06_builders.py — 策略构建函数

职责: 为每种策略类型实现具体的 strike 选择和 leg 组装逻辑。
      所有 premium/IV/Greeks 直接读取 ORATS 数据，不自行用 BS 公式计算。
依赖: engine.steps._s06_helpers, engine.core.pricing
被依赖: engine.steps.s06_strategy_calculator
"""

from __future__ import annotations

import logging
from typing import Callable

import pandas as pd

from engine.core.pricing import SMVSurface
from engine.models.strategy import StrategyCandidate
from engine.steps._s06_helpers import (
    DEFAULT_MIN_OI,
    assemble_candidate,
    build_leg,
    find_adjacent_strike,
    select_strike_by_delta,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Delta 目标常量 (design-doc §8.3)
# ---------------------------------------------------------------------------

BCS_BUY_DELTA: float = 0.55
BCS_SELL_DELTA: float = 0.30
BPS_BUY_DELTA: float = -0.55
BPS_SELL_DELTA: float = -0.30
IC_SELL_PUT_DELTA: float = -0.20
IC_SELL_CALL_DELTA: float = 0.20
IC_WING_STEPS: int = 2
IB_ATM_DELTA: float = 0.50
IB_WING_CALL_DELTA: float = 0.20
IB_WING_PUT_DELTA: float = -0.20
STRADDLE_DELTA: float = 0.50
CALENDAR_DELTA: float = 0.50


# ---------------------------------------------------------------------------
# 策略构建函数
# ---------------------------------------------------------------------------


def build_bull_call_spread(
    strikes_df: pd.DataFrame,
    spot: float,
    expiry: str,
    smv_surface: SMVSurface,
    min_oi: int = DEFAULT_MIN_OI,
) -> StrategyCandidate | None:
    """Bull Call Spread: buy ITM/ATM call + sell OTM call."""
    buy = select_strike_by_delta(strikes_df, BCS_BUY_DELTA, "call", expiry, min_oi)
    sell = select_strike_by_delta(strikes_df, BCS_SELL_DELTA, "call", expiry, min_oi)
    if buy is None or sell is None:
        return None

    legs = [build_leg(buy, "buy", "call"), build_leg(sell, "sell", "call")]
    return assemble_candidate(
        "bull_call_spread", "Bull Call Spread", legs, spot, smv_surface,
    )


def build_bear_put_spread(
    strikes_df: pd.DataFrame,
    spot: float,
    expiry: str,
    smv_surface: SMVSurface,
    min_oi: int = DEFAULT_MIN_OI,
) -> StrategyCandidate | None:
    """Bear Put Spread: buy ATM/ITM put + sell OTM put."""
    buy = select_strike_by_delta(strikes_df, BPS_BUY_DELTA, "put", expiry, min_oi)
    sell = select_strike_by_delta(strikes_df, BPS_SELL_DELTA, "put", expiry, min_oi)
    if buy is None or sell is None:
        return None

    legs = [build_leg(buy, "buy", "put"), build_leg(sell, "sell", "put")]
    return assemble_candidate(
        "bear_put_spread", "Bear Put Spread", legs, spot, smv_surface,
    )


def build_iron_condor(
    strikes_df: pd.DataFrame,
    spot: float,
    expiry: str,
    smv_surface: SMVSurface,
    min_oi: int = DEFAULT_MIN_OI,
) -> StrategyCandidate | None:
    """Iron Condor: sell put/call near walls + buy protective wings."""
    sp = select_strike_by_delta(
        strikes_df, IC_SELL_PUT_DELTA, "put", expiry, min_oi,
    )
    sc = select_strike_by_delta(
        strikes_df, IC_SELL_CALL_DELTA, "call", expiry, min_oi,
    )
    if sp is None or sc is None:
        return None

    bp = find_adjacent_strike(
        strikes_df, float(sp["strike"]), expiry, -1, IC_WING_STEPS,
    )
    bc = find_adjacent_strike(
        strikes_df, float(sc["strike"]), expiry, +1, IC_WING_STEPS,
    )
    if bp is None or bc is None:
        return None

    legs = [
        build_leg(bp, "buy", "put"),
        build_leg(sp, "sell", "put"),
        build_leg(sc, "sell", "call"),
        build_leg(bc, "buy", "call"),
    ]
    return assemble_candidate(
        "iron_condor", "Iron Condor", legs, spot, smv_surface,
    )


def build_iron_butterfly(
    strikes_df: pd.DataFrame,
    spot: float,
    expiry: str,
    smv_surface: SMVSurface,
    min_oi: int = DEFAULT_MIN_OI,
) -> StrategyCandidate | None:
    """Iron Butterfly: sell ATM call+put + buy OTM wings."""
    atm = select_strike_by_delta(
        strikes_df, IB_ATM_DELTA, "call", expiry, min_oi,
    )
    wing_c = select_strike_by_delta(
        strikes_df, IB_WING_CALL_DELTA, "call", expiry, min_oi,
    )
    wing_p = select_strike_by_delta(
        strikes_df, IB_WING_PUT_DELTA, "put", expiry, min_oi,
    )
    if atm is None or wing_c is None or wing_p is None:
        return None

    legs = [
        build_leg(wing_p, "buy", "put"),
        build_leg(atm, "sell", "put"),
        build_leg(atm, "sell", "call"),
        build_leg(wing_c, "buy", "call"),
    ]
    return assemble_candidate(
        "iron_butterfly", "Iron Butterfly", legs, spot, smv_surface,
    )


def build_long_straddle(
    strikes_df: pd.DataFrame,
    spot: float,
    expiry: str,
    smv_surface: SMVSurface,
    min_oi: int = DEFAULT_MIN_OI,
) -> StrategyCandidate | None:
    """Long Straddle: buy ATM call + buy ATM put (same strike)."""
    atm = select_strike_by_delta(
        strikes_df, STRADDLE_DELTA, "call", expiry, min_oi,
    )
    if atm is None:
        return None

    legs = [build_leg(atm, "buy", "call"), build_leg(atm, "buy", "put")]
    return assemble_candidate(
        "long_straddle", "Long Straddle", legs, spot, smv_surface,
    )


def build_short_straddle(
    strikes_df: pd.DataFrame,
    spot: float,
    expiry: str,
    smv_surface: SMVSurface,
    min_oi: int = DEFAULT_MIN_OI,
) -> StrategyCandidate | None:
    """Short Straddle: sell ATM call + sell ATM put (same strike)."""
    atm = select_strike_by_delta(
        strikes_df, STRADDLE_DELTA, "call", expiry, min_oi,
    )
    if atm is None:
        return None

    legs = [build_leg(atm, "sell", "call"), build_leg(atm, "sell", "put")]
    return assemble_candidate(
        "short_straddle", "Short Straddle", legs, spot, smv_surface,
    )


def build_calendar_spread(
    strikes_df: pd.DataFrame,
    spot: float,
    front_expiry: str,
    back_expiry: str,
    smv_surface: SMVSurface,
    min_oi: int = DEFAULT_MIN_OI,
) -> StrategyCandidate | None:
    """Calendar Spread: sell front-month ATM call + buy back-month ATM call."""
    front = select_strike_by_delta(
        strikes_df, CALENDAR_DELTA, "call", front_expiry, min_oi,
    )
    if front is None:
        return None

    target_strike = float(front["strike"])
    mask = (
        (strikes_df["expirDate"] == back_expiry)
        & (strikes_df["strike"] == target_strike)
    )
    back_rows = strikes_df[mask]
    if back_rows.empty:
        return None
    back = back_rows.iloc[0]

    legs = [build_leg(front, "sell", "call"), build_leg(back, "buy", "call")]
    return assemble_candidate(
        "calendar_spread", "Calendar Spread", legs, spot, smv_surface,
    )


# ---------------------------------------------------------------------------
# 构建函数注册表
# ---------------------------------------------------------------------------

BuilderFn = Callable[..., StrategyCandidate | None]

BUILDER_REGISTRY: dict[str, BuilderFn] = {
    "bull_call_spread": build_bull_call_spread,
    "bear_put_spread": build_bear_put_spread,
    "iron_condor": build_iron_condor,
    "iron_butterfly": build_iron_butterfly,
    "long_straddle": build_long_straddle,
    "short_straddle": build_short_straddle,
    "calendar_spread": build_calendar_spread,
}
