"""
engine/models/scenario.py — 场景分析结果数据模型

职责: 定义场景分析输出的 Pydantic 数据模型。
依赖: pydantic
被依赖: engine.steps.s05_scenario_analyzer, engine.steps.s06_strategy_calculator,
        engine.models.snapshots
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

ScenarioLabel = Literal[
    "trend", "range", "transition",
    "volatility_mean_reversion", "event_volatility",
]

AnalysisMethod = Literal["rule_engine", "llm_fallback"]


class ScenarioResult(BaseModel):
    """场景分析结果"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: ScenarioLabel              # 场景标签
    confidence: float                    # [0, 1]
    method: AnalysisMethod               # 使用的分析方法标识
    invalidate_conditions: list[str]     # 使此场景失效的条件列表
