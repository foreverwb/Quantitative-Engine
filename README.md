# Swing & Volatility Quantitative Analysis Engine

## 1. 项目定位

本系统是一个面向美股期权的波段与波动率量化分析引擎。它从 ORATS 获取期权结构数据（Greeks Exposure、IV 曲面、期限结构），从 Meso 层获取方向/波动偏置信号，对指定标的执行 8 步串行分析流水线。输入一个 symbol 和交易日期，输出场景判定（trend/range/transition/volatility\_mean\_reversion/event\_volatility）和最多 3 个可执行的期权策略（含 legs、Greeks、payoff 曲线、盈利概率）。系统还提供后台监控循环，持续对比基线快照与实时数据，在偏移超阈值时触发增量重算和告警。

## 2. 系统架构

**数据源层** 由三个 provider 封装：MesoClient 通过 HTTP 调用 Meso API 获取方向得分（s\_dir）、波动得分（s\_vol）、象限和事件状态；MicroClient 直接 import Micro-Provider 仓库的 OratsProvider 获取 StrikesFrame、MoniesFrame、SummaryRecord 等期权结构数据，并编排 GEX/DEX 计算、期限结构/skew 构建、zero gamma 定位；FutuClient（可选）通过富途 OpenAPI 获取实时 bid/ask 报价。

**计算层** 是一条 Step 2→9 的串行流水线（Step 1 由 Meso 系统完成）：Step 2 构建 RegimeContext 并门控（STRESS+临近事件则跳过）→ Step 3 计算动态参数（窗口百分比、strike 范围、DTE 桶、场景种子）→ Step 4 拉取 micro 数据并计算四维评分（GammaScore/BreakScore/DirectionScore/IVScore）→ Step 5 规则引擎判定场景 → Step 6 按场景-策略映射构建候选策略（含 leg 选取、payoff、POP）→ Step 7 标注风险偏好 → Step 8 硬过滤+六项加权排序 → Step 9 汇总生成报告快照和 payoff 曲线数据。

**存储层** 使用 SQLite，五张表：market\_parameter\_snapshots（市场参数快照）、analysis\_result\_snapshots（分析结果）、monitor\_state\_snapshots（监控状态）、alert\_events（告警事件）、tracked\_positions（跟踪持仓）。通过 Alembic 管理迁移。

**服务层** 是一个 FastAPI 应用（端口 18001），提供分析触发、结果查询、payoff 重算、监控状态、告警日志、持仓管理等 REST 端点。

**监控层** 在 FastAPI lifespan 中启动后台 asyncio task（MonitorLoop），每 5 分钟采集市场快照，经三级告警引擎评估后，若触发红色告警则自动从对应 Step 开始增量重算。

## 3. 核心设计决策

**Q: 为什么不用 BSM 恒定 IV 定价，而用 SMV 曲面插值？**
A: ORATS 的 MoniesFrame 提供了 vol0-vol100 共 21 个 delta 采样点 × 多个 expiry 的无套利 IV 曲面数据。系统从中构建 RectBivariateSpline 二维插值曲面，对任意 (strike, dte) 查询得到该点的 IV。这意味着 OTM put 查到的 IV 高于 ATM（反映 skew），近月查到的 IV 可能高于远月（反映 term structure）。恒定 IV 假设会系统性低估 OTM put 价格、高估 OTM call 价格，导致策略 POP 和 payoff 失真。

**Q: 为什么 Greeks 不自行计算，而直接用 ORATS 返回值？**
A: ORATS 返回的 delta/gamma/theta/vega 已经基于其 SMV 模型校准，内嵌了 skew 和 term structure 信息。自行用 BS 公式计算的 Greeks 使用恒定 IV，无法反映 Vanna（delta 对 vol 的敏感度）和 Volga（vega 对 vol 的敏感度）效应。本系统只在 Slider 假设场景中通过有限差分从曲面计算 Greeks（此时 spot bump 自然带动 IV 变化，隐式包含 Vanna/Volga）。

