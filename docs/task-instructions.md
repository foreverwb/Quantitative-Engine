# Claude Code 开发任务指令集

> **运行环境**: Claude Code for VS Code  
> **前置条件**: 已 clone 包含 MESO 和 Micro-Provider 代码的 monorepo  
> **设计文档位置**: 项目根目录 `docs/design-doc.md`（将详细设计方案放入此路径）

---

## 任务执行规范

### Claude Code 配置说明

| 参数 | 说明 |
|---|---|
| **Model** | `opus` = 复杂架构/多文件联动任务; `sonnet` = 单文件/简单逻辑任务 |
| **Effort** | `high` = 需要深度理解上下文; `medium` = 标准实现; `low` = 简单修改 |
| **Thinking** | `extended` = 需要规划多步; `standard` = 直接实现 |

### 通用上下文指令（每个 Task 前缀）

```
请先阅读 docs/design-doc.md 中的相关章节，理解模块的输入/输出/算法定义。
所有 Python 代码使用 Python 3.11+，pydantic v2，类型注解完整。
所有测试使用 pytest，fixture 放在 tests/fixtures/ 目录。
```

---

## Phase 0: 项目骨架搭建

### Task 0.1: 初始化 engine 项目结构

**Config**: Model=`sonnet` | Effort=`medium` | Thinking=`standard`

```
阅读 docs/design-doc.md 第 1.3 节（仓库结构）和第 18 节（配置文件设计）。

在 monorepo 中创建 apps/engine/ 项目骨架：

1. 创建 apps/engine/pyproject.toml:
   - name: "meso-engine"
   - requires-python: ">=3.11"
   - dependencies: fastapi, uvicorn, sqlalchemy>=2.0, pydantic>=2.9, alembic,
     httpx, scipy, pyyaml, pandas, numpy
   - dev dependencies: pytest, httpx

2. 按 1.3 节的目录结构创建所有 __init__.py 空文件和目录

3. 创建 apps/engine/engine/config/ 下的三个配置文件:
   - engine.yaml (第 18 节完整内容)
   - thresholds.yaml (第 14.1 节完整内容)
   - strategies.yaml (第 8.1 节策略族映射)
   - event_calendar.json (空模板，含一个示例 FOMC 日期)

4. 创建 apps/engine/engine/db/session.py:
   - 复用 apps/api/app/db/session.py 的模式
   - 默认数据库路径: apps/engine/data/engine.db

5. 创建 apps/engine/engine/db/models.py:
   - 实现第 17 节定义的 5 个表的 SQLAlchemy ORM 模型

6. 创建 Alembic 初始化:
   - apps/engine/alembic.ini
   - apps/engine/alembic/env.py
   - 创建初始 migration (第 17 节的 CREATE TABLE)

不要实现任何业务逻辑，只搭建骨架和配置。
```

---

## Phase 1: 核心计算模块

### Task 1.1: SMV 曲面定价模块

**Config**: Model=`opus` | Effort=`high` | Thinking=`extended`

