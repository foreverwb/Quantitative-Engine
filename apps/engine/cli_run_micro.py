"""
cli_run_micro.py — CLI Command 2: Execute Micro-Provider Quantitative Analysis from Snapshot

职责: 读取 cli_fetch_symbols 生成的快照文件，为每个标的执行完整量化分析流水线。
依赖: engine.pipeline, engine.db.session, engine.db.persist, engine.models.batch_snapshot,
      engine.config.engine.yaml, provider.orats, compute.*, regime.*
被依赖: cli_fetch_symbols (via --auto-run)

Usage:
    cd apps/engine && python -m cli_run_micro --snapshot data/snapshots/fetch_2026-04-15_a1b2c3d4.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("engine.cli.run_micro")

_CONFIG_PATH = Path(__file__).parent / "engine" / "config" / "engine.yaml"
_SEPARATOR = "═" * 39


@dataclass
class BatchResult:
    trade_date: date
    total: int
    success: int
    skipped: int
    failed: int
    failures: list[tuple[str, str]]  # [(symbol, error_message), ...]


def _expand_env_vars(text: str) -> str:
    return re.sub(r"\$\{([^}]+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), text)


def _load_config(path: Path = _CONFIG_PATH) -> dict[str, Any]:
    return yaml.safe_load(_expand_env_vars(path.read_text(encoding="utf-8")))


def _load_snapshot(snapshot_path: str) -> Any:
    from engine.models.batch_snapshot import FetchSymbolsSnapshot

    path = Path(snapshot_path)
    if not path.exists():
        raise FileNotFoundError(f"Snapshot file not found: {snapshot_path}")
    return FetchSymbolsSnapshot.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _verify_micro_provider() -> None:
    """Abort if Micro-Provider modules are not importable."""
    try:
        from provider.orats import OratsProvider  # noqa: F401
        from compute.exposure.calculator import compute_gex  # noqa: F401
        from regime.boundary import classify  # noqa: F401
    except ImportError as e:
        print(f"Micro-Provider modules not available: {e}", file=sys.stderr)
        print("Ensure Micro-Provider is installed and in PYTHONPATH", file=sys.stderr)
        sys.exit(1)


def _verify_orats_token() -> None:
    """Abort if ORATS_API_TOKEN is not set."""
    if not os.environ.get("ORATS_API_TOKEN"):
        print("Error: ORATS_API_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)


def _do_persist(baseline: Any, result: Any) -> None:
    """Persist a single analysis result using the shared persist function."""
    from engine.db.persist import persist_analysis_result
    from engine.db.session import get_db

    gen = get_db()
    db = next(gen)
    try:
        persist_analysis_result(db, baseline, result)
    finally:
        gen.close()


async def run_batch(
    snapshot_path: str,
    config: dict[str, Any],
    max_symbols: int | None = None,
    skip_persist: bool = False,
    symbol_override: list[str] | None = None,
) -> BatchResult:
    """Run micro analysis for all symbols in a snapshot.

    Returns:
        BatchResult with success/skip/fail counts and details
    """
    from engine.pipeline import AnalysisPipeline

    snapshot = _load_snapshot(snapshot_path)
    trade_date: date = snapshot.trade_date
    snapshot_filename = Path(snapshot_path).name

    if symbol_override is not None:
        symbols = [s.strip().upper() for s in symbol_override if s.strip()]
    else:
        symbols = list(snapshot.symbols)

    if max_symbols is not None:
        symbols = symbols[:max_symbols]

    total = len(symbols)
    success_count = 0
    skipped_count = 0
    failed_count = 0
    failures: list[tuple[str, str]] = []

    pipeline = AnalysisPipeline(config)
    batch_start = time.monotonic()

    for idx, symbol in enumerate(symbols, start=1):
        prefix = f"[{idx}/{total}]"
        print(f"{prefix} Analyzing {symbol}...", flush=True)
        sym_start = time.monotonic()

        try:
            result_tuple = await pipeline.run_full(symbol, trade_date)
            elapsed = time.monotonic() - sym_start

            if result_tuple is None:
                skipped_count += 1
                print(f"{prefix} {symbol} \u2298 skipped (regime gate) ({elapsed:.1f}s)")
                continue

            baseline, result = result_tuple
            success_count += 1
            n_strategies = len(result.strategies) if result.strategies else 0
            print(
                f"{prefix} {symbol} \u2713 scenario={result.scenario} "
                f"confidence={result.scenario_confidence:.2f} "
                f"strategies={n_strategies} ({elapsed:.1f}s)"
            )
            if not skip_persist:
                _do_persist(baseline, result)

        except Exception as exc:
            elapsed = time.monotonic() - sym_start
            error_type = type(exc).__name__
            error_msg = str(exc)
            failed_count += 1
            failures.append((symbol, f"{error_type}: {error_msg}"))
            print(f"{prefix} {symbol} \u2717 {error_type}: {error_msg} ({elapsed:.1f}s)", file=sys.stderr)
            logger.error("Pipeline failed for %s: %s", symbol, exc, exc_info=True)

    total_elapsed = time.monotonic() - batch_start

    print(_SEPARATOR)
    print("Micro Analysis Summary")
    print(f"Snapshot: {snapshot_filename}")
    print(f"Date: {trade_date}")
    print(f"Total: {total} | Success: {success_count} | Skipped: {skipped_count} | Failed: {failed_count}")
    print(f"Elapsed: {total_elapsed:.1f}s")
    print(_SEPARATOR)
    if failures:
        print("Failed:")
        for sym, msg in failures:
            print(f"  {sym:<6} \u2014 {msg}")
        print(_SEPARATOR)

    return BatchResult(
        trade_date=trade_date,
        total=total,
        success=success_count,
        skipped=skipped_count,
        failed=failed_count,
        failures=failures,
    )


async def run_from_snapshot(snapshot_path: str) -> int:
    """Programmatic async entry for --auto-run invocation from cli_fetch_symbols.

    Returns exit code: 0 on any success, 1 if all failed.
    """
    try:
        config = _load_config()
    except FileNotFoundError:
        logger.error("Config not found: %s", _CONFIG_PATH)
        return 1

    _verify_orats_token()

    from engine.db.session import init_db
    init_db(config.get("database", {}).get("url", "sqlite:///data/engine.db"))

    result = await run_batch(snapshot_path=snapshot_path, config=config)
    return 0 if (result.success > 0 or result.skipped > 0) else 1


async def run(args: argparse.Namespace) -> int:
    """Main async entry point. Returns exit code."""
    try:
        config = _load_config()
    except FileNotFoundError:
        logger.error("Config not found: %s", _CONFIG_PATH)
        return 1

    _verify_micro_provider()
    _verify_orats_token()

    if not args.skip_persist:
        from engine.db.session import init_db
        init_db(config.get("database", {}).get("url", "sqlite:///data/engine.db"))

    symbol_override: list[str] | None = None
    if args.symbols:
        symbol_override = [s.strip() for s in args.symbols.split(",") if s.strip()]

    result = await run_batch(
        snapshot_path=args.snapshot,
        config=config,
        max_symbols=args.max_symbols,
        skip_persist=args.skip_persist,
        symbol_override=symbol_override,
    )
    return 0 if (result.success > 0 or result.skipped > 0) else 1


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    p = argparse.ArgumentParser(description="Run Micro-Provider quantitative analysis from a snapshot file")
    p.add_argument("--snapshot", required=True, help="Path to snapshot JSON file (from cli_fetch_symbols)")
    p.add_argument("--max-symbols", type=int, default=None, dest="max_symbols", help="Max symbols to process")
    p.add_argument("--sequential", action="store_true", help="Run sequentially (default). Async parallel is future work.")
    p.add_argument("--skip-persist", action="store_true", dest="skip_persist", help="Run analysis but don't persist to database")
    p.add_argument("--symbols", type=str, default=None, help="Comma-separated symbol override (ignore snapshot's symbol list)")
    sys.exit(asyncio.run(run(p.parse_args())))


if __name__ == "__main__":
    main()