**Q: 为什么 POP 用 Breeden-Litzenberger 而非 log-normal？**
A: Breeden-Litzenberger 定理从 call 价格的二阶导数提取风险中性密度：p(K) = e^(rT) × d^2C/dK^2。由于 C(K) 使用曲面感知定价（每个 K 查不同的 IV），提取出的密度自然包含 skew 信息——左尾比 log-normal 更厚，右尾更薄。这使得 OTM put spread 的 POP 估算比 log-normal 假设更保守、更贴近实际。

**Q: 为什么场景分析用规则引擎而非纯 LLM？**
A: 规则引擎覆盖约 85% 的常见场景，执行确定性高、延迟低、可审计。每条规则都有量化阈值（如 direction\_score > 60 且 DEX 同向 → trend）和明确的失效条件列表。规则无法覆盖时退化为默认 range 场景（confidence=0.50）。

**Q: 为什么策略排序用 6 项加权而非单一 EV？**
A: 单一 EV 忽略了流动性（低 OI 的 strike 无法实际成交）、tail risk（max\_loss 极端时 EV 为正仍不可执行）、场景适配度（一个 iron condor 在 trend 场景下 EV 可能为正但方向错误）。六项分别衡量场景匹配度（20%）、滑点调整 EV（25%）、尾部风险（15%）、流动性（15%）、theta 效率（10%）和资本效率（15%）。

**Q: 为什么监控采用增量重算而非全量重跑？**
A: 全量重跑（Step 2-9）需要多次 ORATS API 调用和 Meso API 调用，耗时数秒。增量重算根据告警类型判断从哪个 Step 开始——例如 spot 偏移 3% 触发 recalc\_from\_step\_4（只重新拉取 micro 数据和后续计算），IV 漂移触发 recalc\_from\_step\_5（只重新分析场景和策略），复用之前已缓存的中间结果。

## 4. 目录结构

