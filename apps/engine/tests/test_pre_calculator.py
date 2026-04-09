"""
tests/test_pre_calculator.py — Pre-Calculator (Step 3) 单元测试

覆盖:
  - PreCalculatorOutput Pydantic 模型字段约束
  - dyn_window_pct 公式与硬边界 (3% / 20%)
  - dyn_strike_band 计算与四舍五入
  - scenario_seed 五种分支:
        earnings event (双桶), STRESS transition (双桶),
        trend, vol_mean_reversion, unknown
  - hist_summary 缺失 / 数据不足时的退化路径
  - meso_signal 为 None 的容错
"""

from __future__ import annotations

import math
from datetime import date
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from engine.models.context import EventInfo, MesoSignal, RegimeContext
from engine.steps.s03_pre_calculator import (
    DYN_WINDOW_PCT_MAX,
    DYN_WINDOW_PCT_MIN,
    PreCalculatorError,
    PreCalculatorOutput,
    run,
)

# ---------------------------------------------------------------------------
# 公共常量 & 工厂
# ---------------------------------------------------------------------------

TRADE_DATE = date(2026, 4, 9)
SYMBOL = "AAPL"
SPOT_PRICE = 200.0


def _make_summary(
    spot_price: float = SPOT_PRICE,
    atm_iv_m1: float | None = 0.30,
) -> SimpleNamespace:
    return SimpleNamespace(spotPrice=spot_price, atmIvM1=atm_iv_m1)


def _make_meso_signal(
    s_dir: float = 0.0,
    s_vol: float = 0.0,
    s_conf: float = 70.0,
    s_pers: float = 60.0,
    quadrant: str = "neutral",
    signal_label: str = "neutral",
    event_regime: str = "neutral",
    prob_tier: str = "medium",
) -> MesoSignal:
    return MesoSignal(
        s_dir=s_dir,
        s_vol=s_vol,
        s_conf=s_conf,
        s_pers=s_pers,
        quadrant=quadrant,
        signal_label=signal_label,
        event_regime=event_regime,
        prob_tier=prob_tier,
    )


def _make_context(
    regime_class: str = "NORMAL",
    event_type: str = "none",
    days_to_event: int | None = None,
    meso_signal: MesoSignal | None = None,
) -> RegimeContext:
    event = EventInfo(
        event_type=event_type,  # type: ignore[arg-type]
        event_date=None,
        days_to_event=days_to_event,
    )
    return RegimeContext(
        symbol=SYMBOL,
        trade_date=TRADE_DATE,
        regime_class=regime_class,  # type: ignore[arg-type]
        event=event,
        meso_signal=meso_signal,
    )


def _make_hist_summary(prices: list[float]) -> SimpleNamespace:
    df = pd.DataFrame({"priorCls": prices})
    return SimpleNamespace(df=df)


# ---------------------------------------------------------------------------
# PreCalculatorOutput 模型测试
# ---------------------------------------------------------------------------


