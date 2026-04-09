"""
tests/test_payoff.py — Payoff 计算引擎单元测试

覆盖: Bull Call Spread / Iron Condor / Long Call 的 max_profit/loss、
breakeven、PayoffResult 形状、POP 区间、参数校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from engine.core.payoff_engine import (
    DEFAULT_NUM_POINTS,
    PayoffEngineError,
    PayoffResult,
    compute_payoff,
)

# ---------------------------------------------------------------------------
# Fixtures: 简易 Leg dataclass (满足 PayoffLeg Protocol 的结构化接口)
# ---------------------------------------------------------------------------


@dataclass
class FakeLeg:
    side: str           # "buy" | "sell"
    option_type: str    # "call" | "put"
    strike: float
    expiry: date
    qty: int
    premium: float
    iv: float


AS_OF = date(2026, 4, 9)
EXPIRY = AS_OF + timedelta(days=30)


def _bull_call_spread(spot: float = 100.0) -> list[FakeLeg]:
    """Bull Call Spread: buy 100C @3, sell 105C @1, net debit = 200."""
    return [
        FakeLeg("buy", "call", 100.0, EXPIRY, 1, 3.0, 0.25),
        FakeLeg("sell", "call", 105.0, EXPIRY, 1, 1.0, 0.25),
    ]


def _iron_condor(spot: float = 100.0) -> list[FakeLeg]:
    """Iron Condor: sell 95P @2, buy 90P @1, sell 105C @2, buy 110C @1.
    Net credit = 200, wing width = 5, max_loss = 300.
    """
    return [
        FakeLeg("sell", "put", 95.0, EXPIRY, 1, 2.0, 0.25),
        FakeLeg("buy", "put", 90.0, EXPIRY, 1, 1.0, 0.25),
        FakeLeg("sell", "call", 105.0, EXPIRY, 1, 2.0, 0.25),
        FakeLeg("buy", "call", 110.0, EXPIRY, 1, 1.0, 0.25),
    ]


def _long_call(spot: float = 100.0) -> list[FakeLeg]:
    """Long Call: buy 100C @5, debit = 500."""
    return [FakeLeg("buy", "call", 100.0, EXPIRY, 1, 5.0, 0.25)]


# ---------------------------------------------------------------------------
# Bull Call Spread
# ---------------------------------------------------------------------------


class TestBullCallSpread:
    def test_max_profit_equals_width_minus_debit(self) -> None:
        """max_profit = (105-100)*100 - 200 = 300"""
        result = compute_payoff(_bull_call_spread(), spot=100.0, as_of_date=AS_OF)
        assert result.max_profit == pytest.approx(300.0, abs=1e-6)

    def test_max_loss_equals_net_debit(self) -> None:
        """max_loss = -net_debit = -200"""
        result = compute_payoff(_bull_call_spread(), spot=100.0, as_of_date=AS_OF)
        assert result.max_loss == pytest.approx(-200.0, abs=1e-6)

    def test_breakeven_between_strikes(self) -> None:
        """Breakeven = 100 + 2 = 102 (在 100 和 105 之间)"""
        result = compute_payoff(_bull_call_spread(), spot=100.0, as_of_date=AS_OF)
        assert len(result.breakevens) == 1
        be = result.breakevens[0]
        assert 100.0 < be < 105.0
        assert be == pytest.approx(102.0, abs=0.2)

    def test_pop_in_unit_interval(self) -> None:
        result = compute_payoff(_bull_call_spread(), spot=100.0, as_of_date=AS_OF)
        assert 0.0 <= result.pop <= 1.0


# ---------------------------------------------------------------------------
# Iron Condor
# ---------------------------------------------------------------------------


class TestIronCondor:
    def test_max_profit_equals_net_credit(self) -> None:
        """max_profit = net credit = 200"""
        result = compute_payoff(_iron_condor(), spot=100.0, as_of_date=AS_OF)
        assert result.max_profit == pytest.approx(200.0, abs=1e-6)

    def test_max_loss_equals_wing_width_minus_credit(self) -> None:
        """max_loss = -(wing_width - net_credit) = -(500 - 200) = -300"""
        result = compute_payoff(_iron_condor(), spot=100.0, as_of_date=AS_OF)
        assert result.max_loss == pytest.approx(-300.0, abs=1e-6)

    def test_two_breakevens(self) -> None:
        """两个 breakeven: put-side ≈ 93, call-side ≈ 107"""
        result = compute_payoff(_iron_condor(), spot=100.0, as_of_date=AS_OF)
        assert len(result.breakevens) == 2
        put_be, call_be = sorted(result.breakevens)
        assert put_be == pytest.approx(93.0, abs=0.3)
        assert call_be == pytest.approx(107.0, abs=0.3)

    def test_max_profit_in_middle_zone(self) -> None:
        """sell strikes (95-105) 之间应为最大盈利区间"""
        result = compute_payoff(_iron_condor(), spot=100.0, as_of_date=AS_OF)
        # spot=100 处一定在最大盈利区间内
        mid_idx = DEFAULT_NUM_POINTS // 2
        assert result.expiry_pnl[mid_idx] == pytest.approx(200.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Long Call
# ---------------------------------------------------------------------------


class TestLongCall:
    def test_max_loss_equals_premium(self) -> None:
        """max_loss = -premium*100 = -500"""
        result = compute_payoff(_long_call(), spot=100.0, as_of_date=AS_OF)
        assert result.max_loss == pytest.approx(-500.0, abs=1e-6)

    def test_max_profit_at_upper_bound(self) -> None:
        """spot_range 上限 = 100*1.15 = 115, intrinsic=15, pnl=15*100-500=1000"""
        result = compute_payoff(
            _long_call(),
            spot=100.0,
            spot_range_pct=0.15,
            as_of_date=AS_OF,
        )
        assert result.max_profit == pytest.approx(1000.0, abs=1e-6)
        # 上限点应为最大值
        assert result.expiry_pnl[-1] == result.max_profit

    def test_breakeven_at_strike_plus_premium(self) -> None:
        """Breakeven = strike + premium = 105"""
        result = compute_payoff(_long_call(), spot=100.0, as_of_date=AS_OF)
        assert len(result.breakevens) == 1
        assert result.breakevens[0] == pytest.approx(105.0, abs=0.2)

    def test_pnl_below_strike_is_constant_loss(self) -> None:
        """spot < strike 时所有点应损失恰为 premium"""
        result = compute_payoff(_long_call(), spot=100.0, as_of_date=AS_OF)
        # 第一个点为 85，远低于 strike=100
        assert result.expiry_pnl[0] == pytest.approx(-500.0, abs=1e-6)


# ---------------------------------------------------------------------------
# PayoffResult shape & current_pnl smoke
# ---------------------------------------------------------------------------


class TestPayoffResultShape:
    def test_arrays_have_num_points_length(self) -> None:
        result = compute_payoff(
            _long_call(),
            spot=100.0,
            num_points=50,
            as_of_date=AS_OF,
        )
        assert len(result.spot_range) == 50
        assert len(result.expiry_pnl) == 50
        assert len(result.current_pnl) == 50

    def test_spot_range_endpoints(self) -> None:
        result = compute_payoff(
            _long_call(),
            spot=100.0,
            spot_range_pct=0.15,
            num_points=200,
            as_of_date=AS_OF,
        )
        assert result.spot_range[0] == pytest.approx(85.0, abs=1e-6)
        assert result.spot_range[-1] == pytest.approx(115.0, abs=1e-6)

    def test_current_pnl_is_finite(self) -> None:
        """current_pnl 由 BSM 计算，应为有限数值列表"""
        import math

        result = compute_payoff(_bull_call_spread(), spot=100.0, as_of_date=AS_OF)
        assert all(math.isfinite(v) for v in result.current_pnl)

    def test_result_is_frozen(self) -> None:
        result = compute_payoff(_long_call(), spot=100.0, as_of_date=AS_OF)
        assert isinstance(result, PayoffResult)
        with pytest.raises((TypeError, ValueError)):
            result.max_profit = 999.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


class TestValidation:
    def test_empty_legs_raises(self) -> None:
        with pytest.raises(PayoffEngineError):
            compute_payoff([], spot=100.0, as_of_date=AS_OF)

    def test_negative_spot_raises(self) -> None:
        with pytest.raises(PayoffEngineError):
            compute_payoff(_long_call(), spot=-1.0, as_of_date=AS_OF)

    def test_invalid_spot_range_pct_raises(self) -> None:
        with pytest.raises(PayoffEngineError):
            compute_payoff(
                _long_call(), spot=100.0, spot_range_pct=1.5, as_of_date=AS_OF
            )

    def test_too_few_num_points_raises(self) -> None:
        with pytest.raises(PayoffEngineError):
            compute_payoff(_long_call(), spot=100.0, num_points=1, as_of_date=AS_OF)
