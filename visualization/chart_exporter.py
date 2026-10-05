"""Executive quarterly infographic: one-page PNG (300 dpi) + two-page PDF (infographic + tables).

Design rules: single-hue bars for magnitude, fixed per-manager categorical colors
(color follows the entity, never its rank; >7 managers fold into "Other"), a
blue<->red diverging pair for flows, recessive grid, text in ink colors only.
"""
from __future__ import annotations

import logging
import math
import textwrap
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.gridspec import GridSpec  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

import config  # noqa: E402

if TYPE_CHECKING:
    from analytics.tracker_engine import QuarterReport

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Palette (validated reference palette; light/dark steps of the same hues)
# --------------------------------------------------------------------------- #
THEMES = {
    "light": {
        "surface": "#fcfcfb", "page": "#f9f9f7", "ink": "#0b0b0b", "ink2": "#52514e",
        "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7", "tile": "#f1f0ec",
        "header": "#0B2545", "header_ink": "#ffffff",
        "bar": "#2a78d6", "bar_soft": "#86b6ef", "pos": "#2a78d6", "neg": "#e34948",
        "up_text": "#006300", "down_text": "#c62828", "other": "#b4b2a9",
        "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7"],
    },
    "dark": {
        "surface": "#1a1a19", "page": "#0d0d0d", "ink": "#ffffff", "ink2": "#c3c2b7",
        "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835", "tile": "#242423",
        "header": "#13315C", "header_ink": "#ffffff",
        "bar": "#3987e5", "bar_soft": "#1c5cab", "pos": "#3987e5", "neg": "#e66767",
        "up_text": "#0ca30c", "down_text": "#e66767", "other": "#5f5e5a",
        "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9"],
    },
}
FONT_STACK = ["Inter", "Helvetica Neue", "Arial", "DejaVu Sans"]
MAX_SERIES = 7  # categorical slots; remaining managers fold into "Other"


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def _missing(x: object) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x)) or x is pd.NA


def fmt_usd(x: float | None, decimals: int = 1) -> str:
    if _missing(x):
        return "—"
    sign = "-" if x < 0 else ""
    a = abs(float(x))
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{sign}${a / div:,.{2 if suf == 'B' else decimals}f}{suf}"
    return f"{sign}${a:,.0f}"


def fmt_pct(x: float | None, signed: bool = True, decimals: int = 1) -> str:
    if _missing(x):
        return "—"
    return f"{x:+.{decimals}f}%" if signed else f"{x:.{decimals}f}%"


def _security_label(ticker: object, issuer: object, width: int = 28) -> str:
    name = str(issuer) if not _missing(issuer) else ""
    label = f"{ticker} · {name}" if isinstance(ticker, str) and ticker else name
    return label if len(label) <= width else label[: width - 1] + "…"


def manager_colors(theme: str = "light") -> dict[str, str]:
    """Fixed manager -> color by configured priority rank (stable across quarters)."""
    pal = THEMES[theme]
    ordered = sorted(config.MANAGERS, key=lambda m: m.rank)
    return {m.key: (pal["series"][i] if i < MAX_SERIES else pal["other"]) for i, m in enumerate(ordered)}


# --------------------------------------------------------------------------- #
# Panels
# --------------------------------------------------------------------------- #
def _style_axis(ax: plt.Axes, t: dict, grid_axis: str = "x") -> None:
    ax.set_facecolor(t["surface"])
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(t["axis"])
    ax.tick_params(colors=t["ink2"], labelsize=9, length=0)
    ax.grid(axis=grid_axis, color=t["grid"], linewidth=0.8)
    ax.set_axisbelow(True)


def _panel_title(ax: plt.Axes, title: str, t: dict, subtitle: str | None = None) -> None:
    ax.set_title(title, loc="left", fontsize=13, fontweight="bold", color=t["ink"], pad=22 if subtitle else 10)
    if subtitle:
        ax.text(0, 1.015, subtitle, transform=ax.transAxes, fontsize=9, color=t["ink2"], va="bottom")


def _empty(ax: plt.Axes, msg: str, t: dict) -> None:
    ax.text(0.5, 0.5, msg, transform=ax.transAxes, ha="center", va="center", color=t["muted"], fontsize=11)
    ax.set_xticks([])
    ax.set_yticks([])


def _usd_axis(ax: plt.Axes, axis: str = "x") -> None:
    fmt = matplotlib.ticker.FuncFormatter(lambda v, _p: fmt_usd(v, 0))
    (ax.xaxis if axis == "x" else ax.yaxis).set_major_formatter(fmt)