```
apps/engine/
├── engine/
│   ├── __init__.py
│   ├── main.py                        # FastAPI 应用入口，lifespan 中初始化 DB 和 Pipeline
│   ├── pipeline.py                    # Step 2-9 编排器，唯一的流水线入口
│   ├── config/
│   │   ├── engine.yaml                # 引擎主配置（API 地址、利率、策略参数等）
│   │   ├── thresholds.yaml            # 三级监控阈值（spot/IV/GEX 偏移等）
│   │   └── strategies.yaml            # 场景→策略族映射配置
│   ├── models/
│   │   ├── __init__.py
│   │   ├── context.py                 # RegimeContext, MesoSignal, EventInfo
│   │   ├── micro.py                   # MicroSnapshot（期权结构全量快照）
│   │   ├── scores.py                  # FieldScores（四维评分）
│   │   ├── scenario.py                # ScenarioResult（场景判定）
│   │   ├── strategy.py                # StrategyCandidate, StrategyLeg, GreeksComposite
│   │   ├── payoff.py                  # PayoffCurve 相关数据模型
│   │   ├── snapshots.py               # 三层快照模型（Market/Analysis/Monitor）
│   │   └── alerts.py                  # AlertEvent, AlertSeverity
│   ├── steps/
│   │   ├── __init__.py
│   │   ├── s02_regime_gating.py       # Regime 门控：构建上下文、决定是否继续
│   │   ├── s03_pre_calculator.py      # 动态参数计算：窗口、strike 带、DTE 桶
│   │   ├── s04_field_calculator.py    # 四维评分：Gamma/Break/Direction/IV Score
│   │   ├── _s04_dir_iv.py             # Direction 和 IV Score 的子计算模块
│   │   ├── s05_scenario_analyzer.py   # 规则引擎场景判定
│   │   ├── s06_strategy_calculator.py # 策略构建：选 strike、计算 payoff 和 POP
│   │   ├── _s06_builders.py           # 各策略类型的 builder 函数注册表
│   │   ├── _s06_helpers.py            # 策略构建辅助函数
│   │   ├── s07_risk_profiler.py       # 风险偏好标注（aggressive/balanced/conservative）
│   │   ├── s08_strategy_ranker.py     # 硬过滤 + 六项加权排序
│   │   └── s09_report_builder.py      # 汇总报告、构建快照和 payoff 曲线数据
│   ├── core/
│   │   ├── __init__.py
│   │   ├── pricing.py                 # SMVSurface 曲面插值 + BS 封闭解 + 有限差分 Greeks
│   │   ├── payoff_engine.py           # 到期/当前 payoff 计算 + Breeden-Litzenberger POP
│   │   └── greeks.py                  # 组合 Greeks 线性加总 + P/L 归因分解
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── meso_client.py             # Meso REST API 异步客户端
│   │   ├── micro_client.py            # Micro-Provider 编排（ORATS + GEX/DEX/Term/Skew）
│   │   └── futu_client.py             # 富途实时报价（可选）+ LiveQuoteEnricher
│   ├── monitor/
│   │   ├── __init__.py
│   │   ├── snapshot_collector.py      # 市场参数快照采集 + 保留策略清理
│   │   ├── alert_engine.py            # 三级告警评估引擎
│   │   ├── incremental_recalc.py      # 增量重算器（从指定 Step 重跑）
│   │   └── monitor_loop.py            # 后台 asyncio 监控循环
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes_analysis.py         # 分析触发/查询/payoff 端点
│   │   ├── routes_monitor.py          # 市场快照/监控状态/告警日志端点
│   │   └── routes_positions.py        # 持仓 CRUD 端点
│   └── db/
│       ├── __init__.py
│       ├── models.py                  # SQLAlchemy ORM 五张表
│       └── session.py                 # DB 会话工厂
├── alembic/
│   ├── env.py
│   └── versions/
│       └── 5d5e4deb843b_initial_schema.py
├── alembic.ini
├── tests/
│   ├── conftest.py
│   ├── fixtures/                      # mock 数据（JSON）
│   ├── test_regime_gating.py
│   ├── test_pre_calculator.py
│   ├── test_field_calculator.py
│   ├── test_scenario_analyzer.py
│   ├── test_strategy_calculator.py
│   ├── test_strategy_ranker.py
│   ├── test_pricing.py
│   ├── test_payoff.py
│   ├── test_greeks.py
│   ├── test_futu_enricher.py
│   ├── test_alert_engine.py
│   ├── test_snapshot_collector.py
│   ├── test_incremental_recalc.py
│   ├── test_pipeline.py
│   └── test_e2e.py
├── start.sh                           # 启动脚本（Alembic 迁移 + uvicorn）
└── pyproject.toml
```

## 5. 快速启动

### 环境要求

- Python >= 3.11
- 本项目的 Micro-Provider 代码（`compute/`、`provider/`、`regime/`、`infra/` 目录）需在 Python path 中可导入
- 本地运行的 Meso API（默认 `http://127.0.0.1:18000`），或者接受 Meso 不可用时的降级运行

### 依赖安装

```bash
cd apps/engine
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 配置

编辑 `engine/config/engine.yaml`，必须设置：

- `orats.api_token`：通过环境变量 `ORATS_API_TOKEN` 注入，这是 ORATS 数据 API 的访问令牌
- `meso_api.base_url`：Meso API 地址，默认 `http://127.0.0.1:18000`
- `futu.enabled`：是否启用富途实时报价，默认 `false`（无富途网关时保持关闭）
- `database.url`：SQLite 数据库路径，默认 `sqlite:///data/engine.db`

```bash
export ORATS_API_TOKEN="your-orats-token"
```

### 数据库初始化

```bash
cd apps/engine
alembic upgrade head
```

### 启动

```bash
# 生产模式
./start.sh

# 开发模式（hot-reload）
./start.sh --reload
```

服务默认监听 `0.0.0.0:18001`，日志输出到 `.run/engine.log`。

