"""
engine/models/alerts.py — 告警数据模型

职责: 定义告警严重级别枚举和告警事件的 Pydantic 数据模型。
依赖: pydantic, datetime, enum
被依赖: engine.monitor.alert_engine, engine.api
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class AlertSeverity(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class AlertEvent(BaseModel):
    """单次告警事件"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    alert_id: str
    symbol: str
    timestamp: datetime
    tier: Literal[1, 2, 3]
    indicator: str
    severity: AlertSeverity
    old_value: float | str | None
    new_value: float | str
    threshold: float | str | None
    action: str | None  # "recalc_from_step_N" or None
