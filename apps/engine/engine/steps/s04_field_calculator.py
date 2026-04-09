"""
engine/steps/s04_field_calculator.py — Field Calculator (Step 4)

职责: 从 MicroSnapshot + PreCalculatorOutput + RegimeContext 计算
      GammaScore / BreakScore / DirectionScore / IVScore 四个核心评分。
依赖: numpy, pandas, engine.models.{context, micro, scores},
      engine.steps.s03_pre_calculator,
      engine.steps._s04_dir_iv (私有 Direction/IV 子模块)
被依赖: engine.steps.s05_scenario_analyzer, engine.pipeline
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
import pandas as pd

from engine.models.context import RegimeContext
from engine.models.micro import MicroSnapshot
from engine.models.scores import FieldScores
from engine.steps._s04_dir_iv import compute_direction_score, compute_iv_score
from engine.steps.s03_pre_calculator import PreCalculatorOutput

logger = logging.getLogger(__name__)


# ── Gamma 子指标权重 (design-doc §6.3) ──
W_GAMMA_NET = 0.30
W_GAMMA_WALL_CONC = 0.25
W_GAMMA_ZERO_DIST = 0.25
W_GAMMA_MONTH = 0.20

# ── Break 子指标权重 ──
W_BREAK_WALL = 0.35
W_BREAK_IMPLIED = 0.30
W_BREAK_FLIP = 0.35

# ── 共享常量 ──
WALL_TOP_K = 3
TRADING_DAYS_PER_YEAR = 365
HV_TRADING_DAYS = 252
HV_LOOKBACK_DAYS = 20
EXPECTED_MOVE_HORIZON_DAYS = 30
IMPLIED_VS_ACTUAL_CAP = 3.0


class FieldCalculatorError(Exception):
    """Field Calculator 步骤执行失败"""


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def compute_field_scores(
    snapshot: MicroSnapshot,
    pre_calc: PreCalculatorOutput,
    context: RegimeContext,
) -> FieldScores:
    """计算 GammaScore / BreakScore / DirectionScore / IVScore。"""
    gamma = _compute_gamma_score(snapshot, pre_calc)
    brk = _compute_break_score(snapshot, pre_calc)
    direction = compute_direction_score(snapshot, context, pre_calc)
    iv = compute_iv_score(snapshot, context)

    scores = FieldScores(
        gamma_score=clip(gamma, 0.0, 100.0),
        break_score=clip(brk, 0.0, 100.0),
        direction_score=clip(direction, -100.0, 100.0),
        iv_score=clip(iv, 0.0, 100.0),
    )
    logger.info(
        "FieldCalculator: symbol=%s gamma=%.2f break=%.2f dir=%.2f iv=%.2f",
        context.symbol, scores.gamma_score, scores.break_score,
        scores.direction_score, scores.iv_score,
    )
    return scores


# ---------------------------------------------------------------------------
# GammaScore
# ---------------------------------------------------------------------------


def _compute_gamma_score(
    snapshot: MicroSnapshot,
    pre_calc: PreCalculatorOutput,
) -> float:
    df = snapshot.gex_frame.df
    spot = pre_calc.spot_price
    if df.empty or "exposure_value" not in df.columns:
        return 0.0

    exposure = df["exposure_value"].to_numpy(dtype=float)
    total_abs = float(np.abs(exposure).sum())
    net_gexn_normalized = (
        float(abs(exposure.sum())) / total_abs * 100.0 if total_abs > 0 else 0.0
    )

    by_strike_abs = df.groupby("strike")["exposure_value"].sum().abs()
    if by_strike_abs.empty or by_strike_abs.sum() == 0:
        wall_concentration = 0.0
    else:
        wall_concentration = float(
            by_strike_abs.nlargest(WALL_TOP_K).sum() / by_strike_abs.sum() * 100.0
        )

    if snapshot.zero_gamma_strike is not None and spot > 0:
        zg_dist_pct = abs(spot - snapshot.zero_gamma_strike) / spot * 100.0
    else:
        zg_dist_pct = 100.0
    zero_gamma_score = clip(100.0 - zg_dist_pct * 10.0, 0.0, 100.0)

    month_score = _month_consistency(df, spot)

    return (
        net_gexn_normalized * W_GAMMA_NET
        + wall_concentration * W_GAMMA_WALL_CONC
        + zero_gamma_score * W_GAMMA_ZERO_DIST
        + month_score * W_GAMMA_MONTH
    )


def _month_consistency(df: pd.DataFrame, spot: float) -> float:
    """前后两个 expiry 的 zero_gamma 之差越小，月度一致性越高 (越接近 100)。"""
    if "expirDate" not in df.columns or spot <= 0:
        return 50.0
    expiries = sorted(df["expirDate"].dropna().unique().tolist())
    if len(expiries) < 2:
        return 50.0
    front = _zero_gamma_for_expiry(df, expiries[0])
    back = _zero_gamma_for_expiry(df, expiries[1])
    if front is None or back is None:
        return 50.0
    diff_pct = abs(front - back) / spot * 100.0
    return clip(100.0 - diff_pct * 10.0, 0.0, 100.0)


def _zero_gamma_for_expiry(df: pd.DataFrame, expiry: Any) -> float | None:
    sub = df[df["expirDate"] == expiry]
    if sub.empty:
        return None
    by_strike = sub.groupby("strike")["exposure_value"].sum().sort_index()
    signs = by_strike.apply(lambda x: 1 if x > 0 else -1)
    flips = signs.diff().abs()
    flip_strikes = flips[flips > 0].index.tolist()
    return float(flip_strikes[0]) if flip_strikes else None


# ---------------------------------------------------------------------------
# BreakScore
# ---------------------------------------------------------------------------


def _compute_break_score(
    snapshot: MicroSnapshot,
    pre_calc: PreCalculatorOutput,
) -> float:
    spot = pre_calc.spot_price

    distances: list[float] = []
    if snapshot.call_wall_strike is not None:
        distances.append(abs(spot - snapshot.call_wall_strike))
    if snapshot.put_wall_strike is not None:
        distances.append(abs(spot - snapshot.put_wall_strike))
    wall_distance = (
        clip(min(distances) / spot * 100.0, 0.0, 100.0)
        if distances and spot > 0 else 0.0
    )

    atm_iv = safe_attr(snapshot.summary, "atmIvM1")
    implied_move_pct = (
        atm_iv * math.sqrt(EXPECTED_MOVE_HORIZON_DAYS / TRADING_DAYS_PER_YEAR)
        if atm_iv is not None else 0.0
    )
    atr20_pct = compute_hv20_pct(snapshot.hist_summary, atm_iv)
    if atr20_pct > 0:
        ratio = clip(implied_move_pct / atr20_pct, 0.0, IMPLIED_VS_ACTUAL_CAP)
        implied_vs_actual = ratio / IMPLIED_VS_ACTUAL_CAP * 100.0
    else:
        implied_vs_actual = 0.0

    if snapshot.zero_gamma_strike is not None and spot > 0:
        zg_dist = abs(spot - snapshot.zero_gamma_strike) / spot
        zero_gamma_flip_risk = clip(
            (1.0 - zg_dist / max(pre_calc.dyn_window_pct, 1e-9)) * 100.0,
            0.0, 100.0,
        )
    else:
        zero_gamma_flip_risk = 0.0

    return (
        wall_distance * W_BREAK_WALL
        + implied_vs_actual * W_BREAK_IMPLIED
        + zero_gamma_flip_risk * W_BREAK_FLIP
    )


# ---------------------------------------------------------------------------
# 共享辅助 (在 _s04_dir_iv 中复用)
# ---------------------------------------------------------------------------


def compute_hv20_pct(hist_summary: Any | None, atm_iv: float | None) -> float:
    """20 日历史已实现波动率（占比，与 atmIv 同量纲）。"""
    prices = extract_prior_close_series(hist_summary)
    if prices is not None and len(prices) >= HV_LOOKBACK_DAYS + 1:
        log_returns = np.log(prices / prices.shift(1)).dropna().tail(HV_LOOKBACK_DAYS)
        if len(log_returns) > 1:
            return float(log_returns.std(ddof=1) * math.sqrt(HV_TRADING_DAYS))
    if atm_iv is None:
        return 0.0
    return float(atm_iv * math.sqrt(HV_LOOKBACK_DAYS / TRADING_DAYS_PER_YEAR))


def extract_prior_close_series(hist_summary: Any | None) -> pd.Series | None:
    if hist_summary is None:
        return None
    df = getattr(hist_summary, "df", None)
    if df is None or "priorCls" not in df.columns:
        return None
    return df["priorCls"].dropna()


def safe_attr(obj: Any, name: str) -> float | None:
    value = getattr(obj, name, None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