### 验证

```bash
# 健康检查
curl http://localhost:18001/health

# 触发一次分析（以 AAPL 为例）
curl -X POST "http://localhost:18001/api/v2/analysis/AAPL"

# 查询分析结果（使用返回的 analysis_id）
curl "http://localhost:18001/api/v2/analysis/{analysis_id}"
```

## 6. API 速查

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/health` | 服务健康检查 |
| POST | `/api/v2/analysis/{symbol}?trade_date=YYYY-MM-DD` | 触发完整分析，返回 analysis\_id |
| GET | `/api/v2/analysis/{analysis_id}` | 获取分析结果详情 |
| GET | `/api/v2/analysis/{analysis_id}/payoff/{strategy_index}` | 获取策略 payoff 数据 |
| POST | `/api/v2/analysis/{analysis_id}/payoff/{strategy_index}/recalc` | Slider 交互式 payoff 重算 |
| GET | `/api/v2/market/{symbol}/snapshot` | 最新市场参数快照 |
| GET | `/api/v2/market/{symbol}/history?hours=4` | 市场参数历史（迷你趋势图） |
| GET | `/api/v2/monitor/{symbol}/state` | 最新监控状态（含告警颜色） |
| GET | `/api/v2/monitor/{symbol}/alerts?limit=50` | 告警日志 |
| POST | `/api/v2/positions` | 新建跟踪持仓 |
| GET | `/api/v2/positions?symbol=&status=` | 查询持仓列表 |
| GET | `/api/v2/positions/{position_id}` | 获取单个持仓 |
| PATCH | `/api/v2/positions/{position_id}` | 更新持仓状态 |
| DELETE | `/api/v2/positions/{position_id}` | 删除持仓记录 |

## 7. 分析流水线详解

### Step 2: Regime Gating（门控）

- **输入**: symbol (str), trade\_date (date)
- **算法**: 调用 Meso API 获取 MesoSignal（方向/波动/象限/事件状态），调用 ORATS 获取 SummaryRecord 和 IVRankRecord，构建 MarketRegime 后调用 `regime.boundary.classify` 判定 RegimeClass（LOW\_VOL / NORMAL / STRESS）。从 Meso 的 event\_regime 字段推断 earnings 事件，从 event\_calendar.json 查找宏观事件（FOMC/CPI）。若 STRESS 且 days\_to\_event <= 1，门控跳过分析。
- **输出**: RegimeContext（regime\_class, event info, meso\_signal）, gate\_result ("proceed" / "skip")
- **源文件**: `engine/steps/s02_regime_gating.py`

### Step 3: Pre-Calculator（动态参数）

- **输入**: RegimeContext, SummaryRecord, HistSummaryFrame（可选）
- **算法**: 计算 dyn\_window\_pct（取 1.25×expected\_move、ATR20、earnings 历史波动三者最大值，裁剪到 3%-20%），据此计算 dyn\_strike\_band（spot ± window）。根据事件窗口、regime class、Meso 方向/波动信号强度决定 scenario\_seed（event/transition/trend/vol\_mean\_reversion/unknown）和对应的 DTE 分桶范围。
- **输出**: PreCalculatorOutput（dyn\_window\_pct, dyn\_strike\_band, dyn\_dte\_range, dyn\_dte\_ranges, scenario\_seed, spot\_price）
- **源文件**: `engine/steps/s03_pre_calculator.py`

### Step 4: Field Calculator（四维评分）

- **输入**: MicroSnapshot, PreCalculatorOutput, RegimeContext
- **算法**: 从 MicroSnapshot 的 GEX/DEX frame、zero gamma、call/put wall 等数据计算四个核心评分。GammaScore [0,100] 由净 GEX 归一化（30%）+ wall 集中度（25%）+ zero gamma 距离（25%）+ 月度一致性（20%）加权。BreakScore [0,100] 由 wall 距离（35%）+ 隐含/已实现波动比（30%）+ zero gamma 翻转风险（35%）加权。DirectionScore [-100,100] 由 Meso s\_dir（25%）+ DEX 斜率（25%）+ Vanna 指标（25%）+ 价格趋势确认（25%）加权。IVScore [0,100] 由 IV consensus（25%）+ IV-RV spread（20%）+ term kink（15%）+ 25-delta skew（20%）+ 事件溢价（20%）加权。
- **输出**: FieldScores（gamma\_score, break\_score, direction\_score, iv\_score）
- **源文件**: `engine/steps/s04_field_calculator.py`、`engine/steps/_s04_dir_iv.py`

### Step 5: Scenario Analyzer（场景判定）

- **输入**: FieldScores, RegimeContext, MicroSnapshot
- **算法**: 五条规则并行评估，每条产生候选场景和置信度。Rule 1 Trend：direction\_score > 60 且 zero gamma 距离 > 3% 且 DEX 同向 → confidence 0.85。Rule 2 Range：正 GEX 环境 + 双侧 wall 紧密（< 8%）+ 无事件 → 0.80。Rule 3 Transition：zero gamma 距离 < 1.5%，或 Meso 方向/波动信号冲突 → 0.70/0.65。Rule 4 Volatility Mean Reversion：iv\_score > 75 + 无事件 → 0.75。Rule 5 Event Volatility：事件窗口内 + front/back IV 比 > 1.15 → 0.80。取最高 confidence 的候选；无匹配时默认 range（0.50）。
- **输出**: ScenarioResult（scenario, confidence, method, invalidate\_conditions）
- **源文件**: `engine/steps/s05_scenario_analyzer.py`

### Step 6: Strategy Calculator（策略构建）

- **输入**: ScenarioResult, MicroSnapshot, PreCalculatorOutput
- **算法**: 根据 `strategies.yaml` 映射表查找当前场景对应的策略类型列表（如 trend-bullish → bull\_call\_spread / long\_call / short\_put\_spread）。对每种类型，通过 builder 函数从 StrikesFrame 中按目标 delta 选取 strike（OI >= 500），读取 ORATS 返回的 callValue/putValue 作为 premium、smvVol 作为 IV、delta/gamma/theta/vega 直接使用。构建 StrategyCandidate 后，调用 payoff\_engine 计算到期和当前 payoff 曲线、breakeven、POP（Breeden-Litzenberger 方法），调用 greeks.py 计算组合 Greeks。
- **输出**: list[StrategyCandidate]（每个含 legs, net\_credit\_debit, max\_profit, max\_loss, breakevens, pop, ev, greeks\_composite）
- **源文件**: `engine/steps/s06_strategy_calculator.py`、`engine/steps/_s06_builders.py`、`engine/steps/_s06_helpers.py`

### Step 7: Risk Profiler（风险偏好标注）

- **输入**: StrategyCandidate
- **算法**: 根据策略特征分配风险标签。Conservative（保守）：有限风险 + |delta| < 0.20 + short gamma 占比 < 30%。Balanced（均衡）：有限风险（max\_loss 有限且 > 0）。Aggressive（进取）：无裸 short leg。默认 balanced。
- **输出**: StrategyCandidate 附加 risk\_profile 字段
- **源文件**: `engine/steps/s07_risk_profiler.py`

### Step 8: Strategy Ranker（排序）

- **输入**: list[StrategyCandidate], ScenarioResult, MicroSnapshot, top\_n
- **算法**: 先做硬过滤（每条 leg OI >= 500、bid-ask spread < 15%、max\_loss < 50000），再对通过的策略计算 TotalScore = 场景匹配度×0.20 + 滑点调整EV×0.25 + 尾部风险×0.15 + 流动性×0.15 + Theta效率×0.10 + 资本效率×0.15。按 TotalScore 降序取 Top-N。
- **输出**: list[StrategyCandidate]（含 total\_score，最多 top\_n 个）
- **源文件**: `engine/steps/s08_strategy_ranker.py`

### Step 9: Report Builder（报告生成）

- **输入**: RegimeContext, FieldScores, ScenarioResult, top\_strategies, MicroSnapshot, 配置参数
- **算法**: 从 MicroSnapshot 和 context 提取市场参数构建 MarketParameterSnapshot（基线快照），为每个 Top 策略计算完整 payoff 曲线（到期和当前 P/L），汇总生成 AnalysisResultSnapshot（含 scores、scenario、strategies 列表、payoff 数据）。
- **输出**: (MarketParameterSnapshot, AnalysisResultSnapshot)
- **源文件**: `engine/steps/s09_report_builder.py`

## 8. 监控体系

### 三层指标

- **Tier 1 市场参数偏移**: 监控 spot 价格偏移、ATM IV 偏移、zero gamma strike 偏移、期限结构翻转（contango/backwardation 切换）、GEX 符号翻转（正/负 gamma 环境切换）、vol PCR 异常。这些指标反映市场微观结构的实质性变化，触发后需要重新拉取数据。

- **Tier 2 分析有效性**: 监控 score 漂移幅度、方向翻转（net DEX 符号变化）、IV score 变化、场景失效条件触发数量。这些指标反映之前的分析结论是否仍然成立。

- **Tier 3 策略健康度**: 监控已建仓位的 max loss 接近度、delta 漂移、theta 实现率（实际 theta 收入 vs 预期）、breakeven 距离、DTE 剩余天数。这些指标反映持仓的即时风险状态。

### 阈值配置

位于 `engine/config/thresholds.yaml`，每个指标定义 yellow（警告）和 red（严重）两级阈值，以及对应的重算动作。例如 spot\_drift\_pct 的 yellow 为 1.5%、red 为 3.0%，触发 recalc\_from\_step\_4。布尔指标（如 term\_structure\_flip、gex\_sign\_flip）仅有 red 级别。Tier 3 策略指标使用低方向阈值（值越低越危险）。

### 增量重算

当红色告警触发时，MonitorLoop 解析 action 字段获取起始 Step 编号（如 `recalc_from_step_4` → step=4）。IncrementalRecalculator 从该 Step 开始重跑，复用之前缓存的上游中间结果：step=4 复用 RegimeContext 和 PreCalculatorOutput，重新拉取 micro 数据；step=5 复用 micro 数据，重新分析场景；step=6 复用场景，重新计算策略。多个红色告警同时存在时，取 step 最小的（重算范围最大）。

## 9. 定价引擎

### MoniesFrame 与 vol0-vol100

ORATS MoniesFrame 每行代表一个到期日（expiry），包含 21 个 IV 采样点：vol0、vol5、vol10、...、vol100。这里的数字是 delta 坐标——delta=50 约等于 ATM（平值），delta < 50 对应 OTM put 侧（越小越深度虚值），delta > 50 对应 OTM call 侧。每个采样点的值是 ORATS 通过 Smooth Market Volatility 模型拟合后的无套利 IV。

### SMVSurface 曲面构建

SMVSurface 以 MoniesFrame 为基础，将 (dte, delta) → IV 数据网格化，使用 scipy 的 RectBivariateSpline 进行二维插值（dte 方向线性、delta 方向三次）。若只有一个 expiry，退化为 delta 方向的一维三次插值。曲面构建完成后，可以对任意 (strike, dte) 组合查询 IV。

### 为什么每个 strike 的 IV 不同（skew）

查询流程：先将 strike 转换为 delta 坐标（优先从 StrikesFrame 精确查找，退而求其次用最近邻 strike 的 delta，最终用 BS 近似公式），再在 (dte, delta) 曲面上插值。由于曲面沿 delta 方向不是常数（vol25 通常 > vol50 > vol75，即 OTM put IV > ATM IV > OTM call IV），同一 expiry 不同 strike 查到的 IV 不同，这就是 volatility skew。

### Payoff 曲线的两条线

到期 P/L（expiry\_pnl）使用解析解——纯粹的内在价值计算（max(0, S-K) 或 max(0, K-S)），与定价模型无关。当前 P/L（current\_pnl）使用曲面感知定价——对每个采样 spot 价格、每条 leg，从 SMVSurface 查询该 (strike, 当前 dte) 的 IV，代入 BS 公式计算理论价，与入场 premium 的差值即为当前未实现 P/L。当前 P/L 曲线比到期 P/L 更平滑（时间价值的缓冲效应），且在 OTM put 侧因 skew 更高的 IV 而显示更大的亏损。

### Breeden-Litzenberger POP

POP（Probability of Profit，盈利概率）采用 Breeden-Litzenberger 方法从曲面提取风险中性概率密度。具体做法：在 spot 采样区间内，对每个 K 用曲面感知 call 价格 C(K) 做数值二阶导，乘以折现因子 e^(rT) 得到密度 p(K)。然后对 expiry\_pnl > 0 的区间积分 p(K)·dK 得到 POP。由于密度来自含 skew 的曲面，左尾密度比 log-normal 更大，使得涉及下行保护的策略 POP 更保守。

## 10. 配置参考

### engine.yaml

| 字段 | 默认值 | 含义 |
|------|--------|------|
| `meso_api.base_url` | `http://127.0.0.1:18000` | Meso 层 REST API 地址 |
| `meso_api.timeout_seconds` | `10` | Meso API 请求超时（秒） |
| `orats.api_token` | `${ORATS_API_TOKEN}` | ORATS 数据 API 令牌（通过环境变量注入） |
| `orats.base_url` | `https://api.orats.io/datav2` | ORATS API 基础 URL |
| `futu.host` | `127.0.0.1` | 富途 OpenD 网关地址 |
| `futu.port` | `11111` | 富途 OpenD 网关端口 |
| `futu.enabled` | `false` | 是否启用富途实时报价 |
| `engine.risk_free_rate` | `0.05` | 年化无风险利率（用于 BS 定价） |
| `engine.payoff_num_points` | `200` | Payoff 曲线 X 轴采样点数 |
| `engine.payoff_range_pct` | `0.15` | Payoff 曲线 spot 浮动范围（±15%） |
| `engine.top_n_strategies` | `3` | 最终输出的 Top-N 策略数 |
| `engine.min_oi` | `500` | strike 最低 OI 过滤 |
| `engine.max_spread_pct` | `0.15` | bid-ask spread / mid 最大容忍比例 |
| `engine.max_loss_limit` | `50000` | 单策略 max\_loss 绝对值上限（美元） |
| `monitor.refresh_interval_seconds` | `300` | 监控循环间隔（5 分钟） |
| `monitor.websocket_spot_interval_seconds` | `10` | WebSocket spot 推送间隔 |
| `monitor.websocket_pnl_interval_seconds` | `30` | WebSocket P/L 推送间隔 |
| `monitor.snapshot_retention_days` | `30` | 快照保留天数 |
| `database.url` | `sqlite:///data/engine.db` | 数据库连接 URL |

