"""
cli_fetch_symbols.py — CLI Command 1: Fetch Symbols from MESO API

职责: 从 MESO API 批量获取指定日期的标的列表，并将结果保存为快照文件。
依赖: engine.providers.meso_client, engine.models.batch_snapshot, engine.config.engine.yaml
被依赖: cli_run_micro (via --auto-run)

Usage:
    cd apps/engine && python -m cli_fetch_symbols [-d YYYY-MM-DD] [options]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("engine.cli.fetch_symbols")

_CONFIG_PATH = Path(__file__).parent / "engine" / "config" / "engine.yaml"
_DEFAULT_OUTPUT_DIR = Path(__file__).parent / "data" / "snapshots"
_SEPARATOR = "\u2550" * 44


def _expand_env_vars(text: str) -> str:
    return re.sub(
        r"\$\{([^}]+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        text,
    )


def _load_config(path: Path = _CONFIG_PATH) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    return yaml.safe_load(_expand_env_vars(raw))


async def _discover_symbols(
    meso_client: Any,
    trade_date: date,
) -> tuple[list[str], str, list[dict] | None]:
    """
    尝试三种策略获取标的列表。

    返回 (symbols, source, chart_points_or_none)。
    """
    from engine.providers.meso_client import MesoClientError

    # Strategy 1: /symbols endpoint
    try:
        symbols = await meso_client.get_symbols(trade_date)
        if symbols:
            return symbols, "meso_symbols", None
    except MesoClientError:
        logger.debug("symbols endpoint failed, trying chart-points fallback")

    # Strategy 2: /chart-points — extract symbols from raw data
    try:
        chart_points = await meso_client.get_chart_points(trade_date)
        if chart_points:
            seen: set[str] = set()
            symbols = []
            for item in chart_points:
                sym = item.get("symbol") if isinstance(item, dict) else None
                if sym and str(sym) not in seen:
                    seen.add(str(sym))
                    symbols.append(str(sym))
            if symbols:
                return symbols, "meso_chart_points", chart_points
    except MesoClientError:
        logger.debug("chart-points endpoint failed, trying date-groups fallback")

    # Strategy 3: get latest available date and retry
    try:
        latest = await meso_client.get_latest_trade_date()
        if latest and latest != trade_date:
            logger.info(
                "No data for %s, retrying with latest available date %s",
                trade_date,
                latest,
            )
            try:
                symbols = await meso_client.get_symbols(latest)
                if symbols:
                    return symbols, "meso_date_groups_fallback", None
            except MesoClientError:
                pass

            try:
                chart_points = await meso_client.get_chart_points(latest)
                if chart_points:
                    seen = set()
                    symbols = []
                    for item in chart_points:
                        sym = item.get("symbol") if isinstance(item, dict) else None
                        if sym and str(sym) not in seen:
                            seen.add(str(sym))
                            symbols.append(str(sym))
                    if symbols:
                        return symbols, "meso_date_groups_fallback", chart_points
            except MesoClientError:
                pass
    except MesoClientError:
        logger.debug("date-groups fallback also failed")

    return [], "meso_symbols", None


def _print_symbols_table(
    trade_date: date,
    source: str,
    symbols: list[str],
) -> None:
    print(_SEPARATOR)
    print("MESO Symbol Fetch")
    print(f"Date: {trade_date} (source: {source})")
    print(f"Symbols: {len(symbols)}")
    print(_SEPARATOR)

    cols = 6
    for i in range(0, len(symbols), cols):
        print("  ".join(f"{s:<8}" for s in symbols[i : i + cols]))

    print(_SEPARATOR)


def _save_snapshot(
    snapshot: Any,
    output_dir: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    short_id = snapshot.snapshot_id[:8]
    filename = f"fetch_{snapshot.trade_date}_{short_id}.json"
    path = output_dir / filename

    # Use model_dump with serialization that handles date/datetime
    data = snapshot.model_dump(mode="json")
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return path


async def run(args: argparse.Namespace) -> int:
    """Main async entry point. Returns exit code."""
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
    meso_base_url: str = meso_cfg.get("base_url", "http://127.0.0.1:18000")
    meso_client = MesoClient(
        base_url=meso_base_url,
        timeout=float(meso_cfg.get("timeout_seconds", 10.0)),
    )

    try:
        symbols, source, chart_points = await _discover_symbols(meso_client, trade_date)
    except MesoClientError as exc:
        print(f"Error: MESO API unreachable — {exc}", file=sys.stderr)
        return 1

    if not symbols:
        print("Error: No symbols found for any available date.", file=sys.stderr)
        return 1

    _print_symbols_table(trade_date, source, symbols)

    if args.dry_run:
        return 0

    from engine.models.batch_snapshot import FetchSymbolsSnapshot

    snapshot = FetchSymbolsSnapshot(
        snapshot_id=str(uuid.uuid4()),
        trade_date=trade_date,
        fetched_at=datetime.now(tz=timezone.utc),
        source=source,
        meso_base_url=meso_base_url,
        symbols=symbols,
        symbol_count=len(symbols),
        chart_points=chart_points,
    )

    output_dir = Path(args.output_dir)
    snapshot_path = _save_snapshot(snapshot, output_dir)
    print(f"Snapshot saved: {snapshot_path}")

    if args.auto_run:
        print("Auto-run enabled, invoking run-micro...")
        try:
            import cli_run_micro  # type: ignore[import]
            return await cli_run_micro.run_from_snapshot(str(snapshot_path))
        except ImportError:
            print(
                "Error: cli_run_micro not found. Run it manually with:",
                file=sys.stderr,
            )
            print(f"  python -m cli_run_micro --snapshot {snapshot_path}", file=sys.stderr)
            return 1

    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Fetch symbols from MESO API and save snapshot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-d", "--date",
        type=str,
        default=None,
        help="Trade date (YYYY-MM-DD). Default: today",
    )
    p.add_argument(
        "--meso-url",
        type=str,
        default=None,
        help="MESO API base URL. Default: from engine.yaml",
    )
    p.add_argument(
        "--output-dir",
        type=str,
        default=str(_DEFAULT_OUTPUT_DIR),
        help="Snapshot output directory. Default: data/snapshots/",
    )
    p.add_argument(
        "--auto-run",
        action="store_true",
        help="After fetching, automatically invoke cli_run_micro",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print discovered symbols, don't save snapshot",
    )
    return p


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    args = _build_parser().parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
