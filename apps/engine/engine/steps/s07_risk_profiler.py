"""
engine/steps/s07_risk_profiler.py — Risk Profiler (Step 7)

职责: 为 StrategyCandidate 分配适用的风险偏好标签 (aggressive/balanced/conservative)。
依赖: engine.models.strategy
被依赖: engine.pipeline, engine.steps.s08_strategy_ranker
"""

from __future__ import annotations

import logging

from engine.models.strategy import StrategyCandidate, StrategyLeg

logger = logging.getLogger(__name__)

RiskProfileLabel = str  # "aggressive" | "balanced" | "conservative"


class RiskProfilerError(Exception):
    """Risk Profiler 步骤执行失败"""


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _has_protective_leg(sell_leg: StrategyLeg, all_legs: list[StrategyLeg]) -> bool:
    """判断 sell_leg 是否有对应的保护腿（同类型 buy leg）。"""
    return any(
        leg.side == "buy"
        and leg.option_type == sell_leg.option_type
        for leg in all_legs
        if leg is not sell_leg
    )


def _compute_short_gamma_ratio(legs: list[StrategyLeg]) -> float:
    """计算 short gamma 占总 gamma 绝对值的比例。"""
    total_gamma = sum(abs(leg.gamma) for leg in legs)
    short_gamma = sum(abs(leg.gamma) for leg in legs if leg.side == "sell")
    return short_gamma / max(total_gamma, 1e-9)


def _has_naked_leg(legs: list[StrategyLeg]) -> bool:
    """检查是否有裸 short（sell leg 没有同类型的 buy 保护）。"""
    return any(
        leg.side == "sell" and not _has_protective_leg(leg, legs)
        for leg in legs
    )


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def assign_risk_profile(strategy: StrategyCandidate) -> list[RiskProfileLabel]:
    """
    为策略分配适用的风险偏好标签。

    规则（可多标签）:
      aggressive  — 无裸 short
      balanced    — max_loss 有限（非 inf 且 > 0）
      conservative — balanced 且 abs_delta < 0.20 且 short_gamma_ratio < 0.30

    Returns:
        非空标签列表；若无规则命中则返回 ["balanced"]
    """
    legs = list(strategy.legs)
    is_defined_risk = (
        strategy.max_loss < float("inf") and strategy.max_loss > 0
    )
    naked = _has_naked_leg(legs)
    short_gamma_ratio = _compute_short_gamma_ratio(legs)
    abs_delta = abs(strategy.greeks_composite.net_delta)

    profiles: list[RiskProfileLabel] = []

    # 进取：无裸 short
    if not naked:
        profiles.append("aggressive")

    # 均衡：有限风险
    if is_defined_risk:
        profiles.append("balanced")

    # 保守：有限风险 + 低 delta + 低 short gamma
    if (
        is_defined_risk
        and abs_delta < 0.20
        and short_gamma_ratio < 0.30
    ):
        profiles.append("conservative")

    return profiles if profiles else ["balanced"]
