"""
engine/core/payoff_engine.py — 期权组合 Payoff 曲线计算引擎

职责: 基于期权 leg 列表计算到期 Payoff、当前 Payoff (BSM)、最大盈亏、
      breakeven 点和 Probability of Profit (POP)。纯计算函数无 IO 副作用。
依赖: math, datetime.date, pydantic, scipy.stats.norm, engine.core.bsm
被依赖: engine.steps.s06_strategy_calculator, engine.steps.s09_payoff
"""

from __future__ import annotations

import math
from datetime import date
from typing import Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict
from scipy.stats import norm

from engine.core.bsm import bsm_price

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_RISK_FREE_RATE: float = 0.05
DEFAULT_SPOT_RANGE_PCT: float = 0.15
DEFAULT_NUM_POINTS: int = 200
CONTRACT_MULTIPLIER: int = 100  # 每张期权合约对应的标的股数
DAYS_PER_YEAR: int = 365


# ---------------------------------------------------------------------------
# 异常 & 类型
# ---------------------------------------------------------------------------


class PayoffEngineError(Exception):
    """Payoff 计算相关错误"""


@runtime_checkable
class PayoffLeg(Protocol):
    """compute_payoff 所需的 leg 最小结构化接口

    任何带有以下属性的对象 (Pydantic StrategyLeg、dataclass 等) 均可作为 leg。
    """

    side: str          # "buy" | "sell"
    option_type: str   # "call" | "put"
    strike: float
    expiry: date
    qty: int
    premium: float
    iv: float


