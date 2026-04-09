"""
engine/core/bsm.py — Black-Scholes-Merton 定价与 Greeks 计算

职责: 实现 BSM 期权定价公式和 Greeks 计算，纯计算函数无 IO 副作用。
依赖: math, scipy.stats.norm
被依赖: engine.core.payoff_engine, engine.steps.s09_payoff
"""

import math

from scipy.stats import norm


class BSMError(Exception):
    """BSM 计算相关错误"""


def _validate_inputs(S: float, K: float, sigma: float, option_type: str) -> None:
    if S <= 0:
        raise BSMError(f"Spot price must be positive, got {S}")
    if K <= 0:
        raise BSMError(f"Strike price must be positive, got {K}")
    if sigma < 0:
        raise BSMError(f"Volatility must be non-negative, got {sigma}")
    if option_type not in ("call", "put"):
        raise BSMError(f"option_type must be 'call' or 'put', got {option_type!r}")


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    """计算 BSM d1 和 d2"""
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return d1, d2


def bsm_price(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str,
) -> float:
    """
    计算欧式期权的 BSM 理论价格。

    Args:
        S: 标的现价
        K: 行权价
        T: 到期时间（年），T <= 0 时返回内在价值
        r: 无风险利率（年化）
        sigma: 隐含波动率（年化）
        option_type: "call" 或 "put"

    Returns:
        期权理论价格（美元）
    """
    _validate_inputs(S, K, sigma, option_type)

    if T <= 0:
        if option_type == "call":
            return max(0.0, S - K)
        return max(0.0, K - S)

    d1, d2 = _d1_d2(S, K, T, r, sigma)
    discount = math.exp(-r * T)

    if option_type == "call":
        return S * norm.cdf(d1) - K * discount * norm.cdf(d2)
    else:
        return K * discount * norm.cdf(-d2) - S * norm.cdf(-d1)


def bsm_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str,
) -> dict[str, float]:
    """
    计算 BSM Greeks。

    Args:
        S: 标的现价
        K: 行权价
        T: 到期时间（年），T <= 0 时返回边界值
        r: 无风险利率（年化）
        sigma: 隐含波动率（年化）
        option_type: "call" 或 "put"

    Returns:
        dict with keys:
            delta: 期权价格对标的价格的一阶导数
            gamma: delta 对标的价格的导数（call 和 put 相同）
            theta: 每日时间衰减（除以 365）
            vega:  每 1% IV 变化对应的期权价值变化
    """
    _validate_inputs(S, K, sigma, option_type)

    if T <= 0:
        if option_type == "call":
            delta = 1.0 if S > K else 0.0
        else:
            delta = -1.0 if S < K else 0.0
        return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    d1, d2 = _d1_d2(S, K, T, r, sigma)
    sqrt_T = math.sqrt(T)
    pdf_d1 = norm.pdf(d1)
    discount = math.exp(-r * T)

    if option_type == "call":
        delta = norm.cdf(d1)
        theta = (
            -(S * pdf_d1 * sigma) / (2 * sqrt_T)
            - r * K * discount * norm.cdf(d2)
        ) / 365
    else:
        delta = norm.cdf(d1) - 1.0
        theta = (
            -(S * pdf_d1 * sigma) / (2 * sqrt_T)
            + r * K * discount * norm.cdf(-d2)
        ) / 365

    gamma = pdf_d1 / (S * sigma * sqrt_T)
    vega = S * pdf_d1 * sqrt_T / 100  # per 1% IV change

    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}