def _draw_header(fig: Figure, gs, report: "QuarterReport", t: dict) -> None:
    ax = fig.add_subplot(gs)
    ax.axis("off")
    ax.add_patch(FancyBboxPatch((0, 0), 1, 1, boxstyle="round,pad=0,rounding_size=0.02",
                                transform=ax.transAxes, facecolor=t["header"], edgecolor="none"))
    ax.text(0.025, 0.68, "Institutional Foreign Ownership — Indonesian Equities (IDX)",
            transform=ax.transAxes, fontsize=24, fontweight="bold", color=t["header_ink"], va="center")
    tag = "  ·  PROVISIONAL" if getattr(report, "provisional", False) else ""
    ax.text(0.025, 0.30, f"Quarterly report  ·  {report.quarter}  ·  as of {report.as_of:%d %B %Y}{tag}",
            transform=ax.transAxes, fontsize=13, color=t["header_ink"], alpha=0.85, va="center")
    ax.text(0.975, 0.30, "Sources: SEC Form N-PORT, NBIM holdings disclosures",
            transform=ax.transAxes, fontsize=10, color=t["header_ink"], alpha=0.75, ha="right", va="center")


def _draw_kpis(fig: Figure, gs, report: "QuarterReport", t: dict) -> None:
    ax = fig.add_subplot(gs)
    ax.axis("off")
    mt = report.manager_table
    d_usd, d_pct = report.qoq_delta_usd, report.qoq_delta_pct
    n_sec = _n_securities(report)
    tiles = [
        ("Tracked IDX exposure", fmt_usd(report.total_idx_usd), None, None),
        ("QoQ change (like-for-like)",
         "n/a" if d_usd is None else fmt_usd(d_usd), None if d_pct is None else fmt_pct(d_pct),
         None if d_usd is None else (t["up_text"] if d_usd >= 0 else t["down_text"])),
        ("Managers reporting", f"{len(mt)} / {getattr(report, 'n_managers_scope', len(config.MANAGERS))}",
         f"{int(mt['stale'].sum())} carried forward" if not mt.empty and mt["stale"].any() else None, None),
        ("IDX securities held", f"{n_sec:,}", None, None),
    ]
    w, gap = 0.235, (1 - 4 * 0.235) / 3
    for i, (label, value, sub, color) in enumerate(tiles):
        x = i * (w + gap)
        ax.add_patch(FancyBboxPatch((x, 0.04), w, 0.92, boxstyle="round,pad=0,rounding_size=0.04",
                                    transform=ax.transAxes, facecolor=t["tile"], edgecolor=t["grid"]))
        ax.text(x + 0.02, 0.76, label.upper(), transform=ax.transAxes, fontsize=9.5,
                color=t["ink2"], fontweight="bold")
        ax.text(x + 0.02, 0.38, value, transform=ax.transAxes, fontsize=26, fontweight="bold",
                color=color or t["ink"])
        if sub:
            arrow = "" if color is None else ("Up " if (d_usd or 0) >= 0 else "Down ")
            ax.text(x + 0.02, 0.14, arrow + sub, transform=ax.transAxes, fontsize=10.5,
                    color=color or t["ink2"])


def _n_securities(report: "QuarterReport") -> int:
    n = getattr(report, "n_securities", None)
    return int(n) if n is not None else int(report.top_holdings.shape[0])


def _draw_managers(fig: Figure, gs, report: "QuarterReport", t: dict) -> None:
    ax = fig.add_subplot(gs)
    _style_axis(ax, t)
    _panel_title(ax, "Exposure by manager", t, "USD value of IDX equities held · QoQ change")
    mt = report.manager_table
    if mt.empty:
        _empty(ax, "No manager data for this quarter", t)
        return
    mt = mt.iloc[::-1]
    y = np.arange(len(mt))
    stale = mt["stale"].astype(bool).to_numpy()
    bars = ax.barh(y, mt["value_usd"], height=0.62, color=t["bar"], edgecolor=t["surface"], linewidth=2)
    for b, s in zip(bars, stale):
        if s:
            b.set_facecolor(t["bar_soft"])
            b.set_hatch("///")
            b.set_edgecolor(t["surface"])
    labels = ["\n".join(textwrap.wrap(n, 22)) + (" †" if s else "") for n, s in zip(mt["manager_name"], stale)]
    ax.set_yticks(y, labels, fontsize=10, color=t["ink"])
    xmax = float(mt["value_usd"].max())
    ax.set_xlim(0, xmax * 1.42)
    _usd_axis(ax)
    ax.text(xmax * 1.42, len(mt) - 0.45, "QoQ", ha="right", va="bottom", fontsize=8.5, color=t["muted"])
    for yi, (v, dp) in enumerate(zip(mt["value_usd"], mt["delta_pct"])):
        ax.text(v + xmax * 0.012, yi, fmt_usd(v), va="center", fontsize=10, color=t["ink"], fontweight="bold")
        ax.text(xmax * 1.42, yi, fmt_pct(dp), va="center", ha="right", fontsize=9,
                color=t["muted"] if _missing(dp) else (t["up_text"] if dp >= 0 else t["down_text"]))


