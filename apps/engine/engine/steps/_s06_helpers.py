"""
engine/steps/_s06_helpers.py — 策略构建辅助函数

职责: strike 选择 (select_strike_by_delta)、相邻 strike 查找、
      StrategyLeg 构建、StrategyCandidate 组装。
依赖: engine.models.strategy, engine.core.payoff_engine, engine.core.pricing
被依赖: engine.steps._s06_builders
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from engine.core.payoff_engine import compute_payoff
from engine.core.pricing import SMVSurface
from engine.models.strategy import (
    GreeksComposite,
    StrategyCandidate,
    StrategyLeg,
)

DEFAULT_MIN_OI: int = 500


# ---------------------------------------------------------------------------
# select_strike_by_delta (design-doc §8.3)
# ---------------------------------------------------------------------------


def select_strike_by_delta(
    strikes_df: pd.DataFrame,
    target_delta: float,
    option_type: str,
    expiry: str,
    min_oi: int = DEFAULT_MIN_OI,
) -> pd.Series | None:
    """从期权链中选择最接近目标 delta 的 strike。"""
    mask = strikes_df["expirDate"] == expiry
    filtered = strikes_df[mask].copy()

    if option_type == "call":
        filtered["delta_dist"] = (filtered["delta"] - target_delta).abs()
    else:
        filtered["put_delta"] = filtered["delta"] - 1
        filtered["delta_dist"] = (filtered["put_delta"] - target_delta).abs()

    oi_col = "callOpenInterest" if option_type == "call" else "putOpenInterest"
    filtered = filtered[filtered[oi_col] >= min_oi]

    if filtered.empty:
        return None
    return filtered.loc[filtered["delta_dist"].idxmin()]


# ---------------------------------------------------------------------------
# 相邻 strike 查找
# ---------------------------------------------------------------------------


def find_adjacent_strike(
    strikes_df: pd.DataFrame,
    ref_strike: float,
    expiry: str,
    direction: int,
    steps: int = 1,
) -> pd.Series | None:
    """在 ref_strike 的 direction 方向找第 steps 个 strike。"""
    mask = strikes_df["expirDate"] == expiry
    filtered = strikes_df[mask].sort_values("strike")
    unique = sorted(filtered["strike"].unique())
    if not unique:
        return None

    closest_idx = min(
        range(len(unique)), key=lambda i: abs(unique[i] - ref_strike),
    )
    target_idx = closest_idx + direction * steps
    if target_idx < 0 or target_idx >= len(unique):
        return None

    target_strike = unique[target_idx]
    rows = filtered[filtered["strike"] == target_strike]
    return rows.iloc[0] if not rows.empty else None


# ---------------------------------------------------------------------------
# StrategyLeg 构建
# ---------------------------------------------------------------------------


def build_leg(
    row: pd.Series,
    side: str,
    option_type: str,
    qty: int = 1,
) -> StrategyLeg:
    """从 StrikesFrame 的一行直接构建 StrategyLeg。"""
    expiry_date = _parse_expiry(row["expirDate"])

    if option_type == "call":
        premium = float(row["callValue"])
        delta = float(row["delta"])
        oi = int(row["callOpenInterest"])
        bid = _safe_float(row.get("callBidPrice"))
        ask = _safe_float(row.get("callAskPrice"))
    else:
        premium = float(row["putValue"])
        delta = float(row["delta"]) - 1.0
        oi = int(row["putOpenInterest"])
        bid = _safe_float(row.get("putBidPrice"))
        ask = _safe_float(row.get("putAskPrice"))

    return StrategyLeg(
        side=side,
        option_type=option_type,
        strike=float(row["strike"]),
        expiry=expiry_date,
        qty=qty,
        premium=premium,
        iv=float(row["smvVol"]),
        delta=delta,
        gamma=float(row["gamma"]),
        theta=float(row["theta"]),
        vega=float(row["vega"]),
        oi=oi,
        bid=bid,
        ask=ask,
    )


def _parse_expiry(raw: object) -> date:
    """将 expirDate 字符串或 date 对象转为 date。"""
    if isinstance(raw, date):
        return raw
    return date.fromisoformat(str(raw))


def _safe_float(val: object) -> float | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# StrategyCandidate 组装
# ---------------------------------------------------------------------------


def assemble_candidate(
    strategy_type: str,
    description: str,
    legs: list[StrategyLeg],
    spot: float,
    smv_surface: SMVSurface,
) -> StrategyCandidate:
    """计算 payoff / greeks / EV 并组装 StrategyCandidate。"""
    greeks = GreeksComposite(
        net_delta=sum(_signed(l, l.delta) for l in legs),
        net_gamma=sum(_signed(l, l.gamma) for l in legs),
        net_theta=sum(_signed(l, l.theta) for l in legs),
        net_vega=sum(_signed(l, l.vega) for l in legs),
    )

    payoff = compute_payoff(legs, spot, smv_surface)
    ev = payoff.pop * payoff.max_profit + (1 - payoff.pop) * payoff.max_loss

    net_cd = sum(
        l.premium * l.qty * (1 if l.side == "sell" else -1) for l in legs
    )

    return StrategyCandidate(
        strategy_type=strategy_type,
        description=description,
        legs=legs,
        net_credit_debit=round(net_cd, 4),
        max_profit=payoff.max_profit,
        max_loss=payoff.max_loss,
        breakevens=payoff.breakevens,
        pop=payoff.pop,
        ev=round(ev, 2),
        greeks_composite=greeks,
    )


def _signed(leg: StrategyLeg, value: float) -> float:
    return value * leg.qty * (1 if leg.side == "buy" else -1)
