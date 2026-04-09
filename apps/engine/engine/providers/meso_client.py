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