def _draw_top_holdings(fig: Figure, gs, report: "QuarterReport", t: dict) -> None:
    ax = fig.add_subplot(gs)
    _style_axis(ax, t)
    _panel_title(ax, "Top 15 IDX holdings", t, "Aggregate across tracked managers · (holders)")
    top = report.top_holdings
    if top.empty:
        _empty(ax, "No holdings for this quarter", t)
        return
    top = top.iloc[::-1]
    y = np.arange(len(top))
    ax.barh(y, top["value_usd"], height=0.62, color=t["bar"], edgecolor=t["surface"], linewidth=2)
    ax.set_yticks(y, [_security_label(tk, nm) for tk, nm in zip(top["ticker"], top["issuer_name"])],
                  fontsize=9.5, color=t["ink"])
    xmax = float(top["value_usd"].max())
    ax.set_xlim(0, xmax * 1.28)
    _usd_axis(ax)
    for yi, (v, n) in enumerate(zip(top["value_usd"], top["n_holders"])):
        ax.text(v + xmax * 0.012, yi, f"{fmt_usd(v)}  ({int(n)})", va="center", fontsize=9, color=t["ink2"])


def _draw_history(fig: Figure, gs, report: "QuarterReport", t: dict, theme: str) -> None:
    ax = fig.add_subplot(gs)
    _style_axis(ax, t, grid_axis="y")
    _panel_title(ax, "Exposure trend by manager", t, "Stacked USD value per calendar quarter, 2020 to present")
    h = report.history
    if h.empty:
        _empty(ax, "No history available", t)
        return
    colors = manager_colors(theme)
    ranks = {m.key: m.rank for m in config.MANAGERS}
    keep = [k for k in sorted(h["manager_key"].unique(), key=lambda k: ranks.get(k, 99))
            if colors.get(k) != t["other"]]
    h = h.assign(series=h["manager_key"].where(h["manager_key"].isin(keep), "_other"))
    pv = h.pivot_table(index="quarter", columns="series", values="value_usd", aggfunc="sum").fillna(0.0)
    cols = [k for k in keep if k in pv.columns] + (["_other"] if "_other" in pv.columns else [])
    pv = pv[cols].sort_index()
    names = {m.key: m.name for m in config.MANAGERS} | {"_other": "Other managers"}
    x = np.arange(len(pv))
    ax.stackplot(x, pv.T.to_numpy(), labels=[names[c] for c in cols],
                 colors=[colors.get(c, t["other"]) for c in cols], edgecolor=t["surface"], linewidth=1.2)
    step = max(1, len(pv) // 14)
    ax.set_xticks(x[::step], [q for q in pv.index[::step]], fontsize=9)
    ax.set_xlim(0, max(len(pv) - 1, 1))
    _usd_axis(ax, "y")
    if report.quarter in pv.index:
        xi = list(pv.index).index(report.quarter)
        ax.axvline(xi, color=t["ink"], linewidth=1.2, linestyle=(0, (3, 2)))
        ax.text(xi, ax.get_ylim()[1], f" {report.quarter}", color=t["ink"], fontsize=9, va="top",
                ha="right" if xi > len(pv) * 0.85 else "left", fontweight="bold")
    leg = ax.legend(loc="lower right", ncol=min(4, len(cols)), fontsize=9, frameon=False,
                    bbox_to_anchor=(1, 1.0))
    for txt in leg.get_texts():
        txt.set_color(t["ink2"])


def _draw_sectors(fig: Figure, gs, report: "QuarterReport", t: dict) -> None:
    ax = fig.add_subplot(gs)
    _style_axis(ax, t)
    _panel_title(ax, "Sector allocation", t, "Share of tracked IDX exposure (IDX-IC sectors)")
    st = report.sector_table
    if st.empty:
        _empty(ax, "No sector data", t)
        return
    st = st.iloc[::-1]
    y = np.arange(len(st))
    ax.barh(y, st["share_pct"], height=0.62, color=t["bar"], edgecolor=t["surface"], linewidth=2)
    ax.set_yticks(y, st["sector"], fontsize=9.5, color=t["ink"])
    xmax = float(st["share_pct"].max())
    ax.set_xlim(0, xmax * 1.25)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _p: f"{v:.0f}%"))
    for yi, (p, v) in enumerate(zip(st["share_pct"], st["value_usd"])):
        ax.text(p + xmax * 0.012, yi, f"{p:.1f}%  {fmt_usd(v)}", va="center", fontsize=9, color=t["ink2"])