class TestPreCalculatorOutputModel:
    def test_output_is_frozen(self) -> None:
        out = PreCalculatorOutput(
            dyn_window_pct=0.05,
            dyn_strike_band=(190.0, 210.0),
            dyn_dte_range="14,45",
            dyn_dte_ranges=["14,45"],
            scenario_seed="trend",
            spot_price=200.0,
        )
        with pytest.raises(Exception):  # frozen
            out.dyn_window_pct = 0.10  # type: ignore[misc]

    def test_output_rejects_extra_fields(self) -> None:
        with pytest.raises(Exception):
            PreCalculatorOutput(
                dyn_window_pct=0.05,
                dyn_strike_band=(190.0, 210.0),
                dyn_dte_range="14,45",
                dyn_dte_ranges=["14,45"],
                scenario_seed="trend",
                spot_price=200.0,
                unexpected="oops",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# Step 3.1: dyn_window_pct
# ---------------------------------------------------------------------------


class TestDynWindowPct:
    @pytest.mark.asyncio
    async def test_hard_lower_bound_3pct(self) -> None:
        """ATM IV 极低 + 无 hist → dyn_window_pct 被裁剪到 3%"""
        ctx = _make_context()
        summary = _make_summary(atm_iv_m1=0.01)  # 极低 IV → 计算值远小于 3%
        out = await run(ctx, summary, hist_summary=None)
        assert out.dyn_window_pct == DYN_WINDOW_PCT_MIN

    @pytest.mark.asyncio
    async def test_hard_upper_bound_20pct(self) -> None:
        """ATM IV 极高 → dyn_window_pct 被裁剪到 20%"""
        ctx = _make_context()
        summary = _make_summary(atm_iv_m1=2.50)  # 极高 IV
        out = await run(ctx, summary, hist_summary=None)
        assert out.dyn_window_pct == DYN_WINDOW_PCT_MAX

    @pytest.mark.asyncio
    async def test_uses_atm_iv_fallback_when_no_hist(self) -> None:
        """无 hist_summary 时使用 ATM IV 公式"""
        ctx = _make_context()
        summary = _make_summary(atm_iv_m1=0.30)
        out = await run(ctx, summary, hist_summary=None)

        atm_iv = 0.30
        atr20_pct = atm_iv * math.sqrt(20 / 365)
        expected_move_pct = atm_iv * math.sqrt(30 / 365)
        expected = max(1.25 * expected_move_pct, atr20_pct, 0.0)
        expected = max(DYN_WINDOW_PCT_MIN, min(DYN_WINDOW_PCT_MAX, expected))
        assert out.dyn_window_pct == pytest.approx(expected, rel=1e-9)

    @pytest.mark.asyncio
    async def test_uses_hist_priorcls_when_provided(self) -> None:
        """有充分 hist_summary 时使用 priorCls 序列计算 ATR20"""
        # 构造 21 天价格序列，每日恒定 +2 → daily_range = 2 → atr20 = 2 → atr20_pct = 1%
        prices = [SPOT_PRICE + i * 2 for i in range(21)]
        hist = _make_hist_summary(prices)
        ctx = _make_context()
        summary = _make_summary(atm_iv_m1=0.05)  # 让 atr_pct 主导而非 IV
        out = await run(ctx, summary, hist_summary=hist)

        # 因为 atr20_pct = 0.01 < 3%，会被裁到 3%
        # 验证: 至少不再走 IV fallback (即使裁剪后等于 3%)
        # 直接验证: 如果换更大的价格变动，结果会反映 hist
        prices_big = [SPOT_PRICE + i * 20 for i in range(21)]  # daily diff=20 → atr_pct=10%
        out_big = await run(ctx, _make_summary(atm_iv_m1=0.05), _make_hist_summary(prices_big))
        assert out_big.dyn_window_pct == pytest.approx(0.10, rel=1e-9)
        # 同时确认裁剪生效用第一种小变动
        assert out.dyn_window_pct == DYN_WINDOW_PCT_MIN

    @pytest.mark.asyncio
    async def test_hist_with_too_few_rows_falls_back_to_iv(self) -> None:
        """priorCls < 2 行时退化到 ATM IV 公式"""
        hist = _make_hist_summary([SPOT_PRICE])  # 仅 1 行
        ctx = _make_context()
        summary = _make_summary(atm_iv_m1=0.30)
        out_with_short_hist = await run(ctx, summary, hist_summary=hist)
        out_without_hist = await run(ctx, summary, hist_summary=None)
        assert out_with_short_hist.dyn_window_pct == out_without_hist.dyn_window_pct

    @pytest.mark.asyncio
    async def test_earnings_inflates_window_via_hist_move(self) -> None:
        """earnings 场景下 earnings_hist_move_pct = 1.5 × expected_move_pct"""
        ctx_earn = _make_context(event_type="earnings", days_to_event=3)
        ctx_none = _make_context()
        summary = _make_summary(atm_iv_m1=0.30)

        out_earn = await run(ctx_earn, summary, hist_summary=None)
        out_none = await run(ctx_none, summary, hist_summary=None)
        # earnings 路径多了一项 1.5 × expected_move_pct，可能成为最大者
        assert out_earn.dyn_window_pct >= out_none.dyn_window_pct

    @pytest.mark.asyncio
    async def test_missing_atm_iv_uses_default(self) -> None:
        """summary.atmIvM1 = None 时不报错，使用 fallback"""
        ctx = _make_context()
        summary = _make_summary(atm_iv_m1=None)
        out = await run(ctx, summary, hist_summary=None)
        assert DYN_WINDOW_PCT_MIN <= out.dyn_window_pct <= DYN_WINDOW_PCT_MAX

    @pytest.mark.asyncio
    async def test_missing_spot_price_raises(self) -> None:
        ctx = _make_context()
        summary = SimpleNamespace(spotPrice=None, atmIvM1=0.30)
        with pytest.raises(PreCalculatorError):
            await run(ctx, summary, hist_summary=None)


# ---------------------------------------------------------------------------
# Step 3.2: dyn_strike_band
# ---------------------------------------------------------------------------


class TestDynStrikeBand:
    @pytest.mark.asyncio
    async def test_band_centered_on_spot(self) -> None:
        ctx = _make_context()
        summary = _make_summary(atm_iv_m1=0.30)
        out = await run(ctx, summary, hist_summary=None)

        lower, upper = out.dyn_strike_band
        # 区间应以 spot 为中心
        assert lower < SPOT_PRICE < upper
        assert (SPOT_PRICE - lower) == pytest.approx(upper - SPOT_PRICE, rel=1e-6)

    @pytest.mark.asyncio
    async def test_band_rounded_to_two_decimals(self) -> None:
        ctx = _make_context()
        summary = _make_summary(spot_price=123.456, atm_iv_m1=0.30)
        out = await run(ctx, summary, hist_summary=None)
        lower, upper = out.dyn_strike_band
        # 验证小数位数 ≤ 2
        assert round(lower, 2) == lower
        assert round(upper, 2) == upper


# ---------------------------------------------------------------------------
# Step 3.3: scenario_seed 分支
# ---------------------------------------------------------------------------


class TestScenarioSeedEarnings:
    @pytest.mark.asyncio
    async def test_earnings_3_days_double_bucket(self) -> None:
        """earnings + days_to_event=3 → seed=event, 双桶 ['0,10','4,60']"""
        ctx = _make_context(event_type="earnings", days_to_event=3)
        out = await run(ctx, _make_summary(), hist_summary=None)
        assert out.scenario_seed == "event"
        assert out.dyn_dte_range == "0,60"
        assert out.dyn_dte_ranges == ["0,10", "4,60"]

    @pytest.mark.asyncio
    async def test_earnings_zero_days_double_bucket(self) -> None:
        """earnings + days_to_event=0 → 双桶 ['0,7','1,60']"""
        ctx = _make_context(event_type="earnings", days_to_event=0)
        out = await run(ctx, _make_summary(), hist_summary=None)
        assert out.scenario_seed == "event"
        assert out.dyn_dte_ranges == ["0,7", "1,60"]

    @pytest.mark.asyncio
    async def test_earnings_outside_window_falls_through(self) -> None:
        """earnings 但 days_to_event=20 (>14) → 走后续分支，非 event"""
        ctx = _make_context(event_type="earnings", days_to_event=20)
        out = await run(ctx, _make_summary(), hist_summary=None)
        assert out.scenario_seed != "event"

    @pytest.mark.asyncio
    async def test_earnings_negative_days_falls_through(self) -> None:
        """earnings 但 days_to_event<0 → 不进入 event 分支"""
        ctx = _make_context(event_type="earnings", days_to_event=-1)
        out = await run(ctx, _make_summary(), hist_summary=None)
        assert out.scenario_seed != "event"


class TestScenarioSeedStress:
    @pytest.mark.asyncio
    async def test_stress_regime_double_bucket_transition(self) -> None:
        """STRESS regime → seed=transition, 双桶 ['7,21','30,60']"""
        ctx = _make_context(regime_class="STRESS")
        out = await run(ctx, _make_summary(), hist_summary=None)
        assert out.scenario_seed == "transition"
        assert out.dyn_dte_range == "7,60"
        assert out.dyn_dte_ranges == ["7,21", "30,60"]

    @pytest.mark.asyncio
    async def test_stress_takes_precedence_over_trend_signal(self) -> None:
        """STRESS regime 优先于 trend 信号"""
        signal = _make_meso_signal(s_dir=80, s_vol=10)
        ctx = _make_context(regime_class="STRESS", meso_signal=signal)
        out = await run(ctx, _make_summary(), hist_summary=None)
        assert out.scenario_seed == "transition"

    @pytest.mark.asyncio
    async def test_earnings_takes_precedence_over_stress(self) -> None:
        """earnings 窗口优先于 STRESS 分支"""
        ctx = _make_context(
            regime_class="STRESS",
            event_type="earnings",
            days_to_event=5,
        )
        out = await run(ctx, _make_summary(), hist_summary=None)
        assert out.scenario_seed == "event"


class TestScenarioSeedTrend:
    @pytest.mark.asyncio
    async def test_trend_when_strong_direction_low_vol(self) -> None:
        """|s_dir|=80 > 50, |s_vol|=10 < 30 → seed=trend, dte=14,45"""
        signal = _make_meso_signal(s_dir=80, s_vol=10)
        ctx = _make_context(meso_signal=signal)
        out = await run(ctx, _make_summary(), hist_summary=None)
        assert out.scenario_seed == "trend"
        assert out.dyn_dte_range == "14,45"
        assert out.dyn_dte_ranges == ["14,45"]

    @pytest.mark.asyncio
    async def test_trend_works_with_negative_direction(self) -> None:
        """绝对值判断: s_dir=-70 同样触发 trend"""
        signal = _make_meso_signal(s_dir=-70, s_vol=15)
        ctx = _make_context(meso_signal=signal)
        out = await run(ctx, _make_summary(), hist_summary=None)
        assert out.scenario_seed == "trend"

    @pytest.mark.asyncio
    async def test_trend_boundary_s_dir_50_excluded(self) -> None:
        """s_dir=50 不严格大于 50 → 不进入 trend 分支"""
        signal = _make_meso_signal(s_dir=50, s_vol=10)
        ctx = _make_context(meso_signal=signal)
        out = await run(ctx, _make_summary(), hist_summary=None)
        assert out.scenario_seed != "trend"


class TestScenarioSeedVolMR:
    @pytest.mark.asyncio
    async def test_vol_mean_reversion_when_high_vol_low_direction(self) -> None:
        """|s_vol|=70, |s_dir|=10 → seed=vol_mean_reversion"""
        signal = _make_meso_signal(s_dir=10, s_vol=70)
        ctx = _make_context(meso_signal=signal)
        out = await run(ctx, _make_summary(), hist_summary=None)
        assert out.scenario_seed == "vol_mean_reversion"
        assert out.dyn_dte_range == "14,45"


class TestScenarioSeedUnknown:
    @pytest.mark.asyncio
    async def test_unknown_when_no_signal(self) -> None:
        """meso_signal=None → seed=unknown"""
        ctx = _make_context(meso_signal=None)
        out = await run(ctx, _make_summary(), hist_summary=None)
        assert out.scenario_seed == "unknown"
        assert out.dyn_dte_range == "7,45"
        assert out.dyn_dte_ranges == ["7,45"]

    @pytest.mark.asyncio
    async def test_unknown_when_mid_range_signals(self) -> None:
        """s_dir=40, s_vol=40 (都不满足任一阈值) → unknown"""
        signal = _make_meso_signal(s_dir=40, s_vol=40)
        ctx = _make_context(meso_signal=signal)
        out = await run(ctx, _make_summary(), hist_summary=None)
        assert out.scenario_seed == "unknown"


# ---------------------------------------------------------------------------
# 端到端: spot_price 透传
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_spot_price_propagated(self) -> None:
        ctx = _make_context()
        summary = _make_summary(spot_price=350.75)
        out = await run(ctx, summary, hist_summary=None)
        assert out.spot_price == 350.75
