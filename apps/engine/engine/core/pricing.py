"""
engine/core/pricing.py — SMV 曲面感知定价引擎

职责:
  1. 从 ORATS MoniesFrame 构建 IV 插值曲面 (SMVSurface)
  2. 对任意 (strike, dte) 查询曲面得到 σ(K,T)
  3. 用 BS 封闭解 + 查到的 σ 计算理论价（情景投射）
  4. 用有限差分计算曲面感知 Greeks（仅 Slider/假设场景使用）

不做:
  - 不做单 leg 真实定价（使用 ORATS callValue/putValue）
  - 不做单 leg 真实 Greeks（使用 ORATS delta/gamma/theta/vega）

依赖: scipy.interpolate, scipy.stats, pandas
被依赖: engine.core.payoff_engine, engine.api.routes_analysis (Slider 重算)
"""

from __future__ import annotations

import math

import pandas as pd
from scipy.interpolate import RectBivariateSpline, interp1d
from scipy.stats import norm


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class PricingError(Exception):
    """定价计算相关错误"""


# ---------------------------------------------------------------------------
# BS 封闭解
# ---------------------------------------------------------------------------


def _validate_inputs(S: float, K: float, option_type: str) -> None:
    """校验 BS 公式基本输入"""
    if S <= 0:
        raise PricingError(f"Spot price must be positive, got {S}")
    if K <= 0:
        raise PricingError(f"Strike price must be positive, got {K}")
    if option_type not in ("call", "put"):
        raise PricingError(f"option_type must be 'call' or 'put', got {option_type!r}")


def bs_formula(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str,
) -> float:
    """BS 封闭解公式。底层封闭解公式，由 SMVSurface 调用。

    sigma 参数由 SMVSurface.get_iv() 提供，逐 strike 不同。

    Args:
        S: 标的现价
        K: 行权价
        T: 到期时间（年），T <= 0 时返回内在价值
        r: 无风险利率（年化）
        sigma: 隐含波动率（年化，来自曲面查询）
        option_type: "call" 或 "put"

    Returns:
        期权理论价格（美元）
    """
    _validate_inputs(S, K, option_type)

    if T <= 0:
        if option_type == "call":
            return max(0.0, S - K)
        return max(0.0, K - S)

    if sigma <= 0:
        if option_type == "call":
            return max(0.0, S - K * math.exp(-r * T))
        return max(0.0, K * math.exp(-r * T) - S)

    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if option_type == "call":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


# ---------------------------------------------------------------------------
# SMV 曲面
# ---------------------------------------------------------------------------