def _draw_movers(fig: Figure, gs, report: "QuarterReport", t: dict) -> None:
    ax = fig.add_subplot(gs)
    _style_axis(ax, t)
    _panel_title(ax, "Biggest QoQ movers", t, "Change in aggregate USD value (price + flows), like-for-like managers")
    mv = report.movers
    if mv.empty:
        _empty(ax, "No prior-quarter comparison available", t)
        return
    mv = mv.sort_values("delta_usd")
    y = np.arange(len(mv))
    colors = [t["pos"] if d >= 0 else t["neg"] for d in mv["delta_usd"]]
    ax.barh(y, mv["delta_usd"], height=0.62, color=colors, edgecolor=t["surface"], linewidth=2)
    ax.axvline(0, color=t["axis"], linewidth=1)
    ax.set_yticks(y, [_security_label(tk, nm, 24) for tk, nm in zip(mv["ticker"], mv["issuer_name"])],
                  fontsize=9.5, color=t["ink"])
    lim = float(mv["delta_usd"].abs().max()) * 1.45
    ax.set_xlim(-lim, lim)
    _usd_axis(ax)
    for yi, d in enumerate(mv["delta_usd"]):
        ax.text(d + (lim * 0.02 if d >= 0 else -lim * 0.02), yi, ("+" if d >= 0 else "") + fmt_usd(d),
                va="center", ha="left" if d >= 0 else "right", fontsize=9, color=t["ink2"])


def _draw_footer(fig: Figure, gs, report: "QuarterReport", t: dict) -> None:
    ax = fig.add_subplot(gs)
    ax.axis("off")
    lines = ["• " + "\n   ".join(textwrap.wrap(n, 190)) for n in report.notes]
    ax.text(0, 1, "Notes & methodology", fontsize=10.5, fontweight="bold", color=t["ink"], va="top",
            transform=ax.transAxes)
    ax.text(0, 0.80, "\n".join(lines), fontsize=8.8, color=t["ink2"], va="top", transform=ax.transAxes,
            linespacing=1.5)
    ax.text(1, 0, f"Generated {datetime.now():%Y-%m-%d %H:%M} · IDX Foreign Ownership Tracker",
            fontsize=8.5, color=t["muted"], ha="right", va="bottom", transform=ax.transAxes)


# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #
def _rc(theme: str) -> dict:
    t = THEMES[theme]
    return {"font.family": "sans-serif", "font.sans-serif": FONT_STACK, "text.color": t["ink"],
            "axes.labelcolor": t["ink2"], "figure.facecolor": t["page"], "hatch.color": t["surface"],
            "hatch.linewidth": 1.4, "pdf.fonttype": 42}


def build_infographic(report: "QuarterReport", theme: str = "light") -> Figure:
    t = THEMES[theme]
    fig = plt.figure(figsize=(16, 21))
    fig.patch.set_facecolor(t["page"])
    gs = GridSpec(6, 2, figure=fig, height_ratios=[0.9, 1.25, 5.2, 4.2, 4.0, 1.5],
                  hspace=0.42, wspace=0.42, left=0.14, right=0.97, top=0.975, bottom=0.025)
    _draw_header(fig, gs[0, :], report, t)
    _draw_kpis(fig, gs[1, :], report, t)
    _draw_managers(fig, gs[2, 0], report, t)
    _draw_top_holdings(fig, gs[2, 1], report, t)
    _draw_history(fig, gs[3, :], report, t, theme)
    _draw_sectors(fig, gs[4, 0], report, t)
    _draw_movers(fig, gs[4, 1], report, t)
    _draw_footer(fig, gs[5, :], report, t)
    # Header/footer/history span the full width: realign them to the page edge.
    for ax in fig.axes:
        pos = ax.get_position()
        if pos.width > 0.6:
            ax.set_position([0.04, pos.y0, 0.93, pos.height])
    return fig


