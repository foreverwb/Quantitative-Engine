"""
tests/test_bsm.py — BSM 定价与 Greeks 单元测试

覆盖: bsm_price, bsm_greeks 的正常路径、边界条件和 put-call parity。
"""

import pytest

from engine.core.bsm import BSMError, bsm_greeks, bsm_price

TOLERANCE = 0.01  # 与理论值的最大允许误差


# ---------------------------------------------------------------------------
# bsm_price 测试
# ---------------------------------------------------------------------------


class TestBsmPrice:
    def test_call_atm_one_year(self) -> None:
        """S=100, K=100, T=1, r=0.05, sigma=0.20, call → 约 10.45"""
        price = bsm_price(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="call")
        assert abs(price - 10.45) < TOLERANCE

    def test_call_atm_expired_returns_zero(self) -> None:
        """S=100, K=100, T=0, call → 内在价值 = 0 (ATM)"""
        price = bsm_price(S=100, K=100, T=0.0, r=0.05, sigma=0.20, option_type="call")
        assert price == 0.0

    def test_call_itm_expired_returns_intrinsic(self) -> None:
        """S=110, K=100, T=0, call → 内在价值 = 10 (ITM)"""
        price = bsm_price(S=110, K=100, T=0.0, r=0.05, sigma=0.20, option_type="call")
        assert price == 10.0

    def test_put_itm_expired_returns_intrinsic(self) -> None:
        """S=90, K=100, T=0, put → 内在价值 = 10 (ITM)"""
        price = bsm_price(S=90, K=100, T=0.0, r=0.05, sigma=0.20, option_type="put")
        assert price == 10.0

    def test_call_otm_expired_returns_zero(self) -> None:
        """S=90, K=100, T=0, call → 内在价值 = 0 (OTM)"""
        price = bsm_price(S=90, K=100, T=0.0, r=0.05, sigma=0.20, option_type="call")
        assert price == 0.0

    def test_put_call_parity(self) -> None:
        """Put-call parity: C - P = S - K·e^(-rT)"""
        import math

        S, K, T, r, sigma = 100.0, 100.0, 1.0, 0.05, 0.20
        call = bsm_price(S, K, T, r, sigma, "call")
        put = bsm_price(S, K, T, r, sigma, "put")
        parity = S - K * math.exp(-r * T)
        assert abs((call - put) - parity) < TOLERANCE

    def test_negative_T_treated_as_expired(self) -> None:
        """T < 0 应视同到期，返回内在价值"""
        call = bsm_price(S=110, K=100, T=-0.01, r=0.05, sigma=0.20, option_type="call")
        assert call == 10.0

    def test_invalid_option_type_raises(self) -> None:
        with pytest.raises(BSMError):
            bsm_price(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="straddle")

    def test_invalid_spot_raises(self) -> None:
        with pytest.raises(BSMError):
            bsm_price(S=0, K=100, T=1.0, r=0.05, sigma=0.20, option_type="call")

    def test_invalid_strike_raises(self) -> None:
        with pytest.raises(BSMError):
            bsm_price(S=100, K=-1, T=1.0, r=0.05, sigma=0.20, option_type="call")


# ---------------------------------------------------------------------------
# bsm_greeks 测试
# ---------------------------------------------------------------------------


class TestBsmGreeks:
    def test_put_call_parity_delta(self) -> None:
        """Put-call parity for delta: delta_call - delta_put = 1"""
        S, K, T, r, sigma = 100.0, 100.0, 1.0, 0.05, 0.20
        call_greeks = bsm_greeks(S, K, T, r, sigma, "call")
        put_greeks = bsm_greeks(S, K, T, r, sigma, "put")
        assert abs(call_greeks["delta"] - put_greeks["delta"] - 1.0) < TOLERANCE

    def test_call_delta_range(self) -> None:
        """Call delta must be in (0, 1)"""
        greeks = bsm_greeks(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="call")
        assert 0.0 < greeks["delta"] < 1.0

    def test_put_delta_range(self) -> None:
        """Put delta must be in (-1, 0)"""
        greeks = bsm_greeks(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="put")
        assert -1.0 < greeks["delta"] < 0.0

    def test_gamma_positive(self) -> None:
        """Gamma must always be positive"""
        for opt in ("call", "put"):
            greeks = bsm_greeks(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type=opt)
            assert greeks["gamma"] > 0.0

    def test_call_and_put_gamma_equal(self) -> None:
        """Call and put share the same gamma"""
        S, K, T, r, sigma = 100.0, 105.0, 0.5, 0.04, 0.25
        call_g = bsm_greeks(S, K, T, r, sigma, "call")
        put_g = bsm_greeks(S, K, T, r, sigma, "put")
        assert abs(call_g["gamma"] - put_g["gamma"]) < 1e-10

    def test_theta_negative_for_long_call(self) -> None:
        """Long call theta (time decay) should be negative"""
        greeks = bsm_greeks(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="call")
        assert greeks["theta"] < 0.0

    def test_vega_positive(self) -> None:
        """Vega must always be positive for long options"""
        for opt in ("call", "put"):
            greeks = bsm_greeks(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type=opt)
            assert greeks["vega"] > 0.0

    def test_call_and_put_vega_equal(self) -> None:
        """Call and put share the same vega"""
        S, K, T, r, sigma = 100.0, 100.0, 1.0, 0.05, 0.20
        call_g = bsm_greeks(S, K, T, r, sigma, "call")
        put_g = bsm_greeks(S, K, T, r, sigma, "put")
        assert abs(call_g["vega"] - put_g["vega"]) < 1e-10

    def test_expired_atm_call_delta_is_zero(self) -> None:
        """到期 ATM call delta = 0 (S == K)"""
        greeks = bsm_greeks(S=100, K=100, T=0.0, r=0.05, sigma=0.20, option_type="call")
        assert greeks["delta"] == 0.0
        assert greeks["gamma"] == 0.0
        assert greeks["theta"] == 0.0
        assert greeks["vega"] == 0.0

    def test_expired_itm_call_delta_is_one(self) -> None:
        """到期 ITM call delta = 1"""
        greeks = bsm_greeks(S=110, K=100, T=0.0, r=0.05, sigma=0.20, option_type="call")
        assert greeks["delta"] == 1.0

    def test_expired_itm_put_delta_is_minus_one(self) -> None:
        """到期 ITM put delta = -1"""
        greeks = bsm_greeks(S=90, K=100, T=0.0, r=0.05, sigma=0.20, option_type="put")
        assert greeks["delta"] == -1.0

    def test_vega_is_per_one_percent_iv(self) -> None:
        """Vega 应为 1% IV 变化对应的期权价值变化（数值验证）"""
        S, K, T, r = 100.0, 100.0, 1.0, 0.05
        sigma = 0.20
        delta_sigma = 0.01  # 1%
        price_up = bsm_price(S, K, T, r, sigma + delta_sigma, "call")
        price_dn = bsm_price(S, K, T, r, sigma, "call")
        numerical_vega = price_up - price_dn
        greeks = bsm_greeks(S, K, T, r, sigma, "call")
        assert abs(greeks["vega"] - numerical_vega) < TOLERANCE

    def test_invalid_option_type_raises(self) -> None:
        with pytest.raises(BSMError):
            bsm_greeks(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="invalid")
