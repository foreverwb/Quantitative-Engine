"""
engine/steps/s05_scenario_analyzer.py — Scenario Analyzer (Step 5)

职责: 基于 FieldScores + RegimeContext + MicroSnapshot，
      通过规则引擎判定当前市场场景（trend / range / transition /
      volatility_mean_reversion / event_volatility）。
依赖: engine.models.scores, engine.models.context, engine.models.micro,
      engine.models.scenario
被依赖: engine.steps.s06_strategy_calculator, engine.pipeline
"""

from __future__ import annotations

import logging

from engine.models.context import RegimeContext
from engine.models.micro import MicroSnapshot
from engine.models.scenario import ScenarioResult
from engine.models.scores import FieldScores

logger = logging.getLogger(__name__)

# ── 规则阈值 (design-doc §7.1) ──
DIRECTION_SCORE_THRESHOLD = 60
ZERO_GAMMA_TREND_DISTANCE_PCT = 0.03
WALL_WIDTH_MAX_PCT = 0.08
ZERO_GAMMA_TRANSITION_DISTANCE_PCT = 0.015
MESO_SIGNAL_CONFLICT_THRESHOLD = 30
IV_SCORE_VMR_THRESHOLD = 75
EVENT_DAYS_MAX = 14
FRONT_BACK_IV_RATIO_THRESHOLD = 1.15


class ScenarioAnalyzerError(Exception):
    """Scenario Analyzer 步骤执行失败"""


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def analyze_scenario(
    scores: FieldScores,
    context: RegimeContext,
    micro: MicroSnapshot,
) -> ScenarioResult:
    """规则引擎场景分析，多候选时取 confidence 最高者。"""
    spot: float = micro.summary.spotPrice

    candidates: list[ScenarioResult] = []
    candidates.extend(_check_trend(scores, micro, spot))
    candidates.extend(_check_range(micro, context, spot))
    candidates.extend(_check_transition_zero_gamma(micro, spot))
    candidates.extend(_check_transition_signal_conflict(context))
    candidates.extend(_check_volatility_mean_reversion(scores, context))
    candidates.extend(_check_event_volatility(context, micro))

    if candidates:
        result = max(candidates, key=lambda c: c.confidence)
    else:
        result = ScenarioResult(
            scenario="range",
            confidence=0.50,
            method="rule_engine",
            invalidate_conditions=["任意方向信号强化至 > 50"],
        )

    logger.info(
        "ScenarioAnalyzer: symbol=%s scenario=%s confidence=%.2f",
        context.symbol, result.scenario, result.confidence,
    )
    return result


# ---------------------------------------------------------------------------
# Rule 1: Trend
# ---------------------------------------------------------------------------


def _check_trend(
    scores: FieldScores,
    micro: MicroSnapshot,
    spot: float,
) -> list[ScenarioResult]:
    if abs(scores.direction_score) <= DIRECTION_SCORE_THRESHOLD:
        return []
    if micro.zero_gamma_strike is None or spot <= 0:
        return []
    zero_gamma_dist = abs(spot - micro.zero_gamma_strike) / spot
    if zero_gamma_dist <= ZERO_GAMMA_TREND_DISTANCE_PCT:
        return []

    net_dex = micro.dex_frame.df["exposure_value"].sum()
    dex_aligned = (net_dex > 0) == (scores.direction_score > 0)
    if not dex_aligned:
        return []

    return [ScenarioResult(
        scenario="trend",
        confidence=0.85,
        method="rule_engine",
        invalidate_conditions=[
            "direction_score 跌破 ±40",
            "zero_gamma_distance 缩至 < 1.5%",
            "DEX 方向翻转",
        ],
    )]


# ---------------------------------------------------------------------------
# Rule 2: Range
# ---------------------------------------------------------------------------


