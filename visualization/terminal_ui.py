"""Interactive terminal dashboard (Rich rendering + Questionary navigation)."""
from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Callable

import pandas as pd
import questionary
from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

import config
from analytics.tracker_engine import QuarterReport, TrackerEngine, quarter_label
from visualization.chart_exporter import fmt_pct, fmt_usd

console = Console()
SPARK = "▁▂▃▄▅▆▇█"
QSTYLE = questionary.Style([("qmark", "fg:#3987e5 bold"), ("pointer", "fg:#3987e5 bold"),
                            ("highlighted", "fg:#3987e5 bold"), ("selected", "fg:#1baf7a")])


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #
def _nan(x: object) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def delta_text(x: float | None, kind: str = "pct") -> Text:
    if _nan(x):
        return Text("—", style="dim")
    s = fmt_pct(x) if kind == "pct" else ("+" if x >= 0 else "") + fmt_usd(x)
    return Text(("▲ " if x >= 0 else "▼ ") + s, style="green" if x >= 0 else "red")


def sparkline(values: list[float]) -> str:
    vals = [v for v in values if not _nan(v)]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1.0
    return "".join(SPARK[min(7, int((v - lo) / span * 7))] if not _nan(v) else " " for v in values)


def bar(value: float, vmax: float, width: int = 24) -> str:
    if vmax <= 0 or _nan(value):
        return ""
    n = value / vmax * width
    return "█" * int(n) + ("▌" if n - int(n) >= 0.5 else "")


def kpi_panel(report: QuarterReport) -> Panel | Group:
    mt = report.manager_table
    tiles = [
        ("Tracked IDX exposure", Text(fmt_usd(report.total_idx_usd), style="bold white")),
        ("QoQ (like-for-like)", Text.assemble(
            delta_text(report.qoq_delta_usd, "usd"), "  ", delta_text(report.qoq_delta_pct))),
        ("Managers reporting", Text(f"{len(mt)} / {report.n_managers_scope}"
                                    + (f"  ({int(mt['stale'].sum())} carried fwd)" if len(mt) and mt["stale"].any() else ""),
                                    style="bold white")),
        ("IDX securities", Text(f"{report.n_securities:,}", style="bold white")),
    ]
    cards = [Panel(v, title=f"[dim]{k}[/]", title_align="left", box=box.ROUNDED, width=34, padding=(0, 1))
             for k, v in tiles]
    panel = Panel(Columns(cards, expand=True),
                 title=f"[bold]IDX Institutional Foreign Ownership · {report.quarter}[/]  "
                       f"[dim]as of {report.as_of:%d %b %Y}[/]",
                 border_style="#3987e5", box=box.HEAVY)
    if report.provisional:
        return Group(Text(" PROVISIONAL: filings for this quarter are still arriving; figures will change. ",
                          style="bold black on yellow"), panel)
    return panel


def manager_table(report: QuarterReport) -> Table:
    t = Table(title="Exposure by manager", title_justify="left", box=box.SIMPLE_HEAVY, expand=True)
    for col, kw in [("#", {"justify": "right", "width": 3}), ("Manager", {}), ("IDX value", {"justify": "right"}),
                    ("QoQ", {"justify": "right"}), ("Share", {"justify": "right"}), ("", {"ratio": 1}),
                    ("Trend", {}), ("Secs", {"justify": "right"})]:
        t.add_column(col, **kw)
    mt = report.manager_table
    if mt.empty:
        t.add_row("", "[dim]No data for this quarter[/]", *[""] * 6)
        return t
    hist = report.history.pivot_table(index="quarter", columns="manager_key", values="value_usd", aggfunc="sum")
    vmax = float(mt["value_usd"].max())
    for r in mt.itertuples():
        trend = sparkline(hist[r.manager_key].tolist()[-16:]) if r.manager_key in hist else ""
        t.add_row(str(r.rank), r.manager_name + (" [yellow]†[/]" if r.stale else ""), fmt_usd(r.value_usd),
                  delta_text(r.delta_pct), fmt_pct(r.share_of_total, signed=False),
                  Text(bar(r.value_usd, vmax), style="#3987e5"), Text(trend, style="cyan"), str(r.n_securities))
    return t


