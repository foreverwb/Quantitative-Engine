"""
engine/steps/s03_pre_calculator.py — Pre-Calculator (Step 3)

职责: 基于 RegimeContext / SummaryRecord / HistSummaryFrame 计算动态分析参数:
      dyn_window_pct, dyn_strike_band, dyn_dte_range(s), scenario_seed。
依赖: engine.models.context (RegimeContext), pydantic
被依赖: engine.steps.s04_field_calculator, engine.pipeline
"""

from __future__ import annotations

import logging
import math
from typing import Any

from pydantic import BaseModel, ConfigDict

from engine.models.context import RegimeContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量 (design-doc 第 5 节)
# ---------------------------------------------------------------------------

# Step 3.1: dyn_window_pct
ATR_LOOKBACK_DAYS = 20
ATR_FALLBACK_PCT = 0.05
DEFAULT_ATM_IV = 0.20
ATR_HORIZON_DAYS = 20
EXPECTED_MOVE_HORIZON_DAYS = 30
TRADING_DAYS_PER_YEAR = 365
EXPECTED_MOVE_MULTIPLIER = 1.25
EARNINGS_HIST_MOVE_MULTIPLIER = 1.5
DYN_WINDOW_PCT_MIN = 0.03
DYN_WINDOW_PCT_MAX = 0.20

# Step 3.3: scenario seed 分支阈值
EARNINGS_DAYS_TO_EVENT_MAX = 14
TREND_DIRECTION_MIN = 50
TREND_VOL_MAX = 30
VOL_MR_VOL_MIN = 50
VOL_MR_DIRECTION_MAX = 30

# Step 3.3: dte 分桶常量
EARNINGS_FRONT_BUCKET_TAIL = 7   # front expiry 上界 = days_to_event + 7
EARNINGS_BACK_BUCKET_HEAD = 1    # back expiry 下界 = days_to_event + 1
EARNINGS_BACK_BUCKET_TAIL = 60
STRESS_FRONT_BUCKET = "7,21"
STRESS_BACK_BUCKET = "30,60"
STRESS_FULL_RANGE = "7,60"
TREND_BUCKET = "14,45"
UNKNOWN_BUCKET = "7,45"

# scenario seed 字面量
SEED_EVENT = "event"
SEED_TRANSITION = "transition"
SEED_TREND = "trend"
SEED_VOL_MEAN_REVERSION = "vol_mean_reversion"
SEED_UNKNOWN = "unknown"

_STRESS_REGIME = "STRESS"
_EARNINGS_EVENT = "earnings"


class PreCalculatorError(Exception):
    """Pre-Calculator 步骤执行失败"""


