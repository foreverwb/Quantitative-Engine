"""
batch_analyze.py — Batch symbol analysis CLI

Usage: cd apps/engine && python -m batch_analyze -d 2026-04-15
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("engine.batch")

_CONFIG_PATH = Path(__file__).parent / "engine" / "config" / "engine.yaml"


def _expand_env_vars(text: str) -> str:
    return re.sub(
        r"\$\{([^}]+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        text,
    )


def _load_config(path: Path = _CONFIG_PATH) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    return yaml.safe_load(_expand_env_vars(raw))


async def discover_symbols(
    meso_client: Any, trade_date: date,
) -> list[str]:
    """Try /symbols, fallback to /chart-points, then /date-groups."""
    from engine.providers.meso_client import MesoClientError

    # Strategy 1: dedicated symbols endpoint
    try:
        symbols = await meso_client.get_symbols(trade_date)
        if symbols:
            return symbols
    except MesoClientError:
        logger.debug("symbols endpoint failed, trying chart-points fallback")

    # Strategy 2: extract from chart-points
    try:
        symbols = await meso_client.get_symbols_from_chart_points(trade_date)
        if symbols:
            return symbols
    except MesoClientError:
        logger.debug("chart-points endpoint failed, trying date-groups fallback")

    # Strategy 3: get latest available date and retry
    try:
        latest = await meso_client.get_latest_date()
        if latest and latest != trade_date:
            logger.info(
                "No data for %s, trying latest available date %s",
                trade_date, latest,
            )
            symbols = await meso_client.get_symbols(latest)
            if symbols:
                return symbols
            symbols = await meso_client.get_symbols_from_chart_points(latest)
            if symbols:
                return symbols
    except MesoClientError:
        logger.debug("date-groups fallback also failed")

    return []


def persist_result(
    baseline: Any,
    result: Any,
    db_session: Any,
) -> None:
    """Persist pipeline results to the database."""
    from engine.db.models import (
        AnalysisResultSnapshotRow,
        MarketParameterSnapshotRow,
    )

    mps_row = MarketParameterSnapshotRow(
        snapshot_id=baseline.snapshot_id,
        symbol=baseline.symbol,
        captured_at=baseline.captured_at,
        data_json=baseline.model_dump_json(),
    )
    db_session.merge(mps_row)

    scores_json = json.dumps(
        {
            "gamma_score": result.gamma_score,
            "break_score": result.break_score,
            "direction_score": result.direction_score,
            "iv_score": result.iv_score,
        }
    )
    ars_row = AnalysisResultSnapshotRow(
        analysis_id=result.analysis_id,
        symbol=result.symbol,
        created_at=result.created_at,
        baseline_snapshot_id=result.baseline_snapshot_id,
        scores_json=scores_json,
        scenario=result.scenario,
        scenario_confidence=result.scenario_confidence,
        strategies_json=json.dumps(result.strategies, default=str),
        meso_json=json.dumps(
            {"s_dir": result.meso_s_dir, "s_vol": result.meso_s_vol}
        ) if result.meso_s_dir is not None else None,
    )
    db_session.merge(ars_row)
    db_session.commit()


async def run_batch(args: argparse.Namespace) -> int:
    """Execute batch analysis. Returns exit code."""
    try:
        config = _load_config()
    except FileNotFoundError:
        logger.error("Config not found: %s", _CONFIG_PATH)
        return 1

    if args.meso_url:
        config.setdefault("meso_api", {})["base_url"] = args.meso_url

    if args.date:
        try:
            trade_date = date.fromisoformat(args.date)
        except ValueError:
            logger.error("Invalid date format: %s (expected YYYY-MM-DD)", args.date)
            return 1
    else:
        trade_date = date.today()

    from engine.providers.meso_client import MesoClient, MesoClientError

    meso_cfg = config.get("meso_api", {})
    meso_client = MesoClient(
        base_url=meso_cfg.get("base_url", "http://127.0.0.1:18000"),
        timeout=meso_cfg.get("timeout_seconds", 10.0),
    )

    print(f"Fetching symbols for {trade_date}...")
    try:
        symbols = await discover_symbols(meso_client, trade_date)
    except MesoClientError as exc:
        print(f"Error: MESO API unreachable - {exc}", file=sys.stderr)
        return 1

    if not symbols:
        print("No symbols found. Nothing to analyze.")
        return 0

    if args.max_symbols:
        symbols = symbols[: args.max_symbols]

    print(f"Discovered {len(symbols)} symbols: {', '.join(symbols)}")

    if args.dry_run:
        return 0

    from engine.db.session import init_db
    db_url: str = config.get("database", {}).get("url", "sqlite:///data/engine.db")
    if db_url.startswith("sqlite:///"):
        db_file = Path(db_url.removeprefix("sqlite:///"))
        db_file.parent.mkdir(parents=True, exist_ok=True)
    init_db(db_url)

    try:
        from engine.pipeline import AnalysisPipeline
    except ImportError as exc:
        print(
            f"Error: Cannot import pipeline (missing Micro-Provider deps: {exc})",
            file=sys.stderr,
        )
        return 1

    pipeline = AnalysisPipeline(config)

    from engine.db.session import get_db
    db_gen = get_db()
    db_session = next(db_gen)
    total = len(symbols)
    success_count = 0
    skip_count = 0
    fail_count = 0

    try:
        for i, symbol in enumerate(symbols, 1):
            print(f"[{i}/{total}] Analyzing {symbol}...", end=" ", flush=True)
            try:
                result = await pipeline.run_full(symbol, trade_date)
                if result is None:
                    print(f"\r[{i}/{total}] {symbol} \u2298 skipped (regime gate)")
                    skip_count += 1
                else:
                    baseline, analysis = result
                    persist_result(baseline, analysis, db_session)
                    strat_count = len(analysis.strategies)
                    print(
                        f"\r[{i}/{total}] {symbol} \u2713 "
                        f"scenario={analysis.scenario} "
                        f"confidence={analysis.scenario_confidence:.2f} "
                        f"strategies={strat_count}"
                    )
                    success_count += 1
            except Exception as exc:
                print(f"\r[{i}/{total}] {symbol} \u2717 error: {exc}")
                fail_count += 1
    finally:
        try:
            next(db_gen, None)
        except StopIteration:
            pass
        db_session.close()

    # Print summary
    print()
    print("\u2550" * 43)
    print("  Batch Analysis Summary")
    print(f"  Date: {trade_date}")
    print(
        f"  Total: {total} | "
        f"Success: {success_count} | "
        f"Skipped: {skip_count} | "
        f"Failed: {fail_count}"
    )
    print("\u2550" * 43)

    return 1 if fail_count == total else 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Batch symbol analysis via MESO API")
    p.add_argument("-d", "--date", type=str, default=None,
                   help="Trade date (YYYY-MM-DD), defaults to today")
    p.add_argument("--meso-url", type=str, default=None,
                   help="MESO API base URL, defaults to config value")
    p.add_argument("--dry-run", action="store_true",
                   help="Only fetch and print symbols, don't run analysis")
    p.add_argument("--max-symbols", type=int, default=None,
                   help="Max number of symbols to process (for testing)")
    p.add_argument("--sequential", action="store_true", default=True,
                   help="Run sequentially (default)")
    return p


def main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    sys.exit(asyncio.run(run_batch(args)))


if __name__ == "__main__":
    main()
