"""
tests/test_pipeline.py — AnalysisPipeline 全流程集成测试

覆盖:
  - run_full 跑通完整 Step 2→9，验证 AnalysisResultSnapshot 结构
  - 输出包含 4 个 Score、场景标签、Top 3 策略（含 payoff 数据）
  - Gate skip 时返回 None
  - 单个 Step 异常时抛出 PipelineError
"""

from __future__ import annotations

import asyncio
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
from engine.pipeline import AnalysisPipeline, PipelineError
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
        s_dir=60.0,
        s_vol=10.0,
        s_conf=80.0,
        s_pers=70.0,
        quadrant="bullish_expansion",
        signal_label="directional_bias",
        event_regime="neutral",
        prob_tier="high",
    )


def _make_regime_context() -> RegimeContext:
    return RegimeContext(
        symbol=SYMBOL,
        trade_date=TRADE_DATE,
        regime_class="NORMAL",
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
    """Mock StrikesFrame with all needed columns."""
    strikes = list(range(170, 235, 5))
    n = len(strikes)
    deltas = [round(0.95 - i * (0.90 / (n - 1)), 4) for i in range(n)]
    rows = []
    for s, d in zip(strikes, deltas):
        dist = abs(s - SPOT) / SPOT
        gamma = round(max(0.005, 0.03 * (1 - dist * 5)), 4)
        call_val = round(max(0.1, SPOT - s + 5) * (0.5 + d * 0.5), 2)
        put_val = round(max(0.1, s - SPOT + 5) * (0.5 + (1 - d) * 0.5), 2)
        rows.append({
            "strike": float(s),
            "expirDate": EXPIRY_STR,
            "dte": 31,
            "delta": d,
            "gamma": gamma,
            "theta": round(-0.03 - gamma * 2, 4),
            "vega": round(0.10 + gamma * 3, 4),
            "callValue": call_val,
            "putValue": put_val,
            "smvVol": FLAT_IV,
            "callOpenInterest": 2000,
            "putOpenInterest": 2000,
            "callBidPrice": round(call_val * 0.95, 2),
            "callAskPrice": round(call_val * 1.05, 2),
            "putBidPrice": round(put_val * 0.95, 2),
            "putAskPrice": round(put_val * 1.05, 2),
            "callMidIv": FLAT_IV,
            "putMidIv": FLAT_IV,
            "spotPrice": SPOT,
            "tradeDate": "2026-04-14",
        })
    return pd.DataFrame(rows)


def _make_monies_df() -> pd.DataFrame:
    vol_cols = [f"vol{d}" for d in range(0, 101, 5)]
    return pd.DataFrame([
        {"dte": 31, "atmiv": FLAT_IV, **{c: FLAT_IV for c in vol_cols}},
        {"dte": 66, "atmiv": FLAT_IV, **{c: FLAT_IV for c in vol_cols}},
    ])


def _frame(df: pd.DataFrame) -> SimpleNamespace:
    return SimpleNamespace(df=df)


def _make_micro() -> MicroSnapshot:
    strikes_df = _make_strikes_df()
    monies_df = _make_monies_df()

    gex_df = pd.DataFrame({
        "strike": [190.0, 195.0, 200.0, 205.0, 210.0],
        "exposure_value": [-20.0, -10.0, 5.0, 30.0, 50.0],
        "expirDate": [EXPIRY_STR] * 5,
    })

    dex_df = pd.DataFrame({
        "exposure_value": [80.0, 50.0, 20.0],
    })

    term_df = pd.DataFrame({
        "dte": [31, 66],
        "atmiv": [0.25, 0.24],
    })

    return MicroSnapshot(
        strikes_combined=_frame(strikes_df),
        monies=_frame(monies_df),
        summary=SimpleNamespace(
            spotPrice=SPOT,
            atmIvM1=0.25,
            atmIvM2=0.24,
            orHv20d=0.22,
            volOfVol=0.05,
            orFcst20d=0.20,
        ),
        ivrank=SimpleNamespace(iv_rank=55.0, iv_pctl=60.0),
        gex_frame=_frame(gex_df),
        dex_frame=_frame(dex_df),
        term=_frame(term_df),
        skew=_frame(pd.DataFrame()),
        zero_gamma_strike=198.0,
        call_wall_strike=210.0,
        call_wall_gex=50.0,
        put_wall_strike=190.0,
        put_wall_gex=-20.0,
        vol_pcr=0.8,
        oi_pcr=1.1,
    )


def _make_scores() -> FieldScores:
    return FieldScores(
        gamma_score=65.0,
        break_score=55.0,
        direction_score=70.0,
        iv_score=60.0,
    )


def _make_scenario() -> ScenarioResult:
    return ScenarioResult(
        scenario="trend",
        confidence=0.85,
        method="rule_engine",
        invalidate_conditions=["direction_score 跌破 ±40"],
    )


def _make_leg(
    strike: float = 200.0,
    option_type: str = "call",
    side: str = "buy",
    premium: float = 5.0,
) -> StrategyLeg:
    return StrategyLeg(
        side=side,
        option_type=option_type,
        strike=strike,
        expiry=EXPIRY,
        premium=premium,
        iv=FLAT_IV,
        delta=0.50,
        gamma=0.02,
        theta=-0.05,
        vega=0.10,
        oi=2000,
        bid=premium * 0.95,
        ask=premium * 1.05,
    )


def _make_candidate(
    strategy_type: str = "bull_call_spread",
    ev: float = 150.0,
) -> StrategyCandidate:
    return StrategyCandidate(
        strategy_type=strategy_type,
        description=f"Mock {strategy_type}",
        legs=[
            _make_leg(strike=195.0, side="buy", premium=8.0),
            _make_leg(strike=210.0, side="sell", premium=3.0),
        ],
        net_credit_debit=-500.0,
        max_profit=1000.0,
        max_loss=500.0,
        breakevens=[200.0],
        pop=0.55,
        ev=ev,
        greeks_composite=GreeksComposite(
            net_delta=0.30,
            net_gamma=0.01,
            net_theta=-0.08,
            net_vega=0.05,
        ),
    )


def _make_summary() -> SimpleNamespace:
    return SimpleNamespace(
        spotPrice=SPOT,
        atmIvM1=0.25,
        atmIvM2=0.24,
        orHv20d=0.22,
        volOfVol=0.05,
        orFcst20d=0.20,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pipeline() -> AnalysisPipeline:
    """Create pipeline with mocked OratsProvider import."""
    with patch("engine.pipeline._create_orats_provider") as mock_create:
        mock_provider = AsyncMock()
        mock_provider.get_summary = AsyncMock(return_value=_make_summary())
        mock_provider.get_hist_summary = AsyncMock(return_value=None)
        mock_create.return_value = mock_provider
        p = AnalysisPipeline(CONFIG)
    return p


# ---------------------------------------------------------------------------
# 全流程测试
# ---------------------------------------------------------------------------


class TestPipelineRunFull:
    """用完整 mock 数据跑通 Step 2→9。"""

    @pytest.mark.asyncio
    async def test_full_pipeline_produces_valid_snapshots(
        self, pipeline: AnalysisPipeline,
    ) -> None:
        """完整流程返回 (MarketParameterSnapshot, AnalysisResultSnapshot)。"""
        context = _make_regime_context()
        pre_calc = _make_pre_calc()
        micro = _make_micro()
        scores = _make_scores()
        scenario = _make_scenario()
        candidates = [
            _make_candidate("bull_call_spread", ev=200.0),
            _make_candidate("bull_call_spread", ev=150.0),
            _make_candidate("bull_call_spread", ev=100.0),
        ]

        with (
            patch(
                "engine.pipeline.s02_regime_gating.run_regime_gating",
                new_callable=AsyncMock,
                return_value=(context, "proceed"),
            ),
            patch(
                "engine.pipeline.s03_pre_calculator.run",
                new_callable=AsyncMock,
                return_value=pre_calc,
            ),
            patch(
                "engine.pipeline.s04_field_calculator.compute_field_scores",
                return_value=scores,
            ),
            patch(
                "engine.pipeline.s05_scenario_analyzer.analyze_scenario",
                return_value=scenario,
            ),
            patch(
                "engine.pipeline.s06_strategy_calculator.calculate_strategies",
                new_callable=AsyncMock,
                return_value=candidates,
            ),
            patch(
                "engine.pipeline.s08_strategy_ranker.rank_strategies",
                return_value=candidates[:3],
            ),
        ):
            # Also mock the micro client
            pipeline._micro_client.fetch_micro_snapshot = AsyncMock(
                return_value=micro,
            )
            pipeline._orats_provider.get_summary = AsyncMock(
                return_value=_make_summary(),
            )
            pipeline._orats_provider.get_hist_summary = AsyncMock(
                return_value=None,
            )

            result = await pipeline.run_full(SYMBOL, TRADE_DATE)

        assert result is not None
        baseline, analysis = result

        # MarketParameterSnapshot 验证
        assert isinstance(baseline, MarketParameterSnapshot)
        assert baseline.symbol == SYMBOL
        assert baseline.spot_price == SPOT
        assert baseline.regime_class == "NORMAL"
        assert baseline.atm_iv_front == 0.25
        assert baseline.zero_gamma_strike == 198.0
        assert baseline.call_wall_strike == 210.0
        assert baseline.put_wall_strike == 190.0

        # AnalysisResultSnapshot 验证
        assert isinstance(analysis, AnalysisResultSnapshot)
        assert analysis.symbol == SYMBOL
        assert analysis.baseline_snapshot_id == baseline.snapshot_id

        # 4 个 Score
        assert analysis.gamma_score == 65.0
        assert analysis.break_score == 55.0
        assert analysis.direction_score == 70.0
        assert analysis.iv_score == 60.0

        # 场景标签
        assert analysis.scenario == "trend"
        assert analysis.scenario_confidence == 0.85
        assert analysis.scenario_method == "rule_engine"
        assert len(analysis.invalidate_conditions) >= 1

        # Top 3 策略 (每个含 payoff 数据)
        assert len(analysis.strategies) == 3
        for strat_dict in analysis.strategies:
            assert "strategy_type" in strat_dict
            assert "legs" in strat_dict
            assert "payoff" in strat_dict
            payoff = strat_dict["payoff"]
            assert "spot_range" in payoff
            assert "expiry_pnl" in payoff
            assert "max_profit" in payoff
            assert "max_loss" in payoff
            assert "breakevens" in payoff
            assert "pop" in payoff

        # Meso 交叉引用
        assert analysis.meso_s_dir == 60.0
        assert analysis.meso_s_vol == 10.0

    @pytest.mark.asyncio
    async def test_gate_skip_returns_none(
        self, pipeline: AnalysisPipeline,
    ) -> None:
        """Gate 被 skip 时 run_full 返回 None。"""
        context = _make_regime_context()

        with patch(
            "engine.pipeline.s02_regime_gating.run_regime_gating",
            new_callable=AsyncMock,
            return_value=(context, "skip"),
        ):
            result = await pipeline.run_full(SYMBOL, TRADE_DATE)

        assert result is None

    @pytest.mark.asyncio
    async def test_step2_failure_raises_pipeline_error(
        self, pipeline: AnalysisPipeline,
    ) -> None:
        """Step 2 失败时抛出 PipelineError。"""
        with (
            patch(
                "engine.pipeline.s02_regime_gating.run_regime_gating",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Meso API down"),
            ),
            pytest.raises(PipelineError, match="Step 2 failed"),
        ):
            await pipeline.run_full(SYMBOL, TRADE_DATE)

    @pytest.mark.asyncio
    async def test_step6_failure_raises_pipeline_error(
        self, pipeline: AnalysisPipeline,
    ) -> None:
        """Step 6 失败时抛出 PipelineError。"""
        context = _make_regime_context()
        pre_calc = _make_pre_calc()
        micro = _make_micro()
        scores = _make_scores()
        scenario = _make_scenario()

        with (
            patch(
                "engine.pipeline.s02_regime_gating.run_regime_gating",
                new_callable=AsyncMock,
                return_value=(context, "proceed"),
            ),
            patch(
                "engine.pipeline.s03_pre_calculator.run",
                new_callable=AsyncMock,
                return_value=pre_calc,
            ),
            patch(
                "engine.pipeline.s04_field_calculator.compute_field_scores",
                return_value=scores,
            ),
            patch(
                "engine.pipeline.s05_scenario_analyzer.analyze_scenario",
                return_value=scenario,
            ),
            patch(
                "engine.pipeline.s06_strategy_calculator.calculate_strategies",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Strategy calc explosion"),
            ),
        ):
            pipeline._micro_client.fetch_micro_snapshot = AsyncMock(
                return_value=micro,
            )
            pipeline._orats_provider.get_summary = AsyncMock(
                return_value=_make_summary(),
            )
            pipeline._orats_provider.get_hist_summary = AsyncMock(
                return_value=None,
            )

            with pytest.raises(PipelineError, match="Step 6 failed"):
                await pipeline.run_full(SYMBOL, TRADE_DATE)


# ---------------------------------------------------------------------------
# Step 9 Report Builder 单元测试
# ---------------------------------------------------------------------------


class TestReportBuilder:
    """直接测试 s09_report_builder.build_report。"""

    def test_build_report_structure(self) -> None:
        """验证 build_report 返回的两个快照结构完整。"""
        from engine.steps.s09_report_builder import build_report

        context = _make_regime_context()
        scores = _make_scores()
        scenario = _make_scenario()
        micro = _make_micro()
        strategies = [
            _make_candidate("bull_call_spread", ev=200.0),
            _make_candidate("bull_call_spread", ev=150.0),
        ]

        baseline, analysis = build_report(
            context=context,
            scores=scores,
            scenario=scenario,
            top_strategies=strategies,
            micro=micro,
            risk_free_rate=0.05,
            payoff_num_points=50,
            payoff_range_pct=0.15,
        )

        assert isinstance(baseline, MarketParameterSnapshot)
        assert isinstance(analysis, AnalysisResultSnapshot)
        assert analysis.baseline_snapshot_id == baseline.snapshot_id
        assert len(analysis.strategies) == 2

    def test_payoff_attached_to_each_strategy(self) -> None:
        """每个策略 dict 都包含 payoff 字段。"""
        from engine.steps.s09_report_builder import build_report

        context = _make_regime_context()
        scores = _make_scores()
        scenario = _make_scenario()
        micro = _make_micro()
        strategies = [_make_candidate()]

        _, analysis = build_report(
            context=context,
            scores=scores,
            scenario=scenario,
            top_strategies=strategies,
            micro=micro,
            risk_free_rate=0.05,
            payoff_num_points=50,
            payoff_range_pct=0.15,
        )

        assert len(analysis.strategies) == 1
        strat = analysis.strategies[0]
        assert "payoff" in strat
        payoff = strat["payoff"]
        assert len(payoff["spot_range"]) == 50
        assert len(payoff["expiry_pnl"]) == 50

    def test_market_snapshot_fields(self) -> None:
        """验证 MarketParameterSnapshot 的核心字段。"""
        from engine.steps.s09_report_builder import build_report

        context = _make_regime_context()
        scores = _make_scores()
        scenario = _make_scenario()
        micro = _make_micro()

        baseline, _ = build_report(
            context=context,
            scores=scores,
            scenario=scenario,
            top_strategies=[],
            micro=micro,
            risk_free_rate=0.05,
            payoff_num_points=50,
            payoff_range_pct=0.15,
        )

        assert baseline.spot_price == SPOT
        assert baseline.atm_iv_front == 0.25
        assert baseline.atm_iv_back == 0.24
        assert baseline.term_spread == pytest.approx(-0.01)
        assert baseline.iv30d == 0.25
        assert baseline.hv20d == 0.22
        assert baseline.vol_of_vol == 0.05
        assert baseline.iv_rank == 55.0
        assert baseline.iv_pctl == 60.0
        assert baseline.regime_class == "NORMAL"
        assert baseline.next_event_type is None
        assert baseline.days_to_event is None

    def test_empty_strategies_ok(self) -> None:
        """没有策略时依然可以构建报告。"""
        from engine.steps.s09_report_builder import build_report

        context = _make_regime_context()
        scores = _make_scores()
        scenario = _make_scenario()
        micro = _make_micro()

        baseline, analysis = build_report(
            context=context,
            scores=scores,
            scenario=scenario,
            top_strategies=[],
            micro=micro,
            risk_free_rate=0.05,
            payoff_num_points=50,
            payoff_range_pct=0.15,
        )

        assert len(analysis.strategies) == 0
        assert isinstance(baseline, MarketParameterSnapshot)