class SMVSurface:
    """从 ORATS MoniesFrame 构建可插值的 IV 曲面。

    MoniesFrame 每行 = 一个 expiry:
    - vol0, vol5, vol10, ..., vol100: delta 0%-100% 的 IV (21 个采样点)
    - dte: 到期天数

    delta 坐标含义: delta=50 ~ ATM, delta<50 = OTM put 侧, delta>50 = OTM call 侧
    """

    DELTA_POINTS: list[int] = list(range(0, 101, 5))
    VOL_COLUMNS: list[str] = [f"vol{d}" for d in DELTA_POINTS]

    def __init__(
        self,
        monies_df: pd.DataFrame,
        strikes_df: pd.DataFrame,
        spot: float,
    ) -> None:
        """构建 SMV 曲面。

        Args:
            monies_df: MoniesFrame.df — 包含 vol0-vol100, dte
            strikes_df: StrikesFrame.df — 包含 strike, delta, dte
            spot: 当前 spot price
        """
        self._spot = spot
        self._build_surface(monies_df)
        self._build_strike_delta_map(strikes_df)

    def _build_surface(self, monies_df: pd.DataFrame) -> None:
        """构建 (delta, dte) -> IV 的 2D 插值网格"""
        df = monies_df.sort_values("dte").copy()
        self._dte_values = df["dte"].values.astype(float)

        iv_matrix = df[self.VOL_COLUMNS].values.astype(float)
        delta_array = [float(d) for d in self.DELTA_POINTS]

        if len(self._dte_values) >= 2:
            self._spline: RectBivariateSpline | None = RectBivariateSpline(
                self._dte_values, delta_array, iv_matrix, kx=1, ky=3,
            )
            self._single_expiry_interp: interp1d | None = None
        else:
            self._single_expiry_interp = interp1d(
                delta_array,
                iv_matrix[0],
                kind="cubic",
                bounds_error=False,
                fill_value=(iv_matrix[0][0], iv_matrix[0][-1]),
            )
            self._spline = None

        self._min_dte = float(self._dte_values.min())
        self._max_dte = float(self._dte_values.max())

    def _build_strike_delta_map(self, strikes_df: pd.DataFrame) -> None:
        """构建 (strike, dte) -> delta 查找表"""
        self._strike_delta_rows = strikes_df[["strike", "dte", "delta"]].copy()

    def get_iv(self, strike: float, dte: int, spot: float | None = None) -> float:
        """查询指定 (strike, dte) 的 SMV IV。

        超出范围时使用边界值（不外推），确保不返回负 IV。
        """
        effective_spot = spot or self._spot
        delta = self._strike_to_delta(strike, dte, effective_spot)
        clamped_dte = max(self._min_dte, min(self._max_dte, float(dte)))

        if self._spline is not None:
            iv = float(self._spline(clamped_dte, delta, grid=False))
        else:
            iv = float(self._single_expiry_interp(delta))  # type: ignore[misc]

        return max(iv, 0.001)

    def _strike_to_delta(self, strike: float, dte: int, spot: float) -> float:
        """将 strike 转换为 delta 坐标 (0-100)。

        优先从 StrikesFrame 查找最近 strike 的 delta；
        若无匹配，用近似公式。
        """
        df = self._strike_delta_rows
        exact = df[(df["strike"] == strike) & (df["dte"] == dte)]
        if not exact.empty:
            return float(exact["delta"].iloc[0]) * 100

        if not df.empty:
            closest_idx = (df["strike"] - strike).abs().idxmin()
            return float(df.loc[closest_idx, "delta"]) * 100

        atm_iv = self.get_iv_at_delta(50.0, dte)
        T = max(dte, 1) / 365.0
        d1 = math.log(spot / strike) / (atm_iv * math.sqrt(T))
        return float(norm.cdf(d1) * 100)

    def get_iv_at_delta(self, delta: float, dte: int) -> float:
        """直接用 delta 坐标 (0-100) 查询 IV"""
        clamped_dte = max(self._min_dte, min(self._max_dte, float(dte)))
        clamped_delta = max(0.0, min(100.0, delta))

        if self._spline is not None:
            return max(
                float(self._spline(clamped_dte, clamped_delta, grid=False)),
                0.001,
            )
        return max(float(self._single_expiry_interp(clamped_delta)), 0.001)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 曲面感知 Greeks (有限差分)
# ---------------------------------------------------------------------------


def surface_greeks(
    spot: float,
    strike: float,
    dte: int,
    smv_surface: SMVSurface,
    option_type: str,
    r: float = 0.05,
) -> dict[str, float]:
    """从 SMV 曲面用有限差分计算 Greeks。

    隐式包含 Vanna/Volga 效应——spot bump 时查到的 IV 也随 skew 变化。
    仅用于 Slider 假设场景，不用于真实仓位 Greeks（后者直接用 ORATS 值）。
    """
    h = spot * 0.005  # 0.5% bump

    def _price_at(s: float, t: int, iv_mult: float = 1.0) -> float:
        iv = smv_surface.get_iv(strike, t, s) * iv_mult
        return bs_formula(s, strike, max(t, 1) / 365, r, iv, option_type)

    v = _price_at(spot, dte)
    v_up = _price_at(spot + h, dte)
    v_down = _price_at(spot - h, dte)

    delta = (v_up - v_down) / (2 * h)
    gamma = (v_up - 2 * v + v_down) / (h**2)

    v_vol_up = _price_at(spot, dte, iv_mult=1.01)
    v_vol_down = _price_at(spot, dte, iv_mult=0.99)
    vega = (v_vol_up - v_vol_down) / 0.02

    if dte > 1:
        v_tomorrow = _price_at(spot, dte - 1)
        theta = v_tomorrow - v
    else:
        theta = -v

    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}