### thresholds.yaml

三级阈值配置。调优建议：

**Tier 1 市场参数**——这些阈值决定数据重拉频率。若 ORATS API 配额紧张，可适当放宽 spot\_drift\_pct 的 red 到 5%；若对实时性要求高（如日内交易），可收紧 yellow 到 1%。gex\_sign\_flip 和 term\_structure\_flip 是布尔指标，只有红色级别，建议保持。

**Tier 2 分析有效性**——score\_drift\_max 控制分析结论的保鲜度。25 分的 red 阈值意味着市场参数偏移达到各自红线的 ~83% 时触发重算。scenario\_invalidation\_count 的 red=2 表示两个失效条件同时触发才重算。

**Tier 3 策略健康度**——max\_loss\_proximity 的 red=0.75 表示当前亏损达到 max\_loss 的 75% 时告警。dte\_remaining 的 red=2 表示距到期仅剩 2 天时告警。这些不触发重算，仅作为持仓风险提醒。

### strategies.yaml

场景→策略族映射。结构为 `strategy_mapping.{scenario}.{sub_key}` → 策略列表。

- `trend.bullish` / `trend.bearish`：按 direction\_score 正负分别映射看涨/看跌策略
- `range`：直接映射中性策略列表
- `transition`：映射到偏斜/跨期策略
- `volatility_mean_reversion`：映射到做空波动率策略
- `event_volatility.pre_event` / `event_volatility.post_event`：按事件前后分别映射

