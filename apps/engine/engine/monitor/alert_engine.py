"""
engine/monitor/alert_engine.py — 三级告警引擎

职责: 按三级指标体系评估市场参数偏移（Tier 1）、分析有效性（Tier 2）
      和策略健康度（Tier 3），产生告警列表并决定最高优先级的重算动作。
依赖: engine.models.alerts, engine.models.snapshots
被依赖: engine.api (监控循环), engine.monitor.incremental_recalc
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

from engine.models.alerts import AlertEvent, AlertSeverity
from engine.models.snapshots import AnalysisResultSnapshot, MarketParameterSnapshot

logger = logging.getLogger(__name__)

# Tier 2 估算缩放因子: 将归一化市场偏移 → 分数点 (0-100)
_SCORE_DRIFT_SCALE = 30
# ATM IV 相对偏移 → IV score 点数
_IV_SCORE_SCALE = 100


class AlertEngineError(Exception):
    """告警引擎异常"""


class AlertEngine:
    """
    三级告警引擎。

    Tier 1: 市场参数偏移（spot / IV / zero-gamma / term / GEX / vol_pcr）
    Tier 2: 分析有效性（score drift / direction flip / IV score / 场景失效）
    Tier 3: 策略健康度（max-loss / delta / theta / breakeven / DTE）
    """

    def __init__(self, thresholds_config: dict) -> None:
        self._thresholds = thresholds_config

    def evaluate(
        self,
        current: MarketParameterSnapshot,
        baseline: MarketParameterSnapshot,
        analysis: AnalysisResultSnapshot,
        positions: list[dict],
    ) -> tuple[list[AlertEvent], str | None]:
        """
        评估所有三级指标，返回告警列表和最高优先级重算动作。

        Returns:
            alerts: 告警事件列表
            recalc_action: "recalc_from_step_N" 或 None
        """
        alerts: list[AlertEvent] = []
        alerts.extend(self._eval_tier1(current, baseline))
        alerts.extend(self._eval_tier2(current, baseline, analysis))
        alerts.extend(self._eval_tier3(current.symbol, positions))

        red_alerts = [
            a for a in alerts if a.severity == AlertSeverity.RED and a.action
        ]
        if red_alerts:
            recalc_action = min(
                red_alerts,
                key=lambda a: int(a.action.split("_")[-1]),  # type: ignore[union-attr]
            ).action
        else:
            recalc_action = None

        return alerts, recalc_action

    # ------------------------------------------------------------------
    # Tier 1: 市场参数偏移
    # ------------------------------------------------------------------

    def _eval_tier1(
        self,
        current: MarketParameterSnapshot,
        baseline: MarketParameterSnapshot,
    ) -> list[AlertEvent]:
        t = self._thresholds.get("tier1_market", {})
        sym = current.symbol
        alerts: list[AlertEvent] = []

        # spot_drift_pct
        drift = _pct_change(current.spot_price, baseline.spot_price)
        _append_if(alerts, _check_abs_drift(
            sym, 1, "spot_drift_pct", drift, t.get("spot_drift_pct", {}),
        ))

        # atm_iv_drift_pct
        drift = _pct_change(current.atm_iv_front, baseline.atm_iv_front)
        _append_if(alerts, _check_abs_drift(
            sym, 1, "atm_iv_drift_pct", drift, t.get("atm_iv_drift_pct", {}),
        ))

        # zero_gamma_drift_pct
        drift = _pct_change(current.zero_gamma_strike, baseline.zero_gamma_strike)
        _append_if(alerts, _check_abs_drift(
            sym, 1, "zero_gamma_drift_pct", drift, t.get("zero_gamma_drift_pct", {}),
        ))

        # term_structure_flip (boolean, red-only)
        flipped = _sign_flipped(current.term_spread, baseline.term_spread)
        _append_if(alerts, _check_bool_trigger(
            sym, 1, "term_structure_flip", flipped, t.get("term_structure_flip", {}),
        ))

        # gex_sign_flip (boolean, red-only)
        flipped = _sign_flipped(current.net_gex, baseline.net_gex)
        _append_if(alerts, _check_bool_trigger(
            sym, 1, "gex_sign_flip", flipped, t.get("gex_sign_flip", {}),
        ))

        # vol_pcr (range: yellow_low/high, red_low/high)
        _append_if(alerts, _check_range(
            sym, 1, "vol_pcr", current.vol_pcr, t.get("vol_pcr", {}),
        ))

        return alerts

    # ------------------------------------------------------------------
    # Tier 2: 分析有效性
    # ------------------------------------------------------------------

    def _eval_tier2(
        self,
        current: MarketParameterSnapshot,
        baseline: MarketParameterSnapshot,
        analysis: AnalysisResultSnapshot,
    ) -> list[AlertEvent]:
        t = self._thresholds.get("tier2_analysis", {})
        sym = current.symbol
        alerts: list[AlertEvent] = []

        spot_drift = abs(_pct_change(current.spot_price, baseline.spot_price))
        iv_drift = abs(_pct_change(current.atm_iv_front, baseline.atm_iv_front))
        zg_drift = abs(_pct_change(
            current.zero_gamma_strike, baseline.zero_gamma_strike,
        ))

        # score_drift_max: 按 Tier 1 红线归一化后缩放到分数量级
        t1 = self._thresholds.get("tier1_market", {})
        spot_red = t1.get("spot_drift_pct", {}).get("red", 0.030)
        iv_red = t1.get("atm_iv_drift_pct", {}).get("red", 0.15)
        zg_red = t1.get("zero_gamma_drift_pct", {}).get("red", 0.020)
        max_norm = max(
            spot_drift / spot_red if spot_red else 0.0,
            iv_drift / iv_red if iv_red else 0.0,
            zg_drift / zg_red if zg_red else 0.0,
        )
        _append_if(alerts, _check_abs_drift(
            sym, 2, "score_drift_max",
            max_norm * _SCORE_DRIFT_SCALE,
            t.get("score_drift_max", {}),
        ))

        # direction_flip: net_dex 符号翻转
        flipped = _sign_flipped(current.net_dex, baseline.net_dex)
        _append_if(alerts, _check_bool_trigger(
            sym, 2, "direction_flip", flipped, t.get("direction_flip", {}),
        ))

        # iv_score_change: ATM IV 相对偏移 × 100
        _append_if(alerts, _check_abs_drift(
            sym, 2, "iv_score_change",
            iv_drift * _IV_SCORE_SCALE,
            t.get("iv_score_change", {}),
        ))

        # scenario_invalidation_count
        inv_count = _count_invalidations(
            analysis.invalidate_conditions, current, baseline,
        )
        _append_if(alerts, _check_abs_drift(
            sym, 2, "scenario_invalidation_count",
            float(inv_count),
            t.get("scenario_invalidation_count", {}),
        ))

        return alerts

    # ------------------------------------------------------------------
    # Tier 3: 策略健康度
    # ------------------------------------------------------------------

    def _eval_tier3(self, symbol: str, positions: list[dict]) -> list[AlertEvent]:
        t = self._thresholds.get("tier3_strategy", {})
        alerts: list[AlertEvent] = []
        for pos in positions:
            self._eval_position(symbol, pos, t, alerts)
        return alerts

    def _eval_position(
        self, symbol: str, pos: dict, t: dict, alerts: list[AlertEvent],
    ) -> None:
        # max_loss_proximity (higher = worse)
        val = pos.get("max_loss_proximity")
        if val is not None:
            _append_if(alerts, _check_abs_drift(
                symbol, 3, "max_loss_proximity", val, t.get("max_loss_proximity", {}),
            ))

        # delta_drift (higher = worse)
        val = pos.get("delta_drift")
        if val is not None:
            _append_if(alerts, _check_abs_drift(
                symbol, 3, "delta_drift", val, t.get("delta_drift", {}),
            ))

        # theta_realization_ratio (lower = worse)
        val = pos.get("theta_realization_ratio")
        if val is not None:
            _append_if(alerts, _check_low_threshold(
                symbol, 3, "theta_realization_ratio", val,
                t.get("theta_realization_ratio", {}),
            ))

        # breakeven_distance_pct (lower = worse)
        val = pos.get("breakeven_distance_pct")
        if val is not None:
            _append_if(alerts, _check_low_threshold(
                symbol, 3, "breakeven_distance_pct", val,
                t.get("breakeven_distance_pct", {}),
            ))

        # dte_remaining (lower = worse)
        val = pos.get("dte_remaining")
        if val is not None:
            _append_if(alerts, _check_low_threshold(
                symbol, 3, "dte_remaining", float(val),
                t.get("dte_remaining", {}),
            ))


# ---------------------------------------------------------------------------
# 通用辅助函数
# ---------------------------------------------------------------------------


def _pct_change(current: float | None, baseline: float | None) -> float:
    """百分比变化 (current - baseline) / |baseline|。"""
    if current is None or baseline is None or baseline == 0.0:
        return 0.0
    return (current - baseline) / abs(baseline)


def _sign_flipped(a: float, b: float) -> bool:
    """检测两个值的符号是否翻转（任一为 0 视为无翻转）。"""
    if a == 0.0 or b == 0.0:
        return False
    return (a > 0.0) != (b > 0.0)


def _append_if(lst: list[AlertEvent], item: AlertEvent | None) -> None:
    if item is not None:
        lst.append(item)


def _make_alert(
    symbol: str,
    tier: Literal[1, 2, 3],
    indicator: str,
    severity: AlertSeverity,
    new_value: float | str,
    threshold: float | str | None,
    action: str | None,
) -> AlertEvent:
    return AlertEvent(
        alert_id=str(uuid.uuid4()),
        symbol=symbol,
        timestamp=datetime.now(tz=timezone.utc),
        tier=tier,
        indicator=indicator,
        severity=severity,
        old_value=None,
        new_value=new_value,
        threshold=threshold,
        action=action,
    )


# ---------------------------------------------------------------------------
# 阈值检查函数
# ---------------------------------------------------------------------------


def _check_abs_drift(
    symbol: str, tier: Literal[1, 2, 3],
    indicator: str, value: float, cfg: dict,
) -> AlertEvent | None:
    """绝对值 ≥ 阈值时触发（higher = worse）。"""
    action = cfg.get("action")
    red = cfg.get("red")
    yellow = cfg.get("yellow")
    if red is not None and abs(value) >= red:
        return _make_alert(symbol, tier, indicator, AlertSeverity.RED, value, red, action)
    if yellow is not None and abs(value) >= yellow:
        return _make_alert(symbol, tier, indicator, AlertSeverity.YELLOW, value, yellow, action)
    return None


def _check_bool_trigger(
    symbol: str, tier: Literal[1, 2, 3],
    indicator: str, triggered: bool, cfg: dict,
) -> AlertEvent | None:
    """布尔触发器（仅 red 级别）。"""
    if triggered and cfg.get("red"):
        return _make_alert(
            symbol, tier, indicator, AlertSeverity.RED,
            "true", "true", cfg.get("action"),
        )
    return None


def _check_range(
    symbol: str, tier: Literal[1, 2, 3],
    indicator: str, value: float | None, cfg: dict,
) -> AlertEvent | None:
    """范围阈值（vol_pcr 风格: yellow_low/high, red_low/high）。"""
    if value is None:
        return None
    action = cfg.get("action")
    if cfg.get("red_high") is not None and value > cfg["red_high"]:
        return _make_alert(symbol, tier, indicator, AlertSeverity.RED, value, cfg["red_high"], action)
    if cfg.get("red_low") is not None and value < cfg["red_low"]:
        return _make_alert(symbol, tier, indicator, AlertSeverity.RED, value, cfg["red_low"], action)
    if cfg.get("yellow_high") is not None and value > cfg["yellow_high"]:
        return _make_alert(symbol, tier, indicator, AlertSeverity.YELLOW, value, cfg["yellow_high"], action)
    if cfg.get("yellow_low") is not None and value < cfg["yellow_low"]:
        return _make_alert(symbol, tier, indicator, AlertSeverity.YELLOW, value, cfg["yellow_low"], action)
    return None


def _check_low_threshold(
    symbol: str, tier: Literal[1, 2, 3],
    indicator: str, value: float, cfg: dict,
) -> AlertEvent | None:
    """低方向阈值（lower = worse）。支持 red/yellow 或 red_low/yellow_low 键。"""
    action = cfg.get("action")
    r = cfg.get("red_low") if cfg.get("red_low") is not None else cfg.get("red")
    y = cfg.get("yellow_low") if cfg.get("yellow_low") is not None else cfg.get("yellow")
    if r is not None and value <= r:
        return _make_alert(symbol, tier, indicator, AlertSeverity.RED, value, r, action)
    if y is not None and value <= y:
        return _make_alert(symbol, tier, indicator, AlertSeverity.YELLOW, value, y, action)
    return None


# ---------------------------------------------------------------------------
# 场景失效条件评估
# ---------------------------------------------------------------------------


def _count_invalidations(
    conditions: list[str],
    current: MarketParameterSnapshot,
    baseline: MarketParameterSnapshot,
) -> int:
    """统计已触发的场景失效条件数量。"""
    return sum(1 for c in conditions if _eval_condition(c, current, baseline))


def _eval_condition(
    cond: str,
    cur: MarketParameterSnapshot,
    base: MarketParameterSnapshot,
) -> bool:
    """按关键词模式匹配评估单个失效条件。无法识别的条件返回 False。"""
    cl = cond.lower()

    # GEX 翻转
    if "gex" in cl and ("翻" in cond or "flip" in cl):
        return _sign_flipped(cur.net_gex, base.net_gex)

    # DEX 方向翻转
    if "dex" in cl and ("翻" in cond or "flip" in cl):
        return _sign_flipped(cur.net_dex, base.net_dex)

    # Spot 突破 wall
    if "spot" in cl and ("wall" in cl or "突破" in cond or "跌破" in cond):
        above_call = (
            cur.call_wall_strike is not None and cur.spot_price > cur.call_wall_strike
        )
        below_put = (
            cur.put_wall_strike is not None and cur.spot_price < cur.put_wall_strike
        )
        return above_call or below_put

    # 事件已过
    if "事件已过" in cond:
        return cur.days_to_event is not None and cur.days_to_event < 0

    # 事件进入窗口
    if "事件" in cond and ("窗口" in cond or "T-3" in cond):
        return cur.days_to_event is not None and cur.days_to_event <= 3

    # 期限结构翻转
    if "term" in cl or "期限" in cond:
        return _sign_flipped(cur.term_spread, base.term_spread)

    return False
