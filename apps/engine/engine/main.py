"""
engine/main.py — FastAPI 应用入口

职责: 初始化 FastAPI app，配置 CORS 中间件，注册路由，
      在 lifespan 中完成数据库初始化和 Pipeline 实例化。
依赖: engine.api.routes_*, engine.db.session, engine.pipeline
被依赖: uvicorn 启动脚本
"""

from __future__ import annotations

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
    try:
        from engine.pipeline import AnalysisPipeline  # noqa: PLC0415
        pipeline = AnalysisPipeline(config)
        routes_analysis.set_pipeline(pipeline)
        logger.info("AnalysisPipeline initialised")
    except ImportError as exc:
        logger.warning("AnalysisPipeline not available (missing deps: %s); "
                       "POST /analysis will return 503", exc)

    yield

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


app = create_app()
