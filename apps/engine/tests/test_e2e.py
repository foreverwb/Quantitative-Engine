"""
tests/test_e2e.py — 端到端集成测试

覆盖:
  1. Pipeline 完整流程: mock ORATS 响应 → run_full → 验证输出完整性
  2. FastAPI TestClient: POST/GET 分析、payoff、slider 重算、监控状态
  3. 监控循环: spot 偏移 3% → 红色告警 → 增量重算 → 新快照创建

依赖: engine.pipeline, engine.api, engine.monitor, engine.db
被依赖: 无（测试文件）
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from engine.models.alerts import AlertSeverity
from engine.models.context import EventInfo, MesoSignal, RegimeContext
from engine.models.micro import MicroSnapshot
from engine.models.scenario import ScenarioResult
from engine.models.scores import FieldScores
from engine.models.snapshots import (
    AnalysisResultSnapshot,
    MarketParameterSnapshot,
)
from engine.models.strategy import GreeksComposite, StrategyCandidate, StrategyLeg
from engine.monitor.alert_engine import AlertEngine
from engine.monitor.incremental_recalc import IncrementalRecalculator, RecalcOutput
from engine.pipeline import AnalysisPipeline, PipelineError
from engine.steps.s03_pre_calculator import PreCalculatorOutput

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SYMBOL = "AAPL"
TRADE_DATE = date(2026, 4, 8)
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

THRESHOLDS = {
    "tier1_market": {
        "spot_drift_pct": {
            "yellow": 0.015,
            "red": 0.030,
            "action": "recalc_from_step_4",
        },
        "atm_iv_drift_pct": {
            "yellow": 0.08,
            "red": 0.15,
            "action": "recalc_from_step_5",
        },
        "zero_gamma_drift_pct": {
            "yellow": 0.010,
            "red": 0.020,
            "action": "recalc_from_step_4",
        },
        "term_structure_flip": {"red": True, "action": "recalc_from_step_3"},
        "gex_sign_flip": {"red": True, "action": "recalc_from_step_4"},
        "vol_pcr": {
            "yellow_high": 1.0,
            "yellow_low": 0.6,
            "red_high": 1.2,
            "red_low": 0.5,
            "action": "recalc_from_step_5",
        },
    },
    "tier2_analysis": {
        "score_drift_max": {"yellow": 15, "red": 25, "action": "recalc_from_step_5"},
        "direction_flip": {"red": True, "action": "recalc_from_step_5"},
        "iv_score_change": {"yellow": 12, "red": 20, "action": "recalc_from_step_6"},
        "scenario_invalidation_count": {
            "yellow": 1,
            "red": 2,
            "action": "recalc_from_step_2",
        },
    },
    "tier3_strategy": {
        "max_loss_proximity": {"yellow": 0.50, "red": 0.75},
        "delta_drift": {"yellow": 0.15, "red": 0.30},
        "theta_realization_ratio": {"yellow_low": 0.70, "red_low": 0.50},
        "breakeven_distance_pct": {"yellow": 0.02, "red": 0.01},
        "dte_remaining": {"yellow": 5, "red": 2},
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
    """Mock StrikesFrame，覆盖完整列集。"""
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
            "dte": 37,
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
            "tradeDate": "2026-04-08",
        })
    return pd.DataFrame(rows)


def _make_monies_df() -> pd.DataFrame:
    vol_cols = [f"vol{d}" for d in range(0, 101, 5)]
    return pd.DataFrame([
        {"dte": 37, "atmiv": FLAT_IV, **{c: FLAT_IV for c in vol_cols}},
        {"dte": 72, "atmiv": FLAT_IV, **{c: FLAT_IV for c in vol_cols}},
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
    dex_df = pd.DataFrame({"exposure_value": [80.0, 50.0, 20.0]})
    term_df = pd.DataFrame({"dte": [37, 72], "atmiv": [0.25, 0.24]})

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
        bid=round(premium * 0.95, 2),
        ask=round(premium * 1.05, 2),
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


def _make_baseline(
    spot: float = SPOT,
    snapshot_id: str | None = None,
) -> MarketParameterSnapshot:
    return MarketParameterSnapshot(
        snapshot_id=snapshot_id or str(uuid.uuid4()),
        symbol=SYMBOL,
        captured_at=datetime.now(tz=timezone.utc),
        spot_price=spot,
        atm_iv_front=0.25,
        atm_iv_back=0.24,
        term_spread=-0.01,
        iv30d=0.25,
        hv20d=0.22,
        vrp=0.03,
        vol_of_vol=0.05,
        iv_rank=55.0,
        iv_pctl=60.0,
        net_gex=55.0,
        net_dex=150.0,
        zero_gamma_strike=198.0,
        call_wall_strike=210.0,
        put_wall_strike=190.0,
        vol_pcr=0.8,
        oi_pcr=1.1,
        regime_class="NORMAL",
    )


def _make_analysis(
    baseline_snapshot_id: str,
    analysis_id: str | None = None,
) -> AnalysisResultSnapshot:
    candidates = [
        _make_candidate("bull_call_spread", ev=200.0),
        _make_candidate("bull_call_spread", ev=150.0),
        _make_candidate("bull_call_spread", ev=100.0),
    ]
    strategies = [c.model_dump() for c in candidates]
    # 模拟 payoff 附加
    for s in strategies:
        s["payoff"] = {
            "spot_range": [190.0 + i for i in range(50)],
            "expiry_pnl": [float(i - 25) for i in range(50)],
            "current_pnl": [float(i - 20) for i in range(50)],
            "max_profit": 25.0,
            "max_loss": -25.0,
            "breakevens": [200.0],
            "pop": 0.55,
        }

    return AnalysisResultSnapshot(
        analysis_id=analysis_id or str(uuid.uuid4()),
        symbol=SYMBOL,
        created_at=datetime.now(tz=timezone.utc),
        baseline_snapshot_id=baseline_snapshot_id,
        gamma_score=65.0,
        break_score=55.0,
        direction_score=70.0,
        iv_score=60.0,
        scenario="trend",
        scenario_confidence=0.85,
        scenario_method="rule_engine",
        invalidate_conditions=["direction_score 跌破 ±40"],
        strategies=strategies,
        meso_s_dir=60.0,
        meso_s_vol=10.0,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pipeline() -> AnalysisPipeline:
    """创建 Pipeline，mock 掉 OratsProvider。"""
    with patch("engine.pipeline._create_orats_provider") as mock_create:
        mock_provider = AsyncMock()
        mock_provider.get_summary = AsyncMock(return_value=_make_summary())
        mock_provider.get_hist_summary = AsyncMock(return_value=None)
        mock_create.return_value = mock_provider
        p = AnalysisPipeline(CONFIG)
    return p


@pytest.fixture
def _test_db():
    """初始化内存 SQLite 数据库（StaticPool 保证单连接共享）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from engine.db.models import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    # 注入到 engine.db.session 全局变量，使 get_db 可用
    import engine.db.session as db_mod
    db_mod._engine = engine
    db_mod._SessionLocal = session_local

    yield session_local

    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture
