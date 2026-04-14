"""
engine/steps/s09_report_builder.py — Report Builder (Step 9)

职责: 汇总 Steps 2-8 全部输出，为 Top-N 策略计算 payoff 数据，
      构建 MarketParameterSnapshot (基线快照) 和 AnalysisResultSnapshot。
依赖: engine.models.context, engine.models.scores, engine.models.scenario,
      engine.models.strategy, engine.models.micro, engine.models.snapshots,
      engine.core.payoff_engine, engine.core.pricing
被依赖: engine.pipeline
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from engine.core.payoff_engine import PayoffResult, compute_payoff
from engine.core.pricing import SMVSurface
from engine.models.context import RegimeContext
from engine.models.micro import MicroSnapshot
from engine.models.scenario import ScenarioResult
from engine.models.scores import FieldScores
from engine.models.snapshots import (
    AnalysisResultSnapshot,
    MarketParameterSnapshot,
)
from engine.models.strategy import StrategyCandidate

logger = logging.getLogger(__name__)


class ReportBuilderError(Exception):
    """Report Builder 步骤执行失败"""


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def build_report(
    context: RegimeContext,
    scores: FieldScores,
    scenario: ScenarioResult,
    top_strategies: list[StrategyCandidate],
    micro: MicroSnapshot,
    risk_free_rate: float,
    payoff_num_points: int,
    payoff_range_pct: float,
) -> tuple[MarketParameterSnapshot, AnalysisResultSnapshot]:
    """
    汇总所有 Step 输出，构建基线快照和分析结果快照。

    Args:
        context: RegimeContext (Step 2)
        scores: FieldScores (Step 4)
        scenario: ScenarioResult (Step 5)
        top_strategies: Top-N 排序后的策略列表 (Step 8)
        micro: MicroSnapshot (Step 4)
        risk_free_rate: 年化无风险利率 (from config)
        payoff_num_points: payoff 曲线采样点数
        payoff_range_pct: payoff 曲线 spot 浮动范围

    Returns:
        (MarketParameterSnapshot, AnalysisResultSnapshot)
    """
    now = datetime.now(UTC)
    snapshot_id = str(uuid.uuid4())
    analysis_id = str(uuid.uuid4())
    spot = float(micro.summary.spotPrice)

    baseline = _build_market_snapshot(
        snapshot_id=snapshot_id,
        context=context,
        micro=micro,
        spot=spot,
        now=now,
    )

    strategies_with_payoff = _attach_payoff_data(
        strategies=top_strategies,
        micro=micro,
        spot=spot,
        risk_free_rate=risk_free_rate,
        num_points=payoff_num_points,
        range_pct=payoff_range_pct,
    )

    analysis = AnalysisResultSnapshot(
        analysis_id=analysis_id,
        symbol=context.symbol,
        created_at=now,
        baseline_snapshot_id=snapshot_id,
        gamma_score=scores.gamma_score,
        break_score=scores.break_score,
        direction_score=scores.direction_score,
        iv_score=scores.iv_score,
        scenario=scenario.scenario,
        scenario_confidence=scenario.confidence,
        scenario_method=scenario.method,
        invalidate_conditions=list(scenario.invalidate_conditions),
        strategies=strategies_with_payoff,
        meso_s_dir=(
            context.meso_signal.s_dir if context.meso_signal else None
        ),
        meso_s_vol=(
            context.meso_signal.s_vol if context.meso_signal else None
        ),
    )

    logger.info(
        "ReportBuilder: symbol=%s analysis_id=%s strategies=%d",
        context.symbol,
        analysis_id,
        len(strategies_with_payoff),
    )

    return baseline, analysis


# ---------------------------------------------------------------------------
# 私有辅助
# ---------------------------------------------------------------------------


def _build_market_snapshot(
    snapshot_id: str,
    context: RegimeContext,
    micro: MicroSnapshot,
    spot: float,
    now: datetime,
) -> MarketParameterSnapshot:
    """从 MicroSnapshot + RegimeContext 构建 MarketParameterSnapshot。"""
    summary = micro.summary
    ivrank = micro.ivrank

    atm_iv_front = _safe_float(summary, "atmIvM1", 0.0)
    atm_iv_back = _safe_float(summary, "atmIvM2", None)
    term_spread = (
        (atm_iv_back - atm_iv_front)
        if atm_iv_back is not None
        else 0.0
    )
    iv30d = atm_iv_front
    hv20d = _safe_float(summary, "orHv20d", None)
    vrp = iv30d - (hv20d or 0.0)
    vol_of_vol = _safe_float(summary, "volOfVol", 0.0)
    iv_rank_val = _safe_float(ivrank, "iv_rank", 0.0)
    iv_pctl_val = _safe_float(ivrank, "iv_pctl", 0.0)

    net_gex = float(micro.gex_frame.df["exposure_value"].sum())
    net_dex = float(micro.dex_frame.df["exposure_value"].sum())

    return MarketParameterSnapshot(
        snapshot_id=snapshot_id,
        symbol=context.symbol,
        captured_at=now,
        spot_price=spot,
        spot_change_pct=0.0,
        atm_iv_front=atm_iv_front,
        atm_iv_back=atm_iv_back,
        term_spread=term_spread,
        iv30d=iv30d,
        hv20d=hv20d,
        vrp=vrp,
        vol_of_vol=vol_of_vol,
        iv_rank=iv_rank_val,
        iv_pctl=iv_pctl_val,
        net_gex=net_gex,
        net_dex=net_dex,
        zero_gamma_strike=micro.zero_gamma_strike,
        call_wall_strike=micro.call_wall_strike,
        put_wall_strike=micro.put_wall_strike,
        vol_pcr=micro.vol_pcr,
        oi_pcr=micro.oi_pcr,
        regime_class=context.regime_class,
        next_event_type=(
            context.event.event_type
            if context.event.event_type != "none"
            else None
        ),
        days_to_event=context.event.days_to_event,
    )


def _attach_payoff_data(
    strategies: list[StrategyCandidate],
    micro: MicroSnapshot,
    spot: float,
    risk_free_rate: float,
    num_points: int,
    range_pct: float,
) -> list[dict]:
    """为每个策略计算 payoff 并返回序列化 dict 列表。"""
    smv_surface = SMVSurface(micro.monies.df, micro.strikes_combined.df, spot)
    result: list[dict] = []

    for strategy in strategies:
        payoff = _compute_strategy_payoff(
            strategy=strategy,
            spot=spot,
            smv_surface=smv_surface,
            risk_free_rate=risk_free_rate,
            num_points=num_points,
            range_pct=range_pct,
        )
        entry = strategy.model_dump()
        if payoff is not None:
            entry["payoff"] = payoff.model_dump()
        result.append(entry)

    return result


def _compute_strategy_payoff(
    strategy: StrategyCandidate,
    spot: float,
    smv_surface: SMVSurface,
    risk_free_rate: float,
    num_points: int,
    range_pct: float,
) -> PayoffResult | None:
    """安全计算单个策略的 payoff，异常时返回 None 并记录日志。"""
    try:
        return compute_payoff(
            legs=list(strategy.legs),
            spot=spot,
            smv_surface=smv_surface,
            risk_free_rate=risk_free_rate,
            spot_range_pct=range_pct,
            num_points=num_points,
        )
    except Exception as exc:
        logger.warning(
            "Payoff computation failed for %s: %s",
            strategy.strategy_type,
            exc,
        )
        return None


def _safe_float(obj: object, attr: str, default: float | None) -> float:
    """安全提取属性为 float，缺失或无效时返回 default。"""
    value = getattr(obj, attr, None)
    if value is None:
        return default  # type: ignore[return-value]
    try:
        return float(value)
    except (TypeError, ValueError):
        return default  # type: ignore[return-value]
