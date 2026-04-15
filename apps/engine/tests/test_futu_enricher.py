"""
tests/test_futu_enricher.py — FutuClient + LiveQuoteEnricher 测试

职责: 验证 LiveQuoteEnricher 正确填充 bid/ask，以及富途不可用时降级逻辑。
依赖: pytest, unittest.mock, engine.providers.futu_client, engine.models.strategy
被依赖: pytest
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from engine.models.strategy import GreeksComposite, StrategyCandidate, StrategyLeg
from engine.providers.futu_client import FutuClient, LiveQuoteEnricher

# ---------------------------------------------------------------------------
# 工厂帮助函数
# ---------------------------------------------------------------------------

def _make_leg(
    strike: float = 185.0,
    expiry: date = date(2024, 4, 19),
    option_type: str = "call",
) -> StrategyLeg:
    return StrategyLeg(
        side="buy",
        option_type=option_type,
        strike=strike,
        expiry=expiry,
        premium=3.5,
        iv=0.25,
        delta=0.45,
        gamma=0.02,
        theta=-0.05,
        vega=0.10,
        oi=1000,
    )


def _make_strategy(legs: list[StrategyLeg]) -> StrategyCandidate:
    return StrategyCandidate(
        strategy_type="long_call",
        description="test strategy",
        legs=legs,
        net_credit_debit=-3.5,
        max_profit=float("inf"),
        max_loss=3.5,
        breakevens=[188.5],
        pop=0.45,
        ev=0.5,
        greeks_composite=GreeksComposite(
            net_delta=0.45,
            net_gamma=0.02,
            net_theta=-0.05,
            net_vega=0.10,
        ),
    )


# ---------------------------------------------------------------------------
# _build_futu_option_code 单元测试
# ---------------------------------------------------------------------------

class TestBuildFutuOptionCode:
    def test_call_code_format(self) -> None:
        leg = _make_leg(strike=185.0, expiry=date(2024, 4, 19), option_type="call")
        code = LiveQuoteEnricher._build_futu_option_code("AAPL", leg)
        # 185.0 × 1000 = 185000 → 00185000 (8 digits)
        assert code == "US.AAPL240419C00185000"

    def test_put_code_format(self) -> None:
        leg = _make_leg(strike=185.0, expiry=date(2024, 4, 19), option_type="put")
        code = LiveQuoteEnricher._build_futu_option_code("AAPL", leg)
        assert code == "US.AAPL240419P00185000"

    def test_symbol_uppercased(self) -> None:
        leg = _make_leg(strike=100.0, expiry=date(2025, 1, 17))
        code = LiveQuoteEnricher._build_futu_option_code("aapl", leg)
        assert code.startswith("US.AAPL")

    def test_strike_with_decimals(self) -> None:
        # strike=150.5 → 150500 → 00150500
        leg = _make_leg(strike=150.5, expiry=date(2024, 6, 21))
        code = LiveQuoteEnricher._build_futu_option_code("TSLA", leg)
        assert code == "US.TSLA240621C00150500"

    def test_two_digit_year(self) -> None:
        leg = _make_leg(expiry=date(2026, 12, 18))
        code = LiveQuoteEnricher._build_futu_option_code("SPY", leg)
        assert "261218" in code


# ---------------------------------------------------------------------------
# LiveQuoteEnricher.enrich 集成测试（mock FutuClient）
# ---------------------------------------------------------------------------

class TestLiveQuoteEnricherEnrich:
    def _make_enricher(self, quotes: list[dict]) -> LiveQuoteEnricher:
        client = MagicMock(spec=FutuClient)
        client.get_realtime_quotes.return_value = quotes
        return LiveQuoteEnricher(client)

    def test_enrich_fills_bid_ask(self) -> None:
        leg = _make_leg(strike=185.0, expiry=date(2024, 4, 19), option_type="call")
        strategy = _make_strategy([leg])

        code = "US.AAPL240419C00185000"
        enricher = self._make_enricher([
            {"code": code, "bid_price": 3.1, "ask_price": 3.3},
        ])

        result = enricher.enrich([strategy], "AAPL")
        assert len(result) == 1
        assert result[0].legs[0].bid == 3.1
        assert result[0].legs[0].ask == 3.3

    def test_enrich_returns_new_objects_not_mutated(self) -> None:
        """StrategyLeg/StrategyCandidate frozen — enrich 必须返回新对象"""
        leg = _make_leg()
        strategy = _make_strategy([leg])

        code = LiveQuoteEnricher._build_futu_option_code("AAPL", leg)
        enricher = self._make_enricher([
            {"code": code, "bid_price": 2.0, "ask_price": 2.5},
        ])

        result = enricher.enrich([strategy], "AAPL")
        # 原对象未修改
        assert strategy.legs[0].bid is None
        assert strategy.legs[0].ask is None
        # 新对象已填充
        assert result[0].legs[0].bid == 2.0
        assert result[0].legs[0].ask == 2.5

    def test_enrich_partial_match(self) -> None:
        """只有部分 leg 匹配时，其他 leg 保持原样"""
        leg1 = _make_leg(strike=185.0, expiry=date(2024, 4, 19), option_type="call")
        leg2 = _make_leg(strike=190.0, expiry=date(2024, 4, 19), option_type="call")
        strategy = _make_strategy([leg1, leg2])

        code1 = LiveQuoteEnricher._build_futu_option_code("AAPL", leg1)
        enricher = self._make_enricher([
            {"code": code1, "bid_price": 3.1, "ask_price": 3.3},
        ])

        result = enricher.enrich([strategy], "AAPL")
        assert result[0].legs[0].bid == 3.1
        assert result[0].legs[1].bid is None  # 无匹配，保持 None

    def test_enrich_no_option_codes_returns_original(self) -> None:
        """空策略列表原样返回，不调用 futu"""
        client = MagicMock(spec=FutuClient)
        enricher = LiveQuoteEnricher(client)
        result = enricher.enrich([], "AAPL")
        assert result == []
        client.get_realtime_quotes.assert_not_called()

    def test_enrich_deduplicates_codes(self) -> None:
        """同一 leg code 出现多次时，只查询一次"""
        leg = _make_leg()
        strategy1 = _make_strategy([leg])
        strategy2 = _make_strategy([leg])

        client = MagicMock(spec=FutuClient)
        client.get_realtime_quotes.return_value = []
        enricher = LiveQuoteEnricher(client)
        enricher.enrich([strategy1, strategy2], "AAPL")

        called_codes = client.get_realtime_quotes.call_args[0][0]
        assert len(called_codes) == len(set(called_codes))

    def test_enrich_degrades_when_futu_returns_empty(self) -> None:
        """富途返回空列表时，策略原样返回（降级）"""
        leg = _make_leg()
        strategy = _make_strategy([leg])

        enricher = self._make_enricher([])  # 空结果
        result = enricher.enrich([strategy], "AAPL")

        assert len(result) == 1
        assert result[0].legs[0].bid is None
        assert result[0].legs[0].ask is None

    def test_enrich_degrades_when_futu_raises(self) -> None:
        """富途抛出异常时，策略原样返回（降级）"""
        leg = _make_leg()
        strategy = _make_strategy([leg])

        client = MagicMock(spec=FutuClient)
        client.get_realtime_quotes.side_effect = ConnectionError("FutuOpenD unreachable")
        enricher = LiveQuoteEnricher(client)

        result = enricher.enrich([strategy], "AAPL")
        assert len(result) == 1
        assert result[0].legs[0].bid is None


# ---------------------------------------------------------------------------
# FutuClient 降级测试（futu-api 未安装）
# ---------------------------------------------------------------------------

class TestFutuClientDegrades:
    def test_get_option_chain_returns_empty_when_futu_not_installed(self) -> None:
        with patch.dict("sys.modules", {"futu": None}):
            client = FutuClient()
            result = client.get_option_chain("US.AAPL", "2024-04-01", "2024-04-30")
        assert result == []

    def test_get_realtime_quotes_returns_empty_when_futu_not_installed(self) -> None:
        with patch.dict("sys.modules", {"futu": None}):
            client = FutuClient()
            result = client.get_realtime_quotes(["US.AAPL240419C00185000"])
        assert result == []

    def test_get_realtime_quotes_empty_codes_skips_call(self) -> None:
        """空列表时不应尝试连接富途"""
        client = FutuClient()
        with patch("engine.providers.futu_client.FutuClient.get_realtime_quotes",
                   wraps=client.get_realtime_quotes) as _:
            result = client.get_realtime_quotes([])
        assert result == []
