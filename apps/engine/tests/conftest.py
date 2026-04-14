"""
tests/conftest.py — 全局 pytest 配置

职责: 在收集阶段预先注入外部依赖的 stub 模块 (compute.*, provider.*, regime.*),
      使得 engine.pipeline 和 engine.providers.micro_client 可以被安全 import,
      即使这些外部包不在当前虚拟环境中。

注意: 此 conftest 仅在外部模块缺失时注入 stub；如果真实模块已安装则不干预。
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

# 需要 stub 的外部模块层级
_EXTERNAL_MODULES = [
    "compute",
    "compute.exposure",
    "compute.exposure.calculator",
    "compute.volatility",
    "compute.volatility.term",
    "compute.volatility.skew",
    "compute.volatility.smile",
    "compute.volatility.surface",
    "compute.flow",
    "compute.flow.pcr",
    "compute.flow.max_pain",
    "compute.flow.unusual",
    "compute.earnings",
    "compute.earnings.implied_move",
    "compute.earnings.iv_rank",
    "provider",
    "provider.orats",
    "provider.fields",
    "provider.models",
    "regime",
    "regime.boundary",
    "infra",
    "infra.cache",
    "infra.rate_limiter",
]


def _stub_module(name: str) -> ModuleType:
    mod = ModuleType(name)
    mod.__path__ = []  # type: ignore[attr-defined]
    return mod


def _ensure_stubs() -> None:
    """为所有缺失的外部模块注入 stub，使 import 不报错。"""
    for name in _EXTERNAL_MODULES:
        if name not in sys.modules:
            sys.modules[name] = _stub_module(name)

    # provider.fields 需要导出常量列表
    fields = sys.modules["provider.fields"]
    if not hasattr(fields, "GEX_FIELDS"):
        fields.GEX_FIELDS = [  # type: ignore[attr-defined]
            "callOpenInterest", "delta", "dte", "expirDate",
            "gamma", "putOpenInterest", "spotPrice", "strike", "tradeDate",
        ]
        fields.DEX_FIELDS = list(fields.GEX_FIELDS)  # type: ignore[attr-defined]
        fields.IV_SURFACE_FIELDS = [  # type: ignore[attr-defined]
            "expirDate", "dte", "strike", "callMidIv", "putMidIv",
            "smvVol", "delta", "spotPrice",
        ]

    # provider.orats 需要 OratsProvider 类
    orats = sys.modules["provider.orats"]
    if not hasattr(orats, "OratsProvider"):
        orats.OratsProvider = type("OratsProvider", (), {  # type: ignore[attr-defined]
            "__init__": lambda self, **kwargs: None,
        })

    # provider.models 需要 StrikesFrame
    models = sys.modules["provider.models"]
    if not hasattr(models, "StrikesFrame"):
        models.StrikesFrame = SimpleNamespace  # type: ignore[attr-defined]

    # compute.exposure.calculator 需要 compute_gex/compute_dex
    calc = sys.modules["compute.exposure.calculator"]
    if not hasattr(calc, "compute_gex"):
        calc.compute_gex = lambda *a, **kw: SimpleNamespace(df=SimpleNamespace())  # type: ignore[attr-defined]
        calc.compute_dex = lambda *a, **kw: SimpleNamespace(df=SimpleNamespace())  # type: ignore[attr-defined]

    # compute.volatility.term 需要 TermBuilder
    term = sys.modules["compute.volatility.term"]
    if not hasattr(term, "TermBuilder"):
        term.TermBuilder = SimpleNamespace(build=staticmethod(lambda *a, **kw: SimpleNamespace(df=SimpleNamespace())))  # type: ignore[attr-defined]

    # compute.volatility.skew 需要 SkewBuilder
    skew = sys.modules["compute.volatility.skew"]
    if not hasattr(skew, "SkewBuilder"):
        skew.SkewBuilder = SimpleNamespace(build=staticmethod(lambda *a, **kw: SimpleNamespace(df=SimpleNamespace())))  # type: ignore[attr-defined]

    # compute.flow.pcr 需要 compute_pcr
    pcr = sys.modules["compute.flow.pcr"]
    if not hasattr(pcr, "compute_pcr"):
        pcr.compute_pcr = lambda *a, **kw: (None, None)  # type: ignore[attr-defined]

    # regime.boundary 需要 MarketRegime, classify
    boundary = sys.modules["regime.boundary"]
    if not hasattr(boundary, "MarketRegime"):
        boundary.MarketRegime = lambda **kw: SimpleNamespace(**kw)  # type: ignore[attr-defined]
        boundary.classify = lambda mr: SimpleNamespace(value="NORMAL")  # type: ignore[attr-defined]


_ensure_stubs()
