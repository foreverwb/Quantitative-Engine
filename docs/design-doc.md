# Swing & Volatility Quantitative Analysis Engine — 详细设计方案

> **版本**: 1.0  
> **受众**: Claude Opus / Sonnet（代码生成模型）  
> **目标**: 基于本文档可完整搭建、运行、测试整个 Python 分析引擎（含监控后端 API）

---

## 目录

1. [系统全局概览](#1-系统全局概览)
2. [已有代码基础设施](#2-已有代码基础设施)
3. [新增模块总览与目录结构](#3-新增模块总览与目录结构)
4. [Step 1-2: 事件检测与 Regime Gating](#4-step-1-2-事件检测与-regime-gating)
5. [Step 3: Pre-Calculator 状态参数层](#5-step-3-pre-calculator-状态参数层)
6. [Step 4: Micro 结构数据获取与 Field Calculator](#6-step-4-micro-结构数据获取与-field-calculator)
7. [Step 5: 场景分析](#7-step-5-场景分析)
8. [Step 6: 策略计算引擎](#8-step-6-策略计算引擎)
9. [Step 7: 策略生成（风险偏好分级）](#9-step-7-策略生成风险偏好分级)
10. [Step 8: 策略对比与排序](#10-step-8-策略对比与排序)
11. [Step 9: SMV 曲面定价与 Payoff 引擎](#11-step-9-smv-曲面定价与-payoff-引擎)
12. [Step 10: 富途数据接入层](#12-step-10-富途数据接入层)
13. [Step 11: 三层快照数据架构](#13-step-11-三层快照数据架构)
14. [Step 12: 监控指标体系与告警引擎](#14-step-12-监控指标体系与告警引擎)
15. [Step 13: 后端 API 设计](#15-step-13-后端-api-设计)
16. [Step 14: API 数据输出规范](#16-step-14-api-数据输出规范)
17. [数据库 Schema](#17-数据库-schema)
18. [配置文件设计](#18-配置文件设计)
19. [测试策略](#19-测试策略)
20. [附录：公式与常量汇总](#20-附录公式与常量汇总)

---

## 1. 系统全局概览

### 1.1 流程总览

```
Symbol 输入
  │
  ├─ Step 1: Symbol Market Meso-level ──→ 已有 (MESO repo)
  │   输出: s_dir, s_vol, quadrant, signal_label, event_regime
  │
  ├─ Step 2: 事件检测 & Regime Gating
  │   输入: Meso 信号 + 事件日历
  │   输出: RegimeContext (regime_class, event_status, days_to_event)
  │
  ├─ Step 3: Pre-Calculator (状态参数层)
  │   输入: RegimeContext + Micro-Provider summary
  │   输出: dyn_window_pct, dyn_strike_band, dyn_dte_range, scenario_seed
  │
  ├─ Step 4: Micro 数据获取 + Field Calculator
  │   输入: Pre-Calculator 窗口参数
  │   输出: MicroSnapshot + GammaScore/BreakScore/DirectionScore/IVScore
  │
  ├─ Step 5: 场景分析
  │   输入: 四个 Score + RegimeContext
  │   输出: scenario, confidence, invalidate_conditions
  │
  ├─ Step 6: 策略计算引擎
  │   输入: scenario + MicroSnapshot + dyn_strike_band
  │   输出: 候选策略列表 (每个含 legs, greeks, payoff)
  │
  ├─ Step 7: 策略生成 (风险偏好分级)
  │   输入: 候选策略列表
  │   输出: 策略 + risk_profile 标签
  │
  ├─ Step 8: 策略对比与排序
  │   输入: 标签后策略 + 实时报价 (富途)
  │   输出: Top 3 策略 (含 TotalScore)
  │
  ├─ Step 9: 报告生成 + Payoff 可视化数据
  │   输入: 全部分析结果
  │   输出: AnalysisResultSnapshot (含 payoff 曲线数据点)
  │
  ├─ Step 10: 监控引擎
  │   输入: 基线快照 + 实时市场数据
  │   输出: MonitorStateSnapshot + AlertEvents
  │
  └─ API: Report-Monitor 数据服务
      REST 端点 + WebSocket 推送
```

### 1.2 技术栈

| 层 | 技术 |
|---|---|
| 分析引擎 | Python 3.11+, asyncio, pydantic v2 |
| 后端 API | FastAPI, SQLAlchemy 2.0, Alembic |
| 数据库 | SQLite (与 Meso 同库不同表) |
| 数据源-期权 | ORATS API (Micro-Provider repo) |
| 数据源-实时 | 富途 OpenAPI (futu-api) |
| 实时通信 | WebSocket (FastAPI websocket) |

### 1.3 仓库结构

本项目作为新目录 `apps/engine` 加入现有 monorepo：

```
.
├── apps/
│   ├── api/              # 已有 Meso API
│   ├── web/              # 已有 Meso Dashboard
│   ├── engine/           # 新增：分析引擎 + 监控后端
│   │   ├── engine/
│   │   │   ├── __init__.py
│   │   │   ├── main.py               # FastAPI 应用入口
│   │   │   ├── config/
│   │   │   │   ├── engine.yaml        # 引擎配置
│   │   │   │   ├── thresholds.yaml    # 监控阈值配置
│   │   │   │   └── strategies.yaml    # 策略族映射配置
│   │   │   ├── models/
│   │   │   │   ├── context.py         # RegimeContext, ScenarioSeed
│   │   │   │   ├── micro.py           # MicroSnapshot
│   │   │   │   ├── scores.py          # FieldScores (4个Score)
│   │   │   │   ├── scenario.py        # ScenarioResult
│   │   │   │   ├── strategy.py        # StrategyCandidate, StrategyLeg
│   │   │   │   ├── payoff.py          # PayoffCurve, PayoffPoint
│   │   │   │   ├── snapshots.py       # 三层快照数据模型
│   │   │   │   └── alerts.py          # AlertEvent, AlertSeverity
│   │   │   ├── steps/
│   │   │   │   ├── s02_regime_gating.py
│   │   │   │   ├── s03_pre_calculator.py
│   │   │   │   ├── s04_field_calculator.py
│   │   │   │   ├── s05_scenario_analyzer.py
│   │   │   │   ├── s06_strategy_calculator.py
│   │   │   │   ├── s07_risk_profiler.py
│   │   │   │   ├── s08_strategy_ranker.py
│   │   │   │   └── s09_report_builder.py
│   │   │   ├── core/
│   │   │   │   ├── pricing.py          # SMV 曲面感知定价引擎
│   │   │   │   ├── payoff_engine.py    # Payoff 曲线计算
│   │   │   │   ├── greeks.py           # 组合 Greeks 聚合
│   │   │   │   └── kelly.py            # Fractional Kelly 仓位
│   │   │   ├── providers/
│   │   │   │   ├── meso_client.py      # 调用 Meso API
│   │   │   │   ├── micro_client.py     # 调用 Micro-Provider
│   │   │   │   └── futu_client.py      # 富途 OpenAPI 封装
│   │   │   ├── monitor/
│   │   │   │   ├── snapshot_collector.py
│   │   │   │   ├── alert_engine.py
│   │   │   │   ├── incremental_recalc.py
│   │   │   │   └── websocket_hub.py
│   │   │   ├── api/
│   │   │   │   ├── routes_analysis.py
│   │   │   │   ├── routes_monitor.py
│   │   │   │   ├── routes_positions.py
│   │   │   │   └── websocket_handler.py
│   │   │   ├── db/
│   │   │   │   ├── models.py           # SQLAlchemy ORM
│   │   │   │   ├── session.py
│   │   │   │   └── migrations/
│   │   │   └── pipeline.py             # 编排 Step 2-9 的主流程
│   │   ├── tests/
│   │   │   ├── test_pricing.py
│   │   │   ├── test_payoff.py
│   │   │   ├── test_field_calculator.py
│   │   │   ├── test_scenario_analyzer.py
│   │   │   ├── test_strategy_calculator.py
│   │   │   ├── test_strategy_ranker.py
│   │   │   ├── test_alert_engine.py
│   │   │   └── fixtures/
│   │   └── pyproject.toml
├── compute/               # 已有 Micro-Provider compute
├── provider/              # 已有 Micro-Provider provider
├── regime/                # 已有 Micro-Provider regime
└── infra/                 # 已有 Micro-Provider infra
```

---

## 2. 已有代码基础设施

### 2.1 MESO Repo（Meso 层）

**消费方式**: 通过 REST API 调用，不直接 import 代码。

| API | 用途 |
|---|---|
| `GET /api/v1/signals/{symbol}?trade_date=YYYY-MM-DD` | 获取 symbol 的 Meso 信号 (s_dir, s_vol, quadrant, signal_label, event_regime, prob_tier) |
| `GET /api/v1/chart-points?trade_date=YYYY-MM-DD` | 获取某日所有 symbol 的信号散点 |
| `GET /api/v1/symbol-history/{symbol}?lookback_days=10` | 获取 symbol 多日历史信号 |

**关键数据字段**:
- `s_dir`: 方向得分 [-100, 100]
- `s_vol`: 波动得分 [-100, 100]
- `s_conf`: 置信度 [0, 100]
- `s_pers`: 持续性 [0, 100]
- `quadrant`: bullish_expansion / bullish_compression / bearish_expansion / bearish_compression / neutral
- `signal_label`: directional_bias / volatility_bias / neutral / trend_change
- `event_regime`: pre_earnings / post_earnings / neutral
- `prob_tier`: high / mid / low

### 2.2 Micro-Provider Repo

**消费方式**: 直接 import Python 模块。

**可用接口**:

| 模块 | 方法 | 输出 |
|---|---|---|
| `provider.orats.OratsProvider` | `get_strikes(ticker, dte, delta, fields)` | `StrikesFrame` |
| `provider.orats.OratsProvider` | `get_monies(ticker, fields)` | `MoniesFrame` |
| `provider.orats.OratsProvider` | `get_summary(ticker)` | `SummaryRecord` |
| `provider.orats.OratsProvider` | `get_ivrank(ticker)` | `IVRankRecord` |
| `provider.orats.OratsProvider` | `get_hist_summary(ticker, start, end)` | `HistSummaryFrame` |
| `compute.exposure.calculator` | `compute_gex(strikes_frame)` | `ExposureFrame` |
| `compute.exposure.calculator` | `compute_dex(strikes_frame)` | `ExposureFrame` |
| `compute.exposure.calculator` | `compute_vex(strikes_frame)` | `ExposureFrame` |
| `compute.volatility.term` | `TermBuilder.build(monies, summary)` | `TermFrame` |
| `compute.volatility.skew` | `SkewBuilder.build(monies, expiry)` | `SkewFrame` |
| `compute.volatility.smile` | `SmileBuilder.build(strikes, expiry)` | `SmileFrame` |
| `compute.volatility.surface` | `SurfaceBuilder.build(metric, data)` | `SurfaceFrame` |
| `compute.flow.max_pain` | `compute_max_pain(strikes_df, spot)` | `(strike, pain_curve)` |
| `compute.flow.pcr` | `compute_pcr(summary)` | `(vol_pcr, oi_pcr)` |
| `compute.flow.unusual` | `detect_unusual(strikes_df, thresholds)` | `DataFrame` |
| `compute.earnings.implied_move` | `compute_implied_move(strikes_df, spot)` | `float` |
| `compute.earnings.iv_rank` | `compute_iv_rank(current_iv, hist_series)` | `(ivr, ivp)` |
| `regime.boundary` | `classify(MarketRegime)` | `RegimeClass` |
| `regime.boundary` | `compute_derived_boundaries(regime, spot, step)` | `DerivedBoundaries` |
| `infra.cache` | `CacheManager` | L1 缓存 |
| `infra.rate_limiter` | `TokenBucket` | 限流 |

**StrikesFrame 字段裁剪常量** (在 `provider.fields` 中已定义):
- `GEX_FIELDS`: tradeDate, expirDate, dte, strike, gamma, callOpenInterest, putOpenInterest, spotPrice
- `DEX_FIELDS`: 同上但 gamma→delta
- `VEX_FIELDS`: 同上但 gamma→vega
- `IV_SURFACE_FIELDS`: expirDate, dte, strike, callMidIv, putMidIv, smvVol, delta, spotPrice

---

## 3. 新增模块总览与目录结构

见 1.3 节。以下各 Step 章节详细定义每个模块的输入、输出、算法和接口。

---

## 4. Step 1-2: 事件检测与 Regime Gating

### 4.1 文件: `engine/models/context.py`

```python
# 数据模型定义

class EventInfo(BaseModel):
    """事件信息"""
    event_type: Literal["earnings", "fomc", "cpi", "none"]
    event_date: date | None
    days_to_event: int | None  # 正=未来, 负=已过, None=无事件

class RegimeContext(BaseModel):
    """Regime 上下文，贯穿整个分析流程"""
    symbol: str
    trade_date: date
    regime_class: Literal["LOW_VOL", "NORMAL", "STRESS"]
    event: EventInfo
    meso_signal: MesoSignal | None  # 来自 Meso API 的信号

class MesoSignal(BaseModel):
    """Meso 层信号"""
    s_dir: float          # [-100, 100]
    s_vol: float          # [-100, 100]
    s_conf: float         # [0, 100]
    s_pers: float         # [0, 100]
    quadrant: str
    signal_label: str
    event_regime: str
    prob_tier: str
```

### 4.2 文件: `engine/steps/s02_regime_gating.py`

**职责**: 构建 RegimeContext，决定是否继续分析。

**输入**:
- `symbol: str`
- `trade_date: date`

**处理逻辑**:

1. 调用 Meso API: `GET /api/v1/signals/{symbol}?trade_date={trade_date}` → 得到 MesoSignal
2. 调用 Micro-Provider: `OratsProvider.get_summary(symbol)` → 得到 SummaryRecord
3. 调用 Micro-Provider: `OratsProvider.get_ivrank(symbol)` → 得到 IVRankRecord
4. 解析事件信息:
   - 从 SummaryRecord 中无法直接获取事件日期，需从 Meso 的 `event_regime` 推断
   - 或从独立的事件日历 JSON 文件读取 (`engine/config/event_calendar.json`)
5. 构建 MarketRegime 对象:
   ```python
   market_regime = MarketRegime(
       iv30d=summary.atmIvM1 or 0.0,  # 使用 M1 ATM IV 作为 iv30d
       contango=(summary.atmIvM2 or 0) - (summary.atmIvM1 or 0),  # M2-M1
       vrp=(summary.atmIvM1 or 0) - (summary.orFcst20d or 0),     # IV - RV forecast
       iv_rank=ivrank.iv_rank,
       iv_pctl=ivrank.iv_pctl,
       vol_of_vol=summary.volOfVol or 0.05,
   )
   ```
6. 调用 `regime.boundary.classify(market_regime)` → `RegimeClass`
7. 门控规则:
   - 若 `RegimeClass == STRESS` 且 `days_to_event <= 1` → 返回 `gate_result = "skip"`, 仅输出监控快照
   - 否则 → 返回 `gate_result = "proceed"`

**输出**: `RegimeContext`

**事件日历文件格式** (`engine/config/event_calendar.json`):
```json
{
  "macro_events": [
    {"type": "fomc", "date": "2026-05-07"},
    {"type": "cpi", "date": "2026-05-13"}
  ]
}
```
财报日期从 Meso 的 `event_regime` 字段推断：若 `event_regime == "pre_earnings"` 则 `event_type = "earnings"`。

---

## 5. Step 3: Pre-Calculator 状态参数层

### 5.1 文件: `engine/steps/s03_pre_calculator.py`

**输入**: `RegimeContext`, `SummaryRecord`, `HistSummaryFrame`(可选)

**处理逻辑**:

```python
class PreCalculatorOutput(BaseModel):
    dyn_window_pct: float       # 动态窗口百分比
    dyn_strike_band: tuple[float, float]  # (lower, upper) strike 边界
    dyn_dte_range: str          # 传给 get_strikes 的 dte 参数 "min,max"
    dyn_dte_ranges: list[str]   # 多桶场景下的多个 dte range
    scenario_seed: str          # "trend" / "range" / "transition" / "event" / "unknown"
    spot_price: float
```

**Step 3.1: 计算 dyn_window_pct**

```python
# ATR20_pct: 从 HistSummaryFrame 计算
# 若无历史数据，用 summary.atmIvM1 * sqrt(20/365) 近似
if hist_summary is not None:
    # 用 priorCls 序列计算 20 日 ATR 百分比
    prices = hist_summary.df["priorCls"].dropna().tail(21)
    if len(prices) >= 2:
        daily_ranges = prices.diff().abs().dropna()
        atr20 = daily_ranges.tail(20).mean()
        atr20_pct = atr20 / spot_price
    else:
        atr20_pct = summary.atmIvM1 * (20/365)**0.5 if summary.atmIvM1 else 0.05
else:
    atr20_pct = summary.atmIvM1 * (20/365)**0.5 if summary.atmIvM1 else 0.05

# expected_move_pct: 从 implied move 计算（需要 strikes 数据，此处用 ATM IV 近似）
expected_move_pct = (summary.atmIvM1 or 0.20) * (30/365)**0.5

# earnings_hist_move_pct: 仅 earnings 场景使用
# 从历史数据回测（此处简化为 expected_move 的 1.5 倍）
earnings_hist_move_pct = expected_move_pct * 1.5 if event.event_type == "earnings" else 0

dyn_window_pct = max(
    1.25 * expected_move_pct,
    atr20_pct,
    earnings_hist_move_pct
)
# 硬边界: 最小 3%, 最大 20%
dyn_window_pct = max(0.03, min(0.20, dyn_window_pct))
```

**Step 3.2: 计算 dyn_strike_band**

```python
lower = spot_price * (1 - dyn_window_pct)
upper = spot_price * (1 + dyn_window_pct)
dyn_strike_band = (round(lower, 2), round(upper, 2))
```

**Step 3.3: 计算 dyn_dte_range 和 scenario_seed**

```python
event_type = context.event.event_type
days_to_event = context.event.days_to_event

if event_type == "earnings" and days_to_event is not None and 0 <= days_to_event <= 14:
    scenario_seed = "event"
    # 双桶: 覆盖事件的 front expiry + 事件后的 next expiry
    dyn_dte_ranges = [f"0,{days_to_event + 7}", f"{days_to_event + 1},60"]
    dyn_dte_range = f"0,60"

elif context.regime_class == "STRESS":
    scenario_seed = "transition"
    dyn_dte_ranges = ["7,21", "30,60"]
    dyn_dte_range = "7,60"

elif abs(context.meso_signal.s_dir or 0) > 50 and abs(context.meso_signal.s_vol or 0) < 30:
    scenario_seed = "trend"
    dyn_dte_ranges = ["14,45"]
    dyn_dte_range = "14,45"

elif abs(context.meso_signal.s_vol or 0) > 50 and abs(context.meso_signal.s_dir or 0) < 30:
    scenario_seed = "vol_mean_reversion"
    dyn_dte_ranges = ["14,45"]
    dyn_dte_range = "14,45"

else:
    scenario_seed = "unknown"
    dyn_dte_ranges = ["7,45"]
    dyn_dte_range = "7,45"
```

---

## 6. Step 4: Micro 结构数据获取与 Field Calculator

### 6.1 文件: `engine/providers/micro_client.py`

**职责**: 编排 Micro-Provider 的接口调用，生成 MicroSnapshot。

**接口调用编排**:

```python
class MicroClient:
    def __init__(self, orats_provider: OratsProvider):
        self._provider = orats_provider

    async def fetch_micro_snapshot(
        self,
        symbol: str,
        pre_calc: PreCalculatorOutput,
        scenario_seed: str,
    ) -> MicroSnapshot:

        # Phase 1: 基础调用 (并行)
        combined_fields = sorted(set(GEX_FIELDS + DEX_FIELDS))
        # = ["callOpenInterest", "delta", "dte", "expirDate", "gamma",
        #    "putOpenInterest", "spotPrice", "strike", "tradeDate"]

        strikes_task = self._provider.get_strikes(
            symbol,
            dte=pre_calc.dyn_dte_range,
            fields=combined_fields,
        )
        monies_task = self._provider.get_monies(symbol)
        summary_task = self._provider.get_summary(symbol)
        ivrank_task = self._provider.get_ivrank(symbol)

        strikes, monies, summary, ivrank = await asyncio.gather(
            strikes_task, monies_task, summary_task, ivrank_task
        )

        # Phase 2: 计算衍生数据
        gex_frame = compute_gex(strikes)
        dex_frame = compute_dex(strikes)
        term = TermBuilder.build(monies, summary)
        skew = SkewBuilder.build(monies)
        vol_pcr, oi_pcr = compute_pcr(summary)

        zero_gamma = self._find_zero_gamma(gex_frame, pre_calc.spot_price)
        call_wall, put_wall = self._find_walls(gex_frame, pre_calc.spot_price)

        # Phase 3: 场景扩展调用 (条件执行)
        strikes_extended = None
        hist_summary = None

        if scenario_seed == "event":
            hist_summary = await self._provider.get_hist_summary(
                symbol,
                start_date=(date.today() - timedelta(days=400)).isoformat(),
                end_date=date.today().isoformat(),
            )
        elif scenario_seed == "vol_mean_reversion":
            strikes_extended = await self._provider.get_strikes(
                symbol,
                dte=pre_calc.dyn_dte_range,
                fields=IV_SURFACE_FIELDS,
            )
        elif scenario_seed == "transition":
            # 双桶分别拉取
            s1 = await self._provider.get_strikes(symbol, dte="7,21", fields=combined_fields)
            s2 = await self._provider.get_strikes(symbol, dte="30,60", fields=combined_fields)
            # 合并为 extended
            strikes_extended = StrikesFrame(df=pd.concat([s1.df, s2.df], ignore_index=True))

        return MicroSnapshot(
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
```

**Zero Gamma 计算**:
```python
def _find_zero_gamma(self, gex_frame: ExposureFrame, spot: float) -> float | None:
    """找到 GEX 由正转负的 strike（net gamma exposure 过零点）"""
    df = gex_frame.df.copy()
    if "strike" not in df.columns or "exposure_value" not in df.columns:
        return None

    by_strike = df.groupby("strike")["exposure_value"].sum().sort_index()
    # 找符号变化的位置
    signs = by_strike.apply(lambda x: 1 if x > 0 else -1)
    changes = signs.diff().abs()
    flip_strikes = changes[changes > 0].index.tolist()

    if not flip_strikes:
        return None

    # 取最接近 spot 的翻转点
    closest = min(flip_strikes, key=lambda s: abs(s - spot))

    # 线性插值精确过零点
    idx = by_strike.index.get_loc(closest)
    if idx > 0:
        s1, v1 = by_strike.index[idx-1], by_strike.iloc[idx-1]
        s2, v2 = closest, by_strike.loc[closest]
        if v1 != v2:
            return s1 + (0 - v1) * (s2 - s1) / (v2 - v1)
    return closest
```

**Wall 计算**:
```python
def _find_walls(self, gex_frame, spot):
    """找 call wall (spot 上方 GEX 最大 strike) 和 put wall (spot 下方)"""
    df = gex_frame.df.copy()
    by_strike = df.groupby("strike")["exposure_value"].sum()

    above = by_strike[by_strike.index > spot]
    below = by_strike[by_strike.index < spot]

    call_wall = (above.idxmax(), above.max()) if not above.empty else None
    put_wall = (below.idxmin(), below.min()) if not below.empty else None
    # put wall 取绝对值最大的负值
    if not below.empty:
        abs_below = below.abs()
        put_wall = (abs_below.idxmax(), below.loc[abs_below.idxmax()])

    return call_wall, put_wall
```

### 6.2 文件: `engine/models/micro.py`

```python
class MicroSnapshot(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    strikes_combined: Any  # StrikesFrame
    monies: Any            # MoniesFrame
    summary: Any           # SummaryRecord
    ivrank: Any            # IVRankRecord
    strikes_extended: Any | None = None
    hist_summary: Any | None = None

    # 衍生计算结果
    gex_frame: Any         # ExposureFrame
    dex_frame: Any         # ExposureFrame
    term: Any              # TermFrame
    skew: Any              # SkewFrame

    zero_gamma_strike: float | None = None
    call_wall_strike: float | None = None
    call_wall_gex: float | None = None
    put_wall_strike: float | None = None
    put_wall_gex: float | None = None
    vol_pcr: float | None = None
    oi_pcr: float | None = None
```

### 6.3 文件: `engine/steps/s04_field_calculator.py`

**四个 Score 的计算规则**:

#### GammaScore [0, 100]

| 子指标 | 权重 | 计算 |
|---|---|---|
| `net_gexn_normalized` | 0.30 | `abs(sum(gex_frame.exposure_value))` → min-max 归一化到 [0,100] |
| `wall_concentration` | 0.25 | top-3 strike 的 abs(GEX) 占 total abs(GEX) 的比例 × 100 |
| `zero_gamma_distance` | 0.25 | `abs(spot - zero_gamma_strike) / spot × 100`, clip [0, 100] |
| `month_consistency` | 0.20 | 前后两个 expiry 的 zero_gamma 之差 / spot × 100, 取反(越小越好) |

```python
gamma_score = (
    net_gexn_normalized * 0.30
    + wall_concentration * 0.25
    + (100 - zero_gamma_distance * 10) * 0.25  # 距离越远得分越高
    + month_consistency_score * 0.20
)
gamma_score = clip(gamma_score, 0, 100)
```

#### BreakScore [0, 100]

| 子指标 | 权重 | 计算 |
|---|---|---|
| `wall_distance` | 0.35 | `min(abs(spot - call_wall), abs(spot - put_wall)) / spot × 100` |
| `implied_vs_actual` | 0.30 | `implied_move_pct / atr20_pct`, clip [0, 3], 归一化到 [0, 100] |
| `zero_gamma_flip_risk` | 0.35 | `(1 - zero_gamma_distance / dyn_window_pct) × 100`, clip [0, 100] |

#### DirectionScore [-100, 100]

| 子指标 | 权重 | 计算 |
|---|---|---|
| `meso_direction` | 0.25 | 直接使用 `meso_signal.s_dir` |
| `dex_slope` | 0.25 | 对 DEX by strike 做线性回归，取 `sign(slope) × min(abs(slope)/max_slope × 100, 100)` |
| `vanna_indicator` | 0.25 | 从 MoniesFrame slope 字段推断: `slope > 0 → bullish (+), slope < 0 → bearish (-)`, 归一化 |
| `price_trend_confirm` | 0.25 | `(spot - sma20) / spot × 100 × sign(meso_direction)`, 同向得正分 |

```python
direction_score = (
    meso_direction * 0.25
    + dex_slope_score * 0.25
    + vanna_score * 0.25
    + price_trend_score * 0.25
)
direction_score = clip(direction_score, -100, 100)
```

#### IVScore [0, 100]

| 子指标 | 权重 | 计算 |
|---|---|---|
| `iv_consensus` | 0.25 | `0.4 × iv_rank + 0.6 × iv_pctl` |
| `iv_rv_spread` | 0.20 | `(atmIvM1 - hv20) / hv20 × 100`, clip [-100, 100], 归一化到 [0, 100] |
| `term_kink` | 0.15 | 对 TermFrame 的 (dte, atmiv) 做二次拟合，取 abs(二阶系数) × 1000 |
| `skew_25d` | 0.20 | `vol25put - vol25call` 从 MoniesFrame: `vol25 - vol75` (vol25=25-delta put, vol75=25-delta call) |
| `event_premium` | 0.20 | `(front_atmiv / back_atmiv - 1) × 100` if 有事件, else 0 |

**输出**: `FieldScores`

```python
class FieldScores(BaseModel):
    gamma_score: float    # [0, 100]
    break_score: float    # [0, 100]
    direction_score: float # [-100, 100]
    iv_score: float       # [0, 100]
```

---

## 7. Step 5: 场景分析

### 7.1 文件: `engine/steps/s05_scenario_analyzer.py`

**规则引擎优先 (~85% 情况)**:

```python
class ScenarioResult(BaseModel):
    scenario: Literal["trend", "range", "transition",
                       "volatility_mean_reversion", "event_volatility"]
    confidence: float  # [0, 1]
    method: Literal["rule_engine", "llm_fallback"]
    invalidate_conditions: list[str]


def analyze_scenario(
    scores: FieldScores,
    context: RegimeContext,
    micro: MicroSnapshot,
) -> ScenarioResult:

    candidates = []

    # Rule 1: Trend
    if (abs(scores.direction_score) > 60
        and micro.zero_gamma_strike is not None
        and abs(micro.summary.spotPrice - micro.zero_gamma_strike)
            / micro.summary.spotPrice > 0.03):
        # 检查 DEX 同向
        net_dex = micro.dex_frame.df["exposure_value"].sum()
        dex_aligned = (net_dex > 0) == (scores.direction_score > 0)
        if dex_aligned:
            candidates.append(ScenarioResult(
                scenario="trend",
                confidence=0.85,
                method="rule_engine",
                invalidate_conditions=[
                    f"direction_score 跌破 ±40",
                    f"zero_gamma_distance 缩至 < 1.5%",
                    f"DEX 方向翻转",
                ],
            ))

    # Rule 2: Range
    if (micro.gex_frame.df["exposure_value"].sum() > 0  # 正 gamma 环境
        and micro.call_wall_strike and micro.put_wall_strike
        and context.event.event_type == "none"):
        wall_width = (micro.call_wall_strike - micro.put_wall_strike) / micro.summary.spotPrice
        if wall_width < 0.08:  # 双侧 wall 紧密
            candidates.append(ScenarioResult(
                scenario="range",
                confidence=0.80,
                method="rule_engine",
                invalidate_conditions=[
                    "net_gex 翻负",
                    "spot 突破 call_wall 或跌破 put_wall",
                    "事件进入 T-3 窗口",
                ],
            ))

    # Rule 3: Transition
    if (micro.zero_gamma_strike is not None
        and abs(micro.summary.spotPrice - micro.zero_gamma_strike)
            / micro.summary.spotPrice < 0.015):
        candidates.append(ScenarioResult(
            scenario="transition",
            confidence=0.70,
            method="rule_engine",
            invalidate_conditions=[
                "zero_gamma_distance 恢复至 > 3%",
                "direction_score 与 s_vol 方向达成一致",
            ],
        ))

    # 方向与波动信号冲突也归入 transition
    if (context.meso_signal and
        (context.meso_signal.s_dir > 0) != (context.meso_signal.s_vol > 0)
        and abs(context.meso_signal.s_dir) > 30
        and abs(context.meso_signal.s_vol) > 30):
        candidates.append(ScenarioResult(
            scenario="transition",
            confidence=0.65,
            method="rule_engine",
            invalidate_conditions=["方向/波动信号冲突解除"],
        ))

    # Rule 4: Volatility Mean Reversion
    if (scores.iv_score > 75
        and context.event.event_type == "none"):
        candidates.append(ScenarioResult(
            scenario="volatility_mean_reversion",
            confidence=0.75,
            method="rule_engine",
            invalidate_conditions=[
                "iv_score 跌破 60",
                "事件进入窗口",
                "term_kink 大幅增加",
            ],
        ))

    # Rule 5: Event Volatility
    if (context.event.event_type != "none"
        and context.event.days_to_event is not None
        and 0 <= context.event.days_to_event <= 14):
        front_iv = micro.term.df["atmiv"].iloc[0] if not micro.term.df.empty else None
        back_iv = micro.term.df["atmiv"].iloc[-1] if len(micro.term.df) > 1 else None
        if front_iv and back_iv and front_iv / back_iv > 1.15:
            candidates.append(ScenarioResult(
                scenario="event_volatility",
                confidence=0.80,
                method="rule_engine",
                invalidate_conditions=[
                    "front/back IV 比率 < 1.10",
                    "事件已过",
                ],
            ))

    # 选择最高 confidence 的候选
    if candidates:
        return max(candidates, key=lambda c: c.confidence)

    # 无规则匹配 → LLM fallback（或默认 range）
    return ScenarioResult(
        scenario="range",
        confidence=0.50,
        method="rule_engine",
        invalidate_conditions=["任意方向信号强化至 > 50"],
    )
```

---

## 8. Step 6: 策略计算引擎

### 8.1 文件: `engine/steps/s06_strategy_calculator.py`

**策略族映射** (配置文件 `engine/config/strategies.yaml`):

```yaml
strategy_mapping:
  trend:
    bullish:
      - type: bull_call_spread
        description: "Bull Call Spread"
      - type: long_call
        description: "Long Call (进取)"
      - type: short_put_spread
        description: "Bull Put Spread"
    bearish:
      - type: bear_put_spread
        description: "Bear Put Spread"
      - type: long_put
        description: "Long Put (进取)"
      - type: short_call_spread
        description: "Bear Call Spread"

  range:
    - type: iron_condor
      description: "Iron Condor"
    - type: iron_butterfly
      description: "Iron Butterfly"
    - type: short_strangle
      description: "Short Strangle (进取)"

  transition:
    - type: iron_condor_skewed
      description: "Skewed Iron Condor"
    - type: calendar_spread
      description: "Calendar Spread"

  volatility_mean_reversion:
    - type: short_straddle
      description: "Short Straddle"
    - type: iron_butterfly
      description: "Iron Butterfly"
    - type: ratio_spread
      description: "Ratio Spread"

  event_volatility:
    pre_event:
      - type: long_straddle
        description: "Long Straddle"
      - type: long_strangle
        description: "Long Strangle"
    post_event:
      - type: short_straddle_protected
        description: "Short Straddle + Wings"
```

### 8.2 策略数据模型

```python
class StrategyLeg(BaseModel):
    side: Literal["buy", "sell"]
    option_type: Literal["call", "put"]
    strike: float
    expiry: date
    qty: int = 1
    premium: float         # ORATS SMV 理论价 (callValue/putValue)
    iv: float              # ORATS smvVol (SMV 拟合 IV)
    delta: float           # ORATS delta (SMV Greeks)
    gamma: float           # ORATS gamma
    theta: float           # ORATS theta
    vega: float            # ORATS vega
    oi: int
    bid: float | None = None   # 富途实时 bid
    ask: float | None = None   # 富途实时 ask

class StrategyCandidate(BaseModel):
    strategy_type: str
    description: str
    legs: list[StrategyLeg]
    net_credit_debit: float    # 正=credit, 负=debit
    max_profit: float
    max_loss: float
    breakevens: list[float]
    pop: float                 # Probability of Profit [0, 1]
    ev: float                  # Expected Value
    greeks_composite: GreeksComposite
    risk_profile: str | None = None  # Step 7 填充
    total_score: float | None = None # Step 8 填充
```

### 8.3 Strike 选择逻辑

对每种策略类型，定义 strike 选择规则：

```python
# Bull Call Spread:
# - buy_strike: ATM 或略 ITM (delta ≈ 0.55-0.60)
# - sell_strike: OTM (delta ≈ 0.30-0.35)
# 约束: 两个 strike 都在 dyn_strike_band 内

# Iron Condor:
# - sell_put_strike: 接近 put_wall (delta ≈ -0.15 to -0.25)
# - buy_put_strike: sell_put - 1~2 个 strike step
# - sell_call_strike: 接近 call_wall (delta ≈ 0.15 to 0.25)
# - buy_call_strike: sell_call + 1~2 个 strike step

# 通用约束:
# - 每个 strike 的 OI >= 500
# - 每个 strike 的 bid-ask spread < 15% of mid
# - strike 在 dyn_strike_band 范围内
```

**从 StrikesFrame 选择具体 strike 的方法**:

**重要: 策略构建需要扩展字段集**。在 `provider/fields.py` 中新增:

```python
STRATEGY_FIELDS = sorted(set(
    GEX_FIELDS + DEX_FIELDS + [
        "callValue", "putValue",          # ORATS SMV 理论价
        "callMidIv", "putMidIv",          # 市场中间 IV
        "smvVol",                         # SMV 拟合 IV
        "callBidPrice", "callAskPrice",   # bid/ask
        "putBidPrice", "putAskPrice",
        "theta", "vega",                  # 额外 Greeks
    ]
))
```

**数据源规则**:
- `premium` → 从 `callValue` 或 `putValue` 读取（ORATS SMV 理论价，非 bid/ask mid）
- `iv` → 从 `smvVol` 读取（SMV 拟合 IV，非 callMidIv/putMidIv）
- `delta/gamma/theta/vega` → 直接从 StrikesFrame 对应列读取（已是 SMV Greeks）
- **不自行用 BS 公式计算**任何 premium 或 Greeks

```python
def select_strike_by_delta(
    strikes_df: pd.DataFrame,
    target_delta: float,
    option_type: str,
    expiry: str,
    min_oi: int = 500,
) -> pd.Series | None:
    """从期权链中选择最接近目标 delta 的 strike"""
    mask = strikes_df["expirDate"] == expiry
    filtered = strikes_df[mask].copy()

    if option_type == "call":
        filtered["delta_dist"] = (filtered["delta"] - target_delta).abs()
    else:
        # put delta = call_delta - 1
        filtered["put_delta"] = filtered["delta"] - 1
        filtered["delta_dist"] = (filtered["put_delta"] - target_delta).abs()

    # OI 过滤
    oi_col = "callOpenInterest" if option_type == "call" else "putOpenInterest"
    filtered = filtered[filtered[oi_col] >= min_oi]

    if filtered.empty:
        return None

    return filtered.loc[filtered["delta_dist"].idxmin()]
```

---

## 9. Step 7: 策略生成（风险偏好分级）

### 9.1 文件: `engine/steps/s07_risk_profiler.py`

```python
def assign_risk_profile(strategy: StrategyCandidate) -> list[str]:
    """为策略分配适用的风险偏好标签"""
    profiles = []

    is_defined_risk = strategy.max_loss < float("inf") and strategy.max_loss > 0
    has_naked_leg = any(
        leg.side == "sell" and not _has_protective_leg(leg, strategy.legs)
        for leg in strategy.legs
    )
    short_gamma_ratio = sum(
        abs(leg.gamma) for leg in strategy.legs if leg.side == "sell"
    ) / max(sum(abs(leg.gamma) for leg in strategy.legs), 1e-9)

    abs_delta = abs(strategy.greeks_composite.delta)

    # 进取
    if not has_naked_leg:  # 禁止裸 short
        profiles.append("aggressive")

    # 均衡
    if is_defined_risk:
        profiles.append("balanced")

    # 保守
    if (is_defined_risk
        and abs_delta < 0.20
        and short_gamma_ratio < 0.30):
        profiles.append("conservative")

    return profiles if profiles else ["balanced"]  # 默认均衡
```

---

## 10. Step 8: 策略对比与排序

### 10.1 文件: `engine/steps/s08_strategy_ranker.py`

**硬过滤（任一不满足直接排除）**:

```python
HARD_FILTERS = {
    "min_oi": 500,              # 每条 leg 的 min OI
    "max_spread_pct": 0.15,     # bid-ask spread / mid < 15%
    "max_loss_limit": 50000,    # max_loss 绝对值上限
}

def hard_filter(strategy: StrategyCandidate) -> tuple[bool, str]:
    for leg in strategy.legs:
        if leg.oi < HARD_FILTERS["min_oi"]:
            return False, f"OI too low: {leg.strike} {leg.option_type} OI={leg.oi}"
        if leg.bid and leg.ask:
            mid = (leg.bid + leg.ask) / 2
            if mid > 0 and (leg.ask - leg.bid) / mid > HARD_FILTERS["max_spread_pct"]:
                return False, f"Spread too wide: {leg.strike}"
    if abs(strategy.max_loss) > HARD_FILTERS["max_loss_limit"]:
        return False, f"Max loss {strategy.max_loss} exceeds limit"
    return True, ""
```

**软评分公式**:

```python
def compute_total_score(
    strategy: StrategyCandidate,
    scenario: ScenarioResult,
    micro: MicroSnapshot,
) -> float:
    # 场景匹配度 (0-100)
    scenario_match = 100 if strategy.strategy_type in SCENARIO_STRATEGY_MAP[scenario.scenario] else 50

    # 滑点调整后 EV
    estimated_slippage_per_leg = 0.05  # $0.05 per contract
    adjusted_ev = strategy.ev - len(strategy.legs) * estimated_slippage_per_leg * 100
    ev_score = clip(adjusted_ev / max(abs(strategy.max_loss), 1) * 100, 0, 100)

    # Tail Risk Score
    tail_risk = (1 - abs(strategy.max_loss) / max(strategy.max_profit, 1)) * 100
    tail_risk = clip(tail_risk, 0, 100)

    # 流动性
    min_leg_oi = min(leg.oi for leg in strategy.legs)
    liquidity = clip(min_leg_oi / 5000 * 100, 0, 100)

    # Theta 效率
    theta_eff = abs(strategy.greeks_composite.theta) / max(abs(strategy.max_loss), 1) * 10000
    theta_eff = clip(theta_eff, 0, 100)

    # 资本效率
    capital_eff = strategy.ev / max(abs(strategy.max_loss), 1) * 100
    capital_eff = clip(capital_eff, 0, 100)

    total = (
        scenario_match * 0.20
        + ev_score * 0.25
        + tail_risk * 0.15
        + liquidity * 0.15
        + theta_eff * 0.10
        + capital_eff * 0.15
    )
    return round(total, 2)
```

---

## 11. Step 9: SMV 曲面定价与 Payoff 引擎

**设计原则**: ORATS 已通过 SMV（Smooth Market Volatility）处理产出了无套利 IV 曲面（vol0–vol100 × 多个 expiry）。本引擎直接消费该曲面进行定价，不重复校准。单 leg 的 Greeks 和理论价全部使用 ORATS API 返回值，不自行计算。BS 封闭解公式仅作为曲面插值后的底层定价工具。

### 11.1 文件: `engine/core/pricing.py`

```python
"""
engine/core/pricing.py — SMV 曲面感知定价引擎

职责:
  1. 从 ORATS MoniesFrame 构建 IV 插值曲面 (SMVSurface)
  2. 对任意 (strike, dte) 查询曲面得到 σ(K,T)
  3. 用 BS 封闭解 + 查到的 σ 计算理论价（情景投射）
  4. 用有限差分计算曲面感知 Greeks（仅 Slider/假设场景使用）

不做:
  - 不做单 leg 真实定价（使用 ORATS callValue/putValue）
  - 不做单 leg 真实 Greeks（使用 ORATS delta/gamma/theta/vega）
  - 不做 Local Vol PDE 求解或 SABR 参数校准

依赖: scipy.interpolate, scipy.stats
被依赖: engine.core.payoff_engine, engine.api.routes_analysis (Slider 重算)
"""

import math
from scipy.stats import norm
from scipy.interpolate import RectBivariateSpline


def bs_formula(
    S: float,       # spot
    K: float,       # strike
    T: float,       # time to expiry (years)
    r: float,       # risk-free rate
    sigma: float,   # IV (来自曲面查询，非常数)
    option_type: str,  # "call" or "put"
) -> float:
    """BS 封闭解公式。sigma 参数由 SMVSurface.get_iv() 提供，逐 strike 不同。"""
    if T <= 0:
        if option_type == "call":
            return max(0.0, S - K)
        return max(0.0, K - S)
    if sigma <= 0:
        if option_type == "call":
            return max(0.0, S - K * math.exp(-r * T))
        return max(0.0, K * math.exp(-r * T) - S)

    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if option_type == "call":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


class SMVSurface:
    """
    从 ORATS MoniesFrame 构建可插值的 IV 曲面。

    MoniesFrame 每行 = 一个 expiry:
    - vol0, vol5, vol10, ..., vol100: delta 0%–100% 的 IV (21 个采样点)
    - dte: 到期天数
    - atmiv: ATM IV

    delta 坐标含义: delta=50 ≈ ATM, delta<50 = OTM put 侧, delta>50 = OTM call 侧
    """

    # delta 采样点: 0, 5, 10, ..., 100
    DELTA_POINTS = list(range(0, 101, 5))
    VOL_COLUMNS = [f"vol{d}" for d in DELTA_POINTS]

    def __init__(self, monies_df, strikes_df, spot: float):
        """
        Args:
            monies_df: MoniesFrame.df — 包含 vol0-vol100, dte, atmiv
            strikes_df: StrikesFrame.df — 包含 strike, delta, dte, spotPrice
            spot: 当前 spot price
        """
        self._spot = spot
        self._build_surface(monies_df)
        self._build_strike_delta_map(strikes_df)

    def _build_surface(self, monies_df):
        """构建 (delta, dte) → IV 的 2D 插值网格"""
        df = monies_df.sort_values("dte").copy()
        self._dte_values = df["dte"].values.astype(float)

        # 提取 vol0-vol100 矩阵: shape = (n_expiries, 21)
        iv_matrix = df[self.VOL_COLUMNS].values.astype(float)
        delta_array = [float(d) for d in self.DELTA_POINTS]

        # RectBivariateSpline: x=dte (升序), y=delta (升序)
        # 如果只有 1 个 expiry，退化为 1D 插值
        if len(self._dte_values) >= 2:
            self._spline = RectBivariateSpline(
                self._dte_values, delta_array, iv_matrix, kx=1, ky=3
            )
        else:
            # 单 expiry: 沿 delta 方向做 1D 插值
            from scipy.interpolate import interp1d
            self._single_expiry_interp = interp1d(
                delta_array, iv_matrix[0], kind="cubic",
                bounds_error=False, fill_value=(iv_matrix[0][0], iv_matrix[0][-1])
            )
            self._spline = None

        self._min_dte = float(self._dte_values.min())
        self._max_dte = float(self._dte_values.max())

    def _build_strike_delta_map(self, strikes_df):
        """构建 (strike, dte) → delta 的查找表，用于将 strike 坐标转换为 delta 坐标"""
        self._strike_delta_rows = strikes_df[["strike", "dte", "delta"]].copy()

    def get_iv(self, strike: float, dte: int, spot: float | None = None) -> float:
        """
        查询指定 (strike, dte) 的 SMV IV。

        步骤:
        1. 将 strike 转换为 delta 坐标
        2. 在 (dte, delta) 二维网格上插值
        3. 返回 IV

        超出范围时使用边界值（不外推），确保不返回负 IV。
        """
        effective_spot = spot or self._spot
        delta = self._strike_to_delta(strike, dte, effective_spot)

        # 限制 dte 在已有范围内
        clamped_dte = max(self._min_dte, min(self._max_dte, float(dte)))

        if self._spline is not None:
            iv = float(self._spline(clamped_dte, delta, grid=False))
        else:
            iv = float(self._single_expiry_interp(delta))

        return max(iv, 0.001)  # IV 下限保护

    def _strike_to_delta(self, strike: float, dte: int, spot: float) -> float:
        """
        将 strike 转换为 delta 坐标 (0-100)。

        优先从 StrikesFrame 查找最近 (strike, dte) 的 delta；
        若无精确匹配，用近似公式: delta ≈ N(ln(S/K) / (σ_atm × √T)) × 100
        """
        # 尝试精确查找
        df = self._strike_delta_rows
        exact = df[(df["strike"] == strike) & (df["dte"] == dte)]
        if not exact.empty:
            return float(exact["delta"].iloc[0]) * 100  # ORATS delta 0-1 → 0-100

        # 近似查找: 最近的 strike
        if not df.empty:
            closest_idx = (df["strike"] - strike).abs().idxmin()
            return float(df.loc[closest_idx, "delta"]) * 100

        # 最后退路: 近似公式
        atm_iv = self.get_iv_at_delta(50.0, dte)
        T = max(dte, 1) / 365.0
        d1 = math.log(spot / strike) / (atm_iv * math.sqrt(T))
        return norm.cdf(d1) * 100

    def get_iv_at_delta(self, delta: float, dte: int) -> float:
        """直接用 delta 坐标 (0-100) 查询 IV"""
        clamped_dte = max(self._min_dte, min(self._max_dte, float(dte)))
        clamped_delta = max(0.0, min(100.0, delta))

        if self._spline is not None:
            return max(float(self._spline(clamped_dte, clamped_delta, grid=False)), 0.001)
        else:
            return max(float(self._single_expiry_interp(clamped_delta)), 0.001)


def surface_greeks(
    spot: float,
    strike: float,
    dte: int,
    smv_surface: SMVSurface,
    option_type: str,
    r: float = 0.05,
) -> dict:
    """
    从 SMV 曲面用有限差分计算 Greeks。
    隐式包含 Vanna/Volga 效应——spot bump 时查到的 IV 也随 skew 变化。

    仅用于 Slider 假设场景，不用于真实仓位 Greeks（后者直接用 ORATS 值）。
    """
    h = spot * 0.005  # 0.5% bump

    def price_at(s, t, iv_mult=1.0):
        iv = smv_surface.get_iv(strike, t, s) * iv_mult
        return bs_formula(s, strike, max(t, 1) / 365, r, iv, option_type)

    V = price_at(spot, dte)
    V_up = price_at(spot + h, dte)
    V_down = price_at(spot - h, dte)

    delta = (V_up - V_down) / (2 * h)
    gamma = (V_up - 2 * V + V_down) / (h ** 2)

    V_vol_up = price_at(spot, dte, iv_mult=1.01)
    V_vol_down = price_at(spot, dte, iv_mult=0.99)
    vega = (V_vol_up - V_vol_down) / 0.02

    if dte > 1:
        V_tomorrow = price_at(spot, dte - 1)
        theta = V_tomorrow - V
    else:
        theta = -V

    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}
```

### 11.2 文件: `engine/core/payoff_engine.py`

```python
"""
engine/core/payoff_engine.py — 曲面感知 Payoff 引擎

职责: 计算策略的到期 payoff（解析解）和当前 payoff（SMV 曲面定价），
      以及基于风险中性密度的 POP 估算。

依赖: engine.core.pricing (SMVSurface, bs_formula)
被依赖: engine.steps.s06_strategy_calculator, engine.api.routes_analysis
"""

class PayoffResult(BaseModel):
    spot_range: list[float]      # X 轴
    expiry_pnl: list[float]      # 到期 payoff (解析解)
    current_pnl: list[float]     # 当前 payoff (SMV 曲面定价)
    max_profit: float
    max_loss: float
    breakevens: list[float]
    pop: float                   # Breeden-Litzenberger 风险中性 POP

def compute_payoff(
    legs: list[StrategyLeg],
    spot: float,
    smv_surface: SMVSurface,          # ORATS IV 曲面
    risk_free_rate: float = 0.05,
    spot_range_pct: float = 0.15,
    num_points: int = 200,
) -> PayoffResult:

    net_premium = sum(
        leg.premium * leg.qty * 100 * (1 if leg.side == "sell" else -1)
        for leg in legs
    )

    lower = spot * (1 - spot_range_pct)
    upper = spot * (1 + spot_range_pct)
    spot_range = [lower + i * (upper - lower) / (num_points - 1) for i in range(num_points)]

    # ── 到期 Payoff (解析解，与定价模型无关) ──
    expiry_pnl = []
    for price in spot_range:
        pnl = net_premium
        for leg in legs:
            if leg.option_type == "call":
                intrinsic = max(0, price - leg.strike)
            else:
                intrinsic = max(0, leg.strike - price)

            if leg.side == "buy":
                pnl += intrinsic * leg.qty * 100
            else:
                pnl -= intrinsic * leg.qty * 100
        expiry_pnl.append(round(pnl, 2))

    # ── 当前 Payoff (SMV 曲面感知定价) ──
    # 关键区别: 每个 (price, strike) 组合查曲面得到不同的 IV
    current_pnl = []
    for price in spot_range:
        pnl = 0.0
        for leg in legs:
            dte_days = max((leg.expiry - date.today()).days, 1)
            iv_at_strike = smv_surface.get_iv(leg.strike, dte_days, price)
            current_value = bs_formula(
                price, leg.strike, dte_days / 365,
                risk_free_rate, iv_at_strike, leg.option_type
            )
            if leg.side == "buy":
                pnl += (current_value - leg.premium) * leg.qty * 100
            else:
                pnl += (leg.premium - current_value) * leg.qty * 100
        current_pnl.append(round(pnl, 2))

    max_profit = max(expiry_pnl)
    max_loss = min(expiry_pnl)

    # Breakevens
    breakevens = []
    for i in range(1, len(expiry_pnl)):
        if expiry_pnl[i-1] * expiry_pnl[i] < 0:
            ratio = abs(expiry_pnl[i-1]) / (abs(expiry_pnl[i-1]) + abs(expiry_pnl[i]))
            be = spot_range[i-1] + ratio * (spot_range[i] - spot_range[i-1])
            breakevens.append(round(be, 2))

    # ── POP: Breeden-Litzenberger 风险中性密度 ──
    avg_dte = sum((leg.expiry - date.today()).days for leg in legs) / len(legs)
    pop = _estimate_pop_from_surface(
        spot, smv_surface, int(avg_dte), risk_free_rate,
        spot_range, expiry_pnl
    )

    return PayoffResult(
        spot_range=[round(s, 2) for s in spot_range],
        expiry_pnl=expiry_pnl,
        current_pnl=current_pnl,
        max_profit=max_profit,
        max_loss=max_loss,
        breakevens=breakevens,
        pop=round(pop, 4),
    )


def _estimate_pop_from_surface(
    spot: float,
    smv_surface: SMVSurface,
    dte_days: int,
    r: float,
    spot_range: list[float],
    expiry_pnl: list[float],
) -> float:
    """
    Breeden-Litzenberger 定理: 风险中性密度 = ∂²C/∂K²

    从 SMV 曲面隐含的密度估算 POP，自然包含 skew 和尾部信息。
    OTM put spread 的 POP 会比 log-normal 估算更低（left tail 更厚）。
    """
    if dte_days <= 0 or len(spot_range) < 3:
        return 0.5

    T = dte_days / 365
    dK = spot_range[1] - spot_range[0]

    # 构建曲面感知 call 价格曲线
    call_prices = []
    for K in spot_range:
        iv = smv_surface.get_iv(K, dte_days, spot)
        c = bs_formula(spot, K, T, r, iv, "call")
        call_prices.append(c)

    # 数值二阶导 → 风险中性密度
    discount = math.exp(r * T)
    pop = 0.0
    for i in range(1, len(call_prices) - 1):
        d2c = (call_prices[i+1] - 2*call_prices[i] + call_prices[i-1]) / (dK ** 2)
        density = d2c * discount
        if density > 0 and expiry_pnl[i] > 0:
            pop += density * dK

    return max(0.0, min(1.0, pop))
```

### 11.3 文件: `engine/core/greeks.py`

```python
"""
engine/core/greeks.py — 组合 Greeks 聚合与 P/L 归因

职责:
  - 将多条 leg 的 ORATS Greeks 线性加总为组合级别
  - 计算 P/L 归因 (Delta/Gamma/Theta/Vega 分解)
  单 leg Greeks 直接来自 ORATS API，本模块不做单 leg 计算。

依赖: engine.models.strategy
被依赖: engine.steps.s06_strategy_calculator, engine.api.routes_positions
"""

def composite_greeks(legs: list[StrategyLeg]) -> GreeksComposite:
    """线性加总各 leg 的 ORATS Greeks"""
    total = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for leg in legs:
        sign = 1 if leg.side == "buy" else -1
        total["delta"] += leg.delta * leg.qty * sign
        total["gamma"] += leg.gamma * leg.qty * sign
        total["theta"] += leg.theta * leg.qty * sign
        total["vega"] += leg.vega * leg.qty * sign
    return GreeksComposite(**{k: round(v, 6) for k, v in total.items()})

def compute_pnl_attribution(
    leg: StrategyLeg,
    current_spot: float,
    entry_spot: float,
    current_iv: float,
    entry_iv: float,
    days_held: int,
) -> dict:
    """P/L 归因分解，使用入场时的 ORATS Greeks"""
    side_sign = 1 if leg.side == "buy" else -1
    return {
        "delta_pnl": leg.delta * (current_spot - entry_spot) * 100 * leg.qty * side_sign,
        "gamma_pnl": 0.5 * leg.gamma * (current_spot - entry_spot) ** 2 * 100 * leg.qty * side_sign,
        "theta_pnl": leg.theta * days_held * 100 * leg.qty * side_sign,
        "vega_pnl": leg.vega * (current_iv - entry_iv) * 100 * leg.qty * side_sign,
    }
```

---

## 12. Step 10: 富途数据接入层

### 12.1 文件: `engine/providers/futu_client.py`

**注意**: 富途 API 使用 TCP 长连接，需要 futu-api SDK。

```python
# 依赖: pip install futu-api

class FutuClient:
    """富途 OpenAPI 封装，提供实时期权报价"""

    def __init__(self, host: str = "127.0.0.1", port: int = 11111):
        """
        需要本地运行 FutuOpenD 网关。
        host/port 为 FutuOpenD 的连接地址。
        """
        self._host = host
        self._port = port

    def get_option_chain(
        self,
        symbol: str,        # 如 "US.AAPL"
        start_date: str,    # "YYYY-MM-DD"
        end_date: str,
    ) -> list[dict]:
        """获取期权链（所有 expiry × strike）"""
        from futu import OpenQuoteContext, SubType

        ctx = OpenQuoteContext(host=self._host, port=self._port)
        try:
            ret, data = ctx.get_option_chain(
                code=symbol,
                start=start_date,
                end=end_date,
            )
            if ret != 0:
                raise RuntimeError(f"Futu get_option_chain error: {data}")
            return data.to_dict("records")
        finally:
            ctx.close()

    def get_realtime_quotes(
        self,
        option_codes: list[str],  # 如 ["US.AAPL240419C185000"]
    ) -> list[dict]:
        """批量获取期权合约的实时快照"""
        from futu import OpenQuoteContext

        ctx = OpenQuoteContext(host=self._host, port=self._port)
        try:
            ret, data = ctx.get_market_snapshot(option_codes)
            if ret != 0:
                raise RuntimeError(f"Futu snapshot error: {data}")
            # 返回 bid/ask/last/volume/OI 等
            return data.to_dict("records")
        finally:
            ctx.close()
```

### 12.2 LiveQuoteEnricher

在 Step 8 排序之前调用：

```python
class LiveQuoteEnricher:
    def __init__(self, futu_client: FutuClient):
        self._futu = futu_client

    def enrich(self, strategies: list[StrategyCandidate], symbol: str) -> list[StrategyCandidate]:
        """用富途实时报价填充 bid/ask"""
        # 收集所有 leg 的期权代码
        option_codes = []
        for strategy in strategies:
            for leg in strategy.legs:
                code = self._build_futu_option_code(symbol, leg)
                option_codes.append(code)

        if not option_codes:
            return strategies

        # 批量查询
        quotes = self._futu.get_realtime_quotes(list(set(option_codes)))
        quote_map = {q["code"]: q for q in quotes}

        # 填充
        for strategy in strategies:
            for leg in strategy.legs:
                code = self._build_futu_option_code(symbol, leg)
                if code in quote_map:
                    q = quote_map[code]
                    leg.bid = q.get("bid_price")
                    leg.ask = q.get("ask_price")

        return strategies
```

---

## 13. Step 11: 三层快照数据架构

### 13.1 文件: `engine/models/snapshots.py`

```python
class MarketParameterSnapshot(BaseModel):
    snapshot_id: str                    # UUID
    symbol: str
    captured_at: datetime

    # 价格层
    spot_price: float
    spot_change_pct: float = 0.0       # vs 基线

    # 波动率层
    atm_iv_front: float
    atm_iv_back: float | None = None
    term_spread: float = 0.0           # back - front
    iv30d: float
    hv20d: float | None = None
    vrp: float = 0.0
    vol_of_vol: float = 0.0
    iv_rank: float = 0.0
    iv_pctl: float = 0.0
    iv_consensus: float = 0.0

    # 期权结构层
    net_gex: float = 0.0
    net_dex: float = 0.0
    zero_gamma_strike: float | None = None
    call_wall_strike: float | None = None
    put_wall_strike: float | None = None
    vol_pcr: float | None = None
    oi_pcr: float | None = None

    # 事件层
    regime_class: str = "NORMAL"
    next_event_type: str | None = None
    days_to_event: int | None = None


class AnalysisResultSnapshot(BaseModel):
    analysis_id: str                   # UUID
    symbol: str
    created_at: datetime
    baseline_snapshot_id: str          # 关联基线

    # Scores
    gamma_score: float
    break_score: float
    direction_score: float
    iv_score: float

    # 场景
    scenario: str
    scenario_confidence: float
    scenario_method: str
    invalidate_conditions: list[str]

    # 策略 (JSON 序列化)
    strategies: list[dict]             # StrategyCandidate.model_dump()

    # Meso 交叉引用
    meso_s_dir: float | None = None
    meso_s_vol: float | None = None


class MonitorStateSnapshot(BaseModel):
    monitor_id: str
    symbol: str
    captured_at: datetime
    analysis_id: str
    baseline_snapshot_id: str

    # 偏移度
    spot_drift_pct: float = 0.0
    iv_drift_pct: float = 0.0
    zero_gamma_drift_pct: float = 0.0
    term_structure_flip: bool = False
    gex_sign_flip: bool = False

    # 策略健康度 (per position)
    positions_health: list[dict] = []

    # 场景有效性
    scenario_still_valid: bool = True
    invalidated_conditions: list[str] = []
    recommended_action: str | None = None  # "hold"/"adjust"/"exit"/"recalc_from_step_N"
```

---

## 14. Step 12: 监控指标体系与告警引擎

### 14.1 文件: `engine/config/thresholds.yaml`

```yaml
tier1_market:
  spot_drift_pct:
    yellow: 0.015
    red: 0.030
    action: "recalc_from_step_4"
  atm_iv_drift_pct:
    yellow: 0.08
    red: 0.15
    action: "recalc_from_step_5"
  zero_gamma_drift_pct:
    yellow: 0.010
    red: 0.020
    action: "recalc_from_step_4"
  term_structure_flip:
    red: true
    action: "recalc_from_step_3"
  gex_sign_flip:
    red: true
    action: "recalc_from_step_4"
  vol_pcr:
    yellow_high: 1.0
    yellow_low: 0.6
    red_high: 1.2
    red_low: 0.5
    action: "recalc_from_step_5"

tier2_analysis:
  score_drift_max:
    yellow: 15
    red: 25
    action: "recalc_from_step_5"
  direction_flip:
    red: true
    action: "recalc_from_step_5"
  iv_score_change:
    yellow: 12
    red: 20
    action: "recalc_from_step_6"
  scenario_invalidation_count:
    yellow: 1
    red: 2
    action: "recalc_from_step_2"

tier3_strategy:
  max_loss_proximity:
    yellow: 0.50
    red: 0.75
  delta_drift:
    yellow: 0.15
    red: 0.30
  theta_realization_ratio:
    yellow_low: 0.70
    red_low: 0.50
  breakeven_distance_pct:
    yellow: 0.02
    red: 0.01
  dte_remaining:
    yellow: 5
    red: 2

refresh_interval_seconds: 300  # 5 分钟
```

### 14.2 文件: `engine/monitor/alert_engine.py`

```python
class AlertSeverity(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"

class AlertEvent(BaseModel):
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

class AlertEngine:
    def __init__(self, thresholds_config: dict):
        self._thresholds = thresholds_config

    def evaluate(
        self,
        current: MarketParameterSnapshot,
        baseline: MarketParameterSnapshot,
        analysis: AnalysisResultSnapshot,
        positions: list[dict],
    ) -> tuple[list[AlertEvent], str | None]:
        """
        Returns:
            alerts: 本次评估产生的告警列表
            recalc_action: 最高优先级的重算动作 (如 "recalc_from_step_3") 或 None
        """
        alerts = []
        recalc_actions = []

        # Tier 1 评估
        alerts.extend(self._eval_tier1(current, baseline))

        # Tier 2 评估
        alerts.extend(self._eval_tier2(current, baseline, analysis))

        # Tier 3 评估
        alerts.extend(self._eval_tier3(positions))

        # 确定最高优先级的重算动作
        red_alerts = [a for a in alerts if a.severity == AlertSeverity.RED and a.action]
        if red_alerts:
            # 选择重算范围最大的 (step 数字最小的)
            recalc_action = min(red_alerts, key=lambda a: int(a.action.split("_")[-1])).action
        else:
            recalc_action = None

        return alerts, recalc_action
```

### 14.3 文件: `engine/monitor/incremental_recalc.py`

```python
class IncrementalRecalculator:
    """增量重算：从指定 Step 开始重跑流程"""

    async def recalc_from(
        self,
        step: int,
        symbol: str,
        cached_context: RegimeContext | None,
        cached_pre_calc: PreCalculatorOutput | None,
        cached_micro: MicroSnapshot | None,
        cached_scores: FieldScores | None,
    ) -> AnalysisResultSnapshot:
        """
        step=2: 全量重跑
        step=3: 从 Pre-Calculator 开始
        step=4: 从 Field Calculator 开始
        step=5: 从场景分析开始
        step=6: 从策略计算开始
        """
        if step <= 2:
            return await pipeline.run_full(symbol)

        if step <= 3:
            # 重新获取 summary 和计算窗口
            pre_calc = await s03.run(cached_context, ...)
            micro = await micro_client.fetch(symbol, pre_calc)
            scores = s04.run(micro, cached_context)
            scenario = s05.run(scores, cached_context, micro)
            # ... 继续 step 6-9
        elif step <= 4:
            # 复用 pre_calc，重新获取 micro 数据
            micro = await micro_client.fetch(symbol, cached_pre_calc)
            scores = s04.run(micro, cached_context)
            scenario = s05.run(scores, cached_context, micro)
            # ...
        elif step <= 5:
            # 复用 micro，重算 scores 和场景
            scores = s04.run(cached_micro, cached_context)
            scenario = s05.run(scores, cached_context, cached_micro)
            # ...
        elif step <= 6:
            # 复用 scores 和场景，重算策略
            # ...
```

---

## 15. Step 13: 后端 API 设计

### 15.1 文件: `engine/api/routes_analysis.py`

```python
router = APIRouter(prefix="/api/v2", tags=["analysis"])

@router.post("/analysis/{symbol}")
async def run_analysis(symbol: str, trade_date: date = Query(default=None)):
    """触发完整分析，返回 analysis_id"""
    result = await pipeline.run_full(symbol, trade_date or date.today())
    return {"analysis_id": result.analysis_id}

@router.get("/analysis/{analysis_id}")
async def get_analysis(analysis_id: str):
    """获取分析结果"""
    # 从 DB 查询 AnalysisResultSnapshot
    return snapshot.model_dump()

@router.get("/analysis/{analysis_id}/payoff/{strategy_index}")
async def get_payoff(analysis_id: str, strategy_index: int):
    """获取策略 payoff 数据"""
    # 从 AnalysisResultSnapshot.strategies[index] 返回 payoff 数据
    return payoff_data
```

### 15.2 文件: `engine/api/routes_monitor.py`

```python
router = APIRouter(prefix="/api/v2", tags=["monitor"])

@router.get("/market/{symbol}/snapshot")
async def get_market_snapshot(symbol: str):
    """最新市场参数快照"""

@router.get("/market/{symbol}/history")
async def get_market_history(symbol: str, hours: int = 4):
    """市场参数历史 (迷你趋势图)"""

@router.get("/monitor/{symbol}/state")
async def get_monitor_state(symbol: str):
    """最新监控状态 (含告警颜色)"""

@router.get("/monitor/{symbol}/alerts")
async def get_alerts(symbol: str, limit: int = 50):
    """告警日志"""
```

### 15.3 文件: `engine/api/websocket_handler.py`

```python
@router.websocket("/ws/v2/live/{symbol}")
async def live_feed(websocket: WebSocket, symbol: str):
    await websocket.accept()

    # 推送类型:
    # {"type": "spot_update", "data": {"spot": 589.23, "timestamp": "..."}}
    # {"type": "pnl_update", "data": {"position_id": "...", "unrealized_pnl": 60.0, ...}}
    # {"type": "alert", "data": {"tier": 1, "indicator": "gex_sign_flip", ...}}

    # 每 10s 推送 spot, 每 30s 推送 pnl, alert 实时推送
```

---

## 16. Step 14: API 数据输出规范

### 16.1 分析报告 API 输出结构

API 端点 `GET /api/v2/analysis/{analysis_id}` 返回完整分析结果，供任意前端消费。

**响应 JSON 结构**:

```json
{
  "analysis_id": "uuid",
  "symbol": "AAPL",
  "created_at": "2026-04-09T14:30:00",
  "regime_class": "NORMAL",
  "event": {"event_type": "none", "days_to_event": null},

  "market_snapshot": {
    "spot_price": 589.23,
    "atm_iv_front": 0.386,
    "atm_iv_back": 0.342,
    "term_spread": -0.044,
    "iv_rank": 65.0,
    "iv_pctl": 58.0,
    "vol_pcr": 0.81,
    "net_gex": -1200000,
    "zero_gamma_strike": 582.5,
    "call_wall_strike": 600.0,
    "put_wall_strike": 570.0
  },

  "scores": {
    "gamma_score": 72.5,
    "break_score": 45.0,
    "direction_score": 38.2,
    "iv_score": 68.0
  },

  "scenario": {
    "scenario": "trend",
    "confidence": 0.85,
    "method": "rule_engine",
    "invalidate_conditions": ["direction_score 跌破 ±40", "..."]
  },

  "strategies": [
    {
      "rank": 1,
      "strategy_type": "bull_put_spread",
      "risk_profile": "balanced",
      "total_score": 78.5,
      "legs": [...],
      "net_credit_debit": 1.25,
      "max_profit": 125.0,
      "max_loss": -375.0,
      "breakevens": [583.75],
      "pop": 0.68,
      "ev": 42.0,
      "greeks_composite": {"delta": 0.07, "gamma": -0.01, "theta": 1.82, "vega": -0.45},
      "payoff_curve": {
        "spot_range": [500.0, 500.75, ...],
        "expiry_pnl": [-375, -375, ...],
        "current_pnl": [-280, -275, ...]
      }
    }
  ],

  "micro_structure": {
    "gex_by_strike": [{"strike": 570, "gex": -50000}, ...],
    "dex_by_strike": [{"strike": 570, "dex": 120000}, ...],
    "term_structure": [{"dte": 10, "atmiv": 0.386}, {"dte": 38, "atmiv": 0.342}],
    "skew": [{"delta": 25, "iv": 0.42}, {"delta": 50, "iv": 0.38}, ...]
  }
}
```

### 16.2 Payoff 重算 API

API 端点 `POST /api/v2/analysis/{analysis_id}/payoff/{strategy_index}/recalc` 支持 Slider 交互式重算。

**请求体**:
```json
{
  "slider_dte": 5,
  "slider_iv_multiplier": 1.5
}
```

**响应**: 与 `payoff_curve` 相同格式的重算结果。

**后端实现** (在 `engine/core/payoff_engine.py` 中):

```python
def recalc_payoff_with_sliders(
    legs: list[StrategyLeg],
    spot: float,
    smv_surface: SMVSurface,          # ORATS IV 曲面
    slider_dte: int,
    slider_iv_multiplier: float,      # 对整张曲面等比缩放
    risk_free_rate: float = 0.05,
    spot_range_pct: float = 0.15,
    num_points: int = 200,
) -> list[float]:
    """
    Slider 交互式 payoff 重算。

    slider_iv_multiplier 作用于整张曲面的每个查询结果:
      adjusted_iv = surface.get_iv(K, T) × multiplier
    保留 skew 形态（所有 strike 等比缩放），比恒定 σ × multiplier 更准确。
    """
    lower = spot * (1 - spot_range_pct)
    upper = spot * (1 + spot_range_pct)
    spot_range = [lower + i * (upper - lower) / (num_points - 1) for i in range(num_points)]

    pnl_curve = []
    for price in spot_range:
        pnl = 0.0
        for leg in legs:
            base_iv = smv_surface.get_iv(leg.strike, slider_dte, price)
            adjusted_iv = base_iv * slider_iv_multiplier
            T = max(slider_dte, 0) / 365
            val = bs_formula(price, leg.strike, T, risk_free_rate, adjusted_iv, leg.option_type)
            if leg.side == "buy":
                pnl += (val - leg.premium) * leg.qty * 100
            else:
                pnl += (leg.premium - val) * leg.qty * 100
        pnl_curve.append(round(pnl, 2))

    return pnl_curve
```

### 16.3 P/L 归因 API

API 端点 `GET /api/v2/positions/{id}/attribution` 返回 P/L 归因分解。

**后端实现** (在 `engine/core/greeks.py` 中):

```python
def compute_pnl_attribution(
    leg: StrategyLeg,
    current_spot: float,
    entry_spot: float,
    current_iv: float,
    entry_iv: float,
    days_held: int,
) -> dict:
    """P/L 归因分解: Delta/Gamma/Theta/Vega"""
    side_sign = 1 if leg.side == "buy" else -1
    return {
        "delta_pnl": leg.delta * (current_spot - entry_spot) * 100 * leg.qty * side_sign,
        "gamma_pnl": 0.5 * leg.gamma * (current_spot - entry_spot) ** 2 * 100 * leg.qty * side_sign,
        "theta_pnl": leg.theta * days_held * 100 * leg.qty * side_sign,
        "vega_pnl": leg.vega * (current_iv - entry_iv) * 100 * leg.qty * side_sign,
    }
```

### 16.4 监控状态 API 输出结构

API 端点 `GET /api/v2/monitor/{symbol}/state` 返回完整监控状态。

**响应 JSON 结构**:
```json
{
  "symbol": "AAPL",
  "captured_at": "2026-04-09T14:35:00",
  "analysis_id": "uuid",

  "tier1_indicators": [
    {"name": "spot_drift", "label": "Spot 偏移", "value": 0.003, "severity": "green", "threshold_yellow": 0.015, "threshold_red": 0.03},
    {"name": "iv_drift", "label": "IV 偏移", "value": 0.12, "severity": "yellow", "threshold_yellow": 0.08, "threshold_red": 0.15},
    ...
  ],

  "tier2_indicators": [
    {"name": "scenario_valid", "label": "场景有效性", "value": true, "severity": "green", "invalidated_count": 0},
    ...
  ],

  "tier3_positions": [
    {
      "position_id": "uuid",
      "strategy_type": "bull_put_spread",
      "unrealized_pnl": 60.0,
      "max_loss_proximity": 0.32,
      "delta_drift": 0.02,
      "severity": "green"
    }
  ],

  "recent_alerts": [
    {"timestamp": "14:35", "severity": "red", "indicator": "gex_sign_flip", "message": "GEX 符号翻转", "action": "recalc_from_step_4"},
    ...
  ]
}
```

### 16.5 WebSocket 推送消息格式

端点: `ws://host:port/ws/v2/live/{symbol}`

```json
// 类型 1: Spot 更新 (每 10s)
{"type": "spot_update", "data": {"spot": 589.23, "timestamp": "2026-04-09T14:35:10"}}

// 类型 2: P/L 更新 (每 30s)
{"type": "pnl_update", "data": {
  "position_id": "uuid",
  "unrealized_pnl": 60.0,
  "greeks": {"delta": 0.07, "gamma": -0.01, "theta": 1.82, "vega": -0.45},
  "attribution": {"delta_pnl": 45, "gamma_pnl": -12, "theta_pnl": 9, "vega_pnl": 18}
}}

// 类型 3: 告警 (实时)
{"type": "alert", "data": {
  "tier": 1, "indicator": "gex_sign_flip", "severity": "red",
  "message": "GEX 符号翻转", "action": "recalc_from_step_4"
}}
```

---

## 17. 数据库 Schema

### 新增表 (Alembic migration)

```sql
CREATE TABLE market_parameter_snapshots (
    id INTEGER PRIMARY KEY,
    snapshot_id TEXT UNIQUE NOT NULL,
    symbol TEXT NOT NULL,
    captured_at DATETIME NOT NULL,
    data_json JSON NOT NULL,  -- MarketParameterSnapshot 全部字段
    UNIQUE(symbol, captured_at)
);
CREATE INDEX ix_mps_symbol_time ON market_parameter_snapshots(symbol, captured_at);

CREATE TABLE analysis_result_snapshots (
    id INTEGER PRIMARY KEY,
    analysis_id TEXT UNIQUE NOT NULL,
    symbol TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    baseline_snapshot_id TEXT NOT NULL,
    scores_json JSON NOT NULL,
    scenario TEXT NOT NULL,
    scenario_confidence REAL NOT NULL,
    strategies_json JSON NOT NULL,
    meso_json JSON,
    FOREIGN KEY (baseline_snapshot_id) REFERENCES market_parameter_snapshots(snapshot_id)
);
CREATE INDEX ix_ars_symbol_time ON analysis_result_snapshots(symbol, created_at);

CREATE TABLE monitor_state_snapshots (
    id INTEGER PRIMARY KEY,
    monitor_id TEXT UNIQUE NOT NULL,
    symbol TEXT NOT NULL,
    captured_at DATETIME NOT NULL,
    analysis_id TEXT NOT NULL,
    baseline_snapshot_id TEXT NOT NULL,
    state_json JSON NOT NULL,
    FOREIGN KEY (analysis_id) REFERENCES analysis_result_snapshots(analysis_id)
);
CREATE INDEX ix_mss_symbol_time ON monitor_state_snapshots(symbol, captured_at);

CREATE TABLE alert_events (
    id INTEGER PRIMARY KEY,
    alert_id TEXT UNIQUE NOT NULL,
    symbol TEXT NOT NULL,
    timestamp DATETIME NOT NULL,
    tier INTEGER NOT NULL,
    indicator TEXT NOT NULL,
    severity TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT NOT NULL,
    threshold TEXT,
    action TEXT
);
CREATE INDEX ix_alerts_symbol_time ON alert_events(symbol, timestamp, severity);

CREATE TABLE tracked_positions (
    id INTEGER PRIMARY KEY,
    position_id TEXT UNIQUE NOT NULL,
    symbol TEXT NOT NULL,
    analysis_id TEXT NOT NULL,
    strategy_index INTEGER NOT NULL,
    entry_time DATETIME NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',  -- active / closed
    legs_json JSON NOT NULL,
    entry_spot REAL NOT NULL,
    entry_iv REAL NOT NULL,
    FOREIGN KEY (analysis_id) REFERENCES analysis_result_snapshots(analysis_id)
);
CREATE INDEX ix_positions_symbol ON tracked_positions(symbol, status);
```

---

## 18. 配置文件设计

### engine/config/engine.yaml

```yaml
meso_api:
  base_url: "http://127.0.0.1:18000"
  timeout_seconds: 10

orats:
  api_token: "${ORATS_API_TOKEN}"
  base_url: "https://api.orats.io/datav2"

futu:
  host: "127.0.0.1"
  port: 11111
  enabled: false  # 可选，无富途时降级到 ORATS 数据

engine:
  risk_free_rate: 0.05
  payoff_num_points: 200
  payoff_range_pct: 0.15
  top_n_strategies: 3
  min_oi: 500
  max_spread_pct: 0.15
  max_loss_limit: 50000

monitor:
  refresh_interval_seconds: 300
  websocket_spot_interval_seconds: 10
  websocket_pnl_interval_seconds: 30
  snapshot_retention_days: 30

database:
  url: "sqlite:///data/engine.db"
```

---

## 19. 测试策略

### 单元测试

| 模块 | 测试文件 | 测试点 |
|---|---|---|
| SMV 曲面定价 | `test_pricing.py` | SMVSurface 插值精度、bs_formula 封闭解、surface_greeks 有限差分 |
| Payoff | `test_payoff.py` | 曲面感知 current_pnl、Breeden-Litzenberger POP、breakeven |
| Field Calculator | `test_field_calculator.py` | 用 mock MicroSnapshot 验证 4 个 Score 的边界条件 |
| Scenario Analyzer | `test_scenario_analyzer.py` | 每种场景的触发条件和 invalidate conditions |
| Strategy Calculator | `test_strategy_calculator.py` | strike 选择逻辑、策略 legs 正确性 |
| Strategy Ranker | `test_strategy_ranker.py` | 硬过滤和软评分的排序正确性 |
| Alert Engine | `test_alert_engine.py` | 阈值触发、颜色状态、重算动作映射 |

### 集成测试

- 用 fixture 数据（mock ORATS 响应）跑完 Step 2-9 全流程
- 验证 AnalysisResultSnapshot 包含完整策略和 payoff 数据
- 验证监控循环的增量重算逻辑

---

## 20. 附录：公式与常量汇总

### 数据源优先级

```
定价真值:     ORATS callValue / putValue (SMV 理论价)
IV 真值:      ORATS smvVol (SMV 拟合 IV)
Greeks 真值:  ORATS delta / gamma / theta / vega (SMV Greeks)
情景投射:     SMVSurface.get_iv(K, T) → bs_formula() (曲面感知 BS)
POP 估算:     Breeden-Litzenberger (曲面隐含风险中性密度)
到期 Payoff:  解析解 intrinsic value (无模型依赖)
```

### BS 封闭解（底层定价公式，σ 由曲面查询提供）

```
d1 = [ln(S/K) + (r + σ²/2)T] / (σ√T)    其中 σ = SMVSurface.get_iv(K, T)
d2 = d1 - σ√T
Call = S·N(d1) - K·e^(-rT)·N(d2)
Put  = K·e^(-rT)·N(-d2) - S·N(-d1)
```

### 曲面有限差分 Greeks（Slider 假设场景用）

```
Delta = [V(S+h) - V(S-h)] / (2h)         h = S × 0.005
Gamma = [V(S+h) - 2V(S) + V(S-h)] / h²
Vega  = [V(σ×1.01) - V(σ×0.99)] / 0.02
Theta = V(T-1day) - V(T)

其中 V(S) = bs_formula(S, K, T, r, SMVSurface.get_iv(K, T, S))
注: spot bump 时 IV 随 skew 变化，隐式包含 Vanna/Volga 效应
```

### Breeden-Litzenberger POP

```
风险中性密度: p(K) = e^(rT) × ∂²C/∂K²
POP = ∫ p(K) dK，积分区间为 expiry_pnl > 0 的区域
其中 C(K) 使用曲面感知定价
```

### GEX 缩放

```
GEX = gamma × OI × spot² × 0.01 × 100
DEX = delta × OI × spot × 100
VEX = vega × OI × 100
```

### 策略排序权重

```
TotalScore = 场景匹配度 × 0.20
           + 滑点调整EV × 0.25
           + Tail Risk × 0.15
           + 流动性 × 0.15
           + Theta效率 × 0.10
           + 资本效率 × 0.15
```

### 监控阈值速查

| 指标 | 黄色 | 红色 |
|---|---|---|
| Spot 偏移 | 1.5% | 3.0% |
| ATM IV 偏移 | 8% | 15% |
| Zero Gamma 偏移 | 1.0% | 2.0% |
| Score 漂移 | 15 | 25 |
| Max Loss 接近 | 50% | 75% |
| Delta 漂移 | 0.15 | 0.30 |
| DTE 剩余 | 5天 | 2天 |
