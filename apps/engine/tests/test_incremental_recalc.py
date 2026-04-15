"""
tests/test_incremental_recalc.py — IncrementalRecalculator 增量重算测试

覆盖:
  - step=4 重算: 不触发 step 2-3，直接复用 cached context + pre_calc
  - step=5 重算: 不重新获取 micro 数据
  - step=2 全量重算: 所有 step 都执行
  - step=3 重算: 复用 context，重新跑 pre_calc 及后续
  - step=6 重算: 复用 scenario，仅重跑策略 tail
  - 缺少必要缓存时抛出 IncrementalRecalcError
  - step 超出范围时抛出 IncrementalRecalcError
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from engine.models.context import EventInfo, MesoSignal, RegimeContext
from engine.models.micro import MicroSnapshot
from engine.models.scenario import ScenarioResult
from engine.models.scores import FieldScores
from engine.models.snapshots import AnalysisResultSnapshot, MarketParameterSnapshot
from engine.models.strategy import GreeksComposite, StrategyCandidate, StrategyLeg
from engine.monitor.incremental_recalc import (
    IncrementalRecalcError,
    IncrementalRecalculator,
    RecalcOutput,
)
from engine.pipeline import AnalysisPipeline
from engine.steps.s03_pre_calculator import PreCalculatorOutput

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SYMBOL = "AAPL"
TRADE_DATE = date(2026, 4, 14)
SPOT = 200.0
EXPIRY = date(2026, 5, 15)
EXPIRY_STR = "2026-05-15"
FLAT_IV = 0.25

CONFIG = {
    "meso_api": {"base_url": "http://mock:18000", "timeout_seconds": 5},
    "orats": {"api_token": "test-token", "base_url": "http://mock-orats"},
    "engine": {
        "risk_free_rate": 0.05,
        "top_n_strategies": 3,
        "payoff_num_points": 50,
        "payoff_range_pct": 0.15,
    },
}


# ---------------------------------------------------------------------------
# Mock 数据工厂
# ---------------------------------------------------------------------------


def _make_meso_signal() -> MesoSignal:
    return MesoSignal(
        s_dir=60.0, s_vol=10.0, s_conf=80.0, s_pers=70.0,
        quadrant="bullish_expansion", signal_label="directional_bias",
        event_regime="neutral", prob_tier="high",
    )


def _make_context() -> RegimeContext:
    return RegimeContext(
        symbol=SYMBOL, trade_date=TRADE_DATE, regime_class="NORMAL",
        event=EventInfo(event_type="none", event_date=None, days_to_event=None),
        meso_signal=_make_meso_signal(),
    )


def _make_pre_calc() -> PreCalculatorOutput:
    return PreCalculatorOutput(
        dyn_window_pct=0.10,
        dyn_strike_band=(180.0, 220.0),
        dyn_dte_range="14,45",
        dyn_dte_ranges=["14,45"],
        scenario_seed="trend",
        spot_price=SPOT,
    )


def _make_strikes_df() -> pd.DataFrame:
    strikes = list(range(170, 235, 5))
    n = len(strikes)
    rows = []
    for i, s in enumerate(strikes):
        d = round(0.95 - i * (0.90 / (n - 1)), 4)
        dist = abs(s - SPOT) / SPOT
        gamma = round(max(0.005, 0.03 * (1 - dist * 5)), 4)
        call_val = round(max(0.1, SPOT - s + 5) * (0.5 + d * 0.5), 2)
        put_val = round(max(0.1, s - SPOT + 5) * (0.5 + (1 - d) * 0.5), 2)
        rows.append({
            "strike": float(s), "expirDate": EXPIRY_STR, "dte": 31,
            "delta": d, "gamma": gamma,
            "theta": round(-0.03 - gamma * 2, 4),
            "vega": round(0.10 + gamma * 3, 4),
            "callValue": call_val, "putValue": put_val,
            "smvVol": FLAT_IV,
            "callOpenInterest": 2000, "putOpenInterest": 2000,
            "callBidPrice": round(call_val * 0.95, 2),
            "callAskPrice": round(call_val * 1.05, 2),
            "putBidPrice": round(put_val * 0.95, 2),
            "putAskPrice": round(put_val * 1.05, 2),
            "callMidIv": FLAT_IV, "putMidIv": FLAT_IV,
            "spotPrice": SPOT, "tradeDate": "2026-04-14",
        })
    return pd.DataFrame(rows)


def _frame(df: pd.DataFrame) -> SimpleNamespace:
    return SimpleNamespace(df=df)


def _make_micro() -> MicroSnapshot:
    strikes_df = _make_strikes_df()
    monies_df = pd.DataFrame([
        {"dte": 31, "atmiv": FLAT_IV,
         **{f"vol{d}": FLAT_IV for d in range(0, 101, 5)}},
        {"dte": 66, "atmiv": FLAT_IV,
         **{f"vol{d}": FLAT_IV for d in range(0, 101, 5)}},
    ])
    gex_df = pd.DataFrame({
        "strike": [190.0, 195.0, 200.0, 205.0, 210.0],
        "exposure_value": [-20.0, -10.0, 5.0, 30.0, 50.0],
        "expirDate": [EXPIRY_STR] * 5,
    })
    dex_df = pd.DataFrame({"exposure_value": [80.0, 50.0, 20.0]})
    term_df = pd.DataFrame({"dte": [31, 66], "atmiv": [0.25, 0.24]})

    return MicroSnapshot(
        strikes_combined=_frame(strikes_df), monies=_frame(monies_df),
        summary=SimpleNamespace(
            spotPrice=SPOT, atmIvM1=0.25, atmIvM2=0.24,
            orHv20d=0.22, volOfVol=0.05, orFcst20d=0.20,
        ),
        ivrank=SimpleNamespace(iv_rank=55.0, iv_pctl=60.0),
        gex_frame=_frame(gex_df), dex_frame=_frame(dex_df),
        term=_frame(term_df), skew=_frame(pd.DataFrame()),
        zero_gamma_strike=198.0, call_wall_strike=210.0, call_wall_gex=50.0,
        put_wall_strike=190.0, put_wall_gex=-20.0,
        vol_pcr=0.8, oi_pcr=1.1,
    )


def _make_scores() -> FieldScores:
    return FieldScores(
        gamma_score=65.0, break_score=55.0,
        direction_score=70.0, iv_score=60.0,
    )


def _make_scenario() -> ScenarioResult:
    return ScenarioResult(
        scenario="trend", confidence=0.85, method="rule_engine",
        invalidate_conditions=["direction_score 跌破 ±40"],
    )


def _make_leg(
    strike: float = 200.0, option_type: str = "call",
    side: str = "buy", premium: float = 5.0,
) -> StrategyLeg:
    return StrategyLeg(
        side=side, option_type=option_type, strike=strike,
        expiry=EXPIRY, premium=premium, iv=FLAT_IV,
        delta=0.50, gamma=0.02, theta=-0.05, vega=0.10,
        oi=2000, bid=premium * 0.95, ask=premium * 1.05,
    )


def _make_candidate(ev: float = 150.0) -> StrategyCandidate:
    return StrategyCandidate(
        strategy_type="bull_call_spread",
        description="Mock bull_call_spread",
        legs=[
            _make_leg(strike=195.0, side="buy", premium=8.0),
            _make_leg(strike=210.0, side="sell", premium=3.0),
        ],
        net_credit_debit=-500.0,
        max_profit=1000.0, max_loss=500.0,
        breakevens=[200.0], pop=0.55, ev=ev,
        greeks_composite=GreeksComposite(
            net_delta=0.30, net_gamma=0.01,
            net_theta=-0.08, net_vega=0.05,
        ),
    )


def _make_summary() -> SimpleNamespace:
    return SimpleNamespace(
        spotPrice=SPOT, atmIvM1=0.25, atmIvM2=0.24,
        orHv20d=0.22, volOfVol=0.05, orFcst20d=0.20,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pipeline() -> AnalysisPipeline:
    """Create pipeline with mocked OratsProvider."""
    with patch("engine.pipeline._create_orats_provider") as mock_create:
        mock_provider = AsyncMock()
        mock_provider.get_summary = AsyncMock(return_value=_make_summary())
        mock_provider.get_hist_summary = AsyncMock(return_value=None)
        mock_create.return_value = mock_provider
        p = AnalysisPipeline(CONFIG)
    return p


@pytest.fixture
def recalculator(pipeline: AnalysisPipeline) -> IncrementalRecalculator:
    return IncrementalRecalculator(pipeline)


# ---------------------------------------------------------------------------
# Step 范围校验
# ---------------------------------------------------------------------------


class TestStepValidation:
    """step 超出范围时抛出错误。"""

    @pytest.mark.asyncio
    async def test_step_below_min_raises_error(
        self, recalculator: IncrementalRecalculator,
    ) -> None:
        with pytest.raises(IncrementalRecalcError, match="step must be 2-6"):
            await recalculator.recalc_from(step=1, symbol=SYMBOL, trade_date=TRADE_DATE)

    @pytest.mark.asyncio
    async def test_step_above_max_raises_error(
        self, recalculator: IncrementalRecalculator,
    ) -> None:
        with pytest.raises(IncrementalRecalcError, match="step must be 2-6"):
            await recalculator.recalc_from(step=7, symbol=SYMBOL, trade_date=TRADE_DATE)


# ---------------------------------------------------------------------------
# 全量重算 (step=2)
# ---------------------------------------------------------------------------


class TestFullRecalc:
    """step=2: 全量重跑，所有 step 都执行。"""

    @pytest.mark.asyncio
    async def test_step2_runs_all_steps(
        self, recalculator: IncrementalRecalculator,
    ) -> None:
        """step=2 应执行 regime_gating → pre_calc → micro → scenario → tail。"""
        context = _make_context()
        pre_calc = _make_pre_calc()
        micro = _make_micro()
        scores = _make_scores()
        scenario = _make_scenario()
        candidates = [_make_candidate(200.0), _make_candidate(150.0)]

        mock_regime = AsyncMock(return_value=(context, "proceed"))
        mock_pre = AsyncMock(return_value=pre_calc)
        mock_scores = MagicMock(return_value=scores)
        mock_scenario = MagicMock(return_value=scenario)
        mock_strategies = AsyncMock(return_value=candidates)
        mock_ranker = MagicMock(return_value=candidates[:2])

        p = recalculator._pipeline
        p._micro_client.fetch_micro_snapshot = AsyncMock(return_value=micro)
        p._orats_provider.get_summary = AsyncMock(return_value=_make_summary())
        p._orats_provider.get_hist_summary = AsyncMock(return_value=None)

        with (
            patch("engine.steps.s02_regime_gating.run_regime_gating", mock_regime),
            patch("engine.steps.s03_pre_calculator.run", mock_pre),
            patch("engine.steps.s04_field_calculator.compute_field_scores", mock_scores),
            patch("engine.steps.s05_scenario_analyzer.analyze_scenario", mock_scenario),
            patch("engine.steps.s06_strategy_calculator.calculate_strategies", mock_strategies),
            patch("engine.steps.s08_strategy_ranker.rank_strategies", mock_ranker),
        ):
            result = await recalculator.recalc_from(
                step=2, symbol=SYMBOL, trade_date=TRADE_DATE,
            )

        assert result is not None
        assert isinstance(result, RecalcOutput)

        # 验证所有 step 都被调用
        mock_regime.assert_awaited_once()
        mock_pre.assert_awaited_once()
        mock_scores.assert_called_once()
        mock_scenario.assert_called_once()
        mock_strategies.assert_awaited_once()
        mock_ranker.assert_called_once()

    @pytest.mark.asyncio
    async def test_step2_gate_skip_returns_none(
        self, recalculator: IncrementalRecalculator,
    ) -> None:
        """step=2 当 gate 返回 skip 时返回 None。"""
        context = _make_context()
        with patch(
            "engine.steps.s02_regime_gating.run_regime_gating",
            AsyncMock(return_value=(context, "skip")),
        ):
            result = await recalculator.recalc_from(
                step=2, symbol=SYMBOL, trade_date=TRADE_DATE,
            )
        assert result is None


# ---------------------------------------------------------------------------
# Step 4 重算: 复用 context + pre_calc
# ---------------------------------------------------------------------------


class TestRecalcFromStep4:
    """step=4: 不触发 step 2-3，直接复用 cached context + pre_calc。"""

    @pytest.mark.asyncio
    async def test_step4_skips_regime_and_pre_calc(
        self, recalculator: IncrementalRecalculator,
    ) -> None:
        """step=4 不调用 regime_gating 和 pre_calculator。"""
        context = _make_context()
        pre_calc = _make_pre_calc()
        micro = _make_micro()
        scores = _make_scores()
        scenario = _make_scenario()
        candidates = [_make_candidate(200.0)]

        mock_regime = AsyncMock()
        mock_pre = AsyncMock()
        mock_scores = MagicMock(return_value=scores)
        mock_scenario = MagicMock(return_value=scenario)
        mock_strategies = AsyncMock(return_value=candidates)
        mock_ranker = MagicMock(return_value=candidates)

        p = recalculator._pipeline
        p._micro_client.fetch_micro_snapshot = AsyncMock(return_value=micro)

        with (
            patch("engine.steps.s02_regime_gating.run_regime_gating", mock_regime),
            patch("engine.steps.s03_pre_calculator.run", mock_pre),
            patch("engine.steps.s04_field_calculator.compute_field_scores", mock_scores),
            patch("engine.steps.s05_scenario_analyzer.analyze_scenario", mock_scenario),
            patch("engine.steps.s06_strategy_calculator.calculate_strategies", mock_strategies),
            patch("engine.steps.s08_strategy_ranker.rank_strategies", mock_ranker),
        ):
            result = await recalculator.recalc_from(
                step=4, symbol=SYMBOL, trade_date=TRADE_DATE,
                cached_context=context,
                cached_pre_calc=pre_calc,
            )

        assert result is not None
        # Step 2 & 3 不应被调用
        mock_regime.assert_not_awaited()
        mock_pre.assert_not_awaited()

        # Step 4+ 应被调用
        p._micro_client.fetch_micro_snapshot.assert_awaited_once()
        mock_scores.assert_called_once()
        mock_scenario.assert_called_once()
        mock_strategies.assert_awaited_once()
        mock_ranker.assert_called_once()

        # 输出缓存应包含原始 context 和 pre_calc
        assert result.context == context
        assert result.pre_calc == pre_calc

    @pytest.mark.asyncio
    async def test_step4_requires_cached_context(
        self, recalculator: IncrementalRecalculator,
    ) -> None:
        """step=4 没有 cached_context 时抛出错误。"""
        with pytest.raises(IncrementalRecalcError, match="cached_context"):
            await recalculator.recalc_from(
                step=4, symbol=SYMBOL, trade_date=TRADE_DATE,
                cached_pre_calc=_make_pre_calc(),
            )

    @pytest.mark.asyncio
    async def test_step4_requires_cached_pre_calc(
        self, recalculator: IncrementalRecalculator,
    ) -> None:
        """step=4 没有 cached_pre_calc 时抛出错误。"""
        with pytest.raises(IncrementalRecalcError, match="cached_pre_calc"):
            await recalculator.recalc_from(
                step=4, symbol=SYMBOL, trade_date=TRADE_DATE,
                cached_context=_make_context(),
            )


# ---------------------------------------------------------------------------
# Step 5 重算: 复用 micro 和 scores
# ---------------------------------------------------------------------------


class TestRecalcFromStep5:
    """step=5: 不重新获取 micro 数据，复用 cached_micro + cached_scores。"""

    @pytest.mark.asyncio
    async def test_step5_skips_micro_fetch(
        self, recalculator: IncrementalRecalculator,
    ) -> None:
        """step=5 不调用 micro_client 和 compute_field_scores。"""
        context = _make_context()
        pre_calc = _make_pre_calc()
        micro = _make_micro()
        scores = _make_scores()
        scenario = _make_scenario()
        candidates = [_make_candidate(200.0)]

        mock_micro_fetch = AsyncMock()
        mock_scores = MagicMock()
        mock_scenario = MagicMock(return_value=scenario)
        mock_strategies = AsyncMock(return_value=candidates)
        mock_ranker = MagicMock(return_value=candidates)

        p = recalculator._pipeline
        p._micro_client.fetch_micro_snapshot = mock_micro_fetch

        with (
            patch("engine.steps.s04_field_calculator.compute_field_scores", mock_scores),
            patch("engine.steps.s05_scenario_analyzer.analyze_scenario", mock_scenario),
            patch("engine.steps.s06_strategy_calculator.calculate_strategies", mock_strategies),
            patch("engine.steps.s08_strategy_ranker.rank_strategies", mock_ranker),
        ):
            result = await recalculator.recalc_from(
                step=5, symbol=SYMBOL, trade_date=TRADE_DATE,
                cached_context=context,
                cached_pre_calc=pre_calc,
                cached_micro=micro,
                cached_scores=scores,
            )

        assert result is not None
        # Micro 获取和 scores 计算不应被调用
        mock_micro_fetch.assert_not_awaited()
        mock_scores.assert_not_called()

        # 场景分析 + tail 应被调用
        mock_scenario.assert_called_once()
        mock_strategies.assert_awaited_once()

        # 输出缓存应包含原始 micro 和 scores
        assert result.micro == micro
        assert result.scores == scores

    @pytest.mark.asyncio
    async def test_step5_requires_cached_micro_and_scores(
        self, recalculator: IncrementalRecalculator,
    ) -> None:
        """step=5 缺少 cached_micro 时抛出错误。"""
        with pytest.raises(IncrementalRecalcError, match="cached_micro"):
            await recalculator.recalc_from(
                step=5, symbol=SYMBOL, trade_date=TRADE_DATE,
                cached_context=_make_context(),
                cached_pre_calc=_make_pre_calc(),
                cached_scores=_make_scores(),
            )


# ---------------------------------------------------------------------------
# Step 3 重算: 复用 context，从 Pre-Calculator 开始
# ---------------------------------------------------------------------------


class TestRecalcFromStep3:
    """step=3: 复用 RegimeContext，从 pre_calculator 开始。"""

    @pytest.mark.asyncio
    async def test_step3_skips_regime_runs_pre_calc(
        self, recalculator: IncrementalRecalculator,
    ) -> None:
        context = _make_context()
        pre_calc = _make_pre_calc()
        micro = _make_micro()
        scores = _make_scores()
        scenario = _make_scenario()
        candidates = [_make_candidate()]

        mock_regime = AsyncMock()
        mock_pre = AsyncMock(return_value=pre_calc)
        mock_scores = MagicMock(return_value=scores)
        mock_scenario = MagicMock(return_value=scenario)
        mock_strategies = AsyncMock(return_value=candidates)
        mock_ranker = MagicMock(return_value=candidates)

        p = recalculator._pipeline
        p._micro_client.fetch_micro_snapshot = AsyncMock(return_value=micro)
        p._orats_provider.get_summary = AsyncMock(return_value=_make_summary())
        p._orats_provider.get_hist_summary = AsyncMock(return_value=None)

        with (
            patch("engine.steps.s02_regime_gating.run_regime_gating", mock_regime),
            patch("engine.steps.s03_pre_calculator.run", mock_pre),
            patch("engine.steps.s04_field_calculator.compute_field_scores", mock_scores),
            patch("engine.steps.s05_scenario_analyzer.analyze_scenario", mock_scenario),
            patch("engine.steps.s06_strategy_calculator.calculate_strategies", mock_strategies),
            patch("engine.steps.s08_strategy_ranker.rank_strategies", mock_ranker),
        ):
            result = await recalculator.recalc_from(
                step=3, symbol=SYMBOL, trade_date=TRADE_DATE,
                cached_context=context,
            )

        assert result is not None
        mock_regime.assert_not_awaited()
        mock_pre.assert_awaited_once()
        mock_scores.assert_called_once()


# ---------------------------------------------------------------------------
# Step 6 重算: 复用 scenario，仅重跑策略 tail
# ---------------------------------------------------------------------------


class TestRecalcFromStep6:
    """step=6: 复用 scores 和 scenario，只重跑策略+排序+报告。"""

    @pytest.mark.asyncio
    async def test_step6_reuses_scenario(
        self, recalculator: IncrementalRecalculator,
    ) -> None:
        context = _make_context()
        pre_calc = _make_pre_calc()
        micro = _make_micro()
        scores = _make_scores()
        scenario = _make_scenario()
        candidates = [_make_candidate()]

        mock_scenario = MagicMock()
        mock_strategies = AsyncMock(return_value=candidates)
        mock_ranker = MagicMock(return_value=candidates)

        with (
            patch("engine.steps.s05_scenario_analyzer.analyze_scenario", mock_scenario),
            patch("engine.steps.s06_strategy_calculator.calculate_strategies", mock_strategies),
            patch("engine.steps.s08_strategy_ranker.rank_strategies", mock_ranker),
        ):
            result = await recalculator.recalc_from(
                step=6, symbol=SYMBOL, trade_date=TRADE_DATE,
                cached_context=context,
                cached_pre_calc=pre_calc,
                cached_micro=micro,
                cached_scores=scores,
                cached_scenario=scenario,
            )

        assert result is not None
        mock_scenario.assert_not_called()
        mock_strategies.assert_awaited_once()
        assert result.scenario == scenario

    @pytest.mark.asyncio
    async def test_step6_requires_cached_scenario(
        self, recalculator: IncrementalRecalculator,
    ) -> None:
        with pytest.raises(IncrementalRecalcError, match="cached_scenario"):
            await recalculator.recalc_from(
                step=6, symbol=SYMBOL, trade_date=TRADE_DATE,
                cached_context=_make_context(),
                cached_pre_calc=_make_pre_calc(),
                cached_micro=_make_micro(),
                cached_scores=_make_scores(),
            )
