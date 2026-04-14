"""
engine/steps/s06_strategy_calculator.py — Strategy Calculator (Step 6)

职责: 基于 ScenarioResult 从策略族映射选取候选策略类型，
      委托 builder 函数从 StrikesFrame 构建 StrategyCandidate。
依赖: engine.models.scenario, engine.models.micro, engine.models.strategy,
      engine.steps.s03_pre_calculator, engine.steps._s06_builders,
      engine.core.pricing
被依赖: engine.pipeline, engine.steps.s07_risk_profiler
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from engine.core.pricing import SMVSurface
from engine.models.micro import MicroSnapshot
from engine.models.scenario import ScenarioResult
from engine.models.strategy import StrategyCandidate
from engine.steps._s06_builders import BUILDER_REGISTRY, build_calendar_spread
from engine.steps.s03_pre_calculator import PreCalculatorOutput

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "strategies.yaml"


class StrategyCalculatorError(Exception):
    """Strategy Calculator 步骤执行失败"""


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------


def _load_strategy_mapping() -> dict[str, Any]:
    """加载 strategies.yaml 的策略族映射。"""
    with open(CONFIG_PATH) as fh:
        data = yaml.safe_load(fh)
    return data["strategy_mapping"]


def _resolve_strategy_types(
    scenario: ScenarioResult,
    direction: str,
) -> list[tuple[str, str]]:
    """根据场景和方向返回 [(strategy_type, description), ...]。"""
    mapping = _load_strategy_mapping()
    label = scenario.scenario
    section = mapping.get(label)
    if section is None:
        return []

    if label == "trend":
        sub = section.get(direction, [])
        return [(e["type"], e["description"]) for e in sub]

    if label == "event_volatility":
        result: list[tuple[str, str]] = []
        for sub_list in section.values():
            result.extend((e["type"], e["description"]) for e in sub_list)
        return result

    if isinstance(section, list):
        return [(e["type"], e["description"]) for e in section]

    return []


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _infer_direction(micro: MicroSnapshot) -> str:
    """从 net DEX 推断方向: bullish / bearish。"""
    net_dex = micro.dex_frame.df["exposure_value"].sum()
    return "bullish" if net_dex >= 0 else "bearish"


def _pick_expiry(strikes_df: Any) -> str | None:
    """选择数据行数最多的 expiry。"""
    counts = strikes_df["expirDate"].value_counts()
    if counts.empty:
        return None
    return str(counts.index[0])


def _pick_front_back_expiry(strikes_df: Any) -> tuple[str, str] | None:
    """选择最近和最远的两个 expiry (用于 calendar spread)。"""
    expiries = sorted(strikes_df["expirDate"].unique())
    if len(expiries) < 2:
        return None
    return str(expiries[0]), str(expiries[-1])


def _build_smv_surface(micro: MicroSnapshot, spot: float) -> SMVSurface:
    """从 MicroSnapshot 构建 SMVSurface。"""
    return SMVSurface(micro.monies.df, micro.strikes_combined.df, spot)


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


async def calculate_strategies(
    scenario: ScenarioResult,
    micro: MicroSnapshot,
    pre_calc: PreCalculatorOutput,
) -> list[StrategyCandidate]:
    """策略计算主入口: 选取策略类型 → 选 strike → 构建候选列表。"""
    spot = pre_calc.spot_price
    strikes_df = micro.strikes_combined.df
    smv_surface = _build_smv_surface(micro, spot)
    direction = _infer_direction(micro)

    strategy_types = _resolve_strategy_types(scenario, direction)
    if not strategy_types:
        logger.warning("No strategy types for scenario=%s", scenario.scenario)
        return []

    expiry = _pick_expiry(strikes_df)
    if expiry is None:
        logger.warning("No valid expiry in strikes_df")
        return []

    front_back = _pick_front_back_expiry(strikes_df)
    candidates: list[StrategyCandidate] = []

    for stype, desc in strategy_types:
        candidate = _dispatch_builder(
            stype, desc, strikes_df, spot, expiry, front_back, smv_surface,
        )
        if candidate is not None:
            candidates.append(candidate)
        else:
            logger.info("Builder skipped: %s (strike selection failed)", stype)

    logger.info(
        "StrategyCalculator: scenario=%s direction=%s built=%d/%d",
        scenario.scenario, direction, len(candidates), len(strategy_types),
    )
    return candidates


def _dispatch_builder(
    stype: str,
    desc: str,
    strikes_df: Any,
    spot: float,
    expiry: str,
    front_back: tuple[str, str] | None,
    smv_surface: SMVSurface,
) -> StrategyCandidate | None:
    """分派到对应的 builder 函数。"""
    if stype == "calendar_spread":
        if front_back is None:
            return None
        return build_calendar_spread(
            strikes_df, spot, front_back[0], front_back[1], smv_surface,
        )

    builder = BUILDER_REGISTRY.get(stype)
    if builder is None:
        logger.warning("No builder for strategy type: %s", stype)
        return None
    return builder(strikes_df, spot, expiry, smv_surface)
