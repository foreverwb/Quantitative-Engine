"""
tests/test_strategy_calculator.py — Strategy Calculator (Step 6) 单元测试

覆盖:
  - Bull Call Spread: legs 正确性 (buy 高 delta call + sell 低 delta call)
  - Iron Condor: 4 条 legs 排列 (buy put < sell put < sell call < buy call)
  - OI 过滤: strike OI < 500 时跳过
  - 验证 leg.premium 来自 callValue/putValue 而非 BSM 计算
  - Bear Put Spread / Straddle / Calendar Spread 基本构建
  - 主入口 calculate_strategies 分派
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from engine.core.pricing import SMVSurface
from engine.models.micro import MicroSnapshot
from engine.models.scenario import ScenarioResult
from engine.models.strategy import StrategyCandidate
from engine.steps._s06_builders import (
    build_bear_put_spread,
    build_bull_call_spread,
    build_calendar_spread,
    build_iron_condor,
    build_iron_butterfly,
    build_long_straddle,
    build_short_straddle,
)
from engine.steps._s06_helpers import select_strike_by_delta
from engine.steps.s03_pre_calculator import PreCalculatorOutput
from engine.steps.s06_strategy_calculator import calculate_strategies

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SPOT = 200.0
EXPIRY_STR = "2026-05-15"
EXPIRY_DATE = date(2026, 5, 15)
BACK_EXPIRY_STR = "2026-06-19"
FLAT_IV = 0.25


# ---------------------------------------------------------------------------
# Mock 数据构建
# ---------------------------------------------------------------------------


def _make_strikes_df() -> pd.DataFrame:
    """构造 mock StrikesFrame 含 callValue/putValue/smvVol 列。

    strikes: 170..230 步长 5, delta 从 0.95 线性递减到 0.05
    gamma 随 ATM 距离递减, 模拟真实期权链。
    """
    strikes = list(range(170, 235, 5))  # 13 strikes
    n = len(strikes)
    deltas = [round(0.95 - i * (0.90 / (n - 1)), 4) for i in range(n)]
    rows = []
    for s, d in zip(strikes, deltas):
        dist = abs(s - SPOT) / SPOT
        gamma = round(max(0.005, 0.03 * (1 - dist * 5)), 4)
        call_val = max(0.1, SPOT - s + 5) * (0.5 + d * 0.5)
        put_val = max(0.1, s - SPOT + 5) * (0.5 + (1 - d) * 0.5)
        rows.append({
            "strike": float(s),
            "expirDate": EXPIRY_STR,
            "dte": 31,
            "delta": d,
            "gamma": gamma,
            "theta": round(-0.03 - gamma * 2, 4),
            "vega": round(0.10 + gamma * 3, 4),
            "callValue": round(call_val, 2),
            "putValue": round(put_val, 2),
            "smvVol": FLAT_IV,
            "callOpenInterest": 1000,
            "putOpenInterest": 1000,
            "callBidPrice": round(call_val * 0.95, 2),
            "callAskPrice": round(call_val * 1.05, 2),
            "putBidPrice": round(put_val * 0.95, 2),
            "putAskPrice": round(put_val * 1.05, 2),
            "callMidIv": FLAT_IV,
            "putMidIv": FLAT_IV,
        })
    return pd.DataFrame(rows)


def _make_strikes_df_with_back_expiry() -> pd.DataFrame:
    """前月 + 后月数据 (用于 calendar spread 测试)。"""
    front = _make_strikes_df()
    back = _make_strikes_df()
    back["expirDate"] = BACK_EXPIRY_STR
    back["dte"] = 66
    back["callValue"] = back["callValue"] * 1.15
    back["putValue"] = back["putValue"] * 1.15
    return pd.concat([front, back], ignore_index=True)


def _make_low_oi_strikes_df() -> pd.DataFrame:
    """所有 strike 的 OI = 100 (低于 500 阈值)。"""
    df = _make_strikes_df()
    df["callOpenInterest"] = 100
    df["putOpenInterest"] = 100
    return df


def _make_flat_surface(
    strikes_df: pd.DataFrame | None = None,
) -> SMVSurface:
    """构造 flat IV 的 mock SMVSurface。"""
    vol_cols = [f"vol{d}" for d in range(0, 101, 5)]
    monies_df = pd.DataFrame([
        {"dte": 31, **{c: FLAT_IV for c in vol_cols}},
        {"dte": 66, **{c: FLAT_IV for c in vol_cols}},
    ])
    if strikes_df is None:
        strikes_df = _make_strikes_df()
    surface_strikes = strikes_df[["strike", "dte", "delta"]].copy()
    return SMVSurface(monies_df, surface_strikes, spot=SPOT)


def _frame(df: pd.DataFrame) -> SimpleNamespace:
    return SimpleNamespace(df=df)


def _make_micro(
    strikes_df: pd.DataFrame | None = None,
    dex_sum: float = 100.0,
) -> MicroSnapshot:
    """构造 MicroSnapshot，包含 strikes + monies + summary。"""
    if strikes_df is None:
        strikes_df = _make_strikes_df()

    vol_cols = [f"vol{d}" for d in range(0, 101, 5)]
    monies_df = pd.DataFrame([
        {"dte": 31, **{c: FLAT_IV for c in vol_cols}},
        {"dte": 66, **{c: FLAT_IV for c in vol_cols}},
    ])

    dex_df = pd.DataFrame({"exposure_value": [dex_sum]})
    gex_df = pd.DataFrame({
        "strike": [SPOT],
        "exposure_value": [50.0],
        "expirDate": [EXPIRY_STR],
    })

    return MicroSnapshot(
        strikes_combined=_frame(strikes_df),
        monies=_frame(monies_df),
        summary=SimpleNamespace(spotPrice=SPOT),
        ivrank=SimpleNamespace(iv_rank=50.0, iv_pctl=50.0),
        gex_frame=_frame(gex_df),
        dex_frame=_frame(dex_df),
        term=_frame(pd.DataFrame({"dte": [31, 66], "atmiv": [0.25, 0.25]})),
        skew=_frame(pd.DataFrame()),
    )


def _make_pre_calc() -> PreCalculatorOutput:
    return PreCalculatorOutput(
        dyn_window_pct=0.10,
        dyn_strike_band=(180.0, 220.0),
        dyn_dte_range="14,45",
        dyn_dte_ranges=["14,45"],
        scenario_seed="trend",
        spot_price=SPOT,
    )


def _make_scenario(label: str = "trend") -> ScenarioResult:
    return ScenarioResult(
        scenario=label,  # type: ignore[arg-type]
        confidence=0.85,
        method="rule_engine",
        invalidate_conditions=[],
    )


# ---------------------------------------------------------------------------
# select_strike_by_delta
# ---------------------------------------------------------------------------


class TestSelectStrikeByDelta:
    def test_selects_closest_call_delta(self) -> None:
        df = _make_strikes_df()
        row = select_strike_by_delta(df, 0.50, "call", EXPIRY_STR)
        assert row is not None
        assert abs(float(row["delta"]) - 0.50) <= 0.10

    def test_selects_closest_put_delta(self) -> None:
        df = _make_strikes_df()
        row = select_strike_by_delta(df, -0.50, "put", EXPIRY_STR)
        assert row is not None
        assert float(row["delta"]) - 1.0 == pytest.approx(-0.50, abs=0.05)

    def test_returns_none_when_oi_too_low(self) -> None:
        df = _make_low_oi_strikes_df()
        row = select_strike_by_delta(df, 0.50, "call", EXPIRY_STR)
        assert row is None

    def test_returns_none_when_expiry_not_found(self) -> None:
        df = _make_strikes_df()
        row = select_strike_by_delta(df, 0.50, "call", "2099-01-01")
        assert row is None


# ---------------------------------------------------------------------------
# Bull Call Spread
# ---------------------------------------------------------------------------


class TestBullCallSpread:
    def test_legs_correctness(self) -> None:
        df = _make_strikes_df()
        surface = _make_flat_surface(df)
        cand = build_bull_call_spread(df, SPOT, EXPIRY_STR, surface)

        assert cand is not None
        assert cand.strategy_type == "bull_call_spread"
        assert len(cand.legs) == 2

        buy_leg = cand.legs[0]
        sell_leg = cand.legs[1]
        assert buy_leg.side == "buy"
        assert buy_leg.option_type == "call"
        assert sell_leg.side == "sell"
        assert sell_leg.option_type == "call"
        assert buy_leg.strike < sell_leg.strike or buy_leg.delta > sell_leg.delta

    def test_premium_from_call_value(self) -> None:
        """验证 leg.premium 来自 callValue 而非 BSM 计算。"""
        df = _make_strikes_df()
        surface = _make_flat_surface(df)
        cand = build_bull_call_spread(df, SPOT, EXPIRY_STR, surface)
        assert cand is not None

        for leg in cand.legs:
            row = df[df["strike"] == leg.strike].iloc[0]
            assert leg.premium == float(row["callValue"])

    def test_net_credit_debit_is_debit(self) -> None:
        """Bull call spread 是 debit 策略, net_credit_debit < 0。"""
        df = _make_strikes_df()
        surface = _make_flat_surface(df)
        cand = build_bull_call_spread(df, SPOT, EXPIRY_STR, surface)
        assert cand is not None
        assert cand.net_credit_debit < 0

    def test_skipped_when_oi_low(self) -> None:
        df = _make_low_oi_strikes_df()
        surface = _make_flat_surface(df)
        cand = build_bull_call_spread(df, SPOT, EXPIRY_STR, surface)
        assert cand is None


# ---------------------------------------------------------------------------
# Bear Put Spread
# ---------------------------------------------------------------------------


class TestBearPutSpread:
    def test_legs_are_puts(self) -> None:
        df = _make_strikes_df()
        surface = _make_flat_surface(df)
        cand = build_bear_put_spread(df, SPOT, EXPIRY_STR, surface)
        assert cand is not None
        assert all(l.option_type == "put" for l in cand.legs)
        buy_leg = [l for l in cand.legs if l.side == "buy"][0]
        sell_leg = [l for l in cand.legs if l.side == "sell"][0]
        assert buy_leg.strike > sell_leg.strike

    def test_premium_from_put_value(self) -> None:
        df = _make_strikes_df()
        surface = _make_flat_surface(df)
        cand = build_bear_put_spread(df, SPOT, EXPIRY_STR, surface)
        assert cand is not None
        for leg in cand.legs:
            row = df[df["strike"] == leg.strike].iloc[0]
            assert leg.premium == float(row["putValue"])


# ---------------------------------------------------------------------------
# Iron Condor
# ---------------------------------------------------------------------------


class TestIronCondor:
    def test_four_legs_ordered(self) -> None:
        """4 条 legs: buy put < sell put < sell call < buy call。"""
        df = _make_strikes_df()
        surface = _make_flat_surface(df)
        cand = build_iron_condor(df, SPOT, EXPIRY_STR, surface)
        assert cand is not None
        assert len(cand.legs) == 4

        bp = cand.legs[0]
        sp = cand.legs[1]
        sc = cand.legs[2]
        bc = cand.legs[3]

        assert bp.side == "buy" and bp.option_type == "put"
        assert sp.side == "sell" and sp.option_type == "put"
        assert sc.side == "sell" and sc.option_type == "call"
        assert bc.side == "buy" and bc.option_type == "call"
        assert bp.strike < sp.strike
        assert sc.strike < bc.strike

    def test_is_credit_strategy(self) -> None:
        df = _make_strikes_df()
        surface = _make_flat_surface(df)
        cand = build_iron_condor(df, SPOT, EXPIRY_STR, surface)
        assert cand is not None
        assert cand.net_credit_debit > 0


# ---------------------------------------------------------------------------
# Iron Butterfly
# ---------------------------------------------------------------------------


class TestIronButterfly:
    def test_atm_sells_same_strike(self) -> None:
        df = _make_strikes_df()
        surface = _make_flat_surface(df)
        cand = build_iron_butterfly(df, SPOT, EXPIRY_STR, surface)
        assert cand is not None
        sell_legs = [l for l in cand.legs if l.side == "sell"]
        assert len(sell_legs) == 2
        assert sell_legs[0].strike == sell_legs[1].strike


# ---------------------------------------------------------------------------
# Straddles
# ---------------------------------------------------------------------------


class TestLongStraddle:
    def test_two_buy_legs_same_strike(self) -> None:
        df = _make_strikes_df()
        surface = _make_flat_surface(df)
        cand = build_long_straddle(df, SPOT, EXPIRY_STR, surface)
        assert cand is not None
        assert len(cand.legs) == 2
        assert all(l.side == "buy" for l in cand.legs)
        assert cand.legs[0].strike == cand.legs[1].strike
        types = {l.option_type for l in cand.legs}
        assert types == {"call", "put"}


class TestShortStraddle:
    def test_two_sell_legs(self) -> None:
        df = _make_strikes_df()
        surface = _make_flat_surface(df)
        cand = build_short_straddle(df, SPOT, EXPIRY_STR, surface)
        assert cand is not None
        assert all(l.side == "sell" for l in cand.legs)
        assert cand.net_credit_debit > 0


# ---------------------------------------------------------------------------
# Calendar Spread
# ---------------------------------------------------------------------------


class TestCalendarSpread:
    def test_two_expiries(self) -> None:
        df = _make_strikes_df_with_back_expiry()
        surface = _make_flat_surface(df)
        cand = build_calendar_spread(
            df, SPOT, EXPIRY_STR, BACK_EXPIRY_STR, surface,
        )
        assert cand is not None
        assert len(cand.legs) == 2
        sell_leg = [l for l in cand.legs if l.side == "sell"][0]
        buy_leg = [l for l in cand.legs if l.side == "buy"][0]
        assert sell_leg.expiry == EXPIRY_DATE
        assert buy_leg.expiry == date(2026, 6, 19)
        assert sell_leg.strike == buy_leg.strike


# ---------------------------------------------------------------------------
# OI 过滤
# ---------------------------------------------------------------------------


class TestOIFiltering:
    def test_all_builders_skip_when_oi_low(self) -> None:
        df = _make_low_oi_strikes_df()
        surface = _make_flat_surface(df)
        assert build_bull_call_spread(df, SPOT, EXPIRY_STR, surface) is None
        assert build_bear_put_spread(df, SPOT, EXPIRY_STR, surface) is None
        assert build_iron_condor(df, SPOT, EXPIRY_STR, surface) is None
        assert build_iron_butterfly(df, SPOT, EXPIRY_STR, surface) is None
        assert build_long_straddle(df, SPOT, EXPIRY_STR, surface) is None
        assert build_short_straddle(df, SPOT, EXPIRY_STR, surface) is None


# ---------------------------------------------------------------------------
# GreeksComposite 验证
# ---------------------------------------------------------------------------


class TestGreeksComposite:
    def test_bull_call_spread_greeks(self) -> None:
        df = _make_strikes_df()
        surface = _make_flat_surface(df)
        cand = build_bull_call_spread(df, SPOT, EXPIRY_STR, surface)
        assert cand is not None
        g = cand.greeks_composite
        assert g.net_delta > 0.0  # bull spread has positive delta

    def test_iv_from_smv_vol(self) -> None:
        """验证 leg.iv 来自 smvVol。"""
        df = _make_strikes_df()
        surface = _make_flat_surface(df)
        cand = build_long_straddle(df, SPOT, EXPIRY_STR, surface)
        assert cand is not None
        for leg in cand.legs:
            assert leg.iv == FLAT_IV


# ---------------------------------------------------------------------------
# 主入口 calculate_strategies
# ---------------------------------------------------------------------------


class TestCalculateStrategies:
    @pytest.mark.asyncio
    async def test_trend_bullish_returns_candidates(self) -> None:
        micro = _make_micro(dex_sum=100.0)
        scenario = _make_scenario("trend")
        pre_calc = _make_pre_calc()
        result = await calculate_strategies(scenario, micro, pre_calc)
        assert isinstance(result, list)
        assert len(result) > 0
        assert all(isinstance(c, StrategyCandidate) for c in result)

    @pytest.mark.asyncio
    async def test_range_scenario(self) -> None:
        micro = _make_micro()
        scenario = _make_scenario("range")
        pre_calc = _make_pre_calc()
        result = await calculate_strategies(scenario, micro, pre_calc)
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_returns_empty_on_unknown_scenario(self) -> None:
        micro = _make_micro()
        scenario = ScenarioResult(
            scenario="trend",
            confidence=0.5,
            method="rule_engine",
            invalidate_conditions=[],
        )
        pre_calc = _make_pre_calc()
        micro_low_oi = _make_micro(_make_low_oi_strikes_df())
        result = await calculate_strategies(scenario, micro_low_oi, pre_calc)
        assert result == []