```
阅读 docs/design-doc.md 第 11.1 节 (SMV 曲面定价) 和第 20 节 (公式附录)。

实现 apps/engine/engine/core/pricing.py:

1. 实现 bs_formula(S, K, T, r, sigma, option_type) -> float
   - 使用 scipy.stats.norm
   - T <= 0 时返回 intrinsic value
   - sigma <= 0 时返回贴现 intrinsic
   - 这是底层封闭解，sigma 参数由 SMVSurface 提供，不是常数

2. 实现 class SMVSurface:
   - __init__(self, monies_df, strikes_df, spot):
     从 ORATS MoniesFrame 的 vol0-vol100 (21 个 delta 采样点)
     + dte 构建 2D 插值网格 (scipy RectBivariateSpline)
     从 StrikesFrame 构建 strike → delta 查找表
   - get_iv(self, strike, dte, spot) -> float:
     将 strike 转为 delta 坐标，查曲面返回 IV
     超出范围使用边界值（不外推）
   - get_iv_at_delta(self, delta, dte) -> float:
     直接用 delta 坐标查询

3. 实现 surface_greeks(spot, strike, dte, smv_surface, option_type, r) -> dict:
   - 有限差分: Delta, Gamma, Vega, Theta
   - spot bump 时 IV 随曲面变化，隐式包含 Vanna/Volga 效应

4. 编写 tests/test_pricing.py:
   - 测试 bs_formula: S=100, K=100, T=1, r=0.05, sigma=0.20, call → 价格应约 10.45
   - 测试 bs_formula: T=0 → intrinsic value
   - 测试 SMVSurface: 构造 mock MoniesFrame (3 个 expiry, 平坦 IV=0.30)
     → get_iv(ATM strike, 30) 应返回 ≈ 0.30
   - 测试 SMVSurface skew: vol25 > vol50 > vol75 的曲面
     → OTM put (低 delta) 的 IV 应高于 ATM
   - 测试 surface_greeks: 验证 delta 在 (0, 1) 范围内

确保所有测试通过。
```

### Task 1.2: Payoff 计算引擎

**Config**: Model=`opus` | Effort=`high` | Thinking=`extended`

```
阅读 docs/design-doc.md 第 11.2 节 (Payoff Engine) 和第 11.3 节 (greeks.py)。

实现 apps/engine/engine/core/payoff_engine.py:

1. 实现 PayoffResult(BaseModel):
   - spot_range, expiry_pnl, current_pnl, max_profit, max_loss, breakevens, pop

2. 实现 compute_payoff(legs, spot, smv_surface, risk_free_rate, spot_range_pct, num_points):
   - 到期 Payoff: 解析解 (intrinsic value，无模型依赖)
   - 当前 Payoff: 曲面感知定价
     对每个 (price, strike) 组合调用 smv_surface.get_iv() 取 IV，
     再用 bs_formula() 定价。每个 strike 的 IV 不同（反映 skew）。
   - Breakeven: 线性插值找过零点
   - POP: Breeden-Litzenberger 方法
     ∂²C/∂K² × e^(rT) → 风险中性密度 → 在盈利区间积分

3. 实现 recalc_payoff_with_sliders():
   - 接收 smv_surface + slider_dte + slider_iv_multiplier
   - IV 调整: surface.get_iv(K, T) × multiplier (保留 skew 形态)

4. 依赖 engine/core/pricing.py (Task 1.1)

5. 同时实现 engine/core/greeks.py:
   - composite_greeks(legs) → 线性加总 ORATS Greeks
   - compute_pnl_attribution(leg, current_spot, entry_spot, ...) → P/L 归因

6. 编写 tests/test_payoff.py:
   - 构造 mock SMVSurface (flat IV=0.30 和 skewed IV)
   - 测试 Bull Call Spread: max_profit/loss/breakeven
   - 测试 Iron Condor: 2 个 breakeven
   - 测试 skewed 曲面 vs flat 曲面的 POP 差异:
     OTM put spread 在 skewed 曲面下 POP 应更低
```

### Task 1.3: Pydantic 数据模型

**Config**: Model=`sonnet` | Effort=`medium` | Thinking=`standard`

