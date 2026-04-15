"""
tests/test_snapshot_collector.py — SnapshotCollector 单元测试

覆盖:
  - collect_market_snapshot: 快照写入数据库、返回正确字段
  - collect_market_snapshot: Micro-Provider 调用失败时抛出 SnapshotCollectorError
  - compute_drift: spot/iv/zero_gamma 偏移度计算
  - compute_drift: baseline 为 0 时返回 0.0（防除零）
  - compute_drift: None 值处理
  - compute_drift: term_structure_flip 检测
  - compute_drift: gex_sign_flip 检测
  - _apply_retention_policy: 30 天内同桶多条 → 只保留最新
  - _apply_retention_policy: 超过 30 天同日多条 → 只保留最新
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from engine.db.models import Base, MarketParameterSnapshotRow
from engine.models.snapshots import MarketParameterSnapshot
from engine.monitor.snapshot_collector import (
    SnapshotCollector,
    SnapshotCollectorError,
    _pct_change,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SYMBOL = "AAPL"
SPOT = 200.0
ATM_IV = 0.25


# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------


def _make_raw(
    spot_price: float = SPOT,
    atm_iv_front: float = ATM_IV,
    atm_iv_back: float | None = 0.28,
    iv30d: float = 0.26,
    hv20d: float | None = 0.22,
    vrp: float = 0.03,
    vol_of_vol: float = 0.05,
    iv_rank: float = 60.0,
    iv_pctl: float = 65.0,
    iv_consensus: float = 0.255,
    net_gex: float = 1_000_000.0,
    net_dex: float = -500_000.0,
    zero_gamma_strike: float | None = 198.0,
    call_wall_strike: float | None = 210.0,
    put_wall_strike: float | None = 190.0,
    vol_pcr: float | None = 0.8,
    oi_pcr: float | None = 0.75,
    regime_class: str = "NORMAL",
    next_event_type: str | None = None,
    days_to_event: int | None = None,
) -> SimpleNamespace:
    """构造 Micro-Provider 返回的原始数据对象（SimpleNamespace duck-type）。"""
    return SimpleNamespace(
        spot_price=spot_price,
        atm_iv_front=atm_iv_front,
        atm_iv_back=atm_iv_back,
        iv30d=iv30d,
        hv20d=hv20d,
        vrp=vrp,
        vol_of_vol=vol_of_vol,
        iv_rank=iv_rank,
        iv_pctl=iv_pctl,
        iv_consensus=iv_consensus,
        net_gex=net_gex,
        net_dex=net_dex,
        zero_gamma_strike=zero_gamma_strike,
        call_wall_strike=call_wall_strike,
        put_wall_strike=put_wall_strike,
        vol_pcr=vol_pcr,
        oi_pcr=oi_pcr,
        regime_class=regime_class,
        next_event_type=next_event_type,
        days_to_event=days_to_event,
    )


def _make_snapshot(
    symbol: str = SYMBOL,
    spot_price: float = SPOT,
    atm_iv_front: float = ATM_IV,
    atm_iv_back: float | None = 0.28,
    term_spread: float = 0.03,
    net_gex: float = 1_000_000.0,
    zero_gamma_strike: float | None = 198.0,
    captured_at: datetime | None = None,
) -> MarketParameterSnapshot:
    """构造 MarketParameterSnapshot 用于偏移度测试。"""
    if captured_at is None:
        captured_at = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    return MarketParameterSnapshot(
        snapshot_id=str(uuid.uuid4()),
        symbol=symbol,
        captured_at=captured_at,
        spot_price=spot_price,
        atm_iv_front=atm_iv_front,
        atm_iv_back=atm_iv_back,
        term_spread=term_spread,
        iv30d=0.26,
        net_gex=net_gex,
        zero_gamma_strike=zero_gamma_strike,
    )


# ---------------------------------------------------------------------------
# DB Fixture: 内存 SQLite
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session() -> Session:
    """提供内存 SQLite Session，每个测试隔离。"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session: Session = SessionLocal()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# collect_market_snapshot 测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_market_snapshot_writes_to_db(db_session: Session) -> None:
    """collect_market_snapshot 应将快照持久化到数据库。"""
    raw = _make_raw()
    mock_micro = AsyncMock()
    mock_micro.get_latest_snapshot = AsyncMock(return_value=raw)

    collector = SnapshotCollector(micro_client=mock_micro, db_session=db_session)
    snapshot = await collector.collect_market_snapshot(SYMBOL)

    assert isinstance(snapshot, MarketParameterSnapshot)
    assert snapshot.symbol == SYMBOL
    assert snapshot.spot_price == pytest.approx(SPOT)
    assert snapshot.atm_iv_front == pytest.approx(ATM_IV)

    # 数据库中应存在该行
    rows = db_session.execute(
        select(MarketParameterSnapshotRow).where(
            MarketParameterSnapshotRow.snapshot_id == snapshot.snapshot_id
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].symbol == SYMBOL


@pytest.mark.asyncio
async def test_collect_market_snapshot_computes_term_spread(db_session: Session) -> None:
    """term_spread 应等于 atm_iv_back - atm_iv_front。"""
    raw = _make_raw(atm_iv_front=0.25, atm_iv_back=0.30)
    mock_micro = AsyncMock()
    mock_micro.get_latest_snapshot = AsyncMock(return_value=raw)

    collector = SnapshotCollector(micro_client=mock_micro, db_session=db_session)
    snapshot = await collector.collect_market_snapshot(SYMBOL)

    assert snapshot.term_spread == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_collect_market_snapshot_handles_none_atm_iv_back(
    db_session: Session,
) -> None:
    """atm_iv_back 为 None 时 term_spread 应为 0.0。"""
    raw = _make_raw(atm_iv_back=None)
    mock_micro = AsyncMock()
    mock_micro.get_latest_snapshot = AsyncMock(return_value=raw)

    collector = SnapshotCollector(micro_client=mock_micro, db_session=db_session)
    snapshot = await collector.collect_market_snapshot(SYMBOL)

    assert snapshot.atm_iv_back is None
    assert snapshot.term_spread == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_collect_market_snapshot_raises_on_provider_failure(
    db_session: Session,
) -> None:
    """Micro-Provider 抛异常时应包装为 SnapshotCollectorError。"""
    mock_micro = AsyncMock()
    mock_micro.get_latest_snapshot = AsyncMock(
        side_effect=RuntimeError("连接超时")
    )

    collector = SnapshotCollector(micro_client=mock_micro, db_session=db_session)
    with pytest.raises(SnapshotCollectorError, match="Micro-Provider 调用失败"):
        await collector.collect_market_snapshot(SYMBOL)


@pytest.mark.asyncio
async def test_collect_market_snapshot_snapshot_id_is_uuid(
    db_session: Session,
) -> None:
    """每次采集应生成唯一的 UUID snapshot_id。"""
    raw = _make_raw()
    mock_micro = AsyncMock()
    mock_micro.get_latest_snapshot = AsyncMock(return_value=raw)

    collector = SnapshotCollector(micro_client=mock_micro, db_session=db_session)
    s1 = await collector.collect_market_snapshot(SYMBOL)
    s2 = await collector.collect_market_snapshot(SYMBOL)

    assert s1.snapshot_id != s2.snapshot_id
    # 确认是合法 UUID
    uuid.UUID(s1.snapshot_id)
    uuid.UUID(s2.snapshot_id)


# ---------------------------------------------------------------------------
# compute_drift 测试
# ---------------------------------------------------------------------------


def _make_collector(db_session: Session) -> SnapshotCollector:
    return SnapshotCollector(micro_client=MagicMock(), db_session=db_session)


def test_compute_drift_spot_drift_pct(db_session: Session) -> None:
    """spot 涨 2% 时 spot_drift_pct 应约为 0.02。"""
    baseline = _make_snapshot(spot_price=200.0)
    current = _make_snapshot(spot_price=204.0)

    drift = _make_collector(db_session).compute_drift(current, baseline)

    assert drift["spot_drift_pct"] == pytest.approx(0.02)


def test_compute_drift_iv_drift_pct(db_session: Session) -> None:
    """ATM IV 从 0.25 升至 0.30 时 iv_drift_pct 应约为 0.20。"""
    baseline = _make_snapshot(atm_iv_front=0.25)
    current = _make_snapshot(atm_iv_front=0.30)

    drift = _make_collector(db_session).compute_drift(current, baseline)

    assert drift["iv_drift_pct"] == pytest.approx(0.20)


def test_compute_drift_zero_gamma_drift_pct(db_session: Session) -> None:
    """zero_gamma_strike 移动 5 点时，偏移度应正确。"""
    baseline = _make_snapshot(zero_gamma_strike=200.0)
    current = _make_snapshot(zero_gamma_strike=210.0)

    drift = _make_collector(db_session).compute_drift(current, baseline)

    assert drift["zero_gamma_drift_pct"] == pytest.approx(0.05)


def test_compute_drift_zero_when_baseline_zero(db_session: Session) -> None:
    """baseline spot 为 0 时 spot_drift_pct 应返回 0.0，不报除零错误。"""
    baseline = _make_snapshot(spot_price=0.0)
    current = _make_snapshot(spot_price=200.0)

    drift = _make_collector(db_session).compute_drift(current, baseline)

    assert drift["spot_drift_pct"] == pytest.approx(0.0)


def test_compute_drift_zero_gamma_none(db_session: Session) -> None:
    """zero_gamma_strike 任一为 None 时 zero_gamma_drift_pct 应为 0.0。"""
    baseline = _make_snapshot(zero_gamma_strike=None)
    current = _make_snapshot(zero_gamma_strike=200.0)

    drift = _make_collector(db_session).compute_drift(current, baseline)

    assert drift["zero_gamma_drift_pct"] == pytest.approx(0.0)


def test_compute_drift_term_structure_flip_detected(db_session: Session) -> None:
    """term_spread 从正转负时 term_structure_flip 应为 True。"""
    baseline = _make_snapshot(term_spread=0.03)    # contango
    current = _make_snapshot(term_spread=-0.02)    # backwardation

    drift = _make_collector(db_session).compute_drift(current, baseline)

    assert drift["term_structure_flip"] is True


def test_compute_drift_term_structure_no_flip(db_session: Session) -> None:
    """term_spread 同符号不翻转时应为 False。"""
    baseline = _make_snapshot(term_spread=0.03)
    current = _make_snapshot(term_spread=0.05)

    drift = _make_collector(db_session).compute_drift(current, baseline)

    assert drift["term_structure_flip"] is False


def test_compute_drift_gex_sign_flip_detected(db_session: Session) -> None:
    """net_gex 从正转负时 gex_sign_flip 应为 True。"""
    baseline = _make_snapshot(net_gex=1_000_000.0)
    current = _make_snapshot(net_gex=-500_000.0)

    drift = _make_collector(db_session).compute_drift(current, baseline)

    assert drift["gex_sign_flip"] is True


def test_compute_drift_gex_sign_no_flip(db_session: Session) -> None:
    """net_gex 同符号不翻转时 gex_sign_flip 应为 False。"""
    baseline = _make_snapshot(net_gex=1_000_000.0)
    current = _make_snapshot(net_gex=800_000.0)

    drift = _make_collector(db_session).compute_drift(current, baseline)

    assert drift["gex_sign_flip"] is False


def test_compute_drift_gex_sign_flip_zero_gex(db_session: Session) -> None:
    """任一 net_gex 为 0 时不视为翻转，返回 False。"""
    baseline = _make_snapshot(net_gex=0.0)
    current = _make_snapshot(net_gex=-500_000.0)

    drift = _make_collector(db_session).compute_drift(current, baseline)

    assert drift["gex_sign_flip"] is False


# ---------------------------------------------------------------------------
# 保留策略测试
# ---------------------------------------------------------------------------


def _insert_row(
    db_session: Session,
    symbol: str,
    captured_at: datetime,
) -> MarketParameterSnapshotRow:
    """在数据库中插入一条快照行（用于保留策略测试）。"""
    snap = _make_snapshot(symbol=symbol, captured_at=captured_at)
    row = MarketParameterSnapshotRow(
        snapshot_id=snap.snapshot_id,
        symbol=symbol,
        captured_at=captured_at,
        data_json=snap.model_dump_json(),
    )
    db_session.add(row)
    db_session.commit()
    return row


def _count_rows(db_session: Session, symbol: str) -> int:
    return len(
        db_session.execute(
            select(MarketParameterSnapshotRow).where(
                MarketParameterSnapshotRow.symbol == symbol
            )
        ).scalars().all()
    )


def test_retention_policy_fine_grain_keeps_latest_per_bucket(
    db_session: Session,
) -> None:
    """
    30 天内同一 5 分钟桶内多条快照 → 只保留 captured_at 最大的一条。
    """
    collector = _make_collector(db_session)
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)

    # 同一 5 分钟桶：t0, t0+1min, t0+2min
    t0 = now - timedelta(hours=1)
    _insert_row(db_session, SYMBOL, t0)
    _insert_row(db_session, SYMBOL, t0 + timedelta(minutes=1))
    latest = _insert_row(db_session, SYMBOL, t0 + timedelta(minutes=2))

    collector._apply_retention_policy(SYMBOL)

    remaining = db_session.execute(
        select(MarketParameterSnapshotRow).where(
            MarketParameterSnapshotRow.symbol == SYMBOL
        )
    ).scalars().all()

    assert len(remaining) == 1
    assert remaining[0].snapshot_id == latest.snapshot_id


