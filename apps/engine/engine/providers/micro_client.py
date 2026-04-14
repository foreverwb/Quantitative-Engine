"""
engine/providers/micro_client.py — Micro-Provider 编排客户端

职责: 编排 Micro-Provider 的接口调用 (ORATS strikes/monies/summary/ivrank +
      可选 hist_summary / extended strikes)，执行衍生计算 (GEX/DEX/Term/Skew/PCR)
      并定位 zero gamma / call & put walls，最终生成 MicroSnapshot。
依赖: asyncio, datetime, pandas,
      provider.orats (OratsProvider),
      provider.fields (GEX_FIELDS, DEX_FIELDS, IV_SURFACE_FIELDS),
      provider.models (StrikesFrame),
      compute.exposure.calculator (compute_gex, compute_dex),
      compute.volatility.term (TermBuilder),
      compute.volatility.skew (SkewBuilder),
      compute.flow.pcr (compute_pcr),
      engine.models.micro (MicroSnapshot),
      engine.steps.s03_pre_calculator (PreCalculatorOutput)
被依赖: engine.pipeline (Step 4 编排)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any

import pandas as pd

from compute.exposure.calculator import compute_dex, compute_gex
from compute.flow.pcr import compute_pcr
from compute.volatility.skew import SkewBuilder
from compute.volatility.term import TermBuilder
from provider.fields import DEX_FIELDS, GEX_FIELDS, IV_SURFACE_FIELDS
from provider.models import StrikesFrame
from provider.orats import OratsProvider

from engine.models.micro import MicroSnapshot
from engine.steps.s03_pre_calculator import PreCalculatorOutput

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量 (design-doc 第 6.1 节)
# ---------------------------------------------------------------------------

# scenario_seed 字面量 (与 s03_pre_calculator 保持一致)
SEED_EVENT = "event"
SEED_VOL_MEAN_REVERSION = "vol_mean_reversion"
SEED_TRANSITION = "transition"

# Hist summary 默认窗口 (用于 event seed 时回测 earnings implied move)
HIST_SUMMARY_LOOKBACK_DAYS = 400

# Transition 双桶 DTE 范围
TRANSITION_FRONT_BUCKET = "7,21"
TRANSITION_BACK_BUCKET = "30,60"

# 必备聚合列
_STRIKE_COL = "strike"
_EXPOSURE_COL = "exposure_value"


class MicroClientError(Exception):
    """MicroClient 编排失败"""


class MicroClient:
    """
    Micro-Provider 编排客户端 (design-doc §6.1)。

    通过依赖注入接受 OratsProvider，按 Phase 1/2/3 顺序执行:
      Phase 1: 并行拉取 strikes / monies / summary / ivrank
      Phase 2: 衍生计算 (GEX/DEX/Term/Skew/PCR/Walls/ZeroGamma)
      Phase 3: 按 scenario_seed 触发条件性扩展调用 (hist_summary / extended strikes)

    用法:
        client = MicroClient(orats_provider)
        snapshot = await client.fetch_micro_snapshot(symbol, pre_calc, scenario_seed)
    """

    def __init__(self, orats_provider: OratsProvider) -> None:
        self._provider = orats_provider

    async def fetch_micro_snapshot(
        self,
        symbol: str,
        pre_calc: PreCalculatorOutput,
        scenario_seed: str,
    ) -> MicroSnapshot:
        """
        编排所有 Micro-Provider 调用，生成 MicroSnapshot。

        Args:
            symbol: 标的代码
            pre_calc: Pre-Calculator 输出 (需 spot_price 和 dyn_dte_range)
            scenario_seed: Pre-Calculator 决定的场景种子

        Returns:
            MicroSnapshot: 包含原始数据帧 + 衍生计算结果
        """
        # ── Phase 1: 基础调用 (并行) ─────────────────────────────
        combined_fields = sorted(set(
            GEX_FIELDS + DEX_FIELDS + [
                "callValue", "putValue",          # ORATS SMV 理论价
                "callMidIv", "putMidIv",          # 市场中间 IV
                "smvVol",                         # SMV 拟合 IV
                "callBidPrice", "callAskPrice",   # bid/ask
                "putBidPrice", "putAskPrice",
                "theta", "vega",                  # 额外 Greeks
            ]
        ))

        strikes, monies, summary, ivrank = await asyncio.gather(
            self._provider.get_strikes(
                symbol,
                dte=pre_calc.dyn_dte_range,
                fields=combined_fields,
            ),
            self._provider.get_monies(symbol),
            self._provider.get_summary(symbol),
            self._provider.get_ivrank(symbol),
        )

        # ── Phase 2: 衍生计算 ────────────────────────────────────
        gex_frame = compute_gex(strikes)
        dex_frame = compute_dex(strikes)
        term = TermBuilder.build(monies, summary)
        skew = SkewBuilder.build(monies)
        vol_pcr, oi_pcr = compute_pcr(summary)

        zero_gamma = self._find_zero_gamma(gex_frame, pre_calc.spot_price)
        call_wall, put_wall = self._find_walls(gex_frame, pre_calc.spot_price)

        # ── Phase 3: 场景扩展调用 (条件执行) ─────────────────────
        strikes_extended, hist_summary = await self._fetch_scenario_extensions(
            symbol=symbol,
            scenario_seed=scenario_seed,
            pre_calc=pre_calc,
            combined_fields=combined_fields,
        )

        snapshot = MicroSnapshot(
            strikes_combined=strikes,
            monies=monies,
            summary=summary,
            ivrank=ivrank,
            strikes_extended=strikes_extended,
            hist_summary=hist_summary,
            gex_frame=gex_frame,
            dex_frame=dex_frame,
            term=term,
            skew=skew,
            zero_gamma_strike=zero_gamma,
            call_wall_strike=call_wall[0] if call_wall else None,
            call_wall_gex=call_wall[1] if call_wall else None,
            put_wall_strike=put_wall[0] if put_wall else None,
            put_wall_gex=put_wall[1] if put_wall else None,
            vol_pcr=vol_pcr,
            oi_pcr=oi_pcr,
        )

        logger.info(
            "MicroClient: symbol=%s seed=%s zero_gamma=%s call_wall=%s put_wall=%s",
            symbol,
            scenario_seed,
            zero_gamma,
            call_wall[0] if call_wall else None,
            put_wall[0] if put_wall else None,
        )

        return snapshot

    # ------------------------------------------------------------------
    # Phase 3: 场景扩展
    # ------------------------------------------------------------------

    async def _fetch_scenario_extensions(
        self,
        symbol: str,
        scenario_seed: str,
        pre_calc: PreCalculatorOutput,
        combined_fields: list[str],
    ) -> tuple[StrikesFrame | None, Any | None]:
        """
        按 scenario_seed 触发条件性扩展调用。

        - event              → 拉取 hist_summary (~400 天)
        - vol_mean_reversion → 拉取 IV_SURFACE_FIELDS 的 strikes_extended
        - transition         → 双桶分别拉取 (7,21) 和 (30,60), 合并为 extended
        - 其他               → 不做扩展调用
        """
        if scenario_seed == SEED_EVENT:
            today = date.today()
            hist_summary = await self._provider.get_hist_summary(
                symbol,
                start_date=(today - timedelta(days=HIST_SUMMARY_LOOKBACK_DAYS)).isoformat(),
                end_date=today.isoformat(),
            )
            return None, hist_summary

        if scenario_seed == SEED_VOL_MEAN_REVERSION:
            strikes_extended = await self._provider.get_strikes(
                symbol,
                dte=pre_calc.dyn_dte_range,
                fields=IV_SURFACE_FIELDS,
            )
            return strikes_extended, None

        if scenario_seed == SEED_TRANSITION:
            front, back = await asyncio.gather(
                self._provider.get_strikes(
                    symbol, dte=TRANSITION_FRONT_BUCKET, fields=combined_fields
                ),
                self._provider.get_strikes(
                    symbol, dte=TRANSITION_BACK_BUCKET, fields=combined_fields
                ),
            )
            merged_df = pd.concat([front.df, back.df], ignore_index=True)
            return StrikesFrame(df=merged_df), None

        return None, None

    # ------------------------------------------------------------------
    # Phase 2: Zero Gamma & Walls
    # ------------------------------------------------------------------

    def _find_zero_gamma(self, gex_frame: Any, spot: float) -> float | None:
        """
        定位 net GEX 由正转负 (或反之) 的 strike (design-doc §6.1)。

        步骤:
          1. 按 strike 聚合 exposure_value
          2. 找符号变化的位置
          3. 取最接近 spot 的翻转点
          4. 在该点附近做线性插值得到精确过零 strike
        """
        df = gex_frame.df
        if _STRIKE_COL not in df.columns or _EXPOSURE_COL not in df.columns:
            return None

        by_strike = df.groupby(_STRIKE_COL)[_EXPOSURE_COL].sum().sort_index()
        if by_strike.empty:
            return None

        signs = by_strike.apply(lambda x: 1 if x > 0 else -1)
        changes = signs.diff().abs()
        flip_strikes = changes[changes > 0].index.tolist()

        if not flip_strikes:
            return None

        closest = min(flip_strikes, key=lambda s: abs(s - spot))

        idx = by_strike.index.get_loc(closest)
        if idx > 0:
            s1 = by_strike.index[idx - 1]
            v1 = float(by_strike.iloc[idx - 1])
            s2 = float(closest)
            v2 = float(by_strike.loc[closest])
            if v1 != v2:
                return float(s1) + (0.0 - v1) * (s2 - float(s1)) / (v2 - v1)

        return float(closest)

    def _find_walls(
        self,
        gex_frame: Any,
        spot: float,
    ) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
        """
        定位 call wall 和 put wall (design-doc §6.1)。

        - call wall: spot 上方 GEX 最大的 strike
        - put wall : spot 下方 abs(GEX) 最大的 strike (取负值)

        Returns:
            (call_wall, put_wall) 每个为 (strike, gex_value) 或 None
        """
        df = gex_frame.df
        if _STRIKE_COL not in df.columns or _EXPOSURE_COL not in df.columns:
            return None, None

        by_strike = df.groupby(_STRIKE_COL)[_EXPOSURE_COL].sum()
        if by_strike.empty:
            return None, None

        above = by_strike[by_strike.index > spot]
        below = by_strike[by_strike.index < spot]

        call_wall: tuple[float, float] | None = None
        if not above.empty:
            call_strike = float(above.idxmax())
            call_wall = (call_strike, float(above.max()))

        put_wall: tuple[float, float] | None = None
        if not below.empty:
            abs_below = below.abs()
            put_strike = float(abs_below.idxmax())
            put_wall = (put_strike, float(below.loc[put_strike]))

        return call_wall, put_wall
