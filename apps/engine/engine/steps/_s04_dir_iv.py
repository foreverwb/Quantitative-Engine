"""
engine/steps/_s04_dir_iv.py — Direction & IV Score 私有计算模块

职责: 实现 DirectionScore 和 IVScore 的子指标计算。从 s04_field_calculator
      拆分出来以满足单文件 ≤ 300 行的代码规范。
依赖: numpy, pandas,
      engine.models.context (RegimeContext),
      engine.models.micro (MicroSnapshot),
      engine.steps.s04_field_calculator (共享辅助函数)
被依赖: engine.steps.s04_field_calculator (单向，作为内部组件)
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from engine.models.context import RegimeContext
from engine.models.micro import MicroSnapshot

# 注: 这些常量与 s04_field_calculator 中的同源 (design-doc §6.3)


# ── Direction 子指标权重 ──
W_DIR_MESO = 0.25
W_DIR_DEX = 0.25
W_DIR_VANNA = 0.25
W_DIR_TREND = 0.25

# ── IV 子指标权重 ──
W_IV_CONSENSUS = 0.25
W_IV_RV_SPREAD = 0.20
W_IV_TERM_KINK = 0.15
W_IV_SKEW = 0.20
W_IV_EVENT = 0.20

# ── 缩放/常量 ──
DEX_SLOPE_RANGE_SCALE = 100.0
VANNA_SLOPE_SCALE = 200.0
TERM_KINK_SCALE = 1000.0
SKEW_25D_SCALE = 500.0
HV_LOOKBACK_DAYS = 20
NET_EVENT_TYPES = {"earnings", "fomc", "cpi"}


# ---------------------------------------------------------------------------
# DirectionScore [-100, 100]
# ---------------------------------------------------------------------------


def compute_direction_score(
    snapshot: MicroSnapshot,
    context: RegimeContext,
    pre_calc: Any,
) -> float:
    """计算 DirectionScore (4 个子指标加权平均)。"""
    meso_direction = (
        float(context.meso_signal.s_dir) if context.meso_signal is not None else 0.0
    )
    dex_slope_score = _dex_slope_score(snapshot.dex_frame.df)
    vanna_score = _vanna_score(snapshot.monies.df)
    trend_score = _price_trend_score(
        snapshot.hist_summary, pre_calc.spot_price, meso_direction
    )

    return (
        meso_direction * W_DIR_MESO
        + dex_slope_score * W_DIR_DEX
        + vanna_score * W_DIR_VANNA
        + trend_score * W_DIR_TREND
    )


def _dex_slope_score(df: pd.DataFrame) -> float:
    if df.empty or "strike" not in df.columns or "exposure_value" not in df.columns:
        return 0.0
    by_strike = df.groupby("strike")["exposure_value"].sum().sort_index()
    if len(by_strike) < 2:
        return 0.0

    strikes = by_strike.index.to_numpy(dtype=float)
    values = by_strike.to_numpy(dtype=float)
    slope, _ = np.polyfit(strikes, values, 1)

    mean_abs = float(np.mean(np.abs(values)))
    strike_range = float(strikes.max() - strikes.min())
    if mean_abs == 0 or strike_range == 0:
        return 0.0
    normalized = slope * strike_range / mean_abs * DEX_SLOPE_RANGE_SCALE
    return float(np.sign(normalized) * min(abs(normalized), 100.0))


def _vanna_score(monies_df: pd.DataFrame) -> float:
    if monies_df.empty or "slope" not in monies_df.columns:
        return 0.0
    if "dte" in monies_df.columns:
        front_row = monies_df.sort_values("dte").iloc[0]
    else:
        front_row = monies_df.iloc[0]
    slope_value = front_row.get("slope")
    if slope_value is None or pd.isna(slope_value):
        return 0.0
    return _clip(float(slope_value) * VANNA_SLOPE_SCALE, -100.0, 100.0)


def _price_trend_score(
    hist_summary: Any | None,
    spot: float,
    meso_direction: float,
) -> float:
    from engine.steps.s04_field_calculator import extract_prior_close_series

    prices = extract_prior_close_series(hist_summary)
    if prices is None or len(prices) < HV_LOOKBACK_DAYS or spot <= 0:
        return 0.0
    sma20 = float(prices.tail(HV_LOOKBACK_DAYS).mean())
    if sma20 == 0:
        return 0.0
    deviation_pct = (spot - sma20) / spot * 100.0
    sign = 1.0 if meso_direction >= 0 else -1.0
    return _clip(deviation_pct * sign, -100.0, 100.0)


# ---------------------------------------------------------------------------
# IVScore [0, 100]
# ---------------------------------------------------------------------------


def compute_iv_score(snapshot: MicroSnapshot, context: RegimeContext) -> float:
    """计算 IVScore (5 个子指标加权平均)。"""
    from engine.steps.s04_field_calculator import compute_hv20_pct, safe_attr

    iv_rank = safe_attr(snapshot.ivrank, "iv_rank") or 0.0
    iv_pctl = safe_attr(snapshot.ivrank, "iv_pctl") or 0.0
    iv_consensus = 0.4 * iv_rank + 0.6 * iv_pctl

    atm_iv = safe_attr(snapshot.summary, "atmIvM1")
    hv20 = compute_hv20_pct(snapshot.hist_summary, atm_iv)
    if atm_iv is not None and hv20 > 0:
        spread_raw = (atm_iv - hv20) / hv20 * 100.0
        iv_rv_spread = (_clip(spread_raw, -100.0, 100.0) + 100.0) / 2.0
    else:
        iv_rv_spread = 50.0

    term_kink = _term_kink(snapshot.term.df)
    skew_25d = _skew_25d(snapshot.monies.df)
    event_premium = _event_premium(snapshot.term.df, context)

    return (
        iv_consensus * W_IV_CONSENSUS
        + iv_rv_spread * W_IV_RV_SPREAD
        + term_kink * W_IV_TERM_KINK
        + skew_25d * W_IV_SKEW
        + event_premium * W_IV_EVENT
    )


def _term_kink(term_df: pd.DataFrame) -> float:
    if "dte" not in term_df.columns or "atmiv" not in term_df.columns:
        return 0.0
    df = term_df.dropna(subset=["dte", "atmiv"])
    if len(df) < 3:
        return 0.0
    coeffs = np.polyfit(
        df["dte"].to_numpy(dtype=float),
        df["atmiv"].to_numpy(dtype=float),
        2,
    )
    return _clip(abs(float(coeffs[0])) * TERM_KINK_SCALE, 0.0, 100.0)


def _skew_25d(monies_df: pd.DataFrame) -> float:
    if monies_df.empty or "vol25" not in monies_df.columns or "vol75" not in monies_df.columns:
        return 0.0
    if "dte" in monies_df.columns:
        row = monies_df.sort_values("dte").iloc[0]
    else:
        row = monies_df.iloc[0]
    vol25 = row.get("vol25")
    vol75 = row.get("vol75")
    if vol25 is None or vol75 is None or pd.isna(vol25) or pd.isna(vol75):
        return 0.0
    return _clip((float(vol25) - float(vol75)) * SKEW_25D_SCALE, 0.0, 100.0)


def _event_premium(term_df: pd.DataFrame, context: RegimeContext) -> float:
    if context.event.event_type not in NET_EVENT_TYPES:
        return 0.0
    if "dte" not in term_df.columns or "atmiv" not in term_df.columns:
        return 0.0
    df = term_df.dropna(subset=["dte", "atmiv"]).sort_values("dte")
    if len(df) < 2:
        return 0.0
    front_iv = float(df.iloc[0]["atmiv"])
    back_iv = float(df.iloc[-1]["atmiv"])
    if back_iv == 0:
        return 0.0
    premium = (front_iv / back_iv - 1.0) * 100.0
    return _clip(premium, 0.0, 100.0)


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