def client(_test_db) -> TestClient:
    """创建 TestClient，跳过 lifespan 以避免真实初始化。"""
    from engine.api.routes_analysis import router as analysis_router
    from engine.api.routes_monitor import router as monitor_router

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(analysis_router)
    app.include_router(monitor_router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Part 1: Pipeline 完整流程测试
# ---------------------------------------------------------------------------


class TestPipelineE2E:
    """使用 fixture 数据模拟完整 pipeline 流程。"""

    @pytest.mark.asyncio
    async def test_run_full_produces_complete_output(
        self, pipeline: AnalysisPipeline,
    ) -> None:
        """run_full 返回完整的 (baseline, analysis) 且结构正确。"""
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

        # MarketParameterSnapshot 完整性
        assert isinstance(baseline, MarketParameterSnapshot)
        assert baseline.symbol == SYMBOL
        assert baseline.spot_price == SPOT
        assert baseline.regime_class == "NORMAL"
        assert baseline.atm_iv_front == 0.25
        assert baseline.zero_gamma_strike == 198.0
        assert baseline.call_wall_strike == 210.0
        assert baseline.put_wall_strike == 190.0

        # AnalysisResultSnapshot 完整性
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

        # Top 3 策略，每个含 payoff 数据
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
            assert len(payoff["spot_range"]) == 50
            assert len(payoff["expiry_pnl"]) == 50

        # Meso 交叉引用
        assert analysis.meso_s_dir == 60.0
        assert analysis.meso_s_vol == 10.0


# ---------------------------------------------------------------------------
# Part 2: FastAPI TestClient API 端点测试
# ---------------------------------------------------------------------------


class TestAPIEndpoints:
    """使用 TestClient 测试 API 端点完整交互流程。"""

    def _seed_analysis(self, client: TestClient) -> tuple[str, str]:
        """向数据库插入一条分析记录，返回 (analysis_id, snapshot_id)。"""
        from engine.db.models import (
            AnalysisResultSnapshotRow,
            MarketParameterSnapshotRow,
        )
        import engine.db.session as db_mod

        db = db_mod._SessionLocal()

        baseline = _make_baseline()
        analysis = _make_analysis(baseline.snapshot_id)

        mps_row = MarketParameterSnapshotRow(
            snapshot_id=baseline.snapshot_id,
            symbol=baseline.symbol,
            captured_at=baseline.captured_at,
            data_json=baseline.model_dump_json(),
        )
        db.add(mps_row)

        scores_json = json.dumps({
            "gamma_score": analysis.gamma_score,
            "break_score": analysis.break_score,
            "direction_score": analysis.direction_score,
            "iv_score": analysis.iv_score,
        })
        ars_row = AnalysisResultSnapshotRow(
            analysis_id=analysis.analysis_id,
            symbol=analysis.symbol,
            created_at=analysis.created_at,
            baseline_snapshot_id=analysis.baseline_snapshot_id,
            scores_json=scores_json,
            scenario=analysis.scenario,
            scenario_confidence=analysis.scenario_confidence,
            strategies_json=json.dumps(analysis.strategies, default=str),
            meso_json=json.dumps(
                {"s_dir": analysis.meso_s_dir, "s_vol": analysis.meso_s_vol},
            ),
        )
        db.add(ars_row)
        db.commit()

        return analysis.analysis_id, baseline.snapshot_id

    def test_post_analysis_via_pipeline(self, client: TestClient) -> None:
        """POST /api/v2/analysis/AAPL → 得到 analysis_id。"""
        from engine.api import routes_analysis

        baseline = _make_baseline()
        analysis = _make_analysis(baseline.snapshot_id)

        mock_pipeline = AsyncMock()
        mock_pipeline.run_full = AsyncMock(return_value=(baseline, analysis))
        routes_analysis.set_pipeline(mock_pipeline)

        resp = client.post(f"/api/v2/analysis/{SYMBOL}")
        assert resp.status_code == 200
        data = resp.json()
        assert "analysis_id" in data
        assert data["analysis_id"] == analysis.analysis_id

        # 清理
        routes_analysis.set_pipeline(None)

    def test_get_analysis_contains_strategies(self, client: TestClient) -> None:
        """GET /api/v2/analysis/{id} → 验证包含 strategies。"""
        analysis_id, _ = self._seed_analysis(client)

        resp = client.get(f"/api/v2/analysis/{analysis_id}")
        assert resp.status_code == 200
        data = resp.json()

        assert data["analysis_id"] == analysis_id
        assert data["symbol"] == SYMBOL
        assert data["scenario"] == "trend"
        assert "scores" in data
        assert data["scores"]["gamma_score"] == 65.0
        assert "strategies" in data
        assert len(data["strategies"]) == 3
        for strat in data["strategies"]:
            assert "strategy_type" in strat
            assert "legs" in strat

    def test_get_analysis_not_found(self, client: TestClient) -> None:
        """GET /api/v2/analysis/{id} → 404。"""
        resp = client.get("/api/v2/analysis/nonexistent-id")
        assert resp.status_code == 404

    def test_get_payoff_returns_data(self, client: TestClient) -> None:
        """GET /api/v2/analysis/{id}/payoff/0 → 验证 payoff 数据。"""
        analysis_id, _ = self._seed_analysis(client)

        resp = client.get(f"/api/v2/analysis/{analysis_id}/payoff/0")
        assert resp.status_code == 200
        data = resp.json()

        assert data["analysis_id"] == analysis_id
        assert data["strategy_index"] == 0
        assert data["strategy_type"] == "bull_call_spread"
        assert "payoff" in data
        payoff = data["payoff"]
        assert "spot_range" in payoff
        assert "expiry_pnl" in payoff
        assert len(payoff["spot_range"]) == 50

    def test_get_payoff_out_of_range(self, client: TestClient) -> None:
        """GET /api/v2/analysis/{id}/payoff/99 → 404。"""
        analysis_id, _ = self._seed_analysis(client)

        resp = client.get(f"/api/v2/analysis/{analysis_id}/payoff/99")
        assert resp.status_code == 404

    def test_post_recalc_payoff_with_sliders(self, client: TestClient) -> None:
        """POST /api/v2/analysis/{id}/payoff/0/recalc → 验证 slider 重算。"""
        analysis_id, _ = self._seed_analysis(client)

        resp = client.post(
            f"/api/v2/analysis/{analysis_id}/payoff/0/recalc",
            json={"slider_dte": 15, "slider_iv_multiplier": 1.2},
        )
        assert resp.status_code == 200
        data = resp.json()

        assert data["analysis_id"] == analysis_id
        assert data["strategy_index"] == 0
        assert data["slider_dte"] == 15
        assert data["slider_iv_multiplier"] == 1.2
        assert "pnl_curve" in data
        assert isinstance(data["pnl_curve"], list)
        assert len(data["pnl_curve"]) == 200  # DEFAULT_NUM_POINTS

    def test_post_recalc_not_found(self, client: TestClient) -> None:
        """POST recalc with nonexistent analysis_id → 404。"""
        resp = client.post(
            "/api/v2/analysis/nonexistent/payoff/0/recalc",
            json={"slider_dte": 10, "slider_iv_multiplier": 1.0},
        )
        assert resp.status_code == 404

    def test_get_monitor_state_not_found(self, client: TestClient) -> None:
        """GET /api/v2/monitor/AAPL/state → 404 (no monitor data yet)。"""
        resp = client.get(f"/api/v2/monitor/{SYMBOL}/state")
        assert resp.status_code == 404

    def test_get_monitor_state_with_data(self, client: TestClient) -> None:
        """GET /api/v2/monitor/AAPL/state → 200 with seeded data。"""
        import engine.db.session as db_mod
        from engine.db.models import MonitorStateSnapshotRow
        from engine.models.snapshots import MonitorStateSnapshot

        db = db_mod._SessionLocal()
        monitor_state = MonitorStateSnapshot(
            monitor_id=str(uuid.uuid4()),
            symbol=SYMBOL,
            captured_at=datetime.now(tz=timezone.utc),
            analysis_id="test-analysis-id",
            baseline_snapshot_id="test-snapshot-id",
            spot_drift_pct=0.02,
            iv_drift_pct=0.05,
        )
        db.add(MonitorStateSnapshotRow(
            monitor_id=monitor_state.monitor_id,
            symbol=monitor_state.symbol,
            captured_at=monitor_state.captured_at,
            analysis_id=monitor_state.analysis_id,
            baseline_snapshot_id=monitor_state.baseline_snapshot_id,
            state_json=monitor_state.model_dump_json(),
        ))
        db.commit()

        resp = client.get(f"/api/v2/monitor/{SYMBOL}/state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == SYMBOL
        assert "state" in data
        state = data["state"]
        assert state["spot_drift_pct"] == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# Part 3: 监控循环验证
# ---------------------------------------------------------------------------


class TestMonitorCycle:
    """验证告警引擎 + 增量重算的完整交互。"""

    def test_spot_3pct_drift_triggers_red_alert(self) -> None:
        """spot 偏移 3% → 触发 Tier 1 红色告警。"""
        baseline = _make_baseline(spot=200.0)
        # 现价 206 → 偏移 3%
        current = _make_baseline(spot=206.0)

        analysis = _make_analysis(baseline.snapshot_id)
        engine = AlertEngine(thresholds_config=THRESHOLDS)

        alerts, recalc_action = engine.evaluate(
            current=current,
            baseline=baseline,
            analysis=analysis,
            positions=[],
        )

        # 应有 spot_drift_pct 红色告警
        red_alerts = [
            a for a in alerts
            if a.severity == AlertSeverity.RED and a.indicator == "spot_drift_pct"
        ]
        assert len(red_alerts) >= 1
        assert recalc_action is not None
        assert "recalc_from_step_" in recalc_action

    def test_spot_3pct_triggers_recalc_from_step_4(self) -> None:
        """spot_drift_pct 红线 action 是 recalc_from_step_4。"""
        baseline = _make_baseline(spot=200.0)
        current = _make_baseline(spot=206.0)
        analysis = _make_analysis(baseline.snapshot_id)

        engine = AlertEngine(thresholds_config=THRESHOLDS)
        alerts, recalc_action = engine.evaluate(
            current=current,
            baseline=baseline,
            analysis=analysis,
            positions=[],
        )

        # 最高优先级 action 应从 step 4 开始
        assert recalc_action is not None
        step = int(recalc_action.split("_")[-1])
        assert step <= 4

    @pytest.mark.asyncio
    async def test_incremental_recalc_from_step4(
        self, pipeline: AnalysisPipeline,
    ) -> None:
        """增量重算从 step 4 开始，复用 context 和 pre_calc。"""
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
                "engine.monitor.incremental_recalc.s04_field_calculator"
                ".compute_field_scores",
                return_value=scores,
            ),
            patch(
                "engine.monitor.incremental_recalc.s05_scenario_analyzer"
                ".analyze_scenario",
                return_value=scenario,
            ),
            patch(
                "engine.monitor.incremental_recalc.s06_strategy_calculator"
                ".calculate_strategies",
                new_callable=AsyncMock,
                return_value=candidates,
            ),
            patch(
                "engine.monitor.incremental_recalc.s08_strategy_ranker"
                ".rank_strategies",
                return_value=candidates[:3],
            ),
        ):
            pipeline._micro_client.fetch_micro_snapshot = AsyncMock(
                return_value=micro,
            )

            recalculator = IncrementalRecalculator(pipeline)
            result = await recalculator.recalc_from(
                step=4,
                symbol=SYMBOL,
                trade_date=TRADE_DATE,
                cached_context=context,
                cached_pre_calc=pre_calc,
            )

        assert result is not None
        assert isinstance(result, RecalcOutput)
        assert isinstance(result.baseline, MarketParameterSnapshot)
        assert isinstance(result.analysis, AnalysisResultSnapshot)
        assert result.analysis.symbol == SYMBOL
        assert len(result.analysis.strategies) == 3

    @pytest.mark.asyncio
    async def test_full_monitor_cycle_creates_new_snapshot(
        self, pipeline: AnalysisPipeline,
    ) -> None:
        """完整监控循环: spot 偏移 → 告警 → 重算 → 新快照。"""
        baseline = _make_baseline(spot=200.0)
        analysis = _make_analysis(baseline.snapshot_id)

        # 构建重算所需的缓存
        context = _make_regime_context()
        pre_calc = _make_pre_calc()
        micro = _make_micro()
        scores = _make_scores()
        scenario = _make_scenario()
        cached_output = RecalcOutput(
            baseline=baseline,
            analysis=analysis,
            context=context,
            pre_calc=pre_calc,
            micro=micro,
            scores=scores,
            scenario=scenario,
        )

        # a. 模拟 spot 偏移 3%
        drifted = _make_baseline(spot=206.0)
        alert_engine = AlertEngine(thresholds_config=THRESHOLDS)
        alerts, recalc_action = alert_engine.evaluate(
            current=drifted,
            baseline=baseline,
            analysis=analysis,
            positions=[],
        )
        assert recalc_action is not None

        # b. 执行增量重算
        candidates = [
            _make_candidate("bull_call_spread", ev=200.0),
            _make_candidate("bull_call_spread", ev=150.0),
            _make_candidate("bull_call_spread", ev=100.0),
        ]

        with (
            patch(
                "engine.monitor.incremental_recalc.s04_field_calculator"
                ".compute_field_scores",
                return_value=scores,
            ),
            patch(
                "engine.monitor.incremental_recalc.s05_scenario_analyzer"
                ".analyze_scenario",
                return_value=scenario,
            ),
            patch(
                "engine.monitor.incremental_recalc.s06_strategy_calculator"
                ".calculate_strategies",
                new_callable=AsyncMock,
                return_value=candidates,
            ),
            patch(
                "engine.monitor.incremental_recalc.s08_strategy_ranker"
                ".rank_strategies",
                return_value=candidates[:3],
            ),
        ):
            pipeline._micro_client.fetch_micro_snapshot = AsyncMock(
                return_value=micro,
            )

            step = int(recalc_action.split("_")[-1])
            recalculator = IncrementalRecalculator(pipeline)
            new_output = await recalculator.recalc_from(
                step=step,
                symbol=SYMBOL,
                trade_date=TRADE_DATE,
                cached_context=cached_output.context,
                cached_pre_calc=cached_output.pre_calc,
                cached_micro=cached_output.micro,
                cached_scores=cached_output.scores,
                cached_scenario=cached_output.scenario,
            )

        # c. 验证新的 AnalysisResultSnapshot 被创建
        assert new_output is not None
        assert isinstance(new_output.analysis, AnalysisResultSnapshot)
        assert new_output.analysis.analysis_id != analysis.analysis_id
        assert new_output.analysis.symbol == SYMBOL
        assert len(new_output.analysis.strategies) == 3

        # d. 验证新 baseline 也被创建
        assert isinstance(new_output.baseline, MarketParameterSnapshot)
        assert new_output.baseline.snapshot_id != baseline.snapshot_id

    def test_no_alert_when_within_threshold(self) -> None:
        """spot 偏移 1% (< 1.5% yellow) → 无 spot 告警。"""
        baseline = _make_baseline(spot=200.0)
        current = _make_baseline(spot=202.0)  # 1% drift
        analysis = _make_analysis(baseline.snapshot_id)

        engine = AlertEngine(thresholds_config=THRESHOLDS)
        alerts, recalc_action = engine.evaluate(
            current=current,
            baseline=baseline,
            analysis=analysis,
            positions=[],
        )

        spot_alerts = [a for a in alerts if a.indicator == "spot_drift_pct"]
        assert len(spot_alerts) == 0

    def test_yellow_alert_at_2pct_drift(self) -> None:
        """spot 偏移 2% (>1.5% yellow, <3% red) → 黄色告警。"""
        baseline = _make_baseline(spot=200.0)
        current = _make_baseline(spot=204.0)  # 2% drift
        analysis = _make_analysis(baseline.snapshot_id)

        engine = AlertEngine(thresholds_config=THRESHOLDS)
        alerts, _ = engine.evaluate(
            current=current,
            baseline=baseline,
            analysis=analysis,
            positions=[],
        )

        spot_yellow = [
            a for a in alerts
            if a.indicator == "spot_drift_pct"
            and a.severity == AlertSeverity.YELLOW
        ]
        assert len(spot_yellow) >= 1
