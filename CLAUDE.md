# CLAUDE.md

> 本文件是 Claude Code 的项目级指令，每次会话自动加载。所有开发行为必须遵守以下规则。

---

## 项目概述

Swing & Volatility Quantitative Analysis Engine — 基于期权结构和波动率特征的量化分析系统。

- **设计文档**: `docs/design-doc.md`（所有实现必须以此为唯一事实来源）
- **任务指令**: `docs/task-instructions.md`（按 Task 编号顺序执行）
- **已有代码**: `apps/api/`（Meso 层）、`compute/` + `provider/` + `regime/` + `infra/`（Micro-Provider 层）
- **新增代码**: `apps/engine/`（分析引擎 + 监控后端 API）

---

## 架构约束（不可违反）

### 1. 模块边界

```
apps/engine/engine/
├── models/      → 只放 Pydantic 数据模型，不含业务逻辑
├── steps/       → 每个 Step 一个文件，只暴露一个公共入口函数
├── core/        → 纯计算函数（SMV 曲面定价、Payoff），不依赖外部 IO
├── providers/   → 封装外部数据源调用（Meso API、ORATS、富途）
├── monitor/     → 监控相关（快照采集、告警引擎、增量重算）
├── api/         → FastAPI 路由，只做参数校验和调用编排
├── db/          → SQLAlchemy ORM 和数据库会话
├── config/      → YAML/JSON 配置文件
└── pipeline.py  → 唯一的 Step 编排入口
```

**禁止行为**:
- ❌ 在 `models/` 中写业务逻辑或 IO 调用
- ❌ 在 `core/` 中 import 任何 provider 或 db 模块
- ❌ 在 `api/` 路由中直接写计算逻辑（必须委托给 steps/ 或 pipeline）
- ❌ 在 `steps/` 中直接操作数据库（通过 pipeline 或 repository 模式）
- ❌ 跨层直接 import（如 monitor/ 直接 import steps/ 的内部函数）

### 2. 依赖方向（单向，不可逆）

```
api/ → pipeline → steps/ → core/
                         → providers/
                         → models/
monitor/ → pipeline (用于增量重算)
         → providers/ (用于数据采集)
db/ ← 只被 pipeline, api/, monitor/ 使用
```

### 3. 与已有代码的交互规则

- **Meso 层** (`apps/api/`): 只通过 HTTP API 调用，使用 `providers/meso_client.py` 封装，**不直接 import** Meso 代码
- **Micro-Provider** (`compute/`, `provider/`, `regime/`, `infra/`): 可以直接 import，但只通过 `providers/micro_client.py` 统一编排调用
- **不修改**已有代码，除非 design-doc 明确要求适配

---

## 编码规范（每个文件都必须遵守）

### Python

```python
# 1. 每个文件顶部必须有模块 docstring，说明职责和依赖关系
"""
engine/steps/s04_field_calculator.py — Field Calculator

职责: 从 MicroSnapshot 计算 GammaScore/BreakScore/DirectionScore/IVScore。
依赖: engine.models.micro, engine.models.scores, engine.models.context
被依赖: engine.steps.s05_scenario_analyzer
"""

# 2. 类型注解完整，不使用 Any（除 MicroSnapshot 中包装 pandas 对象）
def compute_gamma_score(micro: MicroSnapshot, spot: float) -> float: ...

# 3. Pydantic 模型统一配置
class FieldScores(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

# 4. 常量定义在文件顶部，使用 UPPER_SNAKE_CASE
GAMMA_SCORE_WEIGHTS = {"net_gexn": 0.30, "wall_concentration": 0.25, ...}

# 5. 错误处理：自定义异常，不裸 raise Exception
class FieldCalculatorError(Exception): ...
```

### 文件大小约束

- **单个 .py 文件不超过 300 行**（含注释和空行）
- 超过 300 行时必须拆分为子模块
- 每个函数不超过 50 行，超过则提取私有辅助函数

### 函数设计