```
阅读 docs/design-doc.md 第 4.1 节 (context.py)、第 6.2 节 (micro.py)、
第 8.2 节 (strategy.py)、第 13.1 节 (snapshots.py)、第 14.2 节 (alerts.py)。

创建以下文件，实现所有 Pydantic 数据模型:

1. engine/models/context.py:
   - EventInfo, RegimeContext, MesoSignal
   - 完全按 design-doc 4.1 节定义

2. engine/models/scores.py:
   - FieldScores(BaseModel): gamma_score, break_score, direction_score, iv_score

3. engine/models/scenario.py:
   - ScenarioResult(BaseModel): scenario, confidence, method, invalidate_conditions

4. engine/models/strategy.py:
   - StrategyLeg, GreeksComposite, StrategyCandidate
   - 按 design-doc 8.2 节定义
   - 注意: StrategyLeg.premium 来自 ORATS callValue/putValue,
     StrategyLeg.iv 来自 ORATS smvVol, Greeks 来自 ORATS SMV Greeks

5. engine/models/payoff.py:
   - 导入并 re-export PayoffResult (from core.payoff_engine)

6. engine/models/snapshots.py:
   - MarketParameterSnapshot, AnalysisResultSnapshot, MonitorStateSnapshot
   - 完全按 design-doc 13.1 节定义

7. engine/models/alerts.py:
   - AlertSeverity(StrEnum), AlertEvent(BaseModel)

所有模型使用 model_config = ConfigDict(extra="forbid", frozen=True)。
使用 Literal 类型约束枚举值。
确保所有模型可正常 import 且无循环依赖。
```

---

## Phase 2: 分析流水线 Steps

### Task 2.1: Meso Client + Regime Gating (Step 2)

**Config**: Model=`sonnet` | Effort=`medium` | Thinking=`standard`

```
阅读 docs/design-doc.md 第 4.2 节 (Regime Gating)。

1. 实现 engine/providers/meso_client.py:
   - class MesoClient:
     - __init__(self, base_url: str)
     - async def get_signal(self, symbol: str, trade_date: date) -> MesoSignal | None
       使用 httpx.AsyncClient 调用 GET /api/v1/signals/{symbol}?trade_date=...
       解析 ApiResponse 格式，404 时返回 None

2. 实现 engine/steps/s02_regime_gating.py:
   - 按 design-doc 4.2 节的完整逻辑
   - 读取 event_calendar.json
   - 构建 MarketRegime (使用 regime.boundary 模块)
   - 调用 classify() 获取 RegimeClass
   - 门控规则: STRESS + days_to_event <= 1 → skip
   - 返回 RegimeContext

3. 编写 tests/test_regime_gating.py:
   - mock Meso API 和 OratsProvider
   - 测试 STRESS + 近事件 → gate_result = "skip"
   - 测试 NORMAL + 无事件 → gate_result = "proceed"
   - 测试事件日期解析 (earnings/fomc)
```

### Task 2.2: Pre-Calculator (Step 3)

**Config**: Model=`opus` | Effort=`high` | Thinking=`extended`

```
阅读 docs/design-doc.md 第 5 节 (Pre-Calculator) 完整内容。

实现 engine/steps/s03_pre_calculator.py:

1. class PreCalculatorOutput(BaseModel):
   按 5.1 节定义

2. async def run(context, summary, hist_summary) -> PreCalculatorOutput:
   - Step 3.1: 计算 dyn_window_pct (三者取最大值逻辑)
   - Step 3.2: 计算 dyn_strike_band
   - Step 3.3: 计算 dyn_dte_range 和 scenario_seed (分支逻辑)

3. 完全按 design-doc 的 Python 伪代码实现

4. 编写 tests/test_pre_calculator.py:
   - 测试 earnings 场景: 双桶 DTE, scenario_seed="event"
   - 测试 trend 场景: 14-45 DTE, scenario_seed="trend"
   - 测试 STRESS regime: 双桶, scenario_seed="transition"
   - 测试 dyn_window_pct 的硬边界 (3%-20%)
```

### Task 2.3: Micro Client + Field Calculator (Step 4)

**Config**: Model=`opus` | Effort=`high` | Thinking=`extended`

