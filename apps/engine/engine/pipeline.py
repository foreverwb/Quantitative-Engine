"""
engine/pipeline.py — 分析流水线编排入口

职责: 按顺序执行 Step 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9，
      将每个 Step 的输出传递给下一个 Step，最终生成 AnalysisResultSnapshot。
依赖: engine.steps.s02-s09, engine.providers.{meso_client, micro_client},
      engine.models.snapshots, engine.config
被依赖: engine.api, engine.monitor
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

from engine.models.snapshots import AnalysisResultSnapshot, MarketParameterSnapshot
from engine.providers.meso_client import MesoClient
from engine.providers.micro_client import MicroClient
from engine.steps import (
    s02_regime_gating,
    s03_pre_calculator,
    s04_field_calculator,
    s05_scenario_analyzer,
    s06_strategy_calculator,
    s07_risk_profiler,
    s08_strategy_ranker,
    s09_report_builder,
)

logger = logging.getLogger(__name__)


class PipelineError(Exception):
    """Pipeline 编排执行失败"""


class AnalysisPipeline:
    """
    分析引擎主编排器。按顺序执行 Step 2-9，每个 Step 的输出传给下一个。

    用法:
        pipeline = AnalysisPipeline(config)
        baseline, result = await pipeline.run_full("AAPL", date(2026, 4, 14))
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """
        初始化所有 client 和配置参数。

        Args:
            config: 引擎配置 (engine.yaml 解析后的 dict)，需包含:
                meso_api.base_url, engine.risk_free_rate, 等
        """
        meso_cfg = config.get("meso_api", {})
        self._meso_client = MesoClient(
            base_url=meso_cfg.get("base_url", "http://127.0.0.1:18000"),
            timeout=meso_cfg.get("timeout_seconds", 10.0),
        )
        self._orats_provider = _create_orats_provider(config)
        self._micro_client = MicroClient(self._orats_provider)

        engine_cfg = config.get("engine", {})
        self._risk_free_rate: float = engine_cfg.get("risk_free_rate", 0.05)
        self._top_n: int = engine_cfg.get("top_n_strategies", 3)
        self._payoff_num_points: int = engine_cfg.get("payoff_num_points", 200)
        self._payoff_range_pct: float = engine_cfg.get("payoff_range_pct", 0.15)

    async def run_full(
        self,
        symbol: str,
        trade_date: date,
    ) -> tuple[MarketParameterSnapshot, AnalysisResultSnapshot] | None:
        """
        执行完整分析流水线 (Step 2 → 9)。

        Args:
            symbol: 标的代码 (e.g. "AAPL")
            trade_date: 分析日期

        Returns:
            (MarketParameterSnapshot, AnalysisResultSnapshot) 或
            None (当 Gate 被 skip 时)
        """
        # ── Step 2: Regime Gating ───────────────────────────────
        try:
            context, gate_result = await s02_regime_gating.run_regime_gating(
                symbol=symbol,
                trade_date=trade_date,
                meso_client=self._meso_client,
                orats_provider=self._orats_provider,
            )
        except Exception as exc:
            logger.error("Step 2 (Regime Gating) failed: %s", exc)
            raise PipelineError(f"Step 2 failed: {exc}") from exc

        if gate_result == "skip":
            logger.info(
                "Pipeline skip: symbol=%s gate_result=skip", symbol
            )
            return None

        # ── Step 3: Pre-Calculator ──────────────────────────────
        try:
            summary = await self._orats_provider.get_summary(symbol)
            hist_summary = None
            try:
                hist_summary = await self._orats_provider.get_hist_summary(
                    symbol, start_date=None, end_date=None,
                )
            except Exception:
                logger.debug("hist_summary not available, using None")

            pre_calc = await s03_pre_calculator.run(
                context=context,
                summary=summary,
                hist_summary=hist_summary,
            )
        except Exception as exc:
            logger.error("Step 3 (Pre-Calculator) failed: %s", exc)
            raise PipelineError(f"Step 3 failed: {exc}") from exc

        # ── Step 4: Micro Data + Field Calculator ───────────────
        try:
            micro = await self._micro_client.fetch_micro_snapshot(
                symbol=symbol,
                pre_calc=pre_calc,
                scenario_seed=pre_calc.scenario_seed,
            )
            scores = s04_field_calculator.compute_field_scores(
                snapshot=micro,
                pre_calc=pre_calc,
                context=context,
            )
        except Exception as exc:
            logger.error("Step 4 (Field Calculator) failed: %s", exc)
            raise PipelineError(f"Step 4 failed: {exc}") from exc

        # ── Step 5: Scenario Analyzer ───────────────────────────
        try:
            scenario = s05_scenario_analyzer.analyze_scenario(
                scores=scores,
                context=context,
                micro=micro,
            )
        except Exception as exc:
            logger.error("Step 5 (Scenario Analyzer) failed: %s", exc)
            raise PipelineError(f"Step 5 failed: {exc}") from exc

        # ── Step 6: Strategy Calculator ─────────────────────────
        try:
            candidates = await s06_strategy_calculator.calculate_strategies(
                scenario=scenario,
                micro=micro,
                pre_calc=pre_calc,
            )
        except Exception as exc:
            logger.error("Step 6 (Strategy Calculator) failed: %s", exc)
            raise PipelineError(f"Step 6 failed: {exc}") from exc

        # ── Step 7: Risk Profiler ───────────────────────────────
        try:
            profiled: list = []
            for candidate in candidates:
                profiles = s07_risk_profiler.assign_risk_profile(candidate)
                label = profiles[0] if profiles else "balanced"
                profiled.append(
                    candidate.model_copy(update={"risk_profile": label})
                )
            candidates = profiled
        except Exception as exc:
            logger.error("Step 7 (Risk Profiler) failed: %s", exc)
            raise PipelineError(f"Step 7 failed: {exc}") from exc

        # ── Step 8: Strategy Ranker ─────────────────────────────
        try:
            top_strategies = s08_strategy_ranker.rank_strategies(
                candidates=candidates,
                scenario=scenario,
                micro=micro,
                top_n=self._top_n,
            )
        except Exception as exc:
            logger.error("Step 8 (Strategy Ranker) failed: %s", exc)
            raise PipelineError(f"Step 8 failed: {exc}") from exc

        # ── Step 9: Report Builder ──────────────────────────────
        try:
            baseline, analysis = s09_report_builder.build_report(
                context=context,
                scores=scores,
                scenario=scenario,
                top_strategies=top_strategies,
                micro=micro,
                risk_free_rate=self._risk_free_rate,
                payoff_num_points=self._payoff_num_points,
                payoff_range_pct=self._payoff_range_pct,
            )
        except Exception as exc:
            logger.error("Step 9 (Report Builder) failed: %s", exc)
            raise PipelineError(f"Step 9 failed: {exc}") from exc

        logger.info(
            "Pipeline completed: symbol=%s analysis_id=%s strategies=%d",
            symbol,
            analysis.analysis_id,
            len(analysis.strategies),
        )

        return baseline, analysis


# ---------------------------------------------------------------------------
# 私有辅助
# ---------------------------------------------------------------------------


def _create_orats_provider(config: dict[str, Any]) -> Any:
    """从配置创建 OratsProvider 实例。"""
    try:
        from provider.orats import OratsProvider
    except ImportError as exc:
        raise PipelineError(
            "无法导入 provider.orats，请确认 Micro-Provider 已安装"
        ) from exc

    orats_cfg = config.get("orats", {})
    return OratsProvider(
        api_token=orats_cfg.get("api_token", ""),
        base_url=orats_cfg.get("base_url", "https://api.orats.io/datav2"),
    )
