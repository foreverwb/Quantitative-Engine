"""
engine/steps/s02_regime_gating.py — Regime Gating (Step 2)

职责: 构建 RegimeContext，基于市场 Regime 和事件日历决定是否继续分析流程。
依赖: engine.models.context, engine.providers.meso_client, regime.boundary
被依赖: engine.pipeline
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Literal

from engine.models.context import EventInfo, MesoSignal, RegimeContext
from engine.providers.meso_client import MesoClient

logger = logging.getLogger(__name__)

# 事件日历配置文件路径（相对于本文件所在 engine/ 包）
_CONFIG_DIR = Path(__file__).parent.parent / "config"
_EVENT_CALENDAR_PATH = _CONFIG_DIR / "event_calendar.json"

# 门控条件常量
_STRESS_CLASS = "STRESS"
_GATE_SKIP = "skip"
_GATE_PROCEED = "proceed"
_DAYS_TO_EVENT_THRESHOLD = 1


class RegimeGatingError(Exception):
    """Regime Gating 步骤执行失败"""


GateResult = Literal["skip", "proceed"]


async def run_regime_gating(
    symbol: str,
    trade_date: date,
    meso_client: MesoClient,
    orats_provider: object,
    event_calendar_path: Path = _EVENT_CALENDAR_PATH,
) -> tuple[RegimeContext, GateResult]:
    """
    执行 Regime Gating 步骤，返回 (RegimeContext, gate_result)。

    gate_result:
      - "skip"    → STRESS Regime 且距最近事件 ≤ 1 天，跳过分析
      - "proceed" → 正常继续后续步骤

    Args:
        symbol: 股票代码，如 "AAPL"
        trade_date: 分析日期
        meso_client: 已初始化的 MesoClient
        orats_provider: OratsProvider 实例 (duck-typed)
        event_calendar_path: 事件日历 JSON 文件路径（可注入，便于测试）
    """
    meso_signal = await _fetch_meso_signal(meso_client, symbol, trade_date)
    summary, ivrank = await _fetch_orats_data(orats_provider, symbol)
    event_info = _resolve_event_info(
        trade_date=trade_date,
        meso_signal=meso_signal,
        event_calendar_path=event_calendar_path,
    )
    regime_class = _classify_regime(summary, ivrank)
    gate_result = _apply_gate_rule(regime_class, event_info)

    context = RegimeContext(
        symbol=symbol,
        trade_date=trade_date,
        regime_class=regime_class,
        event=event_info,
        meso_signal=meso_signal,
    )

    logger.info(
        "Regime gating: symbol=%s date=%s regime=%s event=%s gate=%s",
        symbol,
        trade_date,
        regime_class,
        event_info.event_type,
        gate_result,
    )

    return context, gate_result


# ---------------------------------------------------------------------------
# 私有辅助函数
# ---------------------------------------------------------------------------


async def _fetch_meso_signal(
    client: MesoClient,
    symbol: str,
    trade_date: date,
) -> MesoSignal | None:
    """调用 Meso API 获取信号，失败时记录警告并返回 None。"""
    try:
        return await client.get_signal(symbol, trade_date)
    except Exception as exc:
        logger.warning("Meso API 调用失败，降级到 None: %s", exc)
        return None


async def _fetch_orats_data(provider: object, symbol: str) -> tuple[object, object]:
    """调用 OratsProvider 获取 summary 和 ivrank。"""
    try:
        summary = await provider.get_summary(symbol)  # type: ignore[attr-defined]
        ivrank = await provider.get_ivrank(symbol)    # type: ignore[attr-defined]
        return summary, ivrank
    except Exception as exc:
        raise RegimeGatingError(
            f"ORATS 数据获取失败: symbol={symbol}, error={exc}"
        ) from exc


def _resolve_event_info(
    trade_date: date,
    meso_signal: MesoSignal | None,
    event_calendar_path: Path,
) -> EventInfo:
    """
    解析事件信息。

    优先级:
    1. 若 meso_signal.event_regime == "pre_earnings" → earnings 事件
    2. 从事件日历 JSON 中查找最近的 macro 事件 (fomc/cpi)
    3. 无事件 → event_type="none"
    """
    if meso_signal is not None and meso_signal.event_regime == "pre_earnings":
        return EventInfo(
            event_type="earnings",
            event_date=None,
            days_to_event=0,
        )

    macro_event = _find_nearest_macro_event(trade_date, event_calendar_path)
    if macro_event is not None:
        return macro_event

    return EventInfo(event_type="none", event_date=None, days_to_event=None)


def _find_nearest_macro_event(
    trade_date: date,
    calendar_path: Path,
) -> EventInfo | None:
    """
    从 event_calendar.json 查找距 trade_date 最近的未来或当日事件。

    返回最近事件的 EventInfo，若无则返回 None。
    """
    if not calendar_path.exists():
        logger.debug("事件日历文件不存在: %s", calendar_path)
        return None

    try:
        with calendar_path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("事件日历文件读取失败: %s", exc)
        return None

    events = data.get("macro_events") or data.get("events") or []
    nearest: EventInfo | None = None
    nearest_abs_days: int | None = None

    for entry in events:
        event_type = entry.get("type") or entry.get("event_type")
        event_date_str = entry.get("date") or entry.get("event_date")
        if not event_type or not event_date_str:
            continue

        try:
            event_date = date.fromisoformat(event_date_str)
        except ValueError:
            logger.warning("事件日历日期格式错误: %s", event_date_str)
            continue

        days = (event_date - trade_date).days  # 正=未来, 负=已过

        # 只考虑未来及当日事件 (days >= 0)，取最近的
        if days < 0:
            continue

        abs_days = abs(days)
        if nearest_abs_days is None or abs_days < nearest_abs_days:
            nearest_abs_days = abs_days
            nearest = EventInfo(
                event_type=event_type,  # type: ignore[arg-type]
                event_date=event_date,
                days_to_event=days,
            )

    return nearest


def _classify_regime(summary: object, ivrank: object) -> Literal["LOW_VOL", "NORMAL", "STRESS"]:
    """
    构建 MarketRegime 并调用 regime.boundary.classify 获取 RegimeClass。
    """
    try:
        from regime.boundary import MarketRegime, classify  # type: ignore[import]
    except ImportError as exc:
        raise RegimeGatingError(
            "无法导入 regime.boundary 模块，请确认 Micro-Provider 已安装"
        ) from exc

    market_regime = MarketRegime(
        iv30d=getattr(summary, "atmIvM1", None) or 0.0,
        contango=(getattr(summary, "atmIvM2", None) or 0)
                 - (getattr(summary, "atmIvM1", None) or 0),
        vrp=(getattr(summary, "atmIvM1", None) or 0)
            - (getattr(summary, "orFcst20d", None) or 0),
        iv_rank=getattr(ivrank, "iv_rank", 0.0),
        iv_pctl=getattr(ivrank, "iv_pctl", 0.0),
        vol_of_vol=getattr(summary, "volOfVol", None) or 0.05,
    )

    result = classify(market_regime)
    # RegimeClass 可能是枚举或字符串
    return str(result.value) if hasattr(result, "value") else str(result)  # type: ignore[return-value]


def _apply_gate_rule(
    regime_class: str,
    event_info: EventInfo,
) -> GateResult:
    """
    门控规则: STRESS + days_to_event <= 1 → "skip"，否则 → "proceed"。
    """
    if (
        regime_class == _STRESS_CLASS
        and event_info.days_to_event is not None
        and event_info.days_to_event <= _DAYS_TO_EVENT_THRESHOLD
    ):
        return _GATE_SKIP
    return _GATE_PROCEED