```
阅读 docs/design-doc.md 第 6 节完整内容 (Micro Client + Field Calculator)。

1. 实现 engine/providers/micro_client.py:
   - class MicroClient 按 6.1 节完整实现
   - Phase 1 并行调用 (asyncio.gather)
   - Phase 2 衍生计算 (compute_gex, compute_dex 等)
   - Phase 3 条件扩展调用
   - _find_zero_gamma() 和 _find_walls() 方法

   注意: 直接 import Micro-Provider repo 的模块:
   from provider.orats import OratsProvider
   from compute.exposure.calculator import compute_gex, compute_dex
   from compute.volatility.term import TermBuilder
   from compute.volatility.skew import SkewBuilder
   from compute.flow.pcr import compute_pcr
   from provider.fields import GEX_FIELDS, DEX_FIELDS, IV_SURFACE_FIELDS

2. 实现 engine/steps/s04_field_calculator.py:
   - 按 6.3 节实现 4 个 Score 的计算:
     - GammaScore: net_gexn_normalized, wall_concentration, zero_gamma_distance, month_consistency
     - BreakScore: wall_distance, implied_vs_actual, zero_gamma_flip_risk
     - DirectionScore: meso_direction, dex_slope, vanna_indicator, price_trend_confirm
     - IVScore: iv_consensus, iv_rv_spread, term_kink, skew_25d, event_premium
   - 输出 FieldScores

3. 编写 tests/test_field_calculator.py:
   - 用 mock MicroSnapshot 测试每个 Score 的边界:
     - GammaScore: 全零 GEX → 低分, 高集中度 → 高分
     - DirectionScore: 强 bullish meso + bullish DEX → 高正分
     - IVScore: 高 IVR + 事件溢价 → 高分
```

### Task 2.4: 场景分析器 (Step 5)

**Config**: Model=`opus` | Effort=`high` | Thinking=`extended`

```
阅读 docs/design-doc.md 第 7 节 (场景分析) 完整内容。

实现 engine/steps/s05_scenario_analyzer.py:

1. 完全按 7.1 节的规则引擎实现 analyze_scenario():
   - Rule 1: Trend 条件
   - Rule 2: Range 条件
   - Rule 3: Transition 条件 (两个子规则)
   - Rule 4: Volatility Mean Reversion 条件
   - Rule 5: Event Volatility 条件
   - 无匹配 → 默认 range

2. 每个规则输出完整的 invalidate_conditions 列表

3. 多候选时取 confidence 最高的

4. 编写 tests/test_scenario_analyzer.py:
   - 5 个测试用例，每个场景一个
   - 验证 scenario 标签和 invalidate_conditions 的正确性
   - 测试边界：多规则同时满足时选最高 confidence
```

### Task 2.5: 策略计算引擎 (Step 6)

**Config**: Model=`opus` | Effort=`high` | Thinking=`extended`

```
阅读 docs/design-doc.md 第 8 节 (策略计算引擎)。

实现 engine/steps/s06_strategy_calculator.py:

1. 读取 config/strategies.yaml 的策略族映射

2. 实现 select_strike_by_delta() (按 8.3 节)

3. 为每种策略类型实现构建函数:
   - build_bull_call_spread(strikes_df, spot, expiry, ...)
   - build_bear_put_spread(...)
   - build_iron_condor(...)
   - build_iron_butterfly(...)
   - build_long_straddle(...)
   - build_short_straddle(...)
   - build_calendar_spread(...)
   每个函数:
   - 用 select_strike_by_delta 选择 strike
   - 从 StrikesFrame 直接读取 ORATS 数据:
     premium → callValue / putValue (SMV 理论价)
     iv → smvVol (SMV 拟合 IV)
     delta/gamma/theta/vega → 对应列 (SMV Greeks)
     不自行用 BS 公式计算任何 premium 或 Greeks
   - 从 MicroSnapshot 构建 SMVSurface，传给 compute_payoff
   - 返回 StrategyCandidate

4. 注意: get_strikes 调用时需使用 STRATEGY_FIELDS (包含 callValue,
   putValue, smvVol 等字段)。MicroClient.fetch_micro_snapshot 的
   Phase 1 combined_fields 需要扩展。

5. 实现主入口:
   async def calculate_strategies(scenario, micro, pre_calc) -> list[StrategyCandidate]

6. 编写 tests/test_strategy_calculator.py:
   - 用 mock StrikesFrame (含 callValue/putValue/smvVol 列) 测试:
     - Bull Call Spread 的 legs 正确性
     - Iron Condor 的 4 条 legs 排列
     - OI 过滤: strike OI < 500 时跳过
     - 验证 leg.premium 来自 callValue/putValue 而非 BSM 计算
```

