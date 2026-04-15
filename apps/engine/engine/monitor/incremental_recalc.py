"""
engine/monitor/incremental_recalc.py — 增量重算器

职责: 从指定 Step 开始重跑分析流程，复用之前已缓存的中间结果，
      避免不必要的数据获取和计算。
依赖: engine.pipeline, engine.steps.s03-s09, engine.models.*,
      engine.providers.micro_client
被依赖: engine.monitor.monitor_loop
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from engine.models.context import RegimeContext
from engine.models.micro import MicroSnapshot
from engine.models.scenario import ScenarioResult
from engine.models.scores import FieldScores
from engine.models.snapshots import AnalysisResultSnapshot, MarketParameterSnapshot
from engine.steps import (
    s04_field_calculator,
    s05_scenario_analyzer,
    s06_strategy_calculator,
    s07_risk_profiler,
    s08_strategy_ranker,
    s09_report_builder,
)
from engine.steps.s03_pre_calculator import PreCalculatorOutput

if TYPE_CHECKING:
    from engine.pipeline import AnalysisPipeline

logger = logging.getLogger(__name__)

# Step 编号有效范围
_MIN_STEP = 2
_MAX_STEP = 6


class IncrementalRecalcError(Exception):
    """增量重算失败"""


@dataclass(frozen=True)
class RecalcOutput:
    """增量重算的完整输出，包含所有中间结果供后续缓存。"""

    baseline: MarketParameterSnapshot
    analysis: AnalysisResultSnapshot
    context: RegimeContext
    pre_calc: PreCalculatorOutput
    micro: MicroSnapshot
    scores: FieldScores
    scenario: ScenarioResult


class IncrementalRecalculator:
    """
    增量重算：从指定 Step 开始重跑流程，复用之前的缓存结果。

    step=2: 全量重跑
    step=3: 从 Pre-Calculator 开始，复用 RegimeContext
    step=4: 从 Field Calculator 开始，复用 pre_calc
    step=5: 从场景分析开始，复用 micro + scores
    step=6: 从策略计算开始，复用 scenario

    用法:
        recalc = IncrementalRecalculator(pipeline)
        output = await recalc.recalc_from(step=4, symbol="AAPL", ...)
    """

    def __init__(self, pipeline: AnalysisPipeline) -> None:
        self._pipeline = pipeline

    async def recalc_from(
        self,
        step: int,
        symbol: str,
        trade_date: date,
        cached_context: RegimeContext | None = None,
        cached_pre_calc: PreCalculatorOutput | None = None,
        cached_micro: MicroSnapshot | None = None,
        cached_scores: FieldScores | None = None,
        cached_scenario: ScenarioResult | None = None,
    ) -> RecalcOutput | None:
        """
        从指定 step 开始重跑分析，复用之前的缓存结果。

        Returns:
            RecalcOutput（含所有中间结果）或 None（gate skip 时）

        Raises:
            IncrementalRecalcError: step 不合法或缺少必需的缓存
        """
        if step < _MIN_STEP or step > _MAX_STEP:
            raise IncrementalRecalcError(
                f"step must be {_MIN_STEP}-{_MAX_STEP}, got {step}"
            )

        logger.info("IncrementalRecalc: step=%d symbol=%s", step, symbol)

        if step <= 2:
            return await self._run_full(symbol, trade_date)

        # step >= 3: 必须有 cached_context
        context = _require(cached_context, "cached_context", step)

        pre_calc = await self._resolve_pre_calc(
            step, symbol, context, cached_pre_calc,
        )
        micro, scores = await self._resolve_micro_scores(
            step, symbol, pre_calc, context, cached_micro, cached_scores,
        )
        scenario = self._resolve_scenario(
            step, scores, context, micro, cached_scenario,
        )
        baseline, analysis = await self._run_tail(
            context, pre_calc, micro, scores, scenario,
        )

        return RecalcOutput(
            baseline=baseline,
            analysis=analysis,
            context=context,
            pre_calc=pre_calc,
            micro=micro,
            scores=scores,
            scenario=scenario,
        )

    # ------------------------------------------------------------------
    # 全量重跑
    # ------------------------------------------------------------------

    async def _run_full(
        self, symbol: str, trade_date: date,
    ) -> RecalcOutput | None:
        """
        全量重跑 Step 2-9，并捕获所有中间结果。

        与 pipeline.run_full 不同，此方法需要保存每一步的中间产物
        以便后续增量重算复用。
        """
        from engine.steps import s02_regime_gating, s03_pre_calculator

        p = self._pipeline

        context, gate_result = await s02_regime_gating.run_regime_gating(
            symbol=symbol,
            trade_date=trade_date,
            meso_client=p._meso_client,
            orats_provider=p._orats_provider,
        )
        if gate_result == "skip":
            return None

        pre_calc = await self._fetch_pre_calc(symbol, context)
        micro, scores = await self._fetch_micro_scores(
            symbol, pre_calc, context,
        )
        scenario = s05_scenario_analyzer.analyze_scenario(
            scores=scores, context=context, micro=micro,
        )
        baseline, analysis = await self._run_tail(
            context, pre_calc, micro, scores, scenario,
        )

        return RecalcOutput(
            baseline=baseline,
            analysis=analysis,
            context=context,
            pre_calc=pre_calc,
            micro=micro,
            scores=scores,
            scenario=scenario,
        )

    # ------------------------------------------------------------------
    # 中间结果解析
    # ------------------------------------------------------------------

    async def _resolve_pre_calc(
        self,
        step: int,
        symbol: str,
        context: RegimeContext,
        cached: PreCalculatorOutput | None,
    ) -> PreCalculatorOutput:
        if step <= 3:
            return await self._fetch_pre_calc(symbol, context)
        return _require(cached, "cached_pre_calc", step)

    async def _resolve_micro_scores(
        self,
        step: int,
        symbol: str,
        pre_calc: PreCalculatorOutput,
        context: RegimeContext,
        cached_micro: MicroSnapshot | None,
        cached_scores: FieldScores | None,
    ) -> tuple[MicroSnapshot, FieldScores]:
        if step <= 4:
            return await self._fetch_micro_scores(
                symbol, pre_calc, context,
            )
        return (
            _require(cached_micro, "cached_micro", step),
            _require(cached_scores, "cached_scores", step),
        )

    def _resolve_scenario(
        self,
        step: int,
        scores: FieldScores,
        context: RegimeContext,
        micro: MicroSnapshot,
        cached: ScenarioResult | None,
    ) -> ScenarioResult:
        if step <= 5:
            return s05_scenario_analyzer.analyze_scenario(
                scores=scores, context=context, micro=micro,
            )
        return _require(cached, "cached_scenario", step)

    # ------------------------------------------------------------------
    # Step 执行辅助
    # ------------------------------------------------------------------

    async def _fetch_pre_calc(
        self, symbol: str, context: RegimeContext,
    ) -> PreCalculatorOutput:
        """执行 Step 3: 获取 summary 并计算 Pre-Calculator 输出。"""
        from engine.steps import s03_pre_calculator

        p = self._pipeline
        summary = await p._orats_provider.get_summary(symbol)
        hist_summary = None
        try:
            hist_summary = await p._orats_provider.get_hist_summary(
                symbol, start_date=None, end_date=None,
            )
        except Exception:
            logger.debug("hist_summary not available, using None")

        return await s03_pre_calculator.run(
            context=context, summary=summary, hist_summary=hist_summary,
        )

    async def _fetch_micro_scores(
        self,
        symbol: str,
        pre_calc: PreCalculatorOutput,
        context: RegimeContext,
    ) -> tuple[MicroSnapshot, FieldScores]:
        """执行 Step 4: 获取 micro 数据并计算 field scores。"""
        p = self._pipeline
        micro = await p._micro_client.fetch_micro_snapshot(
            symbol=symbol,
            pre_calc=pre_calc,
            scenario_seed=pre_calc.scenario_seed,
        )
        scores = s04_field_calculator.compute_field_scores(
            snapshot=micro, pre_calc=pre_calc, context=context,
        )
        return micro, scores

    async def _run_tail(
        self,
        context: RegimeContext,
        pre_calc: PreCalculatorOutput,
        micro: MicroSnapshot,
        scores: FieldScores,
        scenario: ScenarioResult,
    ) -> tuple[MarketParameterSnapshot, AnalysisResultSnapshot]:
        """执行 Steps 6-9: 策略 → 风险 → 排序 → 报告。"""
        p = self._pipeline

        candidates = await s06_strategy_calculator.calculate_strategies(
            scenario=scenario, micro=micro, pre_calc=pre_calc,
        )

        profiled: list[Any] = []
        for c in candidates:
            labels = s07_risk_profiler.assign_risk_profile(c)
            label = labels[0] if labels else "balanced"
            profiled.append(c.model_copy(update={"risk_profile": label}))

        top = s08_strategy_ranker.rank_strategies(
            candidates=profiled, scenario=scenario,
            micro=micro, top_n=p._top_n,
        )

        return s09_report_builder.build_report(
            context=context,
            scores=scores,
            scenario=scenario,
            top_strategies=top,
            micro=micro,
            risk_free_rate=p._risk_free_rate,
            payoff_num_points=p._payoff_num_points,
            payoff_range_pct=p._payoff_range_pct,
        )


# ---------------------------------------------------------------------------
# 私有辅助
# ---------------------------------------------------------------------------

_T = Any  # generic type var placeholder


def _require(value: _T | None, name: str, step: int) -> _T:
    """验证缓存值非 None，否则抛出 IncrementalRecalcError。"""
    if value is None:
        raise IncrementalRecalcError(
            f"{name} is required when recalc_from step={step}"
        )
    return value
