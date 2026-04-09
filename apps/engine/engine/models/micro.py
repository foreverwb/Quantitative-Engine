"""
engine/models/micro.py — 微观市场数据快照模型

职责: 定义包含期权链、Greeks 暴露等衍生计算结果的 MicroSnapshot 数据模型。
依赖: pydantic
被依赖: engine.steps.s03_pre_calculator, engine.steps.s04_field_calculator
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class MicroSnapshot(BaseModel):
    """微观市场数据快照，包装 pandas 数据帧和标量派生结果"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    strikes_combined: Any           # StrikesFrame
    monies: Any                     # MoniesFrame
    summary: Any                    # SummaryRecord
    ivrank: Any                     # IVRankRecord
    strikes_extended: Any | None = None
    hist_summary: Any | None = None

    # 衍生计算结果
    gex_frame: Any                  # ExposureFrame
    dex_frame: Any                  # ExposureFrame
    term: Any                       # TermFrame
    skew: Any                       # SkewFrame

    zero_gamma_strike: float | None = None
    call_wall_strike: float | None = None
    call_wall_gex: float | None = None
    put_wall_strike: float | None = None
    put_wall_gex: float | None = None
    vol_pcr: float | None = None
    oi_pcr: float | None = None