### Task 2.6: 风险分级 + 排序 (Steps 7-8)

**Config**: Model=`sonnet` | Effort=`medium` | Thinking=`standard`

```
阅读 docs/design-doc.md 第 9 节 (风险偏好) 和第 10 节 (排序)。

1. 实现 engine/steps/s07_risk_profiler.py:
   - assign_risk_profile() 按 9.1 节规则
   - aggressive / balanced / conservative 标签

2. 实现 engine/steps/s08_strategy_ranker.py:
   - hard_filter() 按 10.1 节
   - compute_total_score() 按 10.1 节的 6 项加权公式
   - rank_strategies(candidates, scenario, micro) -> top N

3. 编写 tests/test_strategy_ranker.py:
   - 测试硬过滤: OI 不足被排除
   - 测试排序: 高 EV + 高流动性排前面
   - 测试 Top 3 截断
```

---

## Phase 2.R: Retrofit — 定价模型升级

> 以下任务用于回溯修改已完成的 Task 1.1 / 1.2 / 1.3 / 2.3 代码。
> 原因: 定价模型从纯 BSM（恒定 σ）升级为 ORATS SMV 曲面感知定价。
> **必须在继续 Task 2.5 之前完成。**

### Task R.1: 定价模块重构 (bsm.py → pricing.py)

**Config**: Model=`opus` | Effort=`high` | Thinking=`extended`

```
阅读 docs/design-doc.md 第 11 节（SMV 曲面定价与 Payoff 引擎）完整内容。

这是一次对已完成代码的 Retrofit 变更。

1. 重命名 engine/core/bsm.py → engine/core/pricing.py:
   - 保留 bsm_price() 但重命名为 bs_formula()
   - docstring 标注: "底层封闭解公式，sigma 参数由 SMVSurface 提供，非常数"
   - 删除 bsm_greeks() 函数（单 leg Greeks 全部来自 ORATS，不自行计算）
   - 新增 class SMVSurface:
     - __init__(self, monies_df, strikes_df, spot)
       从 ORATS vol0-vol100 构建 2D 插值网格 (RectBivariateSpline)
       从 StrikesFrame 构建 strike → delta 查找表
     - get_iv(self, strike, dte, spot) -> float
     - get_iv_at_delta(self, delta, dte) -> float
     - 完全按 design-doc 11.1 节实现
   - 新增 surface_greeks() 函数（有限差分 + 曲面查询）

2. 更新 engine/core/payoff_engine.py:
   - compute_payoff() 新增参数 smv_surface: SMVSurface
   - current_pnl 每个点调用 smv_surface.get_iv(strike, dte, price) 取 IV
   - 删除 _estimate_pop()，替换为 _estimate_pop_from_surface()
     使用 Breeden-Litzenberger 方法 (call 价格二阶导 → 风险中性密度)
   - 新增 recalc_payoff_with_sliders() 接收 smv_surface

3. 新增 engine/core/greeks.py (若不存在):
   - composite_greeks(legs) → 线性加总 ORATS Greeks
   - compute_pnl_attribution() → P/L 归因

4. 更新 engine/models/strategy.py:
   - StrategyLeg 字段注释更新:
     premium → "ORATS callValue/putValue (SMV 理论价)"
     iv → "ORATS smvVol (SMV 拟合 IV)"
     delta/gamma/theta/vega → "ORATS SMV Greeks"

5. 更新 engine/providers/micro_client.py:
   - Phase 1 的 combined_fields 扩展，新增:
     callValue, putValue, smvVol, callMidIv, putMidIv,
     callBidPrice, callAskPrice, putBidPrice, putAskPrice,
     theta, vega
   - 确保 MicroSnapshot 中 monies 和 strikes_combined 包含足够字段
     以构建 SMVSurface

6. 修复所有 import 路径:
   - from engine.core.bsm import bsm_price → from engine.core.pricing import bs_formula
   - 搜索整个 engine/ 目录确保无遗漏

7. 更新测试:
   - 重命名 tests/test_bsm.py → tests/test_pricing.py
   - 新增 SMVSurface 测试:
     - flat IV 曲面: get_iv 返回常数
     - skewed 曲面: OTM put 的 IV > ATM IV
     - 单 expiry 退化处理
   - 更新 tests/test_payoff.py:
     - 构造 mock SMVSurface fixture
     - 验证 skewed 曲面下 POP < flat 曲面 POP (OTM put spread)
   - 确保 tests/test_field_calculator.py 无 BSM 相关 import

确保所有测试通过后提交:
git commit -m "refactor(engine): Task R.1 - BSM → SMV 曲面感知定价

- pricing.py: SMVSurface + bs_formula + surface_greeks
- payoff_engine: 曲面感知 current_pnl + Breeden-Litzenberger POP
- greeks.py: 组合 Greeks 聚合 + P/L 归因
- micro_client: 扩展 combined_fields
- 删除 bsm_greeks, 所有单 leg Greeks 来自 ORATS"
```

