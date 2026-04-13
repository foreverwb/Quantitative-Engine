"""
tests/test_pricing.py — SMV 曲面定价引擎单元测试

覆盖: bs_formula 的正常路径、边界条件、put-call parity；
      SMVSurface 插值精度与边界行为；surface_greeks 有限差分。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from engine.core.pricing import (
    PricingError,
    SMVSurface,
    bs_formula,
    surface_greeks,
)

TOLERANCE = 0.01


# ---------------------------------------------------------------------------
# Helpers: 构造 mock MoniesFrame / StrikesFrame DataFrames
# ---------------------------------------------------------------------------


def _make_monies_df(
    dtes: list[int],
    flat_iv: float = 0.25,
) -> pd.DataFrame:
    """构造 MoniesFrame DataFrame，所有 vol 列为同一 flat_iv。"""
    vol_cols = [f"vol{d}" for d in range(0, 101, 5)]
    rows = []
    for dte in dtes:
        row: dict[str, float | int] = {"dte": dte}
        for col in vol_cols:
            row[col] = flat_iv
        rows.append(row)
    return pd.DataFrame(rows)


def _make_strikes_df(
    strikes: list[float],
    dtes: list[int],
    spot: float = 100.0,
    sigma: float = 0.25,
) -> pd.DataFrame:
    """构造 StrikesFrame DataFrame，delta 用 N(d1) 近似 (0-1 scale)。"""
    from scipy.stats import norm as _norm

    rows = []
    for dte in dtes:
        T = max(dte, 1) / 365.0
        for strike in strikes:
            d1 = (math.log(spot / strike) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
            delta = float(_norm.cdf(d1))  # call delta, 0-1
            rows.append({"strike": strike, "dte": dte, "delta": delta})
    return pd.DataFrame(rows)


def _make_skew_monies_df(dtes: list[int]) -> pd.DataFrame:
    """构造带 skew 的 MoniesFrame: OTM put 侧 IV 高于 OTM call 侧。"""
    delta_points = list(range(0, 101, 5))
    vol_cols = [f"vol{d}" for d in delta_points]
    rows = []
    for dte in dtes:
        row: dict[str, float | int] = {"dte": dte}
        for d in delta_points:
            # skew: vol 从 delta=0 (OTM put) 0.40 递减到 delta=100 (OTM call) 0.18
            row[f"vol{d}"] = 0.40 - 0.22 * (d / 100.0)
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# bs_formula 测试
# ---------------------------------------------------------------------------


class TestBsFormula:
    def test_call_atm_one_year(self) -> None:
        """S=100, K=100, T=1, r=0.05, sigma=0.20, call ~ 10.45"""
        price = bs_formula(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="call")
        assert abs(price - 10.45) < TOLERANCE

    def test_call_atm_expired_returns_zero(self) -> None:
        price = bs_formula(S=100, K=100, T=0.0, r=0.05, sigma=0.20, option_type="call")
        assert price == 0.0

    def test_call_itm_expired_returns_intrinsic(self) -> None:
        price = bs_formula(S=110, K=100, T=0.0, r=0.05, sigma=0.20, option_type="call")
        assert price == 10.0

    def test_put_itm_expired_returns_intrinsic(self) -> None:
        price = bs_formula(S=90, K=100, T=0.0, r=0.05, sigma=0.20, option_type="put")
        assert price == 10.0

    def test_call_otm_expired_returns_zero(self) -> None:
        price = bs_formula(S=90, K=100, T=0.0, r=0.05, sigma=0.20, option_type="call")
        assert price == 0.0

    def test_put_call_parity(self) -> None:
        S, K, T, r, sigma = 100.0, 100.0, 1.0, 0.05, 0.20
        call = bs_formula(S, K, T, r, sigma, "call")
        put = bs_formula(S, K, T, r, sigma, "put")
        parity = S - K * math.exp(-r * T)
        assert abs((call - put) - parity) < TOLERANCE

    def test_negative_T_treated_as_expired(self) -> None:
        call = bs_formula(S=110, K=100, T=-0.01, r=0.05, sigma=0.20, option_type="call")
        assert call == 10.0

    def test_zero_sigma_returns_intrinsic_discounted(self) -> None:
        """sigma=0 时返回折现后的内在价值"""
        price = bs_formula(S=110, K=100, T=1.0, r=0.05, sigma=0.0, option_type="call")
        expected = max(0.0, 110 - 100 * math.exp(-0.05))
        assert abs(price - expected) < TOLERANCE

    def test_invalid_option_type_raises(self) -> None:
        with pytest.raises(PricingError):
            bs_formula(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="straddle")

    def test_invalid_spot_raises(self) -> None:
        with pytest.raises(PricingError):
            bs_formula(S=0, K=100, T=1.0, r=0.05, sigma=0.20, option_type="call")

    def test_invalid_strike_raises(self) -> None:
        with pytest.raises(PricingError):
            bs_formula(S=100, K=-1, T=1.0, r=0.05, sigma=0.20, option_type="call")


# ---------------------------------------------------------------------------
# SMVSurface 测试
# ---------------------------------------------------------------------------


class TestSMVSurfaceFlatIV:
    """flat IV 曲面下，get_iv 应处处返回相同值。"""

    def test_get_iv_returns_flat_iv(self) -> None:
        monies = _make_monies_df([30, 60], flat_iv=0.30)
        strikes = _make_strikes_df([95, 100, 105], [30, 60])
        surface = SMVSurface(monies, strikes, spot=100.0)

        iv = surface.get_iv(100.0, 30)
        assert abs(iv - 0.30) < 0.01

    def test_get_iv_interpolates_dte(self) -> None:
        monies = _make_monies_df([30, 60], flat_iv=0.25)
        strikes = _make_strikes_df([100], [30, 60])
        surface = SMVSurface(monies, strikes, spot=100.0)

        iv = surface.get_iv(100.0, 45)
        assert abs(iv - 0.25) < 0.01

    def test_get_iv_clamps_dte_below_min(self) -> None:
        monies = _make_monies_df([30, 60], flat_iv=0.25)
        strikes = _make_strikes_df([100], [30, 60])
        surface = SMVSurface(monies, strikes, spot=100.0)

        iv = surface.get_iv(100.0, 5)  # below min dte=30
        assert abs(iv - 0.25) < 0.01

    def test_get_iv_clamps_dte_above_max(self) -> None:
        monies = _make_monies_df([30, 60], flat_iv=0.25)
        strikes = _make_strikes_df([100], [30, 60])
        surface = SMVSurface(monies, strikes, spot=100.0)

        iv = surface.get_iv(100.0, 120)  # above max dte=60
        assert abs(iv - 0.25) < 0.01

    def test_iv_floor_never_negative(self) -> None:
        monies = _make_monies_df([30], flat_iv=0.001)
        strikes = _make_strikes_df([100], [30])
        surface = SMVSurface(monies, strikes, spot=100.0)

        iv = surface.get_iv(100.0, 30)
        assert iv >= 0.001


class TestSMVSurfaceSkew:
    """带 skew 的曲面应反映不同 strike 的 IV 差异。"""

    def test_otm_put_iv_higher_than_otm_call(self) -> None:
        monies = _make_skew_monies_df([30, 60])
        # ORATS delta 约定: delta<0.5 = OTM put, delta=0.5 = ATM, delta>0.5 = OTM call
        strikes_df = pd.DataFrame([
            {"strike": 80.0, "dte": 30, "delta": 0.05},   # deep OTM put
            {"strike": 100.0, "dte": 30, "delta": 0.50},  # ATM
            {"strike": 120.0, "dte": 30, "delta": 0.95},  # deep OTM call
            {"strike": 80.0, "dte": 60, "delta": 0.08},
            {"strike": 100.0, "dte": 60, "delta": 0.50},
            {"strike": 120.0, "dte": 60, "delta": 0.92},
        ])
        surface = SMVSurface(monies, strikes_df, spot=100.0)

        iv_otm_put = surface.get_iv(80.0, 30, 100.0)
        iv_atm = surface.get_iv(100.0, 30, 100.0)
        iv_otm_call = surface.get_iv(120.0, 30, 100.0)

        assert iv_otm_put > iv_atm
        assert iv_atm > iv_otm_call


class TestSMVSurfaceSingleExpiry:
    """单 expiry 退化为 1D delta 插值。"""

    def test_single_expiry_returns_iv(self) -> None:
        monies = _make_monies_df([30], flat_iv=0.28)
        strikes = _make_strikes_df([100], [30])
        surface = SMVSurface(monies, strikes, spot=100.0)

        iv = surface.get_iv(100.0, 30)
        assert abs(iv - 0.28) < 0.01


class TestSMVSurfaceGetIvAtDelta:
    """直接用 delta 坐标查询。"""

    def test_get_iv_at_delta_50_returns_atm(self) -> None:
        monies = _make_monies_df([30, 60], flat_iv=0.22)
        strikes = _make_strikes_df([100], [30, 60])
        surface = SMVSurface(monies, strikes, spot=100.0)

        iv = surface.get_iv_at_delta(50.0, 30)
        assert abs(iv - 0.22) < 0.01


# ---------------------------------------------------------------------------
# surface_greeks 测试
# ---------------------------------------------------------------------------


class TestSurfaceGreeks:
    def test_call_delta_positive(self) -> None:
        monies = _make_monies_df([30, 60], flat_iv=0.25)
        strikes = _make_strikes_df([95, 100, 105], [30, 60])
        surface = SMVSurface(monies, strikes, spot=100.0)

        greeks = surface_greeks(100.0, 100.0, 30, surface, "call")
        assert greeks["delta"] > 0

    def test_put_delta_negative(self) -> None:
        monies = _make_monies_df([30, 60], flat_iv=0.25)
        strikes = _make_strikes_df([95, 100, 105], [30, 60])
        surface = SMVSurface(monies, strikes, spot=100.0)

        greeks = surface_greeks(100.0, 100.0, 30, surface, "put")
        assert greeks["delta"] < 0

    def test_gamma_positive(self) -> None:
        monies = _make_monies_df([30, 60], flat_iv=0.25)
        strikes = _make_strikes_df([95, 100, 105], [30, 60])
        surface = SMVSurface(monies, strikes, spot=100.0)

        greeks = surface_greeks(100.0, 100.0, 30, surface, "call")
        assert greeks["gamma"] > 0

    def test_theta_negative_for_long_call(self) -> None:
        monies = _make_monies_df([30, 60], flat_iv=0.25)
        strikes = _make_strikes_df([95, 100, 105], [30, 60])
        surface = SMVSurface(monies, strikes, spot=100.0)

        greeks = surface_greeks(100.0, 100.0, 30, surface, "call")
        assert greeks["theta"] < 0

    def test_vega_positive(self) -> None:
        monies = _make_monies_df([30, 60], flat_iv=0.25)
        strikes = _make_strikes_df([95, 100, 105], [30, 60])
        surface = SMVSurface(monies, strikes, spot=100.0)

        greeks = surface_greeks(100.0, 100.0, 30, surface, "call")
        assert greeks["vega"] > 0
