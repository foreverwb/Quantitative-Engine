"""
tests/test_scenario_analyzer.py — Scenario Analyzer (Step 5) 单元测试

覆盖:
  - Rule 1: Trend — 方向性强 + zero_gamma 远离 + DEX 同向 → trend
  - Rule 2: Range — 正 gamma + 双墙紧密 + 无事件 → range
  - Rule 3: Transition — zero_gamma 接近 spot / 方向波动冲突 → transition
  - Rule 4: Volatility Mean Reversion — 高 iv_score + 无事件 → vol_mean_reversion
  - Rule 5: Event Volatility — 事件窗口内 + front/back IV 陡峭 → event_volatility
  - 边界: 无规则匹配 → 默认 range; 多规则同时满足取最高 confidence
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

from engine.models.context import EventInfo, MesoSignal, RegimeContext
from engine.models.micro import MicroSnapshot
from engine.models.scenario import ScenarioResult
from engine.models.scores import FieldScores
from engine.steps.s05_scenario_analyzer import analyze_scenario

# ---------------------------------------------------------------------------
# 公共常量 & 工厂
# ---------------------------------------------------------------------------

TRADE_DATE = date(2026, 4, 10)
SYMBOL = "AAPL"
SPOT_PRICE = 200.0


def _frame(df: pd.DataFrame) -> SimpleNamespace:
    """轻量包装 (duck-typed 替代 ExposureFrame / TermFrame 等)。"""
    return SimpleNamespace(df=df)


def _make_scores(
    *,
    gamma_score: float = 50.0,
    break_score: float = 50.0,
    direction_score: float = 0.0,
    iv_score: float = 50.0,
) -> FieldScores:
    return FieldScores(
        gamma_score=gamma_score,
        break_score=break_score,
        direction_score=direction_score,
        iv_score=iv_score,
    )


def _make_context(
    *,
    s_dir: float = 0.0,
    s_vol: float = 0.0,
    event_type: str = "none",
    days_to_event: int | None = None,
    meso_signal: MesoSignal | None = None,
) -> RegimeContext:
    if meso_signal is None:
        meso_signal = MesoSignal(
            s_dir=s_dir,
            s_vol=s_vol,
            s_conf=70.0,
            s_pers=60.0,
            quadrant="neutral",
            signal_label="neutral",
            event_regime="neutral",
            prob_tier="medium",
        )
    event = EventInfo(
        event_type=event_type,  # type: ignore[arg-type]
        event_date=None,
        days_to_event=days_to_event,
    )
    return RegimeContext(
        symbol=SYMBOL,
        trade_date=TRADE_DATE,
        regime_class="NORMAL",
        event=event,
        meso_signal=meso_signal,
    )


def _make_snapshot(
    *,
    gex_exposures: list[float] | None = None,
    dex_exposures: list[float] | None = None,
    term_dtes: list[int] | None = None,
    term_atmivs: list[float] | None = None,
    zero_gamma_strike: float | None = None,
    call_wall_strike: float | None = None,
    put_wall_strike: float | None = None,
    spot_price: float = SPOT_PRICE,
) -> MicroSnapshot:
    gex_vals = gex_exposures if gex_exposures is not None else [100.0]
    gex_strikes = [spot_price + i for i in range(len(gex_vals))]
    gex_df = pd.DataFrame({
        "strike": gex_strikes,
        "exposure_value": gex_vals,
        "expirDate": ["2026-05-15"] * len(gex_vals),
    })

    dex_vals = dex_exposures if dex_exposures is not None else [0.0]
    dex_strikes = [spot_price + i for i in range(len(dex_vals))]
    dex_df = pd.DataFrame({
        "strike": dex_strikes,
        "exposure_value": dex_vals,
    })

    t_dtes = term_dtes if term_dtes is not None else [30, 60]
    t_ivs = term_atmivs if term_atmivs is not None else [0.30, 0.30]
    term_df = pd.DataFrame({"dte": t_dtes, "atmiv": t_ivs})

    return MicroSnapshot(
        strikes_combined=_frame(pd.DataFrame()),
        monies=_frame(pd.DataFrame()),
        summary=SimpleNamespace(spotPrice=spot_price),
        ivrank=SimpleNamespace(iv_rank=50.0, iv_pctl=50.0),
        gex_frame=_frame(gex_df),
        dex_frame=_frame(dex_df),
        term=_frame(term_df),
        skew=_frame(pd.DataFrame()),
        hist_summary=None,
        zero_gamma_strike=zero_gamma_strike,
        call_wall_strike=call_wall_strike,
        put_wall_strike=put_wall_strike,
    )


# ---------------------------------------------------------------------------
# Rule 1: Trend
# ---------------------------------------------------------------------------


class TestTrendScenario:
    def test_trend_when_direction_strong_and_dex_aligned(self) -> None:
        """direction > 60, zero_gamma 远离 spot > 3%, DEX 同向 → trend"""
        scores = _make_scores(direction_score=75.0)
        micro = _make_snapshot(
            zero_gamma_strike=215.0,   # 7.5% 偏离 > 3%
            dex_exposures=[100.0, 200.0, 300.0],  # net > 0, 与 direction > 0 同向
        )
        ctx = _make_context()
        result = analyze_scenario(scores, ctx, micro)

        assert result.scenario == "trend"
        assert result.confidence == 0.85
        assert result.method == "rule_engine"
        assert len(result.invalidate_conditions) == 3

    def test_no_trend_when_dex_opposes_direction(self) -> None:
        """direction > 0 但 DEX 净值 < 0 → 不匹配 trend"""
        scores = _make_scores(direction_score=75.0)
        micro = _make_snapshot(
            zero_gamma_strike=215.0,
            dex_exposures=[-300.0, -200.0, -100.0],  # net < 0
        )
        result = analyze_scenario(scores, _make_context(), micro)
        assert result.scenario != "trend"


# ---------------------------------------------------------------------------
# Rule 2: Range
# ---------------------------------------------------------------------------


class TestRangeScenario:
    def test_range_when_positive_gamma_tight_walls_no_event(self) -> None:
        """正 gamma + call/put wall 紧密 (< 8%) + 无事件 → range"""
        scores = _make_scores()
        micro = _make_snapshot(
            gex_exposures=[500.0, 300.0, 200.0],  # net > 0
            call_wall_strike=210.0,  # (210-194)/200 = 8% → 需要更紧
            put_wall_strike=194.0,
        )
        # wall_width = (210 - 194) / 200 = 0.08, need < 0.08
        # 调整使 wall_width < 0.08
        micro_tight = _make_snapshot(
            gex_exposures=[500.0, 300.0, 200.0],
            call_wall_strike=208.0,
            put_wall_strike=194.0,
        )
        ctx = _make_context(event_type="none")
        result = analyze_scenario(scores, ctx, micro_tight)

        assert result.scenario == "range"
        assert result.confidence == 0.80
        assert "net_gex 翻负" in result.invalidate_conditions

    def test_no_range_during_event(self) -> None:
        """有事件时 Rule 2 不触发"""
        scores = _make_scores()
        micro = _make_snapshot(
            gex_exposures=[500.0],
            call_wall_strike=208.0,
            put_wall_strike=194.0,
        )
        ctx = _make_context(event_type="earnings", days_to_event=5)
        result = analyze_scenario(scores, ctx, micro)
        # Range rule blocked; might match something else or default
        assert result.scenario != "range" or result.confidence == 0.50


# ---------------------------------------------------------------------------
# Rule 3: Transition
# ---------------------------------------------------------------------------


class TestTransitionScenario:
    def test_transition_when_zero_gamma_near_spot(self) -> None:
        """zero_gamma 距离 spot < 1.5% → transition"""
        scores = _make_scores(direction_score=10.0)  # 低方向不触发 trend
        micro = _make_snapshot(
            zero_gamma_strike=201.0,  # 0.5% 偏离 < 1.5%
        )
        ctx = _make_context()
        result = analyze_scenario(scores, ctx, micro)

        assert result.scenario == "transition"
        assert result.confidence == 0.70
        assert any("zero_gamma" in c for c in result.invalidate_conditions)

    def test_transition_when_direction_vol_conflict(self) -> None:
        """s_dir 和 s_vol 方向冲突且均强 → transition (confidence=0.65)"""
        scores = _make_scores()
        micro = _make_snapshot()
        ctx = _make_context(s_dir=50.0, s_vol=-50.0)
        result = analyze_scenario(scores, ctx, micro)

        assert result.scenario == "transition"
        assert result.confidence == 0.65
        assert "方向/波动信号冲突解除" in result.invalidate_conditions


# ---------------------------------------------------------------------------
# Rule 4: Volatility Mean Reversion
# ---------------------------------------------------------------------------


class TestVolatilityMeanReversionScenario:
    def test_vmr_when_high_iv_score_no_event(self) -> None:
        """iv_score > 75 + 无事件 → volatility_mean_reversion"""
        scores = _make_scores(iv_score=85.0)
        micro = _make_snapshot()
        ctx = _make_context(event_type="none")
        result = analyze_scenario(scores, ctx, micro)

        assert result.scenario == "volatility_mean_reversion"
        assert result.confidence == 0.75
        assert any("iv_score" in c for c in result.invalidate_conditions)


# ---------------------------------------------------------------------------
# Rule 5: Event Volatility
# ---------------------------------------------------------------------------


class TestEventVolatilityScenario:
    def test_event_vol_when_front_iv_steep(self) -> None:
        """事件窗口内 + front/back IV > 1.15 → event_volatility"""
        scores = _make_scores()
        micro = _make_snapshot(
            term_dtes=[7, 30, 60],
            term_atmivs=[0.50, 0.35, 0.30],  # front/back = 0.50/0.30 ≈ 1.67
        )
        ctx = _make_context(event_type="earnings", days_to_event=5)
        result = analyze_scenario(scores, ctx, micro)

        assert result.scenario == "event_volatility"
        assert result.confidence == 0.80
        assert "事件已过" in result.invalidate_conditions


# ---------------------------------------------------------------------------
# 默认 & 多规则竞争
# ---------------------------------------------------------------------------


class TestDefaultAndTieBreaking:
    def test_default_range_when_no_rule_matches(self) -> None:
        """所有规则都不满足 → 默认 range, confidence=0.50"""
        scores = _make_scores(direction_score=10.0, iv_score=30.0)
        micro = _make_snapshot(
            gex_exposures=[-100.0],  # 负 gamma → Rule 2 不触发
            zero_gamma_strike=None,   # 无 zero gamma → Rule 1, 3 不触发
        )
        ctx = _make_context(event_type="none")  # 无事件 → Rule 5 不触发
        result = analyze_scenario(scores, ctx, micro)

        assert result.scenario == "range"
        assert result.confidence == 0.50
        assert result.method == "rule_engine"

    def test_highest_confidence_wins_among_multiple_candidates(self) -> None:
        """trend(0.85) + transition(0.70) 同时满足 → 选 trend"""
        scores = _make_scores(direction_score=75.0)
        micro = _make_snapshot(
            # zero_gamma 在 < 1.5% 范围内 → triggers transition
            # 但也需要 > 3% 给 trend... 矛盾，所以换思路:
            # 让 trend + vmr 同时触发
        )
        # 同时满足: trend(0.85) + vmr(0.75)
        scores_multi = _make_scores(direction_score=75.0, iv_score=85.0)
        micro_multi = _make_snapshot(
            zero_gamma_strike=215.0,  # 7.5% → trend
            dex_exposures=[100.0, 200.0, 300.0],  # DEX 同向
        )
        ctx = _make_context(event_type="none")
        result = analyze_scenario(scores_multi, ctx, micro_multi)

        # trend (0.85) > vmr (0.75)
        assert result.scenario == "trend"
        assert result.confidence == 0.85
