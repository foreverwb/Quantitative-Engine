"""
tests/test_regime_gating.py — Regime Gating 步骤单元测试

覆盖:
  - MesoClient.get_signal: 正常路径、404 返回 None、解析失败
  - run_regime_gating: STRESS+近事件→skip, NORMAL+无事件→proceed
  - 事件日历解析: earnings(来自 event_regime), fomc, cpi
  - 门控边界条件: days_to_event=0, =1, =2
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from engine.models.context import EventInfo, MesoSignal, RegimeContext
from engine.providers.meso_client import MesoClient, MesoClientError
from engine.steps.s02_regime_gating import (
    GateResult,
    RegimeGatingError,
    _apply_gate_rule,
    _find_nearest_macro_event,
    _resolve_event_info,
    run_regime_gating,
)

# ---------------------------------------------------------------------------
# 共用常量 & 工厂
# ---------------------------------------------------------------------------

TRADE_DATE = date(2026, 4, 9)
SYMBOL = "AAPL"

_MESO_DATA: dict[str, Any] = {
    "s_dir": 45.0,
    "s_vol": -20.0,
    "s_conf": 70.0,
    "s_pers": 60.0,
    "quadrant": "bullish_compression",
    "signal_label": "directional_bias",
    "event_regime": "neutral",
    "prob_tier": "high",
}

_API_RESPONSE_OK: dict[str, Any] = {"success": True, "data": _MESO_DATA}


def _make_meso_signal(**overrides: Any) -> MesoSignal:
    data = {**_MESO_DATA, **overrides}
    return MesoSignal(**data)


def _make_summary(
    atm_iv_m1: float = 0.30,
    atm_iv_m2: float = 0.32,
    or_fcst20d: float = 0.25,
    vol_of_vol: float = 0.05,
) -> SimpleNamespace:
    return SimpleNamespace(
        atmIvM1=atm_iv_m1,
        atmIvM2=atm_iv_m2,
        orFcst20d=or_fcst20d,
        volOfVol=vol_of_vol,
    )


def _make_ivrank(iv_rank: float = 50.0, iv_pctl: float = 55.0) -> SimpleNamespace:
    return SimpleNamespace(iv_rank=iv_rank, iv_pctl=iv_pctl)


def _make_orats_provider(
    summary: SimpleNamespace | None = None,
    ivrank: SimpleNamespace | None = None,
) -> AsyncMock:
    provider = AsyncMock()
    provider.get_summary.return_value = summary or _make_summary()
    provider.get_ivrank.return_value = ivrank or _make_ivrank()
    return provider


def _make_event_calendar(events: list[dict[str, str]], tmp_path: Path) -> Path:
    path = tmp_path / "event_calendar.json"
    path.write_text(json.dumps({"macro_events": events}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# MesoClient 测试
# ---------------------------------------------------------------------------


class TestMesoClientGetSignal:
    """unit tests for MesoClient.get_signal"""

    @pytest.mark.asyncio
    async def test_returns_meso_signal_on_200(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _API_RESPONSE_OK

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.__aenter__.return_value = mock_http
            mock_http.__aexit__.return_value = False
            mock_http.get.return_value = mock_response
            mock_cls.return_value = mock_http

            client = MesoClient(base_url="http://localhost:18000")
            result = await client.get_signal(SYMBOL, TRADE_DATE)

        assert isinstance(result, MesoSignal)
        assert result.s_dir == 45.0
        assert result.quadrant == "bullish_compression"
        assert result.event_regime == "neutral"

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.__aenter__.return_value = mock_http
            mock_http.__aexit__.return_value = False
            mock_http.get.return_value = mock_response
            mock_cls.return_value = mock_http

            client = MesoClient(base_url="http://localhost:18000")
            result = await client.get_signal(SYMBOL, TRADE_DATE)

        assert result is None

    @pytest.mark.asyncio
    async def test_raises_on_non_200_non_404(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.__aenter__.return_value = mock_http
            mock_http.__aexit__.return_value = False
            mock_http.get.return_value = mock_response
            mock_cls.return_value = mock_http

            client = MesoClient(base_url="http://localhost:18000")
            with pytest.raises(MesoClientError, match="500"):
                await client.get_signal(SYMBOL, TRADE_DATE)

    @pytest.mark.asyncio
    async def test_raises_on_request_error(self) -> None:
        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.__aenter__.return_value = mock_http
            mock_http.__aexit__.return_value = False
            mock_http.get.side_effect = httpx.ConnectError("connection refused")
            mock_cls.return_value = mock_http

            client = MesoClient(base_url="http://localhost:18000")
            with pytest.raises(MesoClientError):
                await client.get_signal(SYMBOL, TRADE_DATE)

    @pytest.mark.asyncio
    async def test_raises_on_success_false(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": False, "error": "not found"}

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.__aenter__.return_value = mock_http
            mock_http.__aexit__.return_value = False
            mock_http.get.return_value = mock_response
            mock_cls.return_value = mock_http

            client = MesoClient(base_url="http://localhost:18000")
            with pytest.raises(MesoClientError, match="success=false"):
                await client.get_signal(SYMBOL, TRADE_DATE)

    @pytest.mark.asyncio
    async def test_raises_on_missing_field(self) -> None:
        incomplete_data = {k: v for k, v in _MESO_DATA.items() if k != "s_conf"}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "data": incomplete_data}

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.__aenter__.return_value = mock_http
            mock_http.__aexit__.return_value = False
            mock_http.get.return_value = mock_response
            mock_cls.return_value = mock_http

            client = MesoClient(base_url="http://localhost:18000")
            with pytest.raises(MesoClientError):
                await client.get_signal(SYMBOL, TRADE_DATE)


# ---------------------------------------------------------------------------
# 事件日历解析测试
# ---------------------------------------------------------------------------


class TestFindNearestMacroEvent:
    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        result = _find_nearest_macro_event(TRADE_DATE, tmp_path / "missing.json")
        assert result is None

    def test_returns_none_when_no_future_events(self, tmp_path: Path) -> None:
        path = _make_event_calendar(
            [{"type": "fomc", "date": "2026-03-01"}], tmp_path
        )
        result = _find_nearest_macro_event(TRADE_DATE, path)
        assert result is None

    def test_finds_nearest_fomc_event(self, tmp_path: Path) -> None:
        path = _make_event_calendar(
            [
                {"type": "fomc", "date": "2026-04-15"},
                {"type": "cpi", "date": "2026-04-20"},
            ],
            tmp_path,
        )
        result = _find_nearest_macro_event(TRADE_DATE, path)
        assert result is not None
        assert result.event_type == "fomc"
        assert result.days_to_event == 6

    def test_finds_nearest_cpi_event(self, tmp_path: Path) -> None:
        path = _make_event_calendar(
            [
                {"type": "cpi", "date": "2026-04-11"},
                {"type": "fomc", "date": "2026-04-30"},
            ],
            tmp_path,
        )
        result = _find_nearest_macro_event(TRADE_DATE, path)
        assert result is not None
        assert result.event_type == "cpi"
        assert result.days_to_event == 2

    def test_event_on_trade_date_has_days_zero(self, tmp_path: Path) -> None:
        path = _make_event_calendar(
            [{"type": "fomc", "date": "2026-04-09"}], tmp_path
        )
        result = _find_nearest_macro_event(TRADE_DATE, path)
        assert result is not None
        assert result.days_to_event == 0

    def test_supports_legacy_events_key(self, tmp_path: Path) -> None:
        # 现有 event_calendar.json 使用 "events" + "event_type"/"event_date" 格式
        path = tmp_path / "event_calendar.json"
        path.write_text(
            json.dumps(
                {
                    "events": [
                        {"event_type": "fomc", "event_date": "2026-04-20"}
                    ]
                }
            ),
            encoding="utf-8",
        )
        result = _find_nearest_macro_event(TRADE_DATE, path)
        assert result is not None
        assert result.event_type == "fomc"
        assert result.days_to_event == 11

    def test_ignores_invalid_date_format(self, tmp_path: Path) -> None:
        path = _make_event_calendar(
            [
                {"type": "fomc", "date": "not-a-date"},
                {"type": "cpi", "date": "2026-04-15"},
            ],
            tmp_path,
        )
        result = _find_nearest_macro_event(TRADE_DATE, path)
        assert result is not None
        assert result.event_type == "cpi"


# ---------------------------------------------------------------------------
# _resolve_event_info 测试
# ---------------------------------------------------------------------------


class TestResolveEventInfo:
    def test_earnings_from_pre_earnings_regime(self, tmp_path: Path) -> None:
        signal = _make_meso_signal(event_regime="pre_earnings")
        empty_calendar = _make_event_calendar([], tmp_path)
        result = _resolve_event_info(
            trade_date=TRADE_DATE,
            meso_signal=signal,
            event_calendar_path=empty_calendar,
        )
        assert result.event_type == "earnings"
        assert result.days_to_event == 0

    def test_macro_event_when_no_earnings_signal(self, tmp_path: Path) -> None:
        signal = _make_meso_signal(event_regime="neutral")
        path = _make_event_calendar(
            [{"type": "fomc", "date": "2026-04-10"}], tmp_path
        )
        result = _resolve_event_info(
            trade_date=TRADE_DATE,
            meso_signal=signal,
            event_calendar_path=path,
        )
        assert result.event_type == "fomc"
        assert result.days_to_event == 1

    def test_none_event_when_no_signal_no_calendar(self, tmp_path: Path) -> None:
        path = _make_event_calendar([], tmp_path)
        result = _resolve_event_info(
            trade_date=TRADE_DATE,
            meso_signal=None,
            event_calendar_path=path,
        )
        assert result.event_type == "none"
        assert result.days_to_event is None


# ---------------------------------------------------------------------------
# _apply_gate_rule 测试
# ---------------------------------------------------------------------------


class TestApplyGateRule:
    def test_stress_with_days_zero_returns_skip(self) -> None:
        event = EventInfo(event_type="fomc", event_date=None, days_to_event=0)
        assert _apply_gate_rule("STRESS", event) == "skip"

    def test_stress_with_days_one_returns_skip(self) -> None:
        event = EventInfo(event_type="earnings", event_date=None, days_to_event=1)
        assert _apply_gate_rule("STRESS", event) == "skip"

    def test_stress_with_days_two_returns_proceed(self) -> None:
        event = EventInfo(event_type="fomc", event_date=None, days_to_event=2)
        assert _apply_gate_rule("STRESS", event) == "proceed"

    def test_stress_with_no_event_returns_proceed(self) -> None:
        event = EventInfo(event_type="none", event_date=None, days_to_event=None)
        assert _apply_gate_rule("STRESS", event) == "proceed"

    def test_normal_with_days_zero_returns_proceed(self) -> None:
        event = EventInfo(event_type="fomc", event_date=None, days_to_event=0)
        assert _apply_gate_rule("NORMAL", event) == "proceed"

    def test_low_vol_with_near_event_returns_proceed(self) -> None:
        event = EventInfo(event_type="earnings", event_date=None, days_to_event=1)
        assert _apply_gate_rule("LOW_VOL", event) == "proceed"


# ---------------------------------------------------------------------------
# run_regime_gating 集成测试 (mock regime.boundary)
# ---------------------------------------------------------------------------


def _install_regime_boundary_mock(regime_value: str) -> MagicMock:
    """将 regime.boundary 注入 sys.modules 并返回 mock 模块。"""
    regime_class_enum = SimpleNamespace(value=regime_value)
    mock_module = MagicMock()
    mock_module.MarketRegime = MagicMock(return_value=SimpleNamespace())
    mock_module.classify = MagicMock(return_value=regime_class_enum)

    regime_mock = MagicMock()
    regime_mock.boundary = mock_module
    sys.modules["regime"] = regime_mock
    sys.modules["regime.boundary"] = mock_module
    return mock_module


@pytest.fixture(autouse=True)
def _cleanup_regime_mock() -> Any:
    """每个测试后清理 regime.boundary mock。"""
    yield
    sys.modules.pop("regime", None)
    sys.modules.pop("regime.boundary", None)


class TestRunRegimeGating:
    """run_regime_gating 端到端场景测试"""

    @pytest.mark.asyncio
    async def test_stress_near_event_returns_skip(self, tmp_path: Path) -> None:
        """STRESS Regime + days_to_event=0 → gate_result = 'skip'"""
        _install_regime_boundary_mock("STRESS")

        meso_client = AsyncMock(spec=MesoClient)
        meso_client.get_signal.return_value = _make_meso_signal(event_regime="pre_earnings")
        orats = _make_orats_provider()
        empty_calendar = _make_event_calendar([], tmp_path)

        context, gate_result = await run_regime_gating(
            symbol=SYMBOL,
            trade_date=TRADE_DATE,
            meso_client=meso_client,
            orats_provider=orats,
            event_calendar_path=empty_calendar,
        )

        assert gate_result == "skip"
        assert context.regime_class == "STRESS"
        assert context.event.event_type == "earnings"
        assert context.event.days_to_event == 0

    @pytest.mark.asyncio
    async def test_normal_no_event_returns_proceed(self, tmp_path: Path) -> None:
        """NORMAL Regime + 无事件 → gate_result = 'proceed'"""
        _install_regime_boundary_mock("NORMAL")

        meso_client = AsyncMock(spec=MesoClient)
        meso_client.get_signal.return_value = _make_meso_signal(event_regime="neutral")
        orats = _make_orats_provider()
        empty_calendar = _make_event_calendar([], tmp_path)

        context, gate_result = await run_regime_gating(
            symbol=SYMBOL,
            trade_date=TRADE_DATE,
            meso_client=meso_client,
            orats_provider=orats,
            event_calendar_path=empty_calendar,
        )

        assert gate_result == "proceed"
        assert context.regime_class == "NORMAL"
        assert context.event.event_type == "none"
        assert context.meso_signal is not None

    @pytest.mark.asyncio
    async def test_stress_event_two_days_away_returns_proceed(
        self, tmp_path: Path
    ) -> None:
        """STRESS + days_to_event=2 → gate_result = 'proceed'（边界条件）"""
        _install_regime_boundary_mock("STRESS")

        meso_client = AsyncMock(spec=MesoClient)
        meso_client.get_signal.return_value = _make_meso_signal(event_regime="neutral")
        orats = _make_orats_provider()
        path = _make_event_calendar(
            [{"type": "fomc", "date": "2026-04-11"}], tmp_path
        )

        context, gate_result = await run_regime_gating(
            symbol=SYMBOL,
            trade_date=TRADE_DATE,
            meso_client=meso_client,
            orats_provider=orats,
            event_calendar_path=path,
        )

        assert gate_result == "proceed"
        assert context.event.days_to_event == 2

    @pytest.mark.asyncio
    async def test_stress_fomc_tomorrow_returns_skip(self, tmp_path: Path) -> None:
        """STRESS + fomc 明天 (days_to_event=1) → gate_result = 'skip'"""
        _install_regime_boundary_mock("STRESS")

        meso_client = AsyncMock(spec=MesoClient)
        meso_client.get_signal.return_value = _make_meso_signal(event_regime="neutral")
        orats = _make_orats_provider()
        path = _make_event_calendar(
            [{"type": "fomc", "date": "2026-04-10"}], tmp_path
        )

        context, gate_result = await run_regime_gating(
            symbol=SYMBOL,
            trade_date=TRADE_DATE,
            meso_client=meso_client,
            orats_provider=orats,
            event_calendar_path=path,
        )

        assert gate_result == "skip"
        assert context.event.event_type == "fomc"
        assert context.event.days_to_event == 1

    @pytest.mark.asyncio
    async def test_meso_api_failure_degrades_gracefully(self, tmp_path: Path) -> None:
        """Meso API 失败时 meso_signal=None，流程继续"""
        _install_regime_boundary_mock("NORMAL")

        meso_client = AsyncMock(spec=MesoClient)
        meso_client.get_signal.side_effect = Exception("connection refused")
        orats = _make_orats_provider()
        empty_calendar = _make_event_calendar([], tmp_path)

        context, gate_result = await run_regime_gating(
            symbol=SYMBOL,
            trade_date=TRADE_DATE,
            meso_client=meso_client,
            orats_provider=orats,
            event_calendar_path=empty_calendar,
        )

        assert gate_result == "proceed"
        assert context.meso_signal is None

    @pytest.mark.asyncio
    async def test_context_fields_populated_correctly(self, tmp_path: Path) -> None:
        """RegimeContext 各字段正确填充"""
        _install_regime_boundary_mock("LOW_VOL")

        meso_client = AsyncMock(spec=MesoClient)
        signal = _make_meso_signal(event_regime="neutral")
        meso_client.get_signal.return_value = signal
        orats = _make_orats_provider()
        empty_calendar = _make_event_calendar([], tmp_path)

        context, _ = await run_regime_gating(
            symbol=SYMBOL,
            trade_date=TRADE_DATE,
            meso_client=meso_client,
            orats_provider=orats,
            event_calendar_path=empty_calendar,
        )

        assert context.symbol == SYMBOL
        assert context.trade_date == TRADE_DATE
        assert context.regime_class == "LOW_VOL"
        assert context.meso_signal == signal

    @pytest.mark.asyncio
    async def test_orats_failure_raises_regime_gating_error(
        self, tmp_path: Path
    ) -> None:
        """ORATS 调用失败时抛出 RegimeGatingError"""
        _install_regime_boundary_mock("NORMAL")

        meso_client = AsyncMock(spec=MesoClient)
        meso_client.get_signal.return_value = None
        orats = AsyncMock()
        orats.get_summary.side_effect = RuntimeError("ORATS down")
        empty_calendar = _make_event_calendar([], tmp_path)

        with pytest.raises(RegimeGatingError, match="ORATS"):
            await run_regime_gating(
                symbol=SYMBOL,
                trade_date=TRADE_DATE,
                meso_client=meso_client,
                orats_provider=orats,
                event_calendar_path=empty_calendar,
            )