def _table_page(report: "QuarterReport", theme: str) -> Figure:
    t = THEMES[theme]
    fig = plt.figure(figsize=(16, 21))
    fig.patch.set_facecolor(t["page"])
    fig.text(0.04, 0.965, f"Appendix — data tables · {report.quarter}", fontsize=20, fontweight="bold", color=t["ink"])

    def render(ax: plt.Axes, title: str, header: list[str], rows: Iterable[list[str]], widths: list[float]) -> None:
        ax.axis("off")
        ax.set_title(title, loc="left", fontsize=13, fontweight="bold", color=t["ink"])
        rows = list(rows) or [["—"] * len(header)]
        tbl = ax.table(cellText=rows, colLabels=header, colWidths=widths, loc="upper left", cellLoc="left")
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(9.5)
        tbl.scale(1, 1.55)
        for (r, _c), cell in tbl.get_celld().items():
            cell.set_edgecolor(t["grid"])
            if r == 0:
                cell.set_facecolor(t["header"])
                cell.get_text().set_color(t["header_ink"])
                cell.get_text().set_fontweight("bold")
            else:
                cell.set_facecolor(t["tile"] if r % 2 == 0 else t["surface"])
                cell.get_text().set_color(t["ink"])

    mt = report.manager_table
    render(fig.add_axes([0.04, 0.60, 0.92, 0.33]), "Managers",
           ["#", "Manager", "IDX value", "Prior Q", "Δ USD", "Δ %", "Share", "Securities", "Stale"],
           [[str(r.rank), r.manager_name, fmt_usd(r.value_usd), fmt_usd(r.value_usd_prev), fmt_usd(r.delta_usd),
             fmt_pct(r.delta_pct), fmt_pct(r.share_of_total, signed=False), str(r.n_securities),
             "†" if r.stale else ""] for r in mt.itertuples()],
           [0.04, 0.26, 0.1, 0.1, 0.1, 0.08, 0.08, 0.1, 0.06])
    top = report.top_holdings
    render(fig.add_axes([0.04, 0.05, 0.92, 0.50]), "Top 15 IDX holdings",
           ["#", "Ticker", "Issuer", "ISIN", "Sector", "Value", "Δ USD", "Δ %", "Holders"],
           [[str(r.rank), r.ticker if isinstance(r.ticker, str) else "—", str(r.issuer_name)[:34],
             r.isin or "—", r.sector if isinstance(r.sector, str) else "—", fmt_usd(r.value_usd),
             fmt_usd(r.delta_usd), fmt_pct(r.delta_pct), str(r.n_holders)] for r in top.itertuples()],
           [0.04, 0.07, 0.22, 0.12, 0.17, 0.09, 0.09, 0.08, 0.07])
    return fig


def render_report(report: "QuarterReport", out_dir: Path | str = config.REPORTS_DIR,
                  formats: Iterable[str] = ("png", "pdf"), theme: str = "light") -> list[Path]:
    """Write the quarterly infographic; returns the paths written."""
    if theme not in THEMES:
        raise ValueError(f"Unknown theme {theme!r}; choose from {sorted(THEMES)}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"IDX_Foreign_Ownership_{report.quarter}" + ("" if theme == "light" else f"_{theme}")
    written: list[Path] = []
    formats = {f.lower() for f in formats}
    with plt.rc_context(_rc(theme)):
        fig = build_infographic(report, theme)
        try:
            if "png" in formats:
                path = out_dir / f"{stem}.png"
                fig.savefig(path, dpi=300, facecolor=fig.get_facecolor())
                written.append(path)
            if "pdf" in formats:
                path = out_dir / f"{stem}.pdf"
                appendix = _table_page(report, theme)
                try:
                    with PdfPages(path, metadata={
                        "Title": f"IDX Institutional Foreign Ownership — {report.quarter}",
                        "Author": "IDX Foreign Ownership Tracker",
                        "Subject": "Quarterly institutional holdings of Indonesian equities",
                        "CreationDate": datetime.now()}) as pdf:
                        pdf.savefig(fig, facecolor=fig.get_facecolor())
                        pdf.savefig(appendix, facecolor=appendix.get_facecolor())
                finally:
                    plt.close(appendix)
                written.append(path)
        finally:
            plt.close(fig)
    logger.info("Wrote %s", ", ".join(map(str, written)))
    return written
