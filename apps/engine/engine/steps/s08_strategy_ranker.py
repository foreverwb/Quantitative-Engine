"""
engine/steps/s08_strategy_ranker.py — Strategy Ranker (Step 8)

职责: 对 StrategyCandidate 列表执行硬过滤和软评分，返回 Top-N 排序结果。
依赖: engine.models.strategy, engine.models.scenario, engine.models.micro
被依赖: engine.pipeline
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from engine.models.micro import MicroSnapshot
from engine.models.scenario import ScenarioResult
from engine.models.strategy import StrategyCandidate

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "strategies.yaml"

DEFAULT_TOP_N = 3


class StrategyRankerError(Exception):
    """Strategy Ranker 步骤执行失败"""


# ---------------------------------------------------------------------------
# 常量（硬过滤阈值）
# ---------------------------------------------------------------------------

HARD_FILTERS: dict[str, float] = {
    "min_oi": 500,           # 每条 leg 的最小 OI
    "max_spread_pct": 0.15,  # bid-ask spread / mid < 15%
    "max_loss_limit": 50000, # max_loss 绝对值上限
}

# 软评分权重（合计 = 1.0）
SCORE_WEIGHTS: dict[str, float] = {
    "scenario_match": 0.20,
    "ev_score": 0.25,
    "tail_risk": 0.15,
    "liquidity": 0.15,
    "theta_eff": 0.10,
    "capital_eff": 0.15,
}

ESTIMATED_SLIPPAGE_PER_LEG = 0.05   # $0.05 per contract
LIQUIDITY_SCALE_OI = 5000           # OI 对应满分 100 的参考值


# ---------------------------------------------------------------------------
# 场景策略映射（从 YAML 派生，用于场景匹配分）
# ---------------------------------------------------------------------------


def _load_scenario_strategy_map() -> dict[str, set[str]]:
    """从 strategies.yaml 构建 {scenario: {strategy_type, ...}} 映射。"""
    with open(CONFIG_PATH) as fh:
        data = yaml.safe_load(fh)
    mapping = data["strategy_mapping"]
    result: dict[str, set[str]] = {}

    for scenario, section in mapping.items():
        types: set[str] = set()
        if isinstance(section, list):
            types.update(e["type"] for e in section)
        elif isinstance(section, dict):
            for sub in section.values():
                if isinstance(sub, list):
                    types.update(e["type"] for e in sub)
        result[scenario] = types

    return result


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _check_oi(strategy: StrategyCandidate) -> tuple[bool, str]:
    for leg in strategy.legs:
        if leg.oi < HARD_FILTERS["min_oi"]:
            return False, f"OI too low: {leg.strike} {leg.option_type} OI={leg.oi}"
    return True, ""


def _check_spread(strategy: StrategyCandidate) -> tuple[bool, str]:
    for leg in strategy.legs:
        if leg.bid is not None and leg.ask is not None:
            mid = (leg.bid + leg.ask) / 2
            if mid > 0 and (leg.ask - leg.bid) / mid > HARD_FILTERS["max_spread_pct"]:
                return False, f"Spread too wide: {leg.strike}"
    return True, ""


def _check_max_loss(strategy: StrategyCandidate) -> tuple[bool, str]:
    if abs(strategy.max_loss) > HARD_FILTERS["max_loss_limit"]:
        return False, f"Max loss {strategy.max_loss} exceeds limit"
    return True, ""


# ---------------------------------------------------------------------------
# 公共函数：硬过滤
# ---------------------------------------------------------------------------


def hard_filter(strategy: StrategyCandidate) -> tuple[bool, str]:
    """
    执行硬过滤。任一条件不满足则直接排除。

    Returns:
        (True, "") 表示通过；(False, reason) 表示被过滤及原因。
    """
    for check in (_check_oi, _check_spread, _check_max_loss):
        passed, reason = check(strategy)
        if not passed:
            return False, reason
    return True, ""


# ---------------------------------------------------------------------------
# 公共函数：软评分
# ---------------------------------------------------------------------------


def compute_total_score(
    strategy: StrategyCandidate,
    scenario: ScenarioResult,
    micro: MicroSnapshot,  # noqa: ARG001  (保留签名以备后续使用)
) -> float:
    """
    计算策略综合评分（0–100）。

    分项（权重合计 1.0）:
      scenario_match (0.20) — 策略类型是否匹配当前场景
      ev_score       (0.25) — 滑点调整后 EV / max_loss
      tail_risk      (0.15) — 1 - |max_loss| / max_profit
      liquidity      (0.15) — min leg OI / 5000
      theta_eff      (0.10) — |theta| / max_loss × 10000
      capital_eff    (0.15) — EV / max_loss
    """
    scenario_map = _load_scenario_strategy_map()
    scenario_types = scenario_map.get(scenario.scenario, set())
    scenario_match = 100.0 if strategy.strategy_type in scenario_types else 50.0

    # 滑点调整后 EV
    adjusted_ev = (
        strategy.ev - len(strategy.legs) * ESTIMATED_SLIPPAGE_PER_LEG * 100
    )
    ev_score = _clip(
        adjusted_ev / max(abs(strategy.max_loss), 1) * 100,
        0, 100,
    )

    # Tail Risk
    tail_risk = (
        1 - abs(strategy.max_loss) / max(strategy.max_profit, 1)
    ) * 100
    tail_risk = _clip(tail_risk, 0, 100)

    # 流动性
    min_leg_oi = min(leg.oi for leg in strategy.legs)
    liquidity = _clip(min_leg_oi / LIQUIDITY_SCALE_OI * 100, 0, 100)

    # Theta 效率
    theta_eff = (
        abs(strategy.greeks_composite.net_theta) / max(abs(strategy.max_loss), 1) * 10000
    )
    theta_eff = _clip(theta_eff, 0, 100)

    # 资本效率
    capital_eff = strategy.ev / max(abs(strategy.max_loss), 1) * 100
    capital_eff = _clip(capital_eff, 0, 100)

    total = (
        scenario_match * SCORE_WEIGHTS["scenario_match"]
        + ev_score * SCORE_WEIGHTS["ev_score"]
        + tail_risk * SCORE_WEIGHTS["tail_risk"]
        + liquidity * SCORE_WEIGHTS["liquidity"]
        + theta_eff * SCORE_WEIGHTS["theta_eff"]
        + capital_eff * SCORE_WEIGHTS["capital_eff"]
    )
    return round(total, 2)


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def rank_strategies(
    candidates: list[StrategyCandidate],
    scenario: ScenarioResult,
    micro: MicroSnapshot,
    top_n: int = DEFAULT_TOP_N,
) -> list[StrategyCandidate]:
    """
    对候选策略列表进行硬过滤 + 软排序，返回 Top-N 结果。

    流程:
      1. 对每个 candidate 执行 hard_filter，不通过则丢弃并记录日志
      2. 对剩余候选计算 compute_total_score
      3. 按评分降序排列，截取前 top_n 个

    Returns:
        评分最高的 top_n 个 StrategyCandidate（已按评分降序排列）
    """
    passed: list[StrategyCandidate] = []
    for candidate in candidates:
        ok, reason = hard_filter(candidate)
        if ok:
            passed.append(candidate)
        else:
            logger.debug(
                "hard_filter rejected %s: %s",
                candidate.strategy_type,
                reason,
            )

    if not passed:
        logger.warning("rank_strategies: all candidates filtered out")
        return []

    scored: list[tuple[float, StrategyCandidate]] = [
        (compute_total_score(c, scenario, micro), c)
        for c in passed
    ]
    scored.sort(key=lambda x: x[0], reverse=True)

    result = [c for _, c in scored[:top_n]]
    logger.info(
        "rank_strategies: %d/%d candidates passed, returning top %d",
        len(passed),
        len(candidates),
        len(result),
    )
    return result