---

## Phase 3: 流水线编排

### Task 3.1: Pipeline 主流程

**Config**: Model=`opus` | Effort=`high` | Thinking=`extended`

```
阅读 docs/design-doc.md 第 1.1 节 (流程总览)。

实现 engine/pipeline.py:

1. class AnalysisPipeline:
   - __init__(self, config): 初始化所有 client 和 step 实例
   - async def run_full(self, symbol: str, trade_date: date) -> AnalysisResultSnapshot:
     按顺序执行 Step 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9
     每个 Step 的输出传给下一个 Step
     最终生成 AnalysisResultSnapshot 并写入数据库

2. 错误处理:
   - 任何 Step 失败时记录日志并返回部分结果
   - Gate 被 skip 时直接返回空结果

3. 实现 Step 9 报告构建:
   - 汇总所有 Step 输出
   - 为每个 Top 3 策略计算 payoff 数据
   - 构建 MarketParameterSnapshot (基线快照)
   - 写入数据库

4. 编写 tests/test_pipeline.py:
   - 用完整 mock 数据跑通全流程
   - 验证最终输出的 AnalysisResultSnapshot 包含:
     - 4 个 Score
     - 场景标签
     - Top 3 策略 (每个含 payoff 数据)
```

### Task 3.2: FastAPI 应用入口

**Config**: Model=`sonnet` | Effort=`medium` | Thinking=`standard`

```
阅读 docs/design-doc.md 第 15 节 (后端 API)。

实现 engine/main.py:

1. FastAPI app 初始化，CORS 中间件

2. 注册路由:
   - engine/api/routes_analysis.py (第 15.1 节)
   - engine/api/routes_monitor.py (第 15.2 节)
   - engine/api/routes_positions.py (CRUD for tracked_positions)

3. 实现 3 个 analysis 端点:
   - POST /api/v2/analysis/{symbol}
   - GET /api/v2/analysis/{analysis_id}
   - GET /api/v2/analysis/{analysis_id}/payoff/{strategy_index}

4. 实现 4 个 monitor 端点:
   - GET /api/v2/market/{symbol}/snapshot
   - GET /api/v2/market/{symbol}/history?hours=4
   - GET /api/v2/monitor/{symbol}/state
   - GET /api/v2/monitor/{symbol}/alerts?limit=50

5. 实现 health check: GET /health

6. 启动脚本: Alembic upgrade head + uvicorn

确保 uvicorn 可正常启动且所有端点返回正确的空/mock 响应。
```

---

## Phase 4: 监控引擎

### Task 4.1: 快照采集器

