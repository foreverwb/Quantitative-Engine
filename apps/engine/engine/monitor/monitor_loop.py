"""
engine/monitor/monitor_loop.py — 监控后台循环

职责: 在 FastAPI lifespan 中作为后台 asyncio task 运行，定时执行
      快照采集 → 告警评估 → 增量重算 → 持久化，支持优雅停止。
依赖: engine.monitor.snapshot_collector, engine.monitor.alert_engine,
      engine.monitor.incremental_recalc, engine.db
被依赖: engine.main (lifespan)
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from engine.db.models import (
    AlertEventRow,
    MonitorStateSnapshotRow,
    TrackedPositionRow,
)
from engine.models.alerts import AlertEvent
from engine.models.snapshots import (
    AnalysisResultSnapshot,
    MarketParameterSnapshot,
    MonitorStateSnapshot,
)
from engine.monitor.alert_engine import AlertEngine
from engine.monitor.incremental_recalc import (
    IncrementalRecalculator,
    RecalcOutput,
)
from engine.monitor.snapshot_collector import SnapshotCollector

logger = logging.getLogger(__name__)

# recalc_from_step_N 中提取 step 数字的分隔符
_ACTION_PREFIX = "recalc_from_step_"


class MonitorLoopError(Exception):
    """监控循环异常"""


class MonitorLoop:
    """
    监控后台循环。

    每隔 refresh_interval 秒对所有已注册的 symbol 执行:
      1. 采集市场参数快照
      2. 评估告警（对比基线）
      3. 若有红色告警，触发增量重算
      4. 持久化 MonitorStateSnapshot 和 AlertEvent

    用法:
        loop = MonitorLoop(config, collector, alert_engine, recalculator,
                           db_session_factory)
        loop.register_symbol("AAPL", recalc_output)
        task = asyncio.create_task(loop.run())
        # ... 停止时:
        loop.shutdown()
        await task
    """

    def __init__(
        self,
        refresh_interval: int,
        snapshot_collector: SnapshotCollector,
        alert_engine: AlertEngine,
        recalculator: IncrementalRecalculator,
        db_session_factory: Any,
    ) -> None:
        self._interval = refresh_interval
        self._collector = snapshot_collector
        self._alert_engine = alert_engine
        self._recalculator = recalculator
        self._db_factory = db_session_factory
        self._shutdown_event = asyncio.Event()
        self._states: dict[str, _SymbolState] = {}

    # ------------------------------------------------------------------
    # Symbol 注册/注销
    # ------------------------------------------------------------------

    def register_symbol(
        self,
        symbol: str,
        baseline: MarketParameterSnapshot,
        analysis: AnalysisResultSnapshot,
        recalc_output: RecalcOutput | None = None,
    ) -> None:
        """注册一个 symbol 进入监控，提供基线快照和分析结果。"""
        self._states[symbol] = _SymbolState(
            baseline=baseline,
            analysis=analysis,
            recalc_output=recalc_output,
        )
        logger.info("MonitorLoop: registered symbol=%s", symbol)

    def unregister_symbol(self, symbol: str) -> None:
        """从监控中移除 symbol。"""
        self._states.pop(symbol, None)
        logger.info("MonitorLoop: unregistered symbol=%s", symbol)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """主监控循环，直到 shutdown() 被调用。"""
        logger.info(
            "MonitorLoop: started (interval=%ds, symbols=%d)",
            self._interval,
            len(self._states),
        )
        while not self._shutdown_event.is_set():
            for symbol in list(self._states):
                try:
                    await self._tick(symbol)
                except Exception:
                    logger.exception(
                        "MonitorLoop: tick failed for %s", symbol,
                    )
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                pass
        logger.info("MonitorLoop: stopped")

    def shutdown(self) -> None:
        """发出优雅停止信号。"""
        self._shutdown_event.set()

    # ------------------------------------------------------------------
    # 单次 tick
    # ------------------------------------------------------------------

    async def _tick(self, symbol: str) -> None:
        """对单个 symbol 执行一轮监控。"""
        state = self._states.get(symbol)
        if state is None:
            return

        # a. 采集最新快照
        snapshot = await self._collector.collect_market_snapshot(symbol)

        # b. 查询活跃持仓
        positions = self._query_positions(symbol)

        # c. 评估告警
        alerts, recalc_action = self._alert_engine.evaluate(
            current=snapshot,
            baseline=state.baseline,
            analysis=state.analysis,
            positions=positions,
        )

        # d. 若有红色告警 → 增量重算
        if recalc_action and recalc_action.startswith(_ACTION_PREFIX):
            await self._do_recalc(symbol, state, recalc_action)

        # e. 持久化
        monitor_state = self._build_monitor_state(
            symbol, snapshot, state, recalc_action,
        )
        self._persist_results(monitor_state, alerts)

    # ------------------------------------------------------------------
    # 增量重算
    # ------------------------------------------------------------------

    async def _do_recalc(
        self, symbol: str, state: _SymbolState, action: str,
    ) -> None:
        """解析 action → step，执行增量重算，更新缓存。"""
        step = _parse_step(action)
        logger.info(
            "MonitorLoop: recalc symbol=%s step=%d", symbol, step,
        )

        cached = state.recalc_output
        result = await self._recalculator.recalc_from(
            step=step,
            symbol=symbol,
            trade_date=date.today(),
            cached_context=cached.context if cached else None,
            cached_pre_calc=cached.pre_calc if cached else None,
            cached_micro=cached.micro if cached else None,
            cached_scores=cached.scores if cached else None,
            cached_scenario=cached.scenario if cached else None,
        )

        if result is not None:
            state.baseline = result.baseline
            state.analysis = result.analysis
            state.recalc_output = result

    # ------------------------------------------------------------------
    # 构建 & 持久化
    # ------------------------------------------------------------------

    def _build_monitor_state(
        self,
        symbol: str,
        snapshot: MarketParameterSnapshot,
        state: _SymbolState,
        recalc_action: str | None,
    ) -> MonitorStateSnapshot:
        """构建 MonitorStateSnapshot。"""
        drift = self._collector.compute_drift(
            current=snapshot, baseline=state.baseline,
        )
        return MonitorStateSnapshot(
            monitor_id=str(uuid.uuid4()),
            symbol=symbol,
            captured_at=datetime.now(tz=timezone.utc),
            analysis_id=state.analysis.analysis_id,
            baseline_snapshot_id=state.analysis.baseline_snapshot_id,
            spot_drift_pct=drift["spot_drift_pct"],
            iv_drift_pct=drift["iv_drift_pct"],
            zero_gamma_drift_pct=drift["zero_gamma_drift_pct"],
            term_structure_flip=drift["term_structure_flip"],
            gex_sign_flip=drift["gex_sign_flip"],
            recommended_action=recalc_action,
        )

    def _persist_results(
        self,
        monitor_state: MonitorStateSnapshot,
        alerts: list[AlertEvent],
    ) -> None:
        """将 MonitorStateSnapshot 和 AlertEvent 写入数据库。"""
        db: Session = self._db_factory()
        try:
            db.add(MonitorStateSnapshotRow(
                monitor_id=monitor_state.monitor_id,
                symbol=monitor_state.symbol,
                captured_at=monitor_state.captured_at,
                analysis_id=monitor_state.analysis_id,
                baseline_snapshot_id=monitor_state.baseline_snapshot_id,
                state_json=monitor_state.model_dump_json(),
            ))
            for alert in alerts:
                db.add(AlertEventRow(
                    alert_id=alert.alert_id,
                    symbol=alert.symbol,
                    timestamp=alert.timestamp,
                    tier=alert.tier,
                    indicator=alert.indicator,
                    severity=alert.severity.value,
                    old_value=str(alert.old_value) if alert.old_value else None,
                    new_value=str(alert.new_value),
                    threshold=str(alert.threshold) if alert.threshold else None,
                    action=alert.action,
                ))
            db.commit()
        finally:
            db.close()

    def _query_positions(self, symbol: str) -> list[dict]:
        """查询活跃持仓列表。"""
        db: Session = self._db_factory()
        try:
            rows = (
                db.query(TrackedPositionRow)
                .filter(
                    TrackedPositionRow.symbol == symbol,
                    TrackedPositionRow.status == "active",
                )
                .all()
            )
            return [
                json.loads(r.legs_json) if isinstance(r.legs_json, str) else {}
                for r in rows
            ]
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 内部数据结构
# ---------------------------------------------------------------------------


class _SymbolState:
    """单个 symbol 的监控状态（可变，不使用 frozen）。"""

    __slots__ = ("baseline", "analysis", "recalc_output")

    def __init__(
        self,
        baseline: MarketParameterSnapshot,
        analysis: AnalysisResultSnapshot,
        recalc_output: RecalcOutput | None,
    ) -> None:
        self.baseline = baseline
        self.analysis = analysis
        self.recalc_output = recalc_output


# ---------------------------------------------------------------------------
# 私有辅助
# ---------------------------------------------------------------------------


def _parse_step(action: str) -> int:
    """从 'recalc_from_step_N' 提取 step 数字。"""
    try:
        return int(action.removeprefix(_ACTION_PREFIX))
    except ValueError as exc:
        raise MonitorLoopError(
            f"Invalid recalc action: {action!r}"
        ) from exc
