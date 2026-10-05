#!/usr/bin/env python3
"""IDX Institutional Foreign Ownership Tracker — CLI entry point.

Usage:
  python main.py                         interactive dashboard (default)
  python main.py ingest [--source sec|nbim|all] [--managers vanguard blackrock] [--refresh]
  python main.py dashboard [--quarter 2025Q2 | --date 2025-05-15]
  python main.py report    [--quarter 2025Q2 | --date 2025-05-15 | --all] [--format png pdf] [--theme light|dark]
  python main.py status
  python main.py web       [--port 8050] [--no-open]   local web dashboard at http://localhost:8050
  python main.py export-parquet
  python main.py demo      synthetic 2020-2026 dataset in a separate DB, then dashboard
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

from rich.logging import RichHandler

import config
import storage
from analytics.tracker_engine import TrackerEngine, quarter_label
from visualization import chart_exporter, terminal_ui

console = terminal_ui.console
log = logging.getLogger("indo_tracker")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING, format="%(message)s",
                        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)])


def _engine(db: Path | None) -> TrackerEngine:
    return TrackerEngine(db_path=db)


def cmd_ingest(source: str = "all", managers: list[str] | None = None, refresh: bool = False) -> None:
    from scrapers import nbim_scraper, sec_edgar

    unknown = set(managers or []) - set(config.MANAGERS_BY_KEY)
    if unknown:
        raise SystemExit(f"Unknown manager key(s): {', '.join(sorted(unknown))}. "
                         f"Valid: {', '.join(config.MANAGERS_BY_KEY)}")
    results: dict[str, int] = {}
    with console.status("Ingesting…") as status:
        progress = lambda msg: status.update(f"[bold]Ingesting[/] {msg}")  # noqa: E731
        if source in ("sec", "all"):
            sec_keys = [k for k in (managers or []) if config.MANAGERS_BY_KEY[k].source == "SEC_NPORT"]
            if not managers or sec_keys:
                results |= sec_edgar.ingest_all(sec_keys or None, refresh=refresh, progress=progress)
        if source in ("nbim", "all") and (not managers or "nbim" in managers):
            results |= nbim_scraper.ingest_all(refresh=refresh, progress=progress)
    for fund, n in results.items():
        console.print(f"  {fund:<6} " + ("[red]failed (see log)[/]" if n < 0 else f"{n:,} rows"))


def _resolve_quarter(engine: TrackerEngine, quarter: str | None, on_date: str | None) -> str:
    available = engine.available_quarters()
    if not available:
        raise SystemExit("No data. Run `python main.py ingest` (or `python main.py demo`).")
    if on_date:
        quarter = quarter_label(date.fromisoformat(on_date))
    quarter = (quarter or engine.latest_complete_quarter()).upper()
    if quarter not in available:
        raise SystemExit(f"No data for {quarter}. Available: {available[0]} … {available[-1]}")
    return quarter


def cmd_report(engine: TrackerEngine, quarter: str | None, on_date: str | None, all_q: bool,
               formats: list[str], theme: str, out_dir: Path) -> None:
    targets = engine.available_quarters() if all_q else [_resolve_quarter(engine, quarter, on_date)]
    for q in targets:
        with console.status(f"Rendering {q}…"):
            paths = chart_exporter.render_report(engine.build_report(q), out_dir, formats, theme)
        for p in paths:
            console.print(f"[green]✓[/] {p}")


def run_demo(db: Path) -> Path:
    from tests.test_mock_pipeline import build_synthetic_holdings

    if db.exists():
        db.unlink()
    storage.upsert_holdings(build_synthetic_holdings().to_dict("records"), db_path=db)
    console.print(f"[green]Synthetic 2020–2026 dataset written to[/] {db}")
    return db


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Track institutional foreign ownership of IDX equities (2020–2026).")
    p.add_argument("--db", type=Path, default=None, help=f"SQLite path (default {config.DB_PATH})")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    ing = sub.add_parser("ingest", help="Download SEC N-PORT and NBIM holdings")
    ing.add_argument("--source", choices=["sec", "nbim", "all"], default="all")
    ing.add_argument("--managers", nargs="+", metavar="KEY", help=f"subset of: {' '.join(config.MANAGERS_BY_KEY)}")
    ing.add_argument("--refresh", action="store_true", help="re-download indexes and re-parse filings")

    for name in ("dashboard", "report"):
        sp = sub.add_parser(name)
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--quarter", help="e.g. 2025Q2")
        g.add_argument("--date", help="any date; mapped to its calendar quarter")
        if name == "report":
            g.add_argument("--all", action="store_true", help="render every available quarter")
            sp.add_argument("--format", nargs="+", choices=["png", "pdf"], default=["png", "pdf"])
            sp.add_argument("--theme", choices=["light", "dark"], default="light")
            sp.add_argument("--out", type=Path, default=config.REPORTS_DIR)

    web = sub.add_parser("web", help="Serve the interactive web dashboard on localhost")
    web.add_argument("--port", type=int, default=8050)
    web.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to allow other devices on your network")
    web.add_argument("--no-open", action="store_true", help="don't open a browser tab")

    sub.add_parser("status", help="Show data coverage per fund")
    sub.add_parser("export-parquet", help="Write data/processed/holdings.parquet")
    sub.add_parser("demo", help="Load synthetic data into a separate DB and open the dashboard")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    db = args.db
    os.environ.setdefault("SEC_USER_AGENT", config.SEC_USER_AGENT)

    try:
        if args.cmd == "ingest":
            cmd_ingest(args.source, args.managers, args.refresh)
        elif args.cmd == "report":
            cmd_report(_engine(db), args.quarter, args.date, args.all, args.format, args.theme, args.out)
        elif args.cmd == "dashboard":
            engine = _engine(db)
            terminal_ui.render_dashboard(engine.build_report(_resolve_quarter(engine, args.quarter, args.date)))
        elif args.cmd == "web":
            from web.server import serve

            serve(args.host, args.port, db, open_browser=not args.no_open)
        elif args.cmd == "status":
            terminal_ui.render_status(_engine(db))
        elif args.cmd == "export-parquet":
            console.print(f"[green]✓[/] {storage.export_parquet(db)}")
        else:  # interactive (default) or demo
            if args.cmd == "demo":
                db = run_demo(config.PROCESSED_DIR / "demo_holdings.db")
            terminal_ui.run_interactive(
                engine_factory=lambda: _engine(db),
                ingest=lambda: cmd_ingest(),
                export=lambda r, fmts, theme: chart_exporter.render_report(r, config.REPORTS_DIR, fmts, theme))
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/]")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