def _check_range(
    micro: MicroSnapshot,
    context: RegimeContext,
    spot: float,
) -> list[ScenarioResult]:
    net_gex = micro.gex_frame.df["exposure_value"].sum()
    if net_gex <= 0:
        return []
    if not micro.call_wall_strike or not micro.put_wall_strike:
        return []
    if context.event.event_type != "none":
        return []

    wall_width = (
        (micro.call_wall_strike - micro.put_wall_strike) / spot
        if spot > 0 else 1.0
    )
    if wall_width >= WALL_WIDTH_MAX_PCT:
        return []

    return [ScenarioResult(
        scenario="range",
        confidence=0.80,
        method="rule_engine",
        invalidate_conditions=[
            "net_gex 翻负",
            "spot 突破 call_wall 或跌破 put_wall",
            "事件进入 T-3 窗口",
        ],
    )]


# ---------------------------------------------------------------------------
# Rule 3: Transition (两个子规则)
# ---------------------------------------------------------------------------


def _check_transition_zero_gamma(
    micro: MicroSnapshot,
    spot: float,
) -> list[ScenarioResult]:
    if micro.zero_gamma_strike is None or spot <= 0:
        return []
    dist = abs(spot - micro.zero_gamma_strike) / spot
    if dist >= ZERO_GAMMA_TRANSITION_DISTANCE_PCT:
        return []

    return [ScenarioResult(
        scenario="transition",
        confidence=0.70,
        method="rule_engine",
        invalidate_conditions=[
            "zero_gamma_distance 恢复至 > 3%",
            "direction_score 与 s_vol 方向达成一致",
        ],
    )]


def _check_transition_signal_conflict(
    context: RegimeContext,
) -> list[ScenarioResult]:
    if context.meso_signal is None:
        return []
    s_dir = context.meso_signal.s_dir
    s_vol = context.meso_signal.s_vol
    if (s_dir > 0) == (s_vol > 0):
        return []
    if (abs(s_dir) <= MESO_SIGNAL_CONFLICT_THRESHOLD
            or abs(s_vol) <= MESO_SIGNAL_CONFLICT_THRESHOLD):
        return []

    return [ScenarioResult(
        scenario="transition",
        confidence=0.65,
        method="rule_engine",
        invalidate_conditions=["方向/波动信号冲突解除"],
    )]


# ---------------------------------------------------------------------------
# Rule 4: Volatility Mean Reversion
# ---------------------------------------------------------------------------


def _check_volatility_mean_reversion(
    scores: FieldScores,
    context: RegimeContext,
) -> list[ScenarioResult]:
    if scores.iv_score <= IV_SCORE_VMR_THRESHOLD:
        return []
    if context.event.event_type != "none":
        return []

    return [ScenarioResult(
        scenario="volatility_mean_reversion",
        confidence=0.75,
        method="rule_engine",
        invalidate_conditions=[
            "iv_score 跌破 60",
            "事件进入窗口",
            "term_kink 大幅增加",
        ],
    )]


# ---------------------------------------------------------------------------
# Rule 5: Event Volatility
# ---------------------------------------------------------------------------


def _check_event_volatility(
    context: RegimeContext,
    micro: MicroSnapshot,
) -> list[ScenarioResult]:
    if context.event.event_type == "none":
        return []
    if (context.event.days_to_event is None
            or not 0 <= context.event.days_to_event <= EVENT_DAYS_MAX):
        return []

    term_df = micro.term.df
    if term_df.empty:
        return []
    front_iv = term_df["atmiv"].iloc[0]
    if len(term_df) <= 1:
        return []
    back_iv = term_df["atmiv"].iloc[-1]

    if not front_iv or not back_iv:
        return []
    if front_iv / back_iv <= FRONT_BACK_IV_RATIO_THRESHOLD:
        return []

    return [ScenarioResult(
        scenario="event_volatility",
        confidence=0.80,
        method="rule_engine",
        invalidate_conditions=[
            "front/back IV 比率 < 1.10",
            "事件已过",
        ],
    )]