class PreCalculatorOutput(BaseModel):
    """Pre-Calculator 输出 (design-doc 5.1)"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dyn_window_pct: float
    dyn_strike_band: tuple[float, float]
    dyn_dte_range: str
    dyn_dte_ranges: list[str]
    scenario_seed: str
    spot_price: float


async def run(
    context: RegimeContext,
    summary: Any,
    hist_summary: Any | None = None,
) -> PreCalculatorOutput:
    """
    执行 Pre-Calculator 步骤。

    Args:
        context: Regime Gating 输出的上下文（包含事件信息和 Meso 信号）
        summary: ORATS SummaryRecord（duck-typed，需含 spotPrice / atmIvM1）
        hist_summary: ORATS HistSummaryFrame（duck-typed，含 .df['priorCls']），可选

    Returns:
        PreCalculatorOutput: 动态分析参数
    """
    spot_price = _extract_spot_price(summary)
    atm_iv = _extract_atm_iv(summary)

    dyn_window_pct = _compute_dyn_window_pct(
        spot_price=spot_price,
        atm_iv=atm_iv,
        hist_summary=hist_summary,
        event_type=context.event.event_type,
    )
    dyn_strike_band = _compute_dyn_strike_band(spot_price, dyn_window_pct)
    scenario_seed, dyn_dte_range, dyn_dte_ranges = _resolve_dte_and_seed(context)

    output = PreCalculatorOutput(
        dyn_window_pct=dyn_window_pct,
        dyn_strike_band=dyn_strike_band,
        dyn_dte_range=dyn_dte_range,
        dyn_dte_ranges=dyn_dte_ranges,
        scenario_seed=scenario_seed,
        spot_price=spot_price,
    )

    logger.info(
        "Pre-Calculator: symbol=%s spot=%.2f window_pct=%.4f band=%s seed=%s dte=%s",
        context.symbol,
        spot_price,
        dyn_window_pct,
        dyn_strike_band,
        scenario_seed,
        dyn_dte_range,
    )

    return output


# ---------------------------------------------------------------------------
# 私有辅助函数
# ---------------------------------------------------------------------------


def _extract_spot_price(summary: Any) -> float:
    """从 SummaryRecord 提取 spotPrice，缺失时抛出。"""
    spot = getattr(summary, "spotPrice", None)
    if spot is None or spot <= 0:
        raise PreCalculatorError(
            f"summary.spotPrice 缺失或非正: {spot!r}"
        )
    return float(spot)


def _extract_atm_iv(summary: Any) -> float | None:
    """从 SummaryRecord 提取 atmIvM1，缺失时返回 None。"""
    iv = getattr(summary, "atmIvM1", None)
    if iv is None:
        return None
    return float(iv)


def _compute_atr20_pct(
    hist_summary: Any | None,
    spot_price: float,
    atm_iv: float | None,
) -> float:
    """
    计算 20 日 ATR 百分比 (design-doc Step 3.1)。

    优先使用历史 priorCls 序列；不足时退化为 atm_iv * sqrt(20/365)。
    """
    if hist_summary is not None:
        df = getattr(hist_summary, "df", None)
        if df is not None and "priorCls" in df.columns:
            prices = df["priorCls"].dropna().tail(ATR_LOOKBACK_DAYS + 1)
            if len(prices) >= 2:
                daily_ranges = prices.diff().abs().dropna()
                atr20 = daily_ranges.tail(ATR_LOOKBACK_DAYS).mean()
                return float(atr20) / spot_price

    return _atm_iv_fallback_pct(atm_iv, ATR_HORIZON_DAYS)


def _atm_iv_fallback_pct(atm_iv: float | None, horizon_days: int) -> float:
    """从 ATM IV 估算指定时间窗口内的预期波动比例。"""
    if atm_iv is None:
        return ATR_FALLBACK_PCT
    return atm_iv * math.sqrt(horizon_days / TRADING_DAYS_PER_YEAR)


def _compute_dyn_window_pct(
    spot_price: float,
    atm_iv: float | None,
    hist_summary: Any | None,
    event_type: str,
) -> float:
    """
    计算 dyn_window_pct (design-doc Step 3.1)。

    取以下三者最大值，并裁剪到 [3%, 20%]:
      - 1.25 × expected_move_pct
      - atr20_pct
      - earnings_hist_move_pct (仅 earnings 场景)
    """
    atr20_pct = _compute_atr20_pct(hist_summary, spot_price, atm_iv)

    expected_move_iv = atm_iv if atm_iv is not None else DEFAULT_ATM_IV
    expected_move_pct = expected_move_iv * math.sqrt(
        EXPECTED_MOVE_HORIZON_DAYS / TRADING_DAYS_PER_YEAR
    )

    if event_type == _EARNINGS_EVENT:
        earnings_hist_move_pct = expected_move_pct * EARNINGS_HIST_MOVE_MULTIPLIER
    else:
        earnings_hist_move_pct = 0.0

    raw = max(
        EXPECTED_MOVE_MULTIPLIER * expected_move_pct,
        atr20_pct,
        earnings_hist_move_pct,
    )
    return max(DYN_WINDOW_PCT_MIN, min(DYN_WINDOW_PCT_MAX, raw))


def _compute_dyn_strike_band(
    spot_price: float,
    dyn_window_pct: float,
) -> tuple[float, float]:
    """计算 dyn_strike_band (design-doc Step 3.2)。"""
    lower = spot_price * (1 - dyn_window_pct)
    upper = spot_price * (1 + dyn_window_pct)
    return (round(lower, 2), round(upper, 2))


def _resolve_dte_and_seed(
    context: RegimeContext,
) -> tuple[str, str, list[str]]:
    """
    计算 scenario_seed / dyn_dte_range / dyn_dte_ranges (design-doc Step 3.3)。

    优先级:
      1. earnings 且 0 ≤ days_to_event ≤ 14 → "event"  (双桶)
      2. STRESS regime                       → "transition" (双桶)
      3. |s_dir|>50 且 |s_vol|<30           → "trend"
      4. |s_vol|>50 且 |s_dir|<30           → "vol_mean_reversion"
      5. 其他                                → "unknown"
    """
    event = context.event
    if _is_earnings_window(event.event_type, event.days_to_event):
        days = event.days_to_event  # 已在 _is_earnings_window 中确认非 None
        assert days is not None
        front_bucket = f"0,{days + EARNINGS_FRONT_BUCKET_TAIL}"
        back_bucket = f"{days + EARNINGS_BACK_BUCKET_HEAD},{EARNINGS_BACK_BUCKET_TAIL}"
        return SEED_EVENT, f"0,{EARNINGS_BACK_BUCKET_TAIL}", [front_bucket, back_bucket]

    if context.regime_class == _STRESS_REGIME:
        return SEED_TRANSITION, STRESS_FULL_RANGE, [STRESS_FRONT_BUCKET, STRESS_BACK_BUCKET]

    s_dir = _abs_signal(context, "s_dir")
    s_vol = _abs_signal(context, "s_vol")

    if s_dir > TREND_DIRECTION_MIN and s_vol < TREND_VOL_MAX:
        return SEED_TREND, TREND_BUCKET, [TREND_BUCKET]

    if s_vol > VOL_MR_VOL_MIN and s_dir < VOL_MR_DIRECTION_MAX:
        return SEED_VOL_MEAN_REVERSION, TREND_BUCKET, [TREND_BUCKET]

    return SEED_UNKNOWN, UNKNOWN_BUCKET, [UNKNOWN_BUCKET]


def _is_earnings_window(event_type: str, days_to_event: int | None) -> bool:
    """判断当前是否处于 earnings 事件分析窗口（0 ≤ days_to_event ≤ 14）。"""
    if event_type != _EARNINGS_EVENT:
        return False
    if days_to_event is None:
        return False
    return 0 <= days_to_event <= EARNINGS_DAYS_TO_EVENT_MAX


def _abs_signal(context: RegimeContext, field: str) -> float:
    """从 meso_signal 中提取字段绝对值；缺失时返回 0。"""
    signal = context.meso_signal
    if signal is None:
        return 0.0
    value = getattr(signal, field, None)
    if value is None:
        return 0.0
    return abs(float(value))