class PayoffResult(BaseModel):
    """期权组合 Payoff 计算结果"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spot_range: list[float]   # X 轴: 标的价格采样点
    expiry_pnl: list[float]   # 到期 P/L (解析解)
    current_pnl: list[float]  # 当前 P/L (BSM 估值)
    max_profit: float         # 区间内最大盈利
    max_loss: float           # 区间内最大亏损 (通常为负值)
    breakevens: list[float]   # P/L 过零点 (线性插值)
    pop: float                # Probability of Profit, [0, 1]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def compute_payoff(
    legs: Sequence[PayoffLeg],
    spot: float,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    spot_range_pct: float = DEFAULT_SPOT_RANGE_PCT,
    num_points: int = DEFAULT_NUM_POINTS,
    as_of_date: date | None = None,
) -> PayoffResult:
    """计算期权组合的 Payoff 曲线和关键指标。

    Args:
        legs: 组合中的期权 leg 列表 (至少一条)
        spot: 标的当前价格
        risk_free_rate: 年化无风险利率，用于 BSM 当前定价
        spot_range_pct: X 轴 spot 浮动范围, [spot*(1-pct), spot*(1+pct)]
        num_points: X 轴采样点数
        as_of_date: 计算当前 payoff 的基准日期，默认 date.today()

    Returns:
        PayoffResult
    """
    _validate(legs, spot, spot_range_pct, num_points)
    as_of = as_of_date or date.today()

    spot_range = _build_spot_range(spot, spot_range_pct, num_points)
    net_premium = _net_premium(legs)

    expiry_pnl = [
        round(_expiry_pnl_at(price, legs, net_premium), 2) for price in spot_range
    ]
    current_pnl = [
        round(_current_pnl_at(price, legs, risk_free_rate, as_of), 2)
        for price in spot_range
    ]

    max_profit = max(expiry_pnl)
    max_loss = min(expiry_pnl)
    breakevens = _find_breakevens(spot_range, expiry_pnl)
    pop = _estimate_pop(
        spot=spot,
        legs=legs,
        risk_free_rate=risk_free_rate,
        spot_range=spot_range,
        expiry_pnl=expiry_pnl,
        as_of_date=as_of,
    )

    return PayoffResult(
        spot_range=[round(s, 2) for s in spot_range],
        expiry_pnl=expiry_pnl,
        current_pnl=current_pnl,
        max_profit=max_profit,
        max_loss=max_loss,
        breakevens=breakevens,
        pop=round(pop, 4),
    )


# ---------------------------------------------------------------------------
# 私有辅助函数
# ---------------------------------------------------------------------------


def _validate(
    legs: Sequence[PayoffLeg],
    spot: float,
    spot_range_pct: float,
    num_points: int,
) -> None:
    if not legs:
        raise PayoffEngineError("legs must not be empty")
    if spot <= 0:
        raise PayoffEngineError(f"spot must be positive, got {spot}")
    if not (0 < spot_range_pct < 1):
        raise PayoffEngineError(
            f"spot_range_pct must be in (0, 1), got {spot_range_pct}"
        )
    if num_points < 2:
        raise PayoffEngineError(f"num_points must be >= 2, got {num_points}")


def _build_spot_range(spot: float, pct: float, n: int) -> list[float]:
    lower = spot * (1 - pct)
    upper = spot * (1 + pct)
    step = (upper - lower) / (n - 1)
    return [lower + i * step for i in range(n)]


def _net_premium(legs: Sequence[PayoffLeg]) -> float:
    """组合的初始净 premium。

    sell 为正 (现金流入)，buy 为负 (现金流出)。
    """
    return sum(
        leg.premium * leg.qty * CONTRACT_MULTIPLIER * (1 if leg.side == "sell" else -1)
        for leg in legs
    )


def _intrinsic_value(price: float, leg: PayoffLeg) -> float:
    if leg.option_type == "call":
        return max(0.0, price - leg.strike)
    return max(0.0, leg.strike - price)


def _expiry_pnl_at(
    price: float,
    legs: Sequence[PayoffLeg],
    net_premium: float,
) -> float:
    pnl = net_premium
    for leg in legs:
        intrinsic = _intrinsic_value(price, leg)
        sign = 1 if leg.side == "buy" else -1
        pnl += sign * intrinsic * leg.qty * CONTRACT_MULTIPLIER
    return pnl


def _current_pnl_at(
    price: float,
    legs: Sequence[PayoffLeg],
    risk_free_rate: float,
    as_of: date,
) -> float:
    pnl = 0.0
    for leg in legs:
        dte_days = max((leg.expiry - as_of).days, 1)
        T = dte_days / DAYS_PER_YEAR
        current_value = bsm_price(
            price, leg.strike, T, risk_free_rate, leg.iv, leg.option_type
        )
        diff = current_value - leg.premium
        sign = 1 if leg.side == "buy" else -1
        pnl += sign * diff * leg.qty * CONTRACT_MULTIPLIER
    return pnl


def _find_breakevens(
    spot_range: list[float],
    expiry_pnl: list[float],
) -> list[float]:
    """通过线性插值定位 expiry P/L 过零点。"""
    breakevens: list[float] = []
    for i in range(1, len(expiry_pnl)):
        prev_pnl = expiry_pnl[i - 1]
        curr_pnl = expiry_pnl[i]
        if prev_pnl == 0.0:
            breakevens.append(round(spot_range[i - 1], 2))
            continue
        if prev_pnl * curr_pnl < 0:
            ratio = abs(prev_pnl) / (abs(prev_pnl) + abs(curr_pnl))
            be = spot_range[i - 1] + ratio * (spot_range[i] - spot_range[i - 1])
            breakevens.append(round(be, 2))
    return breakevens


def _estimate_pop(
    spot: float,
    legs: Sequence[PayoffLeg],
    risk_free_rate: float,
    spot_range: list[float],
    expiry_pnl: list[float],
    as_of_date: date,
) -> float:
    """用 log-normal 分布近似估算 Probability of Profit。

    注意: 当盈利区间在 spot_range 之外延伸时，会因区间截断低估 POP。
    """
    dtes = [max((leg.expiry - as_of_date).days, 1) for leg in legs]
    avg_dte = sum(dtes) / len(dtes)
    T = avg_dte / DAYS_PER_YEAR
    avg_iv = sum(leg.iv for leg in legs) / len(legs)
    if T <= 0 or avg_iv <= 0:
        return 0.5

    mu = math.log(spot) + (risk_free_rate - 0.5 * avg_iv ** 2) * T
    sigma = avg_iv * math.sqrt(T)

    profit_regions = _collect_profit_regions(spot_range, expiry_pnl)
    prob = 0.0
    for lo, hi in profit_regions:
        p_lo = norm.cdf((math.log(lo) - mu) / sigma) if lo > 0 else 0.0
        p_hi = norm.cdf((math.log(hi) - mu) / sigma) if hi > 0 else 1.0
        prob += p_hi - p_lo
    return max(0.0, min(1.0, prob))


def _collect_profit_regions(
    spot_range: list[float],
    expiry_pnl: list[float],
) -> list[tuple[float, float]]:
    """收集 expiry P/L > 0 的连续区间 [lo, hi]。"""
    regions: list[tuple[float, float]] = []
    in_profit = False
    start: float = 0.0
    for i, p in enumerate(expiry_pnl):
        if p > 0 and not in_profit:
            start = spot_range[i]
            in_profit = True
        elif p <= 0 and in_profit:
            regions.append((start, spot_range[i]))
            in_profit = False
    if in_profit:
        regions.append((start, spot_range[-1]))
    return regions
