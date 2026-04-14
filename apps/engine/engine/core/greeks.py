"""
engine/core/greeks.py — 组合 Greeks 聚合与 P/L 归因

职责:
  - 将多条 leg 的 ORATS Greeks 线性加总为组合级别 (composite_greeks)
  - 计算 P/L 归因 (Delta/Gamma/Theta/Vega 分解)
  单 leg Greeks 直接来自 ORATS API，本模块不做单 leg 计算。

依赖: engine.models.strategy
被依赖: engine.steps.s06_strategy_calculator, engine.api.routes_positions
"""

from __future__ import annotations

CONTRACT_MULTIPLIER: int = 100  # 每张期权合约对应的标的股数

from engine.models.strategy import GreeksComposite, StrategyLeg


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class GreeksError(Exception):
    """Greeks 计算相关错误"""


# ---------------------------------------------------------------------------
# 公共函数
# ---------------------------------------------------------------------------


def composite_greeks(legs: list[StrategyLeg]) -> GreeksComposite:
    """线性加总各 leg 的 ORATS Greeks 为组合级别。

    buy 为正，sell 为负；以 leg.qty 加权。

    Args:
        legs: 组合中的期权 leg 列表（Greeks 字段来自 ORATS）

    Returns:
        GreeksComposite 组合净 Greeks
    """
    if not legs:
        raise GreeksError("legs must not be empty")

    net_delta = 0.0
    net_gamma = 0.0
    net_theta = 0.0
    net_vega = 0.0

    for leg in legs:
        sign = 1 if leg.side == "buy" else -1
        net_delta += leg.delta * leg.qty * sign
        net_gamma += leg.gamma * leg.qty * sign
        net_theta += leg.theta * leg.qty * sign
        net_vega += leg.vega * leg.qty * sign

    return GreeksComposite(
        net_delta=round(net_delta, 6),
        net_gamma=round(net_gamma, 6),
        net_theta=round(net_theta, 6),
        net_vega=round(net_vega, 6),
    )


def compute_pnl_attribution(
    leg: StrategyLeg,
    current_spot: float,
    entry_spot: float,
    current_iv: float,
    entry_iv: float,
    days_held: int,
) -> dict[str, float]:
    """P/L 归因分解: Delta/Gamma/Theta/Vega。

    使用入场时的 ORATS Greeks（leg 字段），不做单 leg 重新计算。

    Args:
        leg: 入场时的 StrategyLeg（含 ORATS Greeks）
        current_spot: 当前标的价格
        entry_spot: 入场时标的价格
        current_iv: 当前 IV
        entry_iv: 入场时 IV
        days_held: 持仓天数

    Returns:
        {"delta_pnl", "gamma_pnl", "theta_pnl", "vega_pnl"}
    """
    side_sign = 1 if leg.side == "buy" else -1
    spot_move = current_spot - entry_spot
    iv_move = current_iv - entry_iv

    return {
        "delta_pnl": leg.delta * spot_move * CONTRACT_MULTIPLIER * leg.qty * side_sign,
        "gamma_pnl": (
            0.5 * leg.gamma * spot_move**2 * CONTRACT_MULTIPLIER * leg.qty * side_sign
        ),
        "theta_pnl": leg.theta * days_held * CONTRACT_MULTIPLIER * leg.qty * side_sign,
        "vega_pnl": leg.vega * iv_move * CONTRACT_MULTIPLIER * leg.qty * side_sign,
    }
