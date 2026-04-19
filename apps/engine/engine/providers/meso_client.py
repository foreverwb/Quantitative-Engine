"""
engine/providers/meso_client.py — Meso API 客户端

职责: 封装对 Meso 层 REST API 的异步 HTTP 调用，解析 ApiResponse 格式。
依赖: httpx, engine.models.context
被依赖: engine.steps.s02_regime_gating
"""

from __future__ import annotations

import logging
from datetime import date

import httpx

from engine.models.context import MesoSignal

logger = logging.getLogger(__name__)

# Meso API 路径模板
_SIGNALS_PATH = "/api/v1/signals/{symbol}"
_SYMBOLS_PATH = "/api/v1/symbols"
_CHART_POINTS_PATH = "/api/v1/chart-points"
_DATE_GROUPS_PATH = "/api/v1/date-groups"

# HTTP 超时配置（秒）
_DEFAULT_TIMEOUT = 10.0


class MesoClientError(Exception):
    """Meso 客户端调用失败"""


class MesoClient:
    """
    异步 HTTP 客户端，调用 Meso 层 REST API。

    用法:
        client = MesoClient(base_url="http://127.0.0.1:18000")
        signal = await client.get_signal("AAPL", date(2026, 4, 9))
    """

    def __init__(self, base_url: str, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def get_signal(self, symbol: str, trade_date: date) -> MesoSignal | None:
        """
        调用 GET /api/v1/signals/{symbol}?trade_date=YYYY-MM-DD。

        返回 MesoSignal，404 时返回 None。
        其他 HTTP 错误或解析失败时抛出 MesoClientError。
        """
        url = self._base_url + _SIGNALS_PATH.format(symbol=symbol)
        params = {"trade_date": trade_date.isoformat()}

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, params=params)
        except httpx.RequestError as exc:
            raise MesoClientError(
                f"Meso API 请求失败 [{symbol}]: {exc}"
            ) from exc

        if response.status_code == 404:
            logger.debug("Meso API 返回 404: symbol=%s date=%s", symbol, trade_date)
            return None

        if response.status_code != 200:
            raise MesoClientError(
                f"Meso API 返回非预期状态码 {response.status_code}: "
                f"symbol={symbol}, date={trade_date}"
            )

        return _parse_signal_response(response.json(), symbol, trade_date)

    async def get_symbols(self, trade_date: date) -> list[str]:
        """
        调用 GET /api/v1/symbols?trade_date=YYYY-MM-DD。

        返回该日期有信号的标的列表。404 或空数据时返回空列表。
        其他 HTTP 错误时抛出 MesoClientError。
        """
        url = self._base_url + _SYMBOLS_PATH
        params = {"trade_date": trade_date.isoformat()}

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, params=params)
        except httpx.RequestError as exc:
            raise MesoClientError(
                f"Meso API 请求失败 [symbols]: {exc}"
            ) from exc

        if response.status_code == 404:
            logger.debug("Meso symbols endpoint 返回 404: date=%s", trade_date)
            return []

        if response.status_code != 200:
            raise MesoClientError(
                f"Meso API 返回非预期状态码 {response.status_code}: "
                f"endpoint=symbols, date={trade_date}"
            )

        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            return []
        return [str(s) for s in data if s]

    async def get_symbols_from_chart_points(
        self, trade_date: date,
    ) -> list[str]:
        """
        从 GET /api/v1/chart-points?trade_date=YYYY-MM-DD 提取唯一标的列表。

        作为 get_symbols 的 fallback：当 /symbols 端点不可用时使用。
        """
        url = self._base_url + _CHART_POINTS_PATH
        params = {"trade_date": trade_date.isoformat()}

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, params=params)
        except httpx.RequestError as exc:
            raise MesoClientError(
                f"Meso API 请求失败 [chart-points]: {exc}"
            ) from exc

        if response.status_code == 404:
            logger.debug(
                "Meso chart-points endpoint 返回 404: date=%s", trade_date,
            )
            return []

        if response.status_code != 200:
            raise MesoClientError(
                f"Meso API 返回非预期状态码 {response.status_code}: "
                f"endpoint=chart-points, date={trade_date}"
            )

        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            return []

        symbols: list[str] = []
        seen: set[str] = set()
        for item in data:
            sym = item.get("symbol") if isinstance(item, dict) else None
            if sym and str(sym) not in seen:
                seen.add(str(sym))
                symbols.append(str(sym))
        return symbols

    async def get_chart_points(self, trade_date: date) -> list[dict]:
        """
        调用 GET /api/v1/chart-points?trade_date=YYYY-MM-DD。

        返回原始 chart point 字典列表。404 或空数据时返回空列表。
        其他 HTTP 错误时抛出 MesoClientError。
        """
        url = self._base_url + _CHART_POINTS_PATH
        params = {"trade_date": trade_date.isoformat()}

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, params=params)
        except httpx.RequestError as exc:
            raise MesoClientError(
                f"Meso API 请求失败 [chart-points]: {exc}"
            ) from exc

        if response.status_code == 404:
            logger.debug("Meso chart-points endpoint 返回 404: date=%s", trade_date)
            return []

        if response.status_code != 200:
            raise MesoClientError(
                f"Meso API 返回非预期状态码 {response.status_code}: "
                f"endpoint=chart-points, date={trade_date}"
            )

        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    async def get_latest_trade_date(self) -> date | None:
        """
        调用 GET /api/v1/date-groups?limit=1 获取最近可用交易日。

        返回最近日期，无数据时返回 None。
        """
        return await self.get_latest_date()

    async def get_latest_date(self) -> date | None:
        """
        调用 GET /api/v1/date-groups?limit=1 获取最近可用日期。

        返回最近日期，无数据时返回 None。
        """
        url = self._base_url + _DATE_GROUPS_PATH
        params = {"limit": 1}

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, params=params)
        except httpx.RequestError as exc:
            raise MesoClientError(
                f"Meso API 请求失败 [date-groups]: {exc}"
            ) from exc

        if response.status_code != 200:
            logger.debug(
                "Meso date-groups endpoint 返回 %d", response.status_code,
            )
            return None

        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data:
            return None

        try:
            return date.fromisoformat(str(data[0]))
        except (ValueError, IndexError):
            return None


def _parse_signal_response(
    body: object,
    symbol: str,
    trade_date: date,
) -> MesoSignal:
    """
    解析 ApiResponse 包装格式，提取 MesoSignal。

    Meso API 统一返回:
        {"success": true, "data": {...}}
    """
    if not isinstance(body, dict):
        raise MesoClientError(
            f"Meso API 响应格式错误 (非 dict): symbol={symbol}, date={trade_date}"
        )

    if not body.get("success", False):
        raise MesoClientError(
            f"Meso API 返回 success=false: symbol={symbol}, date={trade_date}"
        )

    data = body.get("data")
    if not isinstance(data, dict):
        raise MesoClientError(
            f"Meso API 响应缺少 data 字段: symbol={symbol}, date={trade_date}"
        )

    try:
        return MesoSignal(
            s_dir=float(data["s_dir"]),
            s_vol=float(data["s_vol"]),
            s_conf=float(data["s_conf"]),
            s_pers=float(data["s_pers"]),
            quadrant=str(data["quadrant"]),
            signal_label=str(data["signal_label"]),
            event_regime=str(data["event_regime"]),
            prob_tier=str(data["prob_tier"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MesoClientError(
            f"Meso API 响应字段解析失败: symbol={symbol}, date={trade_date}, error={exc}"
        ) from exc
