"""
engine/core/payoff_engine.py — 曲面感知 Payoff 引擎

职责: 计算策略的到期 payoff（解析解）和当前 payoff（SMV 曲面定价），
      以及基于 Breeden-Litzenberger 风险中性密度的 POP 估算。
      纯计算函数无 IO 副作用。
依赖: math, datetime.date, pydantic, engine.core.pricing
被依赖: engine.steps.s06_strategy_calculator, engine.steps.s09_payoff
"""

from __future__ import annotations

import math
from datetime import date
from typing import Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict

from engine.core.pricing import SMVSurface, bs_formula

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


class PayoffResult(BaseModel):
    """期权组合 Payoff 计算结果"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spot_range: list[float]   # X 轴: 标的价格采样点
    expiry_pnl: list[float]   # 到期 P/L (解析解)
    current_pnl: list[float]  # 当前 P/L (SMV 曲面定价)
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
    smv_surface: SMVSurface,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    spot_range_pct: float = DEFAULT_SPOT_RANGE_PCT,
    num_points: int = DEFAULT_NUM_POINTS,
    as_of_date: date | None = None,
) -> PayoffResult:
    """计算期权组合的 Payoff 曲线和关键指标。

    Args:
        legs: 组合中的期权 leg 列表 (至少一条)
        spot: 标的当前价格
        smv_surface: ORATS SMV IV 曲面
        risk_free_rate: 年化无风险利率
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
        round(_current_pnl_at(price, legs, risk_free_rate, as_of, smv_surface), 2)
        for price in spot_range
    ]

    max_profit = max(expiry_pnl)
    max_loss = min(expiry_pnl)
    breakevens = _find_breakevens(spot_range, expiry_pnl)

    avg_dte = sum(max((leg.expiry - as_of).days, 1) for leg in legs) / len(legs)
    pop = _estimate_pop_from_surface(
        spot=spot,
        smv_surface=smv_surface,
        dte_days=int(avg_dte),
        risk_free_rate=risk_free_rate,
        spot_range=spot_range,
        expiry_pnl=expiry_pnl,
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
    """组合的初始净 premium。sell 为正 (现金流入)，buy 为负 (现金流出)。"""
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
    smv_surface: SMVSurface,
) -> float:
    """当前 P/L：逐 strike 从 SMV 曲面查询 IV 后用 BS 公式定价。"""
    pnl = 0.0
    for leg in legs:
        dte_days = max((leg.expiry - as_of).days, 1)
        iv_at_strike = smv_surface.get_iv(leg.strike, dte_days, price)
        current_value = bs_formula(
            price,
            leg.strike,
            dte_days / DAYS_PER_YEAR,
            risk_free_rate,
            iv_at_strike,
            leg.option_type,
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


def recalc_payoff_with_sliders(
    legs: Sequence[PayoffLeg],
    spot: float,
    smv_surface: SMVSurface,
    slider_dte: int,
    slider_iv_multiplier: float,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    spot_range_pct: float = DEFAULT_SPOT_RANGE_PCT,
    num_points: int = DEFAULT_NUM_POINTS,
) -> list[float]:
    """Slider 交互式 payoff 重算。

    slider_iv_multiplier 作用于整张曲面的每个查询结果:
      adjusted_iv = surface.get_iv(K, T) × multiplier
    保留 skew 形态（所有 strike 等比缩放）。

    Args:
        legs: 组合中的期权 leg 列表
        spot: 标的当前价格
        smv_surface: ORATS IV 曲面
        slider_dte: UI 滑块指定的 DTE（天数）
        slider_iv_multiplier: 曲面整体缩放倍数（保留 skew 形态）
        risk_free_rate: 年化无风险利率
        spot_range_pct: X 轴 spot 浮动范围
        num_points: X 轴采样点数

    Returns:
        pnl_curve: list[float]，长度 = num_points
    """
    spot_range = _build_spot_range(spot, spot_range_pct, num_points)
    T = max(slider_dte, 0) / DAYS_PER_YEAR

    pnl_curve: list[float] = []
    for price in spot_range:
        pnl = 0.0
        for leg in legs:
            base_iv = smv_surface.get_iv(leg.strike, slider_dte, price)
            adjusted_iv = base_iv * slider_iv_multiplier
            val = bs_formula(
                price, leg.strike, T, risk_free_rate, adjusted_iv, leg.option_type
            )
            if leg.side == "buy":
                pnl += (val - leg.premium) * leg.qty * CONTRACT_MULTIPLIER
            else:
                pnl += (leg.premium - val) * leg.qty * CONTRACT_MULTIPLIER
        pnl_curve.append(round(pnl, 2))

    return pnl_curve


def _estimate_pop_from_surface(
    spot: float,
    smv_surface: SMVSurface,
    dte_days: int,
    risk_free_rate: float,
    spot_range: list[float],
    expiry_pnl: list[float],
) -> float:
    """Breeden-Litzenberger 定理: 风险中性密度 = d^2C/dK^2

    从 SMV 曲面隐含的密度估算 POP，自然包含 skew 和尾部信息。
    """
    if dte_days <= 0 or len(spot_range) < 3:
        return 0.5

    T = dte_days / DAYS_PER_YEAR
    dK = spot_range[1] - spot_range[0]

    call_prices: list[float] = []
    for K in spot_range:
        iv = smv_surface.get_iv(K, dte_days, spot)
        c = bs_formula(spot, K, T, risk_free_rate, iv, "call")
        call_prices.append(c)

    discount = math.exp(risk_free_rate * T)
    pop = 0.0
    for i in range(1, len(call_prices) - 1):
        d2c = (
            (call_prices[i + 1] - 2 * call_prices[i] + call_prices[i - 1])
            / (dK**2)
        )
        density = d2c * discount
        if density > 0 and expiry_pnl[i] > 0:
            pop += density * dK

    return max(0.0, min(1.0, pop))
