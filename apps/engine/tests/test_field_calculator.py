"""
tests/test_field_calculator.py — Field Calculator (Step 4) 单元测试

覆盖:
  - compute_field_scores 整体调用与 FieldScores 输出范围
  - GammaScore: 全零 GEX → 低分; 高墙集中度 → 高分
  - BreakScore: 墙距 + zero_gamma_flip_risk 边界
  - DirectionScore: bullish meso + bullish DEX → 高正分; 双 bearish → 强负分
  - IVScore: 高 IVR/IVP + earnings 事件溢价 → 高分; 全零基线 → 中性
  - 边界条件: 空 DataFrame, hist_summary 缺失, monies slope 缺失
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from engine.models.context import EventInfo, MesoSignal, RegimeContext
from engine.models.micro import MicroSnapshot
from engine.models.scores import FieldScores
from engine.steps.s03_pre_calculator import PreCalculatorOutput
from engine.steps.s04_field_calculator import (
    FieldCalculatorError,
    compute_field_scores,
    compute_hv20_pct,
    extract_prior_close_series,
    safe_attr,
)

# ---------------------------------------------------------------------------
# 公共常量 & 工厂
# ---------------------------------------------------------------------------

TRADE_DATE = date(2026, 4, 9)
SYMBOL = "AAPL"
SPOT_PRICE = 200.0


def _frame(df: pd.DataFrame) -> SimpleNamespace:
    """轻量包装 (duck-typed 替代 ExposureFrame / TermFrame / MoniesFrame 等)。"""
    return SimpleNamespace(df=df)


def _make_pre_calc(
    spot_price: float = SPOT_PRICE,
    dyn_window_pct: float = 0.05,
) -> PreCalculatorOutput:
    return PreCalculatorOutput(
        dyn_window_pct=dyn_window_pct,
        dyn_strike_band=(spot_price * 0.95, spot_price * 1.05),
        dyn_dte_range="14,45",
        dyn_dte_ranges=["14,45"],
        scenario_seed="trend",
        spot_price=spot_price,
    )


def _make_context(
    *,
    s_dir: float = 0.0,
    s_vol: float = 0.0,
    event_type: str = "none",
    days_to_event: int | None = None,
) -> RegimeContext:
    signal = MesoSignal(
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
        meso_signal=signal,
    )


def _make_gex_df(
    strikes: list[float],
    exposures: list[float],
    expiry: str = "2026-05-15",
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "strike": strikes,
            "exposure_value": exposures,
            "expirDate": [expiry] * len(strikes),
        }
    )


def _make_dex_df(
    strikes: list[float],
    exposures: list[float],
) -> pd.DataFrame:
    return pd.DataFrame({"strike": strikes, "exposure_value": exposures})


def _make_monies_df(
    slope: float = 0.0,
    vol25: float = 0.30,
    vol75: float = 0.30,
    dte: int = 30,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "slope": slope,
                "vol25": vol25,
                "vol75": vol75,
                "dte": dte,
                "atmiv": 0.30,
            }
        ]
    )


def _make_term_df(dtes: list[int], atmivs: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"dte": dtes, "atmiv": atmivs})


def _make_summary(atm_iv_m1: float | None = 0.30) -> SimpleNamespace:
    return SimpleNamespace(spotPrice=SPOT_PRICE, atmIvM1=atm_iv_m1)


def _make_ivrank(iv_rank: float = 50.0, iv_pctl: float = 50.0) -> SimpleNamespace:
    return SimpleNamespace(iv_rank=iv_rank, iv_pctl=iv_pctl)


def _make_snapshot(
    *,
    gex_df: pd.DataFrame | None = None,
    dex_df: pd.DataFrame | None = None,
    monies_df: pd.DataFrame | None = None,
    term_df: pd.DataFrame | None = None,
    summary: object | None = None,
    ivrank: object | None = None,
    hist_summary: object | None = None,
    zero_gamma_strike: float | None = None,
    call_wall_strike: float | None = None,
    put_wall_strike: float | None = None,
) -> MicroSnapshot:
    return MicroSnapshot(
        strikes_combined=_frame(pd.DataFrame()),
        monies=_frame(monies_df if monies_df is not None else _make_monies_df()),
        summary=summary if summary is not None else _make_summary(),
        ivrank=ivrank if ivrank is not None else _make_ivrank(),
        gex_frame=_frame(gex_df if gex_df is not None else _make_gex_df([200.0], [0.0])),
        dex_frame=_frame(dex_df if dex_df is not None else _make_dex_df([200.0], [0.0])),
        term=_frame(term_df if term_df is not None else _make_term_df([30, 60], [0.30, 0.30])),
        skew=_frame(pd.DataFrame({"delta": [25, 50, 75], "iv": [0.32, 0.30, 0.30]})),
        hist_summary=hist_summary,
        zero_gamma_strike=zero_gamma_strike,
        call_wall_strike=call_wall_strike,
        put_wall_strike=put_wall_strike,
    )


# ---------------------------------------------------------------------------
# 整体输出范围 & 类型
# ---------------------------------------------------------------------------


class TestComputeFieldScoresShape:
    def test_returns_field_scores_within_bounds(self) -> None:
        snapshot = _make_snapshot()
        scores = compute_field_scores(snapshot, _make_pre_calc(), _make_context())

        assert isinstance(scores, FieldScores)
        assert 0.0 <= scores.gamma_score <= 100.0
        assert 0.0 <= scores.break_score <= 100.0
        assert -100.0 <= scores.direction_score <= 100.0
        assert 0.0 <= scores.iv_score <= 100.0


# ---------------------------------------------------------------------------
# GammaScore
# ---------------------------------------------------------------------------


class TestGammaScore:
    def test_zero_gex_returns_low_gamma_score(self) -> None:
        """全零 GEX → 没有有效集中度/Net 暴露 → 接近 0"""
        snapshot = _make_snapshot(
            gex_df=_make_gex_df(
                strikes=[190.0, 195.0, 200.0, 205.0, 210.0],
                exposures=[0.0, 0.0, 0.0, 0.0, 0.0],
            )
        )
        scores = compute_field_scores(snapshot, _make_pre_calc(), _make_context())
        assert scores.gamma_score < 25.0

    def test_concentrated_walls_produce_high_gamma_score(self) -> None:
        """3 个 strike 集中了所有 GEX 暴露 → wall_concentration ≈ 100"""
        snapshot = _make_snapshot(
            gex_df=_make_gex_df(
                strikes=[180.0, 185.0, 190.0, 195.0, 200.0, 205.0, 210.0, 215.0],
                exposures=[0.0, 0.0, 1500.0, 0.0, 1200.0, 0.0, 1000.0, 0.0],
            ),
            zero_gamma_strike=199.0,  # 距离 spot=200 极近 → flip risk + zero_gamma_score 高
        )
        scores = compute_field_scores(snapshot, _make_pre_calc(), _make_context())
        assert scores.gamma_score > 60.0

    def test_zero_gamma_far_from_spot_lowers_zero_distance_subscore(self) -> None:
        """zero_gamma 距离 spot 很远 → zero_gamma 子分被 clip 到 0"""
        far = _make_snapshot(
            gex_df=_make_gex_df([195.0, 205.0], [500.0, 500.0]),
            zero_gamma_strike=140.0,  # 30% 偏离
        )
        near = _make_snapshot(
            gex_df=_make_gex_df([195.0, 205.0], [500.0, 500.0]),
            zero_gamma_strike=200.5,  # 0.25% 偏离
        )
        far_score = compute_field_scores(far, _make_pre_calc(), _make_context())
        near_score = compute_field_scores(near, _make_pre_calc(), _make_context())
        assert near_score.gamma_score > far_score.gamma_score


# ---------------------------------------------------------------------------
# BreakScore
# ---------------------------------------------------------------------------


class TestBreakScore:
    def test_no_walls_no_zero_gamma_returns_zero_break(self) -> None:
        snapshot = _make_snapshot(call_wall_strike=None, put_wall_strike=None)
        scores = compute_field_scores(snapshot, _make_pre_calc(), _make_context())
        # wall_distance=0, implied_vs_actual 仅由 IV/HV 比, zero_gamma_flip_risk=0
        assert scores.break_score < 50.0

    def test_zero_gamma_at_spot_maximizes_flip_risk(self) -> None:
        """zero_gamma_strike == spot → flip_risk 子项 ≈ 100"""
        snapshot = _make_snapshot(
            zero_gamma_strike=SPOT_PRICE,
            call_wall_strike=210.0,
            put_wall_strike=190.0,
        )
        scores = compute_field_scores(snapshot, _make_pre_calc(), _make_context())
        assert scores.break_score > 30.0


# ---------------------------------------------------------------------------
# DirectionScore
# ---------------------------------------------------------------------------


class TestDirectionScore:
    def test_strong_bullish_meso_and_dex_yields_high_positive_score(self) -> None:
        """meso=+80, DEX 随 strike 单调递增 (slope>0), monies slope>0 → 高正分"""
        snapshot = _make_snapshot(
            dex_df=_make_dex_df(
                strikes=[180.0, 190.0, 200.0, 210.0, 220.0],
                exposures=[100.0, 200.0, 300.0, 400.0, 500.0],
            ),
            monies_df=_make_monies_df(slope=0.30),
        )
        ctx = _make_context(s_dir=80.0)
        scores = compute_field_scores(snapshot, _make_pre_calc(), ctx)
        assert scores.direction_score > 30.0

    def test_strong_bearish_meso_and_dex_yields_negative_score(self) -> None:
        """meso=-80, DEX 单调递减, monies slope<0 → 强负分"""
        snapshot = _make_snapshot(
            dex_df=_make_dex_df(
                strikes=[180.0, 190.0, 200.0, 210.0, 220.0],
                exposures=[500.0, 400.0, 300.0, 200.0, 100.0],
            ),
            monies_df=_make_monies_df(slope=-0.30),
        )
        ctx = _make_context(s_dir=-80.0)
        scores = compute_field_scores(snapshot, _make_pre_calc(), ctx)
        assert scores.direction_score < -30.0

    def test_neutral_meso_no_dex_returns_near_zero(self) -> None:
        snapshot = _make_snapshot(
            dex_df=_make_dex_df([195.0, 200.0, 205.0], [10.0, 10.0, 10.0]),
            monies_df=_make_monies_df(slope=0.0),
        )
        scores = compute_field_scores(snapshot, _make_pre_calc(), _make_context())
        assert abs(scores.direction_score) < 5.0


# ---------------------------------------------------------------------------
# IVScore
# ---------------------------------------------------------------------------


class TestIVScore:
    def test_high_iv_rank_and_event_premium_yields_high_iv_score(self) -> None:
        """高 IVR + earnings 事件 + front_iv >> back_iv (event premium)"""
        snapshot = _make_snapshot(
            ivrank=_make_ivrank(iv_rank=85.0, iv_pctl=90.0),
            term_df=_make_term_df(dtes=[7, 30, 60], atmivs=[0.60, 0.40, 0.30]),
            monies_df=_make_monies_df(slope=0.0, vol25=0.45, vol75=0.30),
        )
        ctx = _make_context(event_type="earnings", days_to_event=3)
        scores = compute_field_scores(snapshot, _make_pre_calc(), ctx)
        assert scores.iv_score > 50.0

    def test_zero_ivrank_no_event_neutral_iv_score(self) -> None:
        snapshot = _make_snapshot(
            ivrank=_make_ivrank(iv_rank=0.0, iv_pctl=0.0),
            term_df=_make_term_df(dtes=[30, 60], atmivs=[0.30, 0.30]),
            monies_df=_make_monies_df(vol25=0.30, vol75=0.30),
        )
        scores = compute_field_scores(snapshot, _make_pre_calc(), _make_context())
        # iv_consensus=0, term_kink=0, skew=0, event_premium=0
        # iv_rv_spread 中性 → ~50 × 0.20 = 10
        assert scores.iv_score < 25.0


# ---------------------------------------------------------------------------
# 共享辅助
# ---------------------------------------------------------------------------


class TestSharedHelpers:
    def test_safe_attr_returns_none_for_missing_attribute(self) -> None:
        obj = SimpleNamespace(foo=1.5)
        assert safe_attr(obj, "foo") == 1.5
        assert safe_attr(obj, "bar") is None

    def test_safe_attr_returns_none_for_unparseable_value(self) -> None:
        obj = SimpleNamespace(foo="not_a_number")
        assert safe_attr(obj, "foo") is None

    def test_extract_prior_close_returns_none_when_missing(self) -> None:
        assert extract_prior_close_series(None) is None
        assert extract_prior_close_series(SimpleNamespace(df=pd.DataFrame())) is None

    def test_compute_hv20_pct_uses_atm_iv_when_no_history(self) -> None:
        result = compute_hv20_pct(None, atm_iv=0.20)
        # 0.20 * sqrt(20/365) ≈ 0.0468
        assert result == pytest.approx(0.20 * np.sqrt(20 / 365), rel=1e-3)

    def test_compute_hv20_pct_returns_zero_when_all_inputs_missing(self) -> None:
        assert compute_hv20_pct(None, atm_iv=None) == 0.0

    def test_compute_hv20_pct_uses_history_when_sufficient(self) -> None:
        # 30 个递增价格 → log returns 标准差非零
        prices = [100.0 + i * 0.5 for i in range(30)]
        hist = SimpleNamespace(df=pd.DataFrame({"priorCls": prices}))
        result = compute_hv20_pct(hist, atm_iv=0.20)
        assert result > 0
