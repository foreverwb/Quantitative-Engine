"""
engine/models/scores.py — Field Scores 数据模型

职责: 定义四个核心 Score 字段的 Pydantic 数据模型。
依赖: pydantic
被依赖: engine.steps.s04_field_calculator, engine.steps.s05_scenario_analyzer
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class FieldScores(BaseModel):
    """四个核心 Field Score 值"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gamma_score: float      # [0, 100]
    break_score: float      # [0, 100]
    direction_score: float  # [-100, 100]
    iv_score: float         # [0, 100]