- **单一职责**: 每个公共函数只做一件事
- **纯函数优先**: `core/` 下的函数必须是纯函数（无副作用）
- **依赖注入**: providers 和 config 通过构造函数注入，不使用全局变量
- **异步规范**: IO 操作使用 async/await，计算操作使用同步函数

---

## 测试规范

### 测试必须先于实现验证

每个 Task 提交后，必须运行对应的测试文件并确认通过：

```bash
cd apps/engine
python -m pytest tests/test_xxx.py -v
```

### 测试结构

```python
# 1. 每个 step 文件对应一个 test 文件
# 2. fixture 放在 tests/fixtures/，使用 JSON 文件存储 mock 数据
# 3. mock 外部调用（ORATS API、Meso API），不在测试中发起真实 HTTP 请求
# 4. 边界条件必须覆盖：空数据、None 值、极端值

# 测试命名规范
def test_gamma_score_returns_zero_when_gex_is_empty(): ...
def test_gamma_score_returns_high_when_wall_concentrated(): ...
def test_scenario_selects_trend_when_direction_strong_and_dex_aligned(): ...
```

### 覆盖率要求

- `core/`: 100% 行覆盖（纯计算，无借口）
- `steps/`: ≥ 90% 分支覆盖
- `providers/`: mock 测试，覆盖正常和异常路径
- `api/`: 每个端点至少一个成功和一个错误测试

---

## 配置管理

- **硬编码禁止**: 所有阈值、权重、URL、端口等放入 `config/*.yaml`
- **环境变量**: 敏感信息（API token）通过环境变量注入，配置文件中用 `${VAR_NAME}` 占位
- **配置加载**: 统一通过 `engine/config/loader.py` 加载，不在业务代码中直接读文件

---

## Git 提交规范

每个 Task 完成后提交一次，commit message 格式：

```
feat(engine): Task X.Y - 简要描述

- 实现了什么
- 测试覆盖了什么
```

---

## 禁止行为清单

1. ❌ **不做设计文档未定义的功能** — 不自行发明新 Step、新 Score、新策略类型
2. ❌ **不合并不相关的模块** — 即使两个文件只有 30 行，也不合并到一个文件
3. ❌ **不在 models/ 中写 classmethod 做数据获取** — 模型只是数据容器
4. ❌ **不使用全局可变状态** — 不用模块级别的 `_cache = {}` 等模式（使用依赖注入的 CacheManager）
5. ❌ **不省略类型注解** — 每个函数签名必须完整标注参数和返回类型
6. ❌ **不写超过 3 层的嵌套 if/for** — 提取为独立函数
7. ❌ **不在 API 路由函数中写超过 10 行业务逻辑** — 委托给 pipeline 或 step 函数
8. ❌ **不跳过测试** — 每个 Task 必须包含对应测试且通过
9. ❌ **不修改已有 Meso/Micro-Provider 代码** — 除非 design-doc 明确要求
10. ❌ **不在一个 Task 中实现多个 Task 的内容** — 严格按任务边界执行
11. ❌ **不对任何 strike 使用恒定 IV 定价** — 每个 strike 的 IV 必须从 SMVSurface 查询或直接读取 ORATS smvVol
12. ❌ **不自行计算单 leg Greeks** — delta/gamma/theta/vega 全部使用 ORATS API 返回值，组合 Greeks 仅做线性加总
13. ❌ **不自行计算单 leg 理论价** — premium 使用 ORATS callValue/putValue，bs_formula 仅用于 payoff 曲线的情景投射

---

## 质量检查清单（每个 Task 完成前自检）

- [ ] 文件顶部有 docstring（职责 + 依赖 + 被依赖）
- [ ] 所有公共函数有完整类型注解
- [ ] 单文件不超过 300 行
- [ ] 单函数不超过 50 行
- [ ] 无硬编码的阈值/权重/URL
- [ ] 无跨层违规 import
- [ ] 测试文件存在且全部通过
- [ ] models/ 中无业务逻辑
- [ ] core/ 中无 IO 操作
- [ ] commit message 符合规范