def test_retention_policy_fine_grain_different_buckets_kept(
    db_session: Session,
) -> None:
    """
    30 天内不同 5 分钟桶的快照应全部保留。
    """
    collector = _make_collector(db_session)
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)

    base = now - timedelta(hours=2)
    for i in range(3):
        _insert_row(db_session, SYMBOL, base + timedelta(minutes=i * 5))

    collector._apply_retention_policy(SYMBOL)

    assert _count_rows(db_session, SYMBOL) == 3


def test_retention_policy_daily_aggregation_old_snapshots(
    db_session: Session,
) -> None:
    """
    超过 30 天的同一天多条快照 → 只保留当天最新一条。
    """
    collector = _make_collector(db_session)
    old_day = datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(days=35)

    _insert_row(db_session, SYMBOL, old_day.replace(hour=9, minute=0))
    _insert_row(db_session, SYMBOL, old_day.replace(hour=12, minute=0))
    latest = _insert_row(db_session, SYMBOL, old_day.replace(hour=16, minute=0))

    collector._apply_retention_policy(SYMBOL)

    remaining = db_session.execute(
        select(MarketParameterSnapshotRow).where(
            MarketParameterSnapshotRow.symbol == SYMBOL
        )
    ).scalars().all()

    assert len(remaining) == 1
    assert remaining[0].snapshot_id == latest.snapshot_id


def test_retention_policy_daily_aggregation_different_days_kept(
    db_session: Session,
) -> None:
    """
    超过 30 天的不同日期快照（每天只有一条）应全部保留。
    """
    collector = _make_collector(db_session)
    base = datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(days=40)

    for i in range(3):
        _insert_row(db_session, SYMBOL, base + timedelta(days=i))

    collector._apply_retention_policy(SYMBOL)

    assert _count_rows(db_session, SYMBOL) == 3


# ---------------------------------------------------------------------------
# _pct_change 辅助函数
# ---------------------------------------------------------------------------


def test_pct_change_positive_drift() -> None:
    assert _pct_change(210.0, 200.0) == pytest.approx(0.05)


def test_pct_change_negative_drift() -> None:
    assert _pct_change(190.0, 200.0) == pytest.approx(-0.05)


def test_pct_change_zero_baseline() -> None:
    assert _pct_change(100.0, 0.0) == pytest.approx(0.0)


def test_pct_change_none_current() -> None:
    assert _pct_change(None, 200.0) == pytest.approx(0.0)


def test_pct_change_none_baseline() -> None:
    assert _pct_change(200.0, None) == pytest.approx(0.0)
