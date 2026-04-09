"""
engine/models/scenario.py — 场景分析结果数据模型

职责: 定义场景分析输出的 Pydantic 数据模型。
依赖: pydantic
被依赖: engine.steps.s05_scenario_analyzer, engine.steps.s06_strategy_calculator,
        engine.models.snapshots
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ScenarioResult(BaseModel):
    """场景分析结果"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: str                        # e.g. "trend", "range", "transition", "event", "unknown"
    confidence: float                    # [0, 1]
    method: str                          # 使用的分析方法标识
    invalidate_conditions: list[str]     # 使此场景失效的条件列表
