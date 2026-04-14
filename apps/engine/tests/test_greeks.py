"""
tests/test_greeks.py — Greeks 聚合与 P/L 归因单元测试

覆盖: composite_greeks 线性加总, compute_pnl_attribution 符号正确性。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from engine.core.greeks import GreeksError, composite_greeks, compute_pnl_attribution
from engine.models.strategy import GreeksComposite, StrategyLeg

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

EXPIRY = date(2026, 5, 9)


def _make_leg(
    side: str,
    option_type: str,
    strike: float,
    delta: float,
    gamma: float,
    theta: float,
    vega: float,
    premium: float = 2.0,
    qty: int = 1,
) -> StrategyLeg:
    return StrategyLeg(
        side=side,
        option_type=option_type,
        strike=strike,
        expiry=EXPIRY,
        qty=qty,
        premium=premium,
        iv=0.25,
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        oi=100,
    )


# ---------------------------------------------------------------------------
# composite_greeks
# ---------------------------------------------------------------------------


class TestCompositeGreeks:
    def test_composite_greeks_sums_correctly(self) -> None:
        """buy call + sell put → net_delta = buy_delta - sell_delta."""
        buy_call = _make_leg("buy", "call", 105.0, delta=0.40, gamma=0.05, theta=-0.03, vega=0.10)
        sell_put = _make_leg("sell", "put", 95.0, delta=-0.35, gamma=0.04, theta=-0.02, vega=0.09)

        result = composite_greeks([buy_call, sell_put])

        assert isinstance(result, GreeksComposite)
        # buy call: sign=+1 → delta += 0.40
        # sell put: sign=-1 → delta -= (-0.35) = +0.35
        expected_delta = 0.40 - (-0.35)
        assert result.net_delta == pytest.approx(expected_delta, abs=1e-6)

    def test_composite_greeks_single_buy_leg(self) -> None:
        """单条 buy leg: 组合 Greeks = leg Greeks × qty。"""
        leg = _make_leg("buy", "call", 100.0, delta=0.50, gamma=0.03, theta=-0.02, vega=0.08, qty=2)
        result = composite_greeks([leg])

        assert result.net_delta == pytest.approx(0.50 * 2, abs=1e-6)
        assert result.net_gamma == pytest.approx(0.03 * 2, abs=1e-6)
        assert result.net_theta == pytest.approx(-0.02 * 2, abs=1e-6)
        assert result.net_vega == pytest.approx(0.08 * 2, abs=1e-6)

    def test_composite_greeks_sell_negates(self) -> None:
        """两条相同的 sell legs 的 Greeks 均为负。"""
        leg = _make_leg("sell", "call", 105.0, delta=0.30, gamma=0.02, theta=-0.01, vega=0.05)
        result = composite_greeks([leg, leg])

        assert result.net_delta == pytest.approx(-0.60, abs=1e-6)
        assert result.net_gamma == pytest.approx(-0.04, abs=1e-6)

    def test_composite_greeks_empty_legs_raises(self) -> None:
        with pytest.raises(GreeksError):
            composite_greeks([])


# ---------------------------------------------------------------------------
# compute_pnl_attribution
# ---------------------------------------------------------------------------


class TestPnlAttribution:
    def test_pnl_attribution_signs_buy_spot_up(self) -> None:
        """buy leg + spot 上涨 → delta_pnl 为正。"""
        leg = _make_leg("buy", "call", 100.0, delta=0.50, gamma=0.03, theta=-0.02, vega=0.10)
        attr = compute_pnl_attribution(
            leg,
            current_spot=105.0,
            entry_spot=100.0,
            current_iv=0.25,
            entry_iv=0.25,
            days_held=1,
        )
        assert attr["delta_pnl"] > 0, "buy + spot up → delta_pnl > 0"
        assert attr["gamma_pnl"] > 0, "gamma_pnl always non-negative for long position"
        assert attr["vega_pnl"] == pytest.approx(0.0, abs=1e-9), "no IV change → vega_pnl = 0"

    def test_pnl_attribution_sell_spot_up_delta_negative(self) -> None:
        """sell call + spot 上涨 → delta_pnl 为负。"""
        leg = _make_leg("sell", "call", 100.0, delta=0.50, gamma=0.03, theta=-0.02, vega=0.10)
        attr = compute_pnl_attribution(
            leg,
            current_spot=105.0,
            entry_spot=100.0,
            current_iv=0.25,
            entry_iv=0.25,
            days_held=0,
        )
        assert attr["delta_pnl"] < 0, "sell + spot up → delta_pnl < 0"

    def test_pnl_attribution_theta_buy_is_negative(self) -> None:
        """buy leg 持仓 1 天 + theta < 0 → theta_pnl < 0（时间衰减）。"""
        leg = _make_leg("buy", "call", 100.0, delta=0.50, gamma=0.03, theta=-0.02, vega=0.10)
        attr = compute_pnl_attribution(
            leg,
            current_spot=100.0,
            entry_spot=100.0,
            current_iv=0.25,
            entry_iv=0.25,
            days_held=1,
        )
        assert attr["theta_pnl"] < 0, "buy + negative theta + 1 day → theta_pnl < 0"

    def test_pnl_attribution_vega_buy_iv_up(self) -> None:
        """buy leg + IV 上升 → vega_pnl 为正。"""
        leg = _make_leg("buy", "call", 100.0, delta=0.50, gamma=0.03, theta=-0.02, vega=0.10)
        attr = compute_pnl_attribution(
            leg,
            current_spot=100.0,
            entry_spot=100.0,
            current_iv=0.30,
            entry_iv=0.25,
            days_held=0,
        )
        assert attr["vega_pnl"] > 0, "buy + IV up → vega_pnl > 0"
