"""
tests/test_strategy_ranker.py — Strategy Ranker (Step 8) 单元测试

覆盖:
  - hard_filter: OI 不足被排除
  - hard_filter: bid-ask spread 过宽被排除
  - hard_filter: max_loss 超限被排除
  - hard_filter: 正常策略通过
  - compute_total_score: 高 EV + 高流动性得分高于低 EV + 低流动性
  - rank_strategies: Top-N 截断
  - rank_strategies: 高分策略排在前面
  - rank_strategies: 所有策略被过滤时返回空列表
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from engine.models.scenario import ScenarioResult
from engine.models.strategy import GreeksComposite, StrategyCandidate, StrategyLeg
from engine.steps.s08_strategy_ranker import (
    HARD_FILTERS,
    compute_total_score,
    hard_filter,
    rank_strategies,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

EXPIRY = date(2026, 5, 15)
SPOT = 200.0


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------


def _make_leg(
    strike: float = 200.0,
    option_type: str = "call",
    side: str = "buy",
    oi: int = 1000,
    bid: float | None = 2.0,
    ask: float | None = 2.2,
    delta: float = 0.50,
    gamma: float = 0.02,
    theta: float = -0.05,
    vega: float = 0.10,
    premium: float = 5.0,
) -> StrategyLeg:
    return StrategyLeg(
        side=side,
        option_type=option_type,
        strike=strike,
        expiry=EXPIRY,
        premium=premium,
        iv=0.25,
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        oi=oi,
        bid=bid,
        ask=ask,
    )


def _make_greeks(
    net_delta: float = 0.10,
    net_gamma: float = 0.01,
    net_theta: float = -0.10,
    net_vega: float = 0.05,
) -> GreeksComposite:
    return GreeksComposite(
        net_delta=net_delta,
        net_gamma=net_gamma,
        net_theta=net_theta,
        net_vega=net_vega,
    )


def _make_candidate(
    strategy_type: str = "iron_condor",
    legs: list[StrategyLeg] | None = None,
    max_profit: float = 300.0,
    max_loss: float = 700.0,
    ev: float = 120.0,
    net_delta: float = 0.05,
    net_theta: float = -0.20,
) -> StrategyCandidate:
    if legs is None:
        legs = [
            _make_leg(strike=190.0, option_type="put", side="buy"),
            _make_leg(strike=195.0, option_type="put", side="sell"),
            _make_leg(strike=205.0, option_type="call", side="sell"),
            _make_leg(strike=210.0, option_type="call", side="buy"),
        ]
    return StrategyCandidate(
        strategy_type=strategy_type,
        description=f"Mock {strategy_type}",
        legs=legs,
        net_credit_debit=100.0,
        max_profit=max_profit,
        max_loss=max_loss,
        breakevens=[194.0, 206.0],
        pop=0.68,
        ev=ev,
        greeks_composite=_make_greeks(net_delta=net_delta, net_theta=net_theta),
    )


def _make_scenario(
    scenario: str = "range",
    confidence: float = 0.80,
) -> ScenarioResult:
    return ScenarioResult(
        scenario=scenario,
        confidence=confidence,
        method="rule_engine",
        invalidate_conditions=[],
    )


def _make_micro() -> SimpleNamespace:
    """最小 MicroSnapshot stub（ranker 当前不读取 micro 字段）。"""
    return SimpleNamespace()


# ---------------------------------------------------------------------------
# hard_filter 测试
# ---------------------------------------------------------------------------


class TestHardFilter:
    def test_passes_valid_strategy(self) -> None:
        candidate = _make_candidate()
        passed, reason = hard_filter(candidate)
        assert passed is True
        assert reason == ""

    def test_rejects_low_oi(self) -> None:
        """任何一条 leg OI < 500 时应被拒绝。"""
        low_oi_leg = _make_leg(oi=100)
        candidate = _make_candidate(legs=[low_oi_leg])
        passed, reason = hard_filter(candidate)
        assert passed is False
        assert "OI too low" in reason
        assert "100" in reason

    def test_rejects_wide_spread(self) -> None:
        """bid-ask spread / mid > 15% 时应被拒绝。"""
        wide_leg = _make_leg(bid=1.0, ask=3.0)  # spread/mid = 2/2 = 100%
        candidate = _make_candidate(legs=[wide_leg])
        passed, reason = hard_filter(candidate)
        assert passed is False
        assert "Spread too wide" in reason

    def test_rejects_excessive_max_loss(self) -> None:
        """max_loss 超过 50000 时应被拒绝。"""
        candidate = _make_candidate(max_loss=60000.0)
        passed, reason = hard_filter(candidate)
        assert passed is False
        assert "Max loss" in reason
        assert "60000" in reason

    def test_passes_when_bid_ask_none(self) -> None:
        """bid/ask 为 None 时跳过 spread 检查，策略应通过。"""
        leg = _make_leg(bid=None, ask=None)
        candidate = _make_candidate(legs=[leg])
        passed, reason = hard_filter(candidate)
        assert passed is True

    def test_oi_exactly_at_threshold_passes(self) -> None:
        """OI 恰好等于阈值（500）时通过（边界值）。"""
        leg = _make_leg(oi=int(HARD_FILTERS["min_oi"]))
        candidate = _make_candidate(legs=[leg])
        passed, _ = hard_filter(candidate)
        assert passed is True


# ---------------------------------------------------------------------------
# compute_total_score 测试
# ---------------------------------------------------------------------------


class TestComputeTotalScore:
    def test_returns_float_in_range(self) -> None:
        candidate = _make_candidate()
        scenario = _make_scenario(scenario="range")
        score = compute_total_score(candidate, scenario, _make_micro())
        assert isinstance(score, float)
        assert 0.0 <= score <= 100.0

    def test_higher_ev_yields_higher_score(self) -> None:
        """EV 高的策略评分应高于 EV 低的策略（其他条件相同）。"""
        scenario = _make_scenario(scenario="range")
        high_ev = _make_candidate(ev=500.0)
        low_ev = _make_candidate(ev=10.0)
        assert compute_total_score(high_ev, scenario, _make_micro()) > compute_total_score(
            low_ev, scenario, _make_micro()
        )

    def test_higher_oi_yields_higher_score(self) -> None:
        """OI 高的策略流动性分更高，综合评分应更高（其他条件相同）。"""
        scenario = _make_scenario(scenario="range")
        high_oi_legs = [_make_leg(oi=5000) for _ in range(4)]
        low_oi_legs = [_make_leg(oi=500) for _ in range(4)]
        high_oi = _make_candidate(legs=high_oi_legs)
        low_oi = _make_candidate(legs=low_oi_legs)
        assert compute_total_score(high_oi, scenario, _make_micro()) > compute_total_score(
            low_oi, scenario, _make_micro()
        )

    def test_scenario_match_boosts_score(self) -> None:
        """策略类型匹配场景时得分应高于不匹配时。"""
        scenario = _make_scenario(scenario="range")
        matching = _make_candidate(strategy_type="iron_condor")   # range 场景包含
        non_matching = _make_candidate(strategy_type="long_call")  # range 场景不包含
        assert compute_total_score(matching, scenario, _make_micro()) > compute_total_score(
            non_matching, scenario, _make_micro()
        )

    def test_score_is_rounded_to_two_decimals(self) -> None:
        candidate = _make_candidate()
        scenario = _make_scenario()
        score = compute_total_score(candidate, scenario, _make_micro())
        assert score == round(score, 2)


# ---------------------------------------------------------------------------
# rank_strategies 测试
# ---------------------------------------------------------------------------


class TestRankStrategies:
    def test_returns_top_3_by_default(self) -> None:
        candidates = [_make_candidate(ev=float(v)) for v in [50, 200, 400, 600, 100]]
        scenario = _make_scenario(scenario="range")
        result = rank_strategies(candidates, scenario, _make_micro())
        assert len(result) == 3

    def test_top_n_respected(self) -> None:
        candidates = [_make_candidate() for _ in range(5)]
        scenario = _make_scenario(scenario="range")
        result = rank_strategies(candidates, scenario, _make_micro(), top_n=2)
        assert len(result) == 2

    def test_descending_order(self) -> None:
        """结果应按评分从高到低排列。"""
        scenario = _make_scenario(scenario="range")
        # iron_condor 匹配 range 场景且有高 EV → 高分
        high_score = _make_candidate(strategy_type="iron_condor", ev=800.0)
        # long_call 不匹配 range 且 EV 低 → 低分
        low_score = _make_candidate(strategy_type="long_call", ev=10.0)
        result = rank_strategies([low_score, high_score], scenario, _make_micro(), top_n=2)
        assert result[0].strategy_type == "iron_condor"
        assert result[1].strategy_type == "long_call"

    def test_filters_out_low_oi(self) -> None:
        """OI 不足的策略被硬过滤，不出现在结果中。"""
        scenario = _make_scenario(scenario="range")
        bad_legs = [_make_leg(oi=50)]
        bad = _make_candidate(strategy_type="iron_butterfly", legs=bad_legs)
        good = _make_candidate(strategy_type="iron_condor")
        result = rank_strategies([bad, good], scenario, _make_micro(), top_n=3)
        types = [c.strategy_type for c in result]
        assert "iron_butterfly" not in types
        assert "iron_condor" in types

    def test_all_filtered_returns_empty(self) -> None:
        """所有候选都被硬过滤时返回空列表。"""
        scenario = _make_scenario(scenario="range")
        bad_legs = [_make_leg(oi=1)]
        candidates = [_make_candidate(legs=bad_legs) for _ in range(3)]
        result = rank_strategies(candidates, scenario, _make_micro())
        assert result == []

    def test_fewer_than_top_n_candidates(self) -> None:
        """候选数量少于 top_n 时返回全部通过过滤的候选。"""
        scenario = _make_scenario(scenario="range")
        candidates = [_make_candidate() for _ in range(2)]
        result = rank_strategies(candidates, scenario, _make_micro(), top_n=5)
        assert len(result) == 2