扩展方式：在对应场景下添加新条目即可。每个条目需包含 `type`（策略类型标识符，需在 `_s06_builders.py` 的 BUILDER\_REGISTRY 中注册对应的构建函数）和 `description`（展示名称）。

## 11. 测试

### 运行命令

```bash
cd apps/engine
python -m pytest tests/ -v
```

### 测试文件对应关系

| 测试文件 | 被测模块 |
|----------|----------|
| `test_regime_gating.py` | `engine/steps/s02_regime_gating.py` |
| `test_pre_calculator.py` | `engine/steps/s03_pre_calculator.py` |
| `test_field_calculator.py` | `engine/steps/s04_field_calculator.py` |
| `test_scenario_analyzer.py` | `engine/steps/s05_scenario_analyzer.py` |
| `test_strategy_calculator.py` | `engine/steps/s06_strategy_calculator.py` |
| `test_strategy_ranker.py` | `engine/steps/s08_strategy_ranker.py` |
| `test_pricing.py` | `engine/core/pricing.py` |
| `test_payoff.py` | `engine/core/payoff_engine.py` |
| `test_greeks.py` | `engine/core/greeks.py` |
| `test_futu_enricher.py` | `engine/providers/futu_client.py` |
| `test_alert_engine.py` | `engine/monitor/alert_engine.py` |
| `test_snapshot_collector.py` | `engine/monitor/snapshot_collector.py` |
| `test_incremental_recalc.py` | `engine/monitor/incremental_recalc.py` |
| `test_pipeline.py` | `engine/pipeline.py` |
| `test_e2e.py` | 端到端集成测试（Step 2-9 全流程） |

