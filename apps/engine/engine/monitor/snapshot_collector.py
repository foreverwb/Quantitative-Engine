"""
engine/monitor/snapshot_collector.py — 市场参数快照采集器

职责: 调用 Micro-Provider 获取最新市场数据，构造 MarketParameterSnapshot，
      写入 market_parameter_snapshots 表，并计算快照间偏移度。
      同时负责执行快照保留策略（30 天内 5 分钟粒度，超过 30 天按日聚合删除）。
依赖: engine.models.snapshots, engine.providers.micro_client,
      engine.db.models, sqlalchemy
被依赖: engine.monitor.alert_engine, engine.monitor.incremental_recalc,
        engine.api (监控循环)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from engine.db.models import MarketParameterSnapshotRow
from engine.models.snapshots import MarketParameterSnapshot

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 保留策略阈值
_RETENTION_DAYS = 30
# 5 分钟粒度（秒）
_GRANULARITY_SECONDS = 300


class SnapshotCollectorError(Exception):
    """快照采集失败"""


class SnapshotCollector:
    """
    市场参数快照采集器。

    职责:
      1. collect_market_snapshot: 从 Micro-Provider 获取最新数据并持久化
      2. compute_drift:           计算两个快照之间的偏移度
      3. apply_retention_policy:  清理超出保留策略的旧快照

    用法:
        collector = SnapshotCollector(micro_client=client, db_session=session)
        snapshot = await collector.collect_market_snapshot("AAPL")
        drift = collector.compute_drift(current=snapshot, baseline=baseline)
    """

    def __init__(
        self,
        micro_client: Any,  # MicroClient — 避免循环 import
        db_session: Session,
    ) -> None:
        self._micro = micro_client
        self._db = db_session

    # ------------------------------------------------------------------
    # 主采集入口
    # ------------------------------------------------------------------

    async def collect_market_snapshot(self, symbol: str) -> MarketParameterSnapshot:
        """
        采集 symbol 的当前市场参数，写入数据库，返回快照对象。

        步骤:
          1. 调用 Micro-Provider 获取 MicroSnapshot（通过注入的 micro_client）
          2. 从 MicroSnapshot 提取所有 MarketParameterSnapshot 字段
          3. 持久化到 market_parameter_snapshots 表
          4. 触发保留策略清理

        Args:
            symbol: 标的代码，如 "AAPL"

        Returns:
            MarketParameterSnapshot: 已持久化的快照

        Raises:
            SnapshotCollectorError: Micro-Provider 调用失败或数据提取异常
        """
        try:
            raw = await self._micro.get_latest_snapshot(symbol)
        except Exception as exc:
            raise SnapshotCollectorError(
                f"Micro-Provider 调用失败 [{symbol}]: {exc}"
            ) from exc

        try:
            snapshot = _build_snapshot(symbol, raw)
        except Exception as exc:
            raise SnapshotCollectorError(
                f"快照字段提取失败 [{symbol}]: {exc}"
            ) from exc

        self._persist(snapshot)
        self._apply_retention_policy(symbol)

        logger.info(
            "SnapshotCollector: 已采集 symbol=%s snapshot_id=%s spot=%.2f atm_iv=%.4f",
            symbol,
            snapshot.snapshot_id,
            snapshot.spot_price,
            snapshot.atm_iv_front,
        )
        return snapshot

    # ------------------------------------------------------------------
    # 偏移度计算
    # ------------------------------------------------------------------

    def compute_drift(
        self,
        current: MarketParameterSnapshot,
        baseline: MarketParameterSnapshot,
    ) -> dict[str, float | bool]:
        """
        计算 current 相对于 baseline 的各维度偏移度。

        返回字段:
          - spot_drift_pct:       现货价格相对偏移 (current-baseline)/baseline
          - iv_drift_pct:         ATM IV 相对偏移
          - zero_gamma_drift_pct: Zero Gamma Strike 相对偏移
          - term_structure_flip:  期限结构翻转 (front/back 符号变化)
          - gex_sign_flip:        GEX 符号翻转

        Args:
            current:  当前快照
            baseline: 基线快照

        Returns:
            偏移度字典，键值与 MonitorStateSnapshot 字段对应
        """
        return {
            "spot_drift_pct": _pct_change(
                current.spot_price, baseline.spot_price
            ),
            "iv_drift_pct": _pct_change(
                current.atm_iv_front, baseline.atm_iv_front
            ),
            "zero_gamma_drift_pct": _pct_change(
                current.zero_gamma_strike, baseline.zero_gamma_strike
            ),
            "term_structure_flip": _detect_term_flip(current, baseline),
            "gex_sign_flip": _detect_gex_flip(current, baseline),
        }

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _persist(self, snapshot: MarketParameterSnapshot) -> None:
        """将快照写入 market_parameter_snapshots 表。"""
        row = MarketParameterSnapshotRow(
            snapshot_id=snapshot.snapshot_id,
            symbol=snapshot.symbol,
            captured_at=snapshot.captured_at,
            data_json=snapshot.model_dump_json(),
        )
        self._db.add(row)
        self._db.commit()

    # ------------------------------------------------------------------
    # 保留策略
    # ------------------------------------------------------------------

    def _apply_retention_policy(self, symbol: str) -> None:
        """
        执行快照保留策略:
          - 30 天内: 按 5 分钟粒度保留（每个 5 分钟窗口只保留最新一条）
          - 超过 30 天: 按日聚合（每天只保留最后一条，其余删除）
        """
        now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
        cutoff_30d = now - timedelta(days=_RETENTION_DAYS)

        self._prune_fine_grain(symbol, cutoff_30d, now)
        self._prune_old_daily(symbol, cutoff_30d)

    def _prune_fine_grain(
        self,
        symbol: str,
        cutoff_30d: datetime,
        now: datetime,
    ) -> None:
        """
        30 天内按 5 分钟粒度去重：
        每个 5 分钟桶内只保留 captured_at 最大的一条，其余删除。
        """
        rows = (
            self._db.execute(
                select(MarketParameterSnapshotRow).where(
                    MarketParameterSnapshotRow.symbol == symbol,
                    MarketParameterSnapshotRow.captured_at >= cutoff_30d,
                    MarketParameterSnapshotRow.captured_at <= now,
                )
            )
            .scalars()
            .all()
        )

        # 按 5 分钟桶分组，每桶保留 captured_at 最大的 id
        buckets: dict[int, list[MarketParameterSnapshotRow]] = {}
        for row in rows:
            ts = row.captured_at.timestamp() if isinstance(row.captured_at, datetime) else 0.0
            bucket = int(ts) // _GRANULARITY_SECONDS
            buckets.setdefault(bucket, []).append(row)

        ids_to_delete: list[int] = []
        for bucket_rows in buckets.values():
            if len(bucket_rows) <= 1:
                continue
            keep = max(bucket_rows, key=lambda r: r.captured_at)
            ids_to_delete.extend(
                r.id for r in bucket_rows if r.id != keep.id
            )

        if ids_to_delete:
            self._db.execute(
                delete(MarketParameterSnapshotRow).where(
                    MarketParameterSnapshotRow.id.in_(ids_to_delete)
                )
            )
            self._db.commit()
            self._db.expire_all()
            logger.debug(
                "SnapshotCollector: 精细粒度清理 symbol=%s 删除 %d 条",
                symbol,
                len(ids_to_delete),
            )

    def _prune_old_daily(self, symbol: str, cutoff_30d: datetime) -> None:
        """
        超过 30 天的快照按日聚合：
        每天只保留 captured_at 最大的一条，其余删除。
        """
        rows = (
            self._db.execute(
                select(MarketParameterSnapshotRow).where(
                    MarketParameterSnapshotRow.symbol == symbol,
                    MarketParameterSnapshotRow.captured_at < cutoff_30d,
                )
            )
            .scalars()
            .all()
        )

        daily_buckets: dict[str, list[MarketParameterSnapshotRow]] = {}
        for row in rows:
            day_key = row.captured_at.strftime("%Y-%m-%d")
            daily_buckets.setdefault(day_key, []).append(row)

        ids_to_delete: list[int] = []
        for day_rows in daily_buckets.values():
            if len(day_rows) <= 1:
                continue
            keep = max(day_rows, key=lambda r: r.captured_at)
            ids_to_delete.extend(
                r.id for r in day_rows if r.id != keep.id
            )

        if ids_to_delete:
            self._db.execute(
                delete(MarketParameterSnapshotRow).where(
                    MarketParameterSnapshotRow.id.in_(ids_to_delete)
                )
            )
            self._db.commit()
            self._db.expire_all()
            logger.debug(
                "SnapshotCollector: 历史日聚合清理 symbol=%s 删除 %d 条",
                symbol,
                len(ids_to_delete),
            )


# ---------------------------------------------------------------------------
# 私有辅助函数
# ---------------------------------------------------------------------------


def _build_snapshot(symbol: str, raw: Any) -> MarketParameterSnapshot:
    """
    从 Micro-Provider 原始数据对象构建 MarketParameterSnapshot。

    raw 需提供以下属性（与 MicroSnapshot + summary/ivrank 字段对应）:
      spot_price, atm_iv_front, atm_iv_back, iv30d, hv20d,
      vrp, vol_of_vol, iv_rank, iv_pctl, iv_consensus,
      net_gex, net_dex, zero_gamma_strike,
      call_wall_strike, put_wall_strike,
      vol_pcr, oi_pcr,
      regime_class, next_event_type, days_to_event
    """
    spot = float(raw.spot_price)
    atm_iv_front = float(raw.atm_iv_front)
    atm_iv_back = _opt_float(getattr(raw, "atm_iv_back", None))
    term_spread = (atm_iv_back - atm_iv_front) if atm_iv_back is not None else 0.0

    return MarketParameterSnapshot(
        snapshot_id=str(uuid.uuid4()),
        symbol=symbol,
        captured_at=datetime.now(tz=timezone.utc).replace(tzinfo=None),
        spot_price=spot,
        spot_change_pct=_opt_float(getattr(raw, "spot_change_pct", 0.0)) or 0.0,
        atm_iv_front=atm_iv_front,
        atm_iv_back=atm_iv_back,
        term_spread=term_spread,
        iv30d=float(raw.iv30d),
        hv20d=_opt_float(getattr(raw, "hv20d", None)),
        vrp=_opt_float(getattr(raw, "vrp", 0.0)) or 0.0,
        vol_of_vol=_opt_float(getattr(raw, "vol_of_vol", 0.0)) or 0.0,
        iv_rank=_opt_float(getattr(raw, "iv_rank", 0.0)) or 0.0,
        iv_pctl=_opt_float(getattr(raw, "iv_pctl", 0.0)) or 0.0,
        iv_consensus=_opt_float(getattr(raw, "iv_consensus", 0.0)) or 0.0,
        net_gex=_opt_float(getattr(raw, "net_gex", 0.0)) or 0.0,
        net_dex=_opt_float(getattr(raw, "net_dex", 0.0)) or 0.0,
        zero_gamma_strike=_opt_float(getattr(raw, "zero_gamma_strike", None)),
        call_wall_strike=_opt_float(getattr(raw, "call_wall_strike", None)),
        put_wall_strike=_opt_float(getattr(raw, "put_wall_strike", None)),
        vol_pcr=_opt_float(getattr(raw, "vol_pcr", None)),
        oi_pcr=_opt_float(getattr(raw, "oi_pcr", None)),
        regime_class=str(getattr(raw, "regime_class", "NORMAL")),
        next_event_type=_opt_str(getattr(raw, "next_event_type", None)),
        days_to_event=_opt_int(getattr(raw, "days_to_event", None)),
    )


def _pct_change(current: float | None, baseline: float | None) -> float:
    """
    计算百分比变化 (current - baseline) / |baseline|。
    任一值为 None 或 baseline 为 0 时返回 0.0。
    """
    if current is None or baseline is None or baseline == 0.0:
        return 0.0
    return (current - baseline) / abs(baseline)


def _detect_term_flip(
    current: MarketParameterSnapshot,
    baseline: MarketParameterSnapshot,
) -> bool:
    """
    检测期限结构翻转: term_spread 符号从正变负或从负变正。
    """
    b_spread = baseline.term_spread
    c_spread = current.term_spread
    if b_spread == 0.0 or c_spread == 0.0:
        return False
    return (b_spread > 0.0) != (c_spread > 0.0)


def _detect_gex_flip(
    current: MarketParameterSnapshot,
    baseline: MarketParameterSnapshot,
) -> bool:
    """
    检测 GEX 符号翻转: net_gex 从正变负或从负变正。
    """
    b_gex = baseline.net_gex
    c_gex = current.net_gex
    if b_gex == 0.0 or c_gex == 0.0:
        return False
    return (b_gex > 0.0) != (c_gex > 0.0)


def _opt_float(val: Any) -> float | None:
    """安全转换为 float，None 或转换失败时返回 None。"""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _opt_str(val: Any) -> str | None:
    return str(val) if val is not None else None


def _opt_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None
