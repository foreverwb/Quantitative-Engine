"""
engine/models/payoff.py — Payoff 结果模型 re-export

职责: 从 core.payoff_engine 导入并 re-export PayoffResult，供其他模块统一从
      engine.models 引用。
依赖: engine.core.payoff_engine
被依赖: engine.steps.s09_payoff, engine.api
"""

from __future__ import annotations

from engine.core.payoff_engine import PayoffResult

__all__ = ["PayoffResult"]