**Config**: Model=`sonnet` | Effort=`medium` | Thinking=`standard`

```
阅读 docs/design-doc.md 第 13.1 节 (三层快照)。

实现 engine/monitor/snapshot_collector.py:

1. class SnapshotCollector:
   - async def collect_market_snapshot(symbol) -> MarketParameterSnapshot:
     调用 Micro-Provider 获取最新数据
     计算所有市场参数字段
     写入 market_parameter_snapshots 表

   - def compute_drift(current, baseline) -> dict:
     计算 spot_drift_pct, iv_drift_pct 等偏移度

2. 快照保留策略:
   - 30 天内按 5 分钟粒度保留
   - 超过 30 天的按日聚合

3. 编写测试: 验证快照写入和偏移度计算
```

### Task 4.2: 告警引擎

**Config**: Model=`opus` | Effort=`high` | Thinking=`extended`

```
阅读 docs/design-doc.md 第 14 节完整内容 (监控指标体系)。

实现 engine/monitor/alert_engine.py:

1. class AlertEngine 按 14.2 节完整实现:
   - 读取 thresholds.yaml
   - evaluate() 方法: 逐指标评估 3 个 Tier
   - 返回 alerts 列表 + 最高优先级 recalc_action

2. Tier 1 评估: 8 个市场参数指标
3. Tier 2 评估: 4 个分析有效性指标
4. Tier 3 评估: 8 个策略健康度指标

5. 重算动作映射: 选择 step 数字最小的红色告警

6. 编写 tests/test_alert_engine.py:
   - 测试每个 Tier 的黄色/红色触发
   - 测试 recalc_action 优先级选择
   - 测试无告警时返回 green
```

### Task 4.3: 增量重算 + WebSocket

**Config**: Model=`opus` | Effort=`high` | Thinking=`extended`

```
阅读 docs/design-doc.md 第 14.3 节 (增量重算) 和第 15.3 节 (WebSocket)。

1. 实现 engine/monitor/incremental_recalc.py:
   - class IncrementalRecalculator
   - recalc_from(step, symbol, cached_*) 方法
   - 从指定 Step 开始重跑，复用之前的缓存结果

2. 实现 engine/monitor/websocket_hub.py:
   - 管理 WebSocket 连接池
   - broadcast_spot_update(symbol, spot, timestamp)
   - broadcast_pnl_update(position_id, pnl, greeks)
   - broadcast_alert(alert_event)

3. 实现 engine/api/websocket_handler.py:
   - @router.websocket("/ws/v2/live/{symbol}")
   - 连接管理 (accept/disconnect)
   - 消息推送 (spot/pnl/alert)

4. 实现监控循环 (background task):
   - 每 refresh_interval_seconds 执行一次:
     a. collect_market_snapshot
     b. alert_engine.evaluate
     c. 若有红色告警 → incremental_recalc
     d. broadcast 结果

5. 编写测试: 验证增量重算的正确性 (从 step 4 重算不触发 step 2-3)
```

---

## Phase 5: 富途数据接入

### Task 5.1: 富途 Client

**Config**: Model=`sonnet` | Effort=`medium` | Thinking=`standard`

```
阅读 docs/design-doc.md 第 12 节 (富途数据接入)。

1. 实现 engine/providers/futu_client.py:
   - class FutuClient 按 12.1 节
   - get_option_chain(symbol, start_date, end_date)
   - get_realtime_quotes(option_codes)
   - 异常处理: 连接失败时返回空结果 (降级)

2. 实现 LiveQuoteEnricher (12.2 节):
   - enrich(strategies, symbol) → 填充 bid/ask
   - _build_futu_option_code(): 将内部格式转为富途代码格式

3. 在 pipeline.py 中集成:
   - Step 8 排序前调用 enricher
   - futu.enabled=false 时跳过

4. 编写测试:
   - mock FutuClient 验证 enricher 逻辑
   - 验证富途不可用时降级正常工作
```

---