def holdings_table(report: QuarterReport) -> Table:
    t = Table(title="Top 15 IDX holdings (aggregate)", title_justify="left", box=box.SIMPLE_HEAVY, expand=True)
    for col, kw in [("#", {"justify": "right", "width": 3}), ("Ticker", {"style": "bold"}), ("Issuer", {}),
                    ("Sector", {"style": "dim"}), ("Value", {"justify": "right"}), ("QoQ", {"justify": "right"}),
                    ("Holders", {"justify": "right"})]:
        t.add_column(col, **kw)
    for r in report.top_holdings.itertuples():
        t.add_row(str(r.rank), r.ticker if isinstance(r.ticker, str) else "—", str(r.issuer_name)[:30],
                  r.sector if isinstance(r.sector, str) else "—", fmt_usd(r.value_usd),
                  delta_text(r.delta_pct), str(r.n_holders))
    return t


def sector_table(report: QuarterReport) -> Table:
    t = Table(title="Sector allocation", title_justify="left", box=box.SIMPLE, expand=True)
    t.add_column("Sector")
    t.add_column("Share", justify="right")
    t.add_column("", ratio=1)
    st = report.sector_table
    vmax = float(st["share_pct"].max()) if len(st) else 0.0
    for r in st.itertuples():
        t.add_row(r.sector, f"{r.share_pct:.1f}%", Text(bar(r.share_pct, vmax, 18), style="#3987e5"))
    return t


def movers_table(report: QuarterReport) -> Table:
    t = Table(title="Biggest QoQ movers (USD value)", title_justify="left", box=box.SIMPLE, expand=True)
    t.add_column("Security")
    t.add_column("Δ USD", justify="right")
    t.add_column("Δ %", justify="right")
    if report.movers.empty:
        t.add_row("[dim]No prior-quarter comparison[/]", "", "")
    for r in report.movers.itertuples():
        name = f"{r.ticker} · {r.issuer_name}" if isinstance(r.ticker, str) else str(r.issuer_name)
        t.add_row(name[:32], delta_text(r.delta_usd, "usd"), delta_text(r.delta_pct))
    return t


def render_dashboard(report: QuarterReport) -> None:
    console.print(kpi_panel(report))
    console.print(manager_table(report))
    console.print(holdings_table(report))
    console.print(Columns([sector_table(report), movers_table(report)], expand=True, equal=True))
    console.print(Panel(Group(*[Text("• " + n, style="dim") for n in report.notes]),
                        title="[dim]Notes[/]", title_align="left", box=box.ROUNDED, border_style="dim"))


def render_manager_drilldown(engine: TrackerEngine, quarter: str, manager_key: str) -> None:
    df = engine.manager_holdings(quarter, manager_key)
    name = config.MANAGERS_BY_KEY[manager_key].name
    t = Table(title=f"{name} · IDX positions · {quarter}", title_justify="left", box=box.SIMPLE_HEAVY, expand=True)
    for col, kw in [("Ticker", {"style": "bold"}), ("Issuer", {}), ("Value", {"justify": "right"}),
                    ("Weight", {"justify": "right"}), ("Prior Q", {"justify": "right"}),
                    ("Δ USD", {"justify": "right"})]:
        t.add_column(col, **kw)
    for r in df.head(40).itertuples():
        t.add_row(r.ticker if isinstance(r.ticker, str) else "—", str(r.issuer_name)[:34], fmt_usd(r.value_usd),
                  fmt_pct(r.weight_pct, signed=False), fmt_usd(r.value_usd_prev), delta_text(r.delta_usd, "usd"))
    console.print(t)
    if len(df) > 40:
        console.print(f"[dim]… {len(df) - 40} more positions[/]")


def render_comparison(engine: TrackerEngine, q_a: str, q_b: str) -> None:
    a, b = engine.manager_totals(q_a), engine.manager_totals(q_b)
    m = a.merge(b, on=["manager_key", "manager_name"], how="outer", suffixes=("_a", "_b"))
    m["delta"] = m["value_usd_b"] - m["value_usd_a"]
    m["pct"] = m["delta"] / m["value_usd_a"] * 100
    t = Table(title=f"Manager exposure: {q_a} → {q_b}", title_justify="left", box=box.SIMPLE_HEAVY, expand=True)
    for c in ("Manager", q_a, q_b, "Δ USD", "Δ %"):
        t.add_column(c, justify="left" if c == "Manager" else "right")
    for r in m.sort_values("value_usd_b", ascending=False).itertuples():
        t.add_row(r.manager_name, fmt_usd(r.value_usd_a), fmt_usd(r.value_usd_b),
                  delta_text(r.delta, "usd"), delta_text(r.pct))
    console.print(t)


