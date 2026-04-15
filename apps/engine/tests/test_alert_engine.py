"""
tests/test_alert_engine.py — AlertEngine 单元测试

覆盖:
  - Tier 1: spot/IV/zero-gamma 偏移黄色/红色、term flip、gex flip、vol_pcr 范围
  - Tier 2: score drift、direction flip、iv_score_change、scenario invalidation
  - Tier 3: max_loss_proximity、delta_drift、theta ratio、breakeven、dte
  - recalc_action: 红色告警优先级选择（step 数字最小）
  - 无告警时返回空列表和 None
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml

from engine.models.alerts import AlertSeverity
from engine.models.snapshots import AnalysisResultSnapshot, MarketParameterSnapshot
from engine.monitor.alert_engine import AlertEngine

# ---------------------------------------------------------------------------
# 加载真实 thresholds 配置
# ---------------------------------------------------------------------------

_THRESHOLDS_PATH = "engine/config/thresholds.yaml"


@pytest.fixture()
def thresholds() -> dict:
    with open(_THRESHOLDS_PATH) as f:
        return yaml.safe_load(f)


@pytest.fixture()
def engine(thresholds: dict) -> AlertEngine:
    return AlertEngine(thresholds)


# ---------------------------------------------------------------------------
# 快照工厂
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 15, 14, 0, 0, tzinfo=timezone.utc)


def _snap(
    *,
    spot: float = 200.0,
    atm_iv: float = 0.25,
    atm_iv_back: float | None = 0.28,
    term_spread: float = 0.03,
    net_gex: float = 1_000_000.0,
    net_dex: float = -500_000.0,
    zero_gamma: float | None = 198.0,
    vol_pcr: float | None = 0.8,
    call_wall: float | None = 210.0,
    put_wall: float | None = 190.0,
    days_to_event: int | None = None,
) -> MarketParameterSnapshot:
    return MarketParameterSnapshot(
        snapshot_id="snap-test",
        symbol="AAPL",
        captured_at=_NOW,
        spot_price=spot,
        atm_iv_front=atm_iv,
        atm_iv_back=atm_iv_back,
        term_spread=term_spread,
        iv30d=0.26,
        net_gex=net_gex,
        net_dex=net_dex,
        zero_gamma_strike=zero_gamma,
        vol_pcr=vol_pcr,
        call_wall_strike=call_wall,
        put_wall_strike=put_wall,
        days_to_event=days_to_event,
    )


def _analysis(
    *,
    invalidate_conditions: list[str] | None = None,
) -> AnalysisResultSnapshot:
    return AnalysisResultSnapshot(
        analysis_id="a-test",
        symbol="AAPL",
        created_at=_NOW,
        baseline_snapshot_id="snap-base",
        gamma_score=60.0,
        break_score=50.0,
        direction_score=45.0,
        iv_score=55.0,
        scenario="trend",
        scenario_confidence=0.85,
        scenario_method="rule_engine",
        invalidate_conditions=invalidate_conditions or [],
        strategies=[],
    )


# ---------------------------------------------------------------------------
# Tier 1 测试
# ---------------------------------------------------------------------------


class TestTier1SpotDrift:
    """spot_drift_pct: yellow=0.015, red=0.030"""

    def test_yellow_when_spot_drifts_2pct(self, engine: AlertEngine) -> None:
        baseline = _snap(spot=200.0)
        current = _snap(spot=204.0)  # +2% drift
        alerts, _ = engine.evaluate(current, baseline, _analysis(), [])
        tier1 = [a for a in alerts if a.indicator == "spot_drift_pct"]
        assert len(tier1) == 1
        assert tier1[0].severity == AlertSeverity.YELLOW

    def test_red_when_spot_drifts_4pct(self, engine: AlertEngine) -> None:
        baseline = _snap(spot=200.0)
        current = _snap(spot=208.0)  # +4% drift
        alerts, _ = engine.evaluate(current, baseline, _analysis(), [])
        tier1 = [a for a in alerts if a.indicator == "spot_drift_pct"]
        assert len(tier1) == 1
        assert tier1[0].severity == AlertSeverity.RED
        assert tier1[0].action == "recalc_from_step_4"


class TestTier1IVDrift:
    """atm_iv_drift_pct: yellow=0.08, red=0.15"""

    def test_yellow_when_iv_drifts_10pct(self, engine: AlertEngine) -> None:
        baseline = _snap(atm_iv=0.25)
        current = _snap(atm_iv=0.275)  # +10% drift
        alerts, _ = engine.evaluate(current, baseline, _analysis(), [])
        iv_alerts = [a for a in alerts if a.indicator == "atm_iv_drift_pct"]
        assert len(iv_alerts) == 1
        assert iv_alerts[0].severity == AlertSeverity.YELLOW

    def test_red_when_iv_drifts_20pct(self, engine: AlertEngine) -> None:
        baseline = _snap(atm_iv=0.25)
        current = _snap(atm_iv=0.30)  # +20%
        alerts, _ = engine.evaluate(current, baseline, _analysis(), [])
        iv_alerts = [a for a in alerts if a.indicator == "atm_iv_drift_pct"]
        assert len(iv_alerts) == 1
        assert iv_alerts[0].severity == AlertSeverity.RED


class TestTier1BooleanTriggers:
    def test_term_structure_flip_red(self, engine: AlertEngine) -> None:
        baseline = _snap(term_spread=0.03)
        current = _snap(term_spread=-0.02)  # 翻转
        alerts, _ = engine.evaluate(current, baseline, _analysis(), [])
        term = [a for a in alerts if a.indicator == "term_structure_flip"]
        assert len(term) == 1
        assert term[0].severity == AlertSeverity.RED
        assert term[0].action == "recalc_from_step_3"

    def test_gex_sign_flip_red(self, engine: AlertEngine) -> None:
        baseline = _snap(net_gex=1_000_000.0)
        current = _snap(net_gex=-500_000.0)  # 翻负
        alerts, _ = engine.evaluate(current, baseline, _analysis(), [])
        gex = [a for a in alerts if a.indicator == "gex_sign_flip"]
        assert len(gex) == 1
        assert gex[0].severity == AlertSeverity.RED

    def test_no_flip_when_same_sign(self, engine: AlertEngine) -> None:
        baseline = _snap(net_gex=1_000_000.0)
        current = _snap(net_gex=500_000.0)
        alerts, _ = engine.evaluate(current, baseline, _analysis(), [])
        gex = [a for a in alerts if a.indicator == "gex_sign_flip"]
        assert len(gex) == 0


class TestTier1VolPCR:
    """vol_pcr: yellow_low=0.6, yellow_high=1.0, red_low=0.5, red_high=1.2"""

    def test_yellow_high(self, engine: AlertEngine) -> None:
        baseline = _snap()
        current = _snap(vol_pcr=1.1)  # > 1.0 but < 1.2
        alerts, _ = engine.evaluate(current, baseline, _analysis(), [])
        pcr = [a for a in alerts if a.indicator == "vol_pcr"]
        assert len(pcr) == 1
        assert pcr[0].severity == AlertSeverity.YELLOW

    def test_red_low(self, engine: AlertEngine) -> None:
        baseline = _snap()
        current = _snap(vol_pcr=0.4)  # < 0.5
        alerts, _ = engine.evaluate(current, baseline, _analysis(), [])
        pcr = [a for a in alerts if a.indicator == "vol_pcr"]
        assert len(pcr) == 1
        assert pcr[0].severity == AlertSeverity.RED

    def test_normal_range_no_alert(self, engine: AlertEngine) -> None:
        baseline = _snap()
        current = _snap(vol_pcr=0.8)  # within [0.6, 1.0]
        alerts, _ = engine.evaluate(current, baseline, _analysis(), [])
        pcr = [a for a in alerts if a.indicator == "vol_pcr"]
        assert len(pcr) == 0


# ---------------------------------------------------------------------------
# Tier 2 测试
# ---------------------------------------------------------------------------


class TestTier2ScoreDrift:
    """score_drift_max: yellow=15, red=25"""

    def test_yellow_on_moderate_drift(self, engine: AlertEngine) -> None:
        baseline = _snap(spot=200.0)
        # spot drift ~1.5% → normalized = 0.015/0.030 = 0.5 → 0.5*30 = 15 → yellow
        current = _snap(spot=203.0)
        alerts, _ = engine.evaluate(current, baseline, _analysis(), [])
        sd = [a for a in alerts if a.indicator == "score_drift_max"]
        assert len(sd) == 1
        assert sd[0].severity == AlertSeverity.YELLOW

    def test_red_on_large_drift(self, engine: AlertEngine) -> None:
        baseline = _snap(spot=200.0)
        # spot drift ~4% → normalized = 0.04/0.03 ≈ 1.33 → 1.33*30 = 40 → red
        current = _snap(spot=208.0)
        alerts, _ = engine.evaluate(current, baseline, _analysis(), [])
        sd = [a for a in alerts if a.indicator == "score_drift_max"]
        assert len(sd) == 1
        assert sd[0].severity == AlertSeverity.RED


class TestTier2DirectionFlip:
    def test_direction_flip_red(self, engine: AlertEngine) -> None:
        baseline = _snap(net_dex=-500_000.0)
        current = _snap(net_dex=300_000.0)  # 翻正
        alerts, _ = engine.evaluate(current, baseline, _analysis(), [])
        df = [a for a in alerts if a.indicator == "direction_flip"]
        assert len(df) == 1
        assert df[0].severity == AlertSeverity.RED
        assert df[0].action == "recalc_from_step_5"


class TestTier2IVScoreChange:
    """iv_score_change: yellow=12, red=20"""

    def test_yellow_on_moderate_iv_shift(self, engine: AlertEngine) -> None:
        baseline = _snap(atm_iv=0.25)
        # IV drift 14% → 0.14 * 100 = 14 → ≥ 12 yellow
        current = _snap(atm_iv=0.285)
        alerts, _ = engine.evaluate(current, baseline, _analysis(), [])
        iv = [a for a in alerts if a.indicator == "iv_score_change"]
        assert len(iv) == 1
        assert iv[0].severity == AlertSeverity.YELLOW


class TestTier2ScenarioInvalidation:
    """scenario_invalidation_count: yellow=1, red=2"""

    def test_red_when_two_conditions_triggered(self, engine: AlertEngine) -> None:
        baseline = _snap(net_gex=1_000_000.0)
        current = _snap(net_gex=-200_000.0, days_to_event=2)
        analysis = _analysis(invalidate_conditions=[
            "net_gex 翻负",
            "事件进入 T-3 窗口",
        ])
        alerts, _ = engine.evaluate(current, baseline, analysis, [])
        si = [a for a in alerts if a.indicator == "scenario_invalidation_count"]
        assert len(si) == 1
        assert si[0].severity == AlertSeverity.RED

    def test_yellow_when_one_condition_triggered(self, engine: AlertEngine) -> None:
        baseline = _snap(net_gex=1_000_000.0)
        current = _snap(net_gex=-200_000.0)
        analysis = _analysis(invalidate_conditions=[
            "net_gex 翻负",
            "spot 突破 call_wall 或跌破 put_wall",  # not triggered
        ])
        alerts, _ = engine.evaluate(current, baseline, analysis, [])
        si = [a for a in alerts if a.indicator == "scenario_invalidation_count"]
        assert len(si) == 1
        assert si[0].severity == AlertSeverity.YELLOW


# ---------------------------------------------------------------------------
# Tier 3 测试
# ---------------------------------------------------------------------------


class TestTier3:
    def test_max_loss_proximity_red(self, engine: AlertEngine) -> None:
        baseline = _snap()
        pos = [{"max_loss_proximity": 0.80}]
        alerts, _ = engine.evaluate(baseline, baseline, _analysis(), pos)
        ml = [a for a in alerts if a.indicator == "max_loss_proximity"]
        assert len(ml) == 1
        assert ml[0].severity == AlertSeverity.RED

    def test_max_loss_proximity_yellow(self, engine: AlertEngine) -> None:
        baseline = _snap()
        pos = [{"max_loss_proximity": 0.55}]
        alerts, _ = engine.evaluate(baseline, baseline, _analysis(), pos)
        ml = [a for a in alerts if a.indicator == "max_loss_proximity"]
        assert len(ml) == 1
        assert ml[0].severity == AlertSeverity.YELLOW

    def test_delta_drift_yellow(self, engine: AlertEngine) -> None:
        baseline = _snap()
        pos = [{"delta_drift": 0.20}]
        alerts, _ = engine.evaluate(baseline, baseline, _analysis(), pos)
        dd = [a for a in alerts if a.indicator == "delta_drift"]
        assert len(dd) == 1
        assert dd[0].severity == AlertSeverity.YELLOW

    def test_theta_realization_low_red(self, engine: AlertEngine) -> None:
        baseline = _snap()
        pos = [{"theta_realization_ratio": 0.40}]
        alerts, _ = engine.evaluate(baseline, baseline, _analysis(), pos)
        tr = [a for a in alerts if a.indicator == "theta_realization_ratio"]
        assert len(tr) == 1
        assert tr[0].severity == AlertSeverity.RED

    def test_breakeven_distance_yellow(self, engine: AlertEngine) -> None:
        baseline = _snap()
        pos = [{"breakeven_distance_pct": 0.015}]
        alerts, _ = engine.evaluate(baseline, baseline, _analysis(), pos)
        bd = [a for a in alerts if a.indicator == "breakeven_distance_pct"]
        assert len(bd) == 1
        assert bd[0].severity == AlertSeverity.YELLOW

    def test_dte_remaining_red(self, engine: AlertEngine) -> None:
        baseline = _snap()
        pos = [{"dte_remaining": 1}]
        alerts, _ = engine.evaluate(baseline, baseline, _analysis(), pos)
        dte = [a for a in alerts if a.indicator == "dte_remaining"]
        assert len(dte) == 1
        assert dte[0].severity == AlertSeverity.RED

    def test_multiple_positions_evaluated(self, engine: AlertEngine) -> None:
        baseline = _snap()
        pos = [
            {"max_loss_proximity": 0.80},
            {"max_loss_proximity": 0.60},
        ]
        alerts, _ = engine.evaluate(baseline, baseline, _analysis(), pos)
        ml = [a for a in alerts if a.indicator == "max_loss_proximity"]
        assert len(ml) == 2
        assert ml[0].severity == AlertSeverity.RED
        assert ml[1].severity == AlertSeverity.YELLOW


# ---------------------------------------------------------------------------
# recalc_action 优先级测试
# ---------------------------------------------------------------------------


class TestRecalcAction:
    def test_selects_lowest_step_red_alert(self, engine: AlertEngine) -> None:
        """多个 red 告警时选择 step 最小的 action"""
        baseline = _snap(spot=200.0, term_spread=0.03, net_gex=1_000_000.0)
        # term_structure_flip → recalc_from_step_3
        # spot drift red → recalc_from_step_4
        # gex flip → recalc_from_step_4
        current = _snap(
            spot=208.0, term_spread=-0.02, net_gex=-500_000.0,
        )
        _, recalc = engine.evaluate(current, baseline, _analysis(), [])
        assert recalc == "recalc_from_step_3"

    def test_no_recalc_when_only_yellow(self, engine: AlertEngine) -> None:
        baseline = _snap(spot=200.0)
        current = _snap(spot=203.0)  # ~1.5% → yellow only
        _, recalc = engine.evaluate(current, baseline, _analysis(), [])
        assert recalc is None

    def test_no_recalc_when_tier3_red(self, engine: AlertEngine) -> None:
        """Tier 3 无 action → 不触发 recalc"""
        baseline = _snap()
        pos = [{"max_loss_proximity": 0.80, "dte_remaining": 1}]
        _, recalc = engine.evaluate(baseline, baseline, _analysis(), pos)
        assert recalc is None


# ---------------------------------------------------------------------------
# 无告警 (Green) 测试
# ---------------------------------------------------------------------------


class TestGreen:
    def test_no_alerts_when_no_drift(self, engine: AlertEngine) -> None:
        baseline = _snap()
        alerts, recalc = engine.evaluate(baseline, baseline, _analysis(), [])
        assert alerts == []
        assert recalc is None

    def test_no_alerts_when_within_thresholds(self, engine: AlertEngine) -> None:
        baseline = _snap(spot=200.0, atm_iv=0.25)
        # 微小偏移，未达黄色阈值
        current = _snap(spot=201.0, atm_iv=0.255)  # 0.5% / 2%
        alerts, recalc = engine.evaluate(current, baseline, _analysis(), [])
        assert len(alerts) == 0
        assert recalc is None