## Phase 6: 集成测试与调优

### Task 6.1: 端到端集成测试

**Config**: Model=`opus` | Effort=`high` | Thinking=`extended`

```
创建 apps/engine/tests/test_e2e.py:

1. 使用 fixture 数据 (mock ORATS 响应) 模拟完整流程:
   - 为 AAPL 创建 mock StrikesFrame / MoniesFrame / SummaryRecord
   - 运行 pipeline.run_full("AAPL", date(2026, 4, 8))
   - 验证输出完整性

2. 使用 FastAPI TestClient 测试 API 端点:
   - POST /api/v2/analysis/AAPL → 得到 analysis_id
   - GET /api/v2/analysis/{id} → 验证包含 strategies
   - GET /api/v2/analysis/{id}/payoff/0 → 验证 payoff 数据
   - POST /api/v2/analysis/{id}/payoff/0/recalc → 验证 slider 重算
   - GET /api/v2/monitor/AAPL/state → 验证监控状态

3. 验证监控循环:
   - 模拟 spot 偏移 3% → 触发红色告警
   - 验证 incremental_recalc 从 step 4 重算
   - 验证新的 AnalysisResultSnapshot 被创建
```

### Task 6.2: 启动脚本

**Config**: Model=`sonnet` | Effort=`low` | Thinking=`standard`

```
创建 apps/engine/start.sh:
- 自动运行 alembic upgrade head
- 启动 FastAPI (uvicorn) on port 18001
- 日志写入 .run/ 目录
- Ctrl+C 停止进程

参考现有的 start.sh 模式。
```

---

## 任务依赖关系

```
Phase 0 (骨架)
  └── Task 0.1 (engine 骨架)

Phase 1 (核心计算) — 依赖 Phase 0
  ├── Task 1.1 (SMV 曲面定价)
  ├── Task 1.2 (Payoff + greeks) → 依赖 1.1
  └── Task 1.3 (数据模型)

Phase 2 (分析步骤) — 依赖 Phase 1
  ├── Task 2.1 (Regime Gating) → 依赖 1.3
  ├── Task 2.2 (Pre-Calculator) → 依赖 2.1
  ├── Task 2.3 (Field Calculator) → 依赖 2.2, 1.3
  └── Task 2.4 (场景分析) → 依赖 2.3

Phase 2.R (Retrofit) — Task 2.4 完成后、Task 2.5 之前
  └── Task R.1 (BSM→SMV 重构) → 回溯修改 1.1, 1.2, 1.3, 2.3

Phase 2 续 — 依赖 R.1
  ├── Task 2.5 (策略计算) → 依赖 R.1, 2.4
  └── Task 2.6 (排序) → 依赖 2.5

Phase 3 (编排) — 依赖 Phase 2
  ├── Task 3.1 (Pipeline) → 依赖 2.1-2.6
  └── Task 3.2 (API) → 依赖 3.1

Phase 4 (监控) — 依赖 Phase 3
  ├── Task 4.1 (快照) → 依赖 3.2
  ├── Task 4.2 (告警) → 依赖 4.1
  └── Task 4.3 (重算+WS) → 依赖 4.2

Phase 5 (富途) — 可与 Phase 4 并行
  └── Task 5.1 → 依赖 3.1

Phase 6 (集成) — 依赖所有
  ├── Task 6.1 (E2E 测试)
  └── Task 6.2 (启动脚本)
```

---

## 执行顺序建议

按以下顺序逐个提交 Task 到 Claude Code:

```
已完成: 0.1 → 1.1 → 1.2 → 1.3 → 2.1 → 2.2 → 2.3 → 2.4
                              ↓
当前:   R.1 (Retrofit: BSM → SMV 曲面定价)
                              ↓
继续:   2.5 → 2.6 → 3.1 → 3.2 → 4.1 → 4.2 → 4.3 → 5.1 → 6.1 → 6.2
```

每个 Task 完成后验证测试通过再进入下一个。
