"""
engine/main.py — FastAPI 应用入口

职责: 初始化 FastAPI app，配置 CORS 中间件，注册路由，
      在 lifespan 中完成数据库初始化和 Pipeline 实例化。
依赖: engine.api.routes_*, engine.db.session, engine.pipeline
被依赖: uvicorn 启动脚本
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from engine.api import routes_analysis, routes_monitor, routes_positions
from engine.db.session import init_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent / "config" / "engine.yaml"


def _expand_env_vars(text: str) -> str:
    """将 ${VAR_NAME} 替换为环境变量值，缺失时保留原占位符。"""
    return re.sub(
        r"\$\{([^}]+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        text,
    )


def _load_config(path: Path = _CONFIG_PATH) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    expanded = _expand_env_vars(raw)
    return yaml.safe_load(expanded)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """启动时初始化 DB + Pipeline；关闭时清理资源。"""
    config = _load_config()

    # 数据库初始化（建表，alembic 方案另行 migrate）
    db_url: str = config.get("database", {}).get("url", "sqlite:///data/engine.db")
    # 确保 SQLite 文件目录存在
    if db_url.startswith("sqlite:///"):
        db_file = Path(db_url.removeprefix("sqlite:///"))
        db_file.parent.mkdir(parents=True, exist_ok=True)
    init_db(db_url)
    logger.info("Database initialised: %s", db_url)

    # Pipeline 实例化并注入路由（延迟导入，避免 compute 模块未安装时启动失败）
    monitor_loop = None
    monitor_task = None
    try:
        from engine.pipeline import AnalysisPipeline  # noqa: PLC0415
        pipeline = AnalysisPipeline(config)
        routes_analysis.set_pipeline(pipeline)
        logger.info("AnalysisPipeline initialised")

        # 监控循环初始化
        monitor_loop, monitor_task = _start_monitor_loop(config, pipeline)
    except ImportError as exc:
        logger.warning("AnalysisPipeline not available (missing deps: %s); "
                       "POST /analysis will return 503", exc)

    yield

    # 优雅停止监控循环
    if monitor_loop is not None and monitor_task is not None:
        monitor_loop.shutdown()
        await monitor_task
        logger.info("MonitorLoop stopped")

    logger.info("Engine shutting down")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(
        title="Swing & Volatility Quantitative Analysis Engine",
        version="0.1.0",
        description="基于期权结构和波动率特征的量化分析后端 API",
        lifespan=lifespan,
    )

    # CORS — 允许本地前端调试；生产环境请收窄 origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册路由
    app.include_router(routes_analysis.router)
    app.include_router(routes_monitor.router)
    app.include_router(routes_positions.router)

    # Health check
    @app.get("/health", tags=["system"], summary="服务健康检查")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _start_monitor_loop(
    config: dict[str, Any],
    pipeline: Any,
) -> tuple[Any, asyncio.Task[None]]:
    """创建并启动监控后台循环，返回 (MonitorLoop, Task)。"""
    from engine.db.session import get_db  # noqa: PLC0415
    from engine.monitor.alert_engine import AlertEngine  # noqa: PLC0415
    from engine.monitor.incremental_recalc import IncrementalRecalculator  # noqa: PLC0415
    from engine.monitor.monitor_loop import MonitorLoop  # noqa: PLC0415
    from engine.monitor.snapshot_collector import SnapshotCollector  # noqa: PLC0415

    monitor_cfg = config.get("monitor", {})
    interval = monitor_cfg.get("refresh_interval_seconds", 300)

    # 加载告警阈值配置
    thresholds_path = Path(__file__).parent / "config" / "thresholds.yaml"
    thresholds: dict[str, Any] = {}
    if thresholds_path.exists():
        raw = thresholds_path.read_text(encoding="utf-8")
        thresholds = yaml.safe_load(raw) or {}

    # db_session_factory: 每次调用返回一个新 Session
    from engine.db.session import _SessionLocal  # noqa: PLC0415
    db_session_factory = _SessionLocal

    collector = SnapshotCollector(
        micro_client=pipeline._micro_client,
        db_session=db_session_factory(),
    )
    alert_engine = AlertEngine(thresholds_config=thresholds)
    recalculator = IncrementalRecalculator(pipeline)

    loop = MonitorLoop(
        refresh_interval=interval,
        snapshot_collector=collector,
        alert_engine=alert_engine,
        recalculator=recalculator,
        db_session_factory=db_session_factory,
    )

    task: asyncio.Task[None] = asyncio.create_task(loop.run())
    logger.info("MonitorLoop started (interval=%ds)", interval)
    return loop, task


app = create_app()