### Mock 策略

测试中 mock 所有外部 IO：ORATS API 调用（OratsProvider 的 get\_strikes/get\_monies/get\_summary 等方法）、Meso API 调用（MesoClient.get\_signal）、富途 API 调用。原因：ORATS 按调用次数计费，测试中真实调用会产生成本；Meso API 可能不在测试环境中运行；mock 数据可以精确控制边界条件（如空数据、None 值、极端值），确保测试覆盖所有分支。测试 fixture 以 JSON 文件形式存放在 `tests/fixtures/` 目录中。

## 12. 与已有系统的关系

### 与 Meso (apps/api) 的交互

本系统通过 MesoClient（`engine/providers/meso_client.py`）以 HTTP 方式调用 Meso REST API，主要使用 `GET /api/v1/signals/{symbol}` 获取方向/波动信号。不直接 import Meso 代码，不共享数据库表，不修改 Meso 任何代码。Meso 不可用时降级运行（meso\_signal 为 None，相关评分子项归零）。

### 与 Micro-Provider (compute/provider/regime/infra) 的交互

本系统直接 import Micro-Provider 仓库的 Python 模块，通过 MicroClient（`engine/providers/micro_client.py`）统一编排调用。使用的模块包括：`provider.orats.OratsProvider`（ORATS API 封装）、`compute.exposure.calculator`（GEX/DEX 计算）、`compute.volatility.term/skew`（期限结构/skew 构建）、`compute.flow.pcr`（PCR 计算）、`regime.boundary`（Regime 分类）等。

### 不修改已有代码

本系统（`apps/engine/`）是新增目录，不修改 `apps/api/`、`compute/`、`provider/`、`regime/`、`infra/` 中的任何已有代码。