def render_status(engine: TrackerEngine) -> None:
    cov = engine.coverage()
    t = Table(title="Data coverage", title_justify="left", box=box.SIMPLE_HEAVY)
    for c in ("Manager", "Fund", "Source", "First snapshot", "Last snapshot", "Snapshots"):
        t.add_column(c)
    for r in cov.itertuples():
        t.add_row(r.manager_name, r.fund_ticker, r.source, f"{r.first:%Y-%m-%d}", f"{r.last:%Y-%m-%d}", str(r.snapshots))
    have = set(cov["fund_ticker"]) if len(cov) else set()
    for m in config.MANAGERS:
        for f in m.funds:
            if f.ticker not in have:
                t.add_row(m.name, f.ticker, m.source, "[red]none[/]", "", "0")
    console.print(t)


# --------------------------------------------------------------------------- #
# Interactive loop
# --------------------------------------------------------------------------- #
def _pick_quarter(engine: TrackerEngine, current: str | None) -> str | None:
    quarters = engine.available_quarters()
    mode = questionary.select("Select period by", choices=["Quarter list", "Specific date (YYYY-MM-DD)"],
                              style=QSTYLE).ask()
    if mode is None:
        return None
    if mode.startswith("Quarter"):
        return questionary.select("Quarter", choices=list(reversed(quarters)),
                                  default=current if current in quarters else None, style=QSTYLE).ask()

    def valid(s: str) -> bool | str:
        try:
            return quarter_label(date.fromisoformat(s)) in quarters or "No data for that quarter"
        except ValueError:
            return "Use YYYY-MM-DD"
    raw = questionary.text("Date", validate=valid, style=QSTYLE).ask()
    return quarter_label(date.fromisoformat(raw)) if raw else None


def run_interactive(engine_factory: Callable[[], TrackerEngine],
                    ingest: Callable[[], None], export: Callable[[QuarterReport, list[str], str], list[Path]]) -> None:
    engine = engine_factory()
    quarters = engine.available_quarters()
    if not quarters:
        console.print("[yellow]No holdings data yet.[/]")
        if questionary.confirm("Run ingestion now (SEC N-PORT + NBIM)?", default=True, style=QSTYLE).ask():
            ingest()
            engine = engine_factory()
            quarters = engine.available_quarters()
        if not quarters:
            console.print("[red]Still no data. Try `python main.py demo` for a synthetic dataset.[/]")
            return
    quarter = engine.latest_complete_quarter() or quarters[-1]
    while True:
        console.clear()
        report = engine.build_report(quarter)
        render_dashboard(report)
        console.print(Rule(style="dim"))
        action = questionary.select("Action", style=QSTYLE, choices=[
            "Change quarter / date", "Previous quarter", "Next quarter", "Drill into a manager",
            "Compare two quarters", "Export infographic (PNG + PDF)", "Data coverage status",
            "Refresh data (ingest)", "Quit"]).ask()
        if action in (None, "Quit"):
            return
        idx = quarters.index(quarter)
        if action.startswith("Change"):
            quarter = _pick_quarter(engine, quarter) or quarter
        elif action == "Previous quarter":
            quarter = quarters[max(0, idx - 1)]
        elif action == "Next quarter":
            quarter = quarters[min(len(quarters) - 1, idx + 1)]
        elif action.startswith("Drill"):
            keys = report.manager_table["manager_key"].tolist()
            if keys:
                choice = questionary.select("Manager", style=QSTYLE, choices=[
                    questionary.Choice(config.MANAGERS_BY_KEY[k].name, k) for k in keys]).ask()
                if choice:
                    render_manager_drilldown(engine, quarter, choice)
                    questionary.press_any_key_to_continue(style=QSTYLE).ask()
        elif action.startswith("Compare"):
            a = questionary.select("From quarter", choices=quarters, style=QSTYLE).ask()
            b = questionary.select("To quarter", choices=quarters, default=quarter, style=QSTYLE).ask()
            if a and b:
                render_comparison(engine, a, b)
                questionary.press_any_key_to_continue(style=QSTYLE).ask()
        elif action.startswith("Export"):
            fmts = questionary.checkbox("Formats", choices=[questionary.Choice("png", checked=True),
                                                            questionary.Choice("pdf", checked=True)],
                                        style=QSTYLE).ask() or []
            theme = questionary.select("Theme", choices=["light", "dark"], style=QSTYLE).ask() or "light"
            if fmts:
                with console.status("Rendering infographic…"):
                    paths = export(report, fmts, theme)
                for p in paths:
                    console.print(f"[green]✓[/] {p}")
                questionary.press_any_key_to_continue(style=QSTYLE).ask()
        elif action.startswith("Data coverage"):
            render_status(engine)
            questionary.press_any_key_to_continue(style=QSTYLE).ask()
        elif action.startswith("Refresh"):
            ingest()
            engine = engine_factory()
            quarters = engine.available_quarters() or quarters
            quarter = quarter if quarter in quarters else quarters[-1]
