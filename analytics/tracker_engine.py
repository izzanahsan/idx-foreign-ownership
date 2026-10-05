"""Aggregation engine: calendar-quarter snapshots, QoQ deltas, top-15 IDX holdings.

Snapping rule: for calendar quarter Q, each fund contributes its latest snapshot
with report_date <= Q end, provided it is no older than the fund's max staleness
(N-PORT fiscal quarters: one quarter; NBIM semi-annual/annual: up to a year).
A snapshot dated before Q's start is carried forward and flagged ``stale``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import config
import idx_securities
import storage

MAX_STALENESS_DAYS = {"SEC_NPORT": 100, "NBIM": 370, "SYNTHETIC": 100}
# N-PORT is due 60 days after each fiscal quarter-end; allow some slack before a
# calendar quarter is treated as complete.
PROVISIONAL_DAYS = 75
TOP_N = 15


@dataclass
class QuarterReport:
    quarter: str
    as_of: date
    total_idx_usd: float
    total_idx_usd_prev: float | None
    manager_table: pd.DataFrame
    top_holdings: pd.DataFrame
    sector_table: pd.DataFrame
    history: pd.DataFrame
    movers: pd.DataFrame
    notes: list[str] = field(default_factory=list)
    total_comparable_usd: float | None = None  # current total over managers also in prior quarter
    n_securities: int = 0
    n_managers_scope: int = len(config.MANAGERS)  # managers in view (after any filter)
    provisional: bool = False  # filings for this quarter may still be arriving

    @property
    def qoq_delta_usd(self) -> float | None:
        if self.total_idx_usd_prev in (None, 0):
            return None
        cur = self.total_idx_usd if self.total_comparable_usd is None else self.total_comparable_usd
        return cur - self.total_idx_usd_prev

    @property
    def qoq_delta_pct(self) -> float | None:
        d = self.qoq_delta_usd
        return None if d is None else d / self.total_idx_usd_prev * 100


# --------------------------------------------------------------------------- #
# Quarter helpers
# --------------------------------------------------------------------------- #
def quarter_label(d: date | pd.Timestamp) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def quarter_bounds(label: str) -> tuple[date, date]:
    year, q = int(label[:4]), int(label[-1])
    start = date(year, 3 * (q - 1) + 1, 1)
    end = (date(year + (q == 4), (3 * q) % 12 + 1, 1)) - timedelta(days=1)
    return start, end


def prev_quarter(label: str) -> str:
    year, q = int(label[:4]), int(label[-1])
    return f"{year - 1}Q4" if q == 1 else f"{year}Q{q - 1}"


def all_quarters(start_year: int = config.START_YEAR, end_year: int = config.END_YEAR) -> list[str]:
    return [f"{y}Q{q}" for y in range(start_year, end_year + 1) for q in range(1, 5)]


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class TrackerEngine:
    """Answers quarter-level questions over the normalized holdings store."""

    def __init__(self, holdings: pd.DataFrame | None = None, db_path: Path | None = None,
                 managers: set[str] | None = None) -> None:
        """``managers`` restricts the engine to a subset of manager keys (None = all)."""
        df = storage.load_holdings(db_path) if holdings is None else holdings.copy()
        self.scope = [m for m in config.MANAGERS if managers is None or m.key in managers]
        if managers is not None and not df.empty:
            df = df[df["manager_key"].isin(managers)]
        if df.empty:
            df = pd.DataFrame(columns=storage.HOLDING_COLUMNS)
        df["report_date"] = pd.to_datetime(df["report_date"])
        df["security_id"] = df["ticker"].where(df["ticker"].notna(), df["isin"])
        df["manager_name"] = df["manager_key"].map(
            lambda k: config.MANAGERS_BY_KEY[k].name if k in config.MANAGERS_BY_KEY else k)
        self.df = df
        self._snap_cache: dict[str, pd.DataFrame] = {}
        self._labels = self._build_security_labels()

    # ---- coverage ------------------------------------------------------------
    @property
    def is_empty(self) -> bool:
        return self.df.empty

    def available_quarters(self) -> list[str]:
        """Quarters (within the window) where at least one fund has a usable snapshot."""
        if self.is_empty:
            return []
        last = min(self.df["report_date"].max().date(), date(config.END_YEAR, 12, 31))
        out = [q for q in all_quarters() if quarter_bounds(q)[0] <= last]
        return [q for q in out if not self.snapshot(q).empty]

    def is_provisional(self, quarter: str, today: date | None = None) -> bool:
        return (today or date.today()) < quarter_bounds(quarter)[1] + timedelta(days=PROVISIONAL_DAYS)

    def latest_complete_quarter(self, today: date | None = None) -> str | None:
        qs = self.available_quarters()
        done = [q for q in qs if not self.is_provisional(q, today)]
        return (done or qs or [None])[-1]

    def quarter_for_date(self, d: date) -> str:
        return quarter_label(d)

    def coverage(self) -> pd.DataFrame:
        """Per fund: first/last snapshot and count — for the data-status screen."""
        if self.is_empty:
            return pd.DataFrame(columns=["manager_name", "fund_ticker", "source", "first", "last", "snapshots"])
        g = self.df.groupby(["manager_name", "fund_ticker", "source"])["report_date"]
        out = g.agg(first="min", last="max", snapshots="nunique").reset_index()
        return out.sort_values(["manager_name", "fund_ticker"])

    # ---- snapshot assembly ---------------------------------------------------
    def snapshot(self, quarter: str) -> pd.DataFrame:
        """Holdings rows effective for ``quarter`` (one snapshot per fund), with a stale flag."""
        if quarter in self._snap_cache:
            return self._snap_cache[quarter]
        q_start, q_end = quarter_bounds(quarter)
        parts = []
        for fund, fdf in self.df.groupby("fund_ticker"):
            src = fdf["source"].iloc[0]
            limit = pd.Timestamp(q_end) - pd.Timedelta(days=MAX_STALENESS_DAYS.get(src, 100))
            dates = fdf["report_date"]
            eligible = dates[(dates <= pd.Timestamp(q_end)) & (dates > limit)]
            if eligible.empty:
                continue
            rd = eligible.max()
            part = fdf[fdf["report_date"] == rd].copy()
            part["stale"] = rd < pd.Timestamp(q_start)
            parts.append(part)
        snap = pd.concat(parts, ignore_index=True) if parts else self.df.head(0).assign(stale=False)
        self._snap_cache[quarter] = snap
        return snap

    def _build_security_labels(self) -> dict[str, tuple[str | None, str, str | None, str]]:
        """security_id -> (ticker, display name, sector, isin) preferring real ISINs."""
        labels: dict[str, tuple[str | None, str, str | None, str]] = {}
        if self.is_empty:
            return labels
        ordered = self.df.sort_values("source", key=lambda s: s.ne("SEC_NPORT"))
        for sid, g in ordered.groupby("security_id", sort=False):
            row = g.iloc[0]
            sec = idx_securities.resolve(row["issuer_name"])
            isins = [i for i in g["isin"].unique() if isinstance(i, str) and i.startswith("ID")]
            labels[sid] = (
                row["ticker"] if isinstance(row["ticker"], str) else None,
                sec[1] if sec else row["issuer_name"],
                sec[2] if sec else None,
                isins[0] if isins else "",
            )
        return labels

    # ---- aggregates ----------------------------------------------------------
    def manager_totals(self, quarter: str) -> pd.DataFrame:
        snap = self.snapshot(quarter)
        if snap.empty:
            return pd.DataFrame(columns=["manager_key", "manager_name", "value_usd", "n_securities", "stale"])
        return (snap.groupby(["manager_key", "manager_name"])
                .agg(value_usd=("value_usd", "sum"), n_securities=("security_id", "nunique"),
                     stale=("stale", "any"))
                .reset_index())

    def security_totals(self, quarter: str, managers: set[str] | None = None) -> pd.DataFrame:
        snap = self.snapshot(quarter)
        if managers is not None:
            snap = snap[snap["manager_key"].isin(managers)]
        if snap.empty:
            return pd.DataFrame(columns=["security_id", "value_usd", "n_holders"])
        return (snap.groupby("security_id")
                .agg(value_usd=("value_usd", "sum"), n_holders=("manager_key", "nunique"))
                .reset_index())

    def history(self, upto: str | None = None) -> pd.DataFrame:
        rows = []
        for q in self.available_quarters():
            if upto and q > upto:
                break
            for r in self.manager_totals(q).itertuples(index=False):
                rows.append({"quarter": q, "as_of": quarter_bounds(q)[1], "manager_key": r.manager_key,
                             "manager_name": r.manager_name, "value_usd": r.value_usd})
        return pd.DataFrame(rows, columns=["quarter", "as_of", "manager_key", "manager_name", "value_usd"])

    def manager_holdings(self, quarter: str, manager_key: str) -> pd.DataFrame:
        """Drill-down: one manager's IDX positions with QoQ deltas."""
        def per_sec(q: str) -> pd.DataFrame:
            s = self.snapshot(q)
            s = s[s["manager_key"] == manager_key]
            return s.groupby("security_id").agg(value_usd=("value_usd", "sum"),
                                                shares=("shares", "sum")).reset_index()
        cur, prev = per_sec(quarter), per_sec(prev_quarter(quarter))
        out = cur.merge(prev, on="security_id", how="outer", suffixes=("", "_prev")).fillna(
            {"value_usd": 0.0, "value_usd_prev": 0.0})
        out = self._label(out)
        out["delta_usd"] = out["value_usd"] - out["value_usd_prev"]
        total = out["value_usd"].sum()
        out["weight_pct"] = out["value_usd"] / total * 100 if total else np.nan
        return out.sort_values("value_usd", ascending=False).reset_index(drop=True)

    def _label(self, df: pd.DataFrame) -> pd.DataFrame:
        lab = df["security_id"].map(lambda s: self._labels.get(s, (None, str(s), None, "")))
        df = df.copy()
        df["ticker"] = lab.map(lambda t: t[0])
        df["issuer_name"] = lab.map(lambda t: t[1])
        df["sector"] = lab.map(lambda t: t[2])
        df["isin"] = lab.map(lambda t: t[3])
        return df

    # ---- report --------------------------------------------------------------
    def build_report(self, quarter: str) -> QuarterReport:
        q_end = quarter_bounds(quarter)[1]
        pq = prev_quarter(quarter)

        cur_m, prev_m = self.manager_totals(quarter), self.manager_totals(pq)
        mt = cur_m.merge(prev_m[["manager_key", "value_usd"]].rename(columns={"value_usd": "value_usd_prev"}),
                         on="manager_key", how="left")
        mt["delta_usd"] = mt["value_usd"] - mt["value_usd_prev"]
        mt["delta_pct"] = mt["delta_usd"] / mt["value_usd_prev"].replace(0, np.nan) * 100
        total = float(mt["value_usd"].sum())
        mt["share_of_total"] = mt["value_usd"] / total * 100 if total else 0.0
        mt = mt.sort_values("value_usd", ascending=False).reset_index(drop=True)
        mt.insert(0, "rank", range(1, len(mt) + 1))

        # Like-for-like QoQ: only managers reporting in both quarters enter the comparison.
        both = set(cur_m["manager_key"]) & set(prev_m["manager_key"])
        comparable = mt["manager_key"].isin(both)
        prev_total = float(mt.loc[comparable, "value_usd_prev"].sum()) if both else None
        cur_comparable = float(mt.loc[comparable, "value_usd"].sum()) if both else None

        cur_s, prev_s = self.security_totals(quarter), self.security_totals(pq, managers=both)
        st = cur_s.merge(prev_s[["security_id", "value_usd"]].rename(columns={"value_usd": "value_usd_prev"}),
                         on="security_id", how="left")
        st["delta_usd"] = st["value_usd"] - st["value_usd_prev"]
        st["delta_pct"] = st["delta_usd"] / st["value_usd_prev"].replace(0, np.nan) * 100
        st = self._label(st).sort_values("value_usd", ascending=False).reset_index(drop=True)
        top = st.head(TOP_N).copy()
        top.insert(0, "rank", range(1, len(top) + 1))

        sec = st.assign(sector=st["sector"].fillna("Other / Unclassified")).groupby("sector")["value_usd"].sum()
        sector = sec.sort_values(ascending=False).reset_index()
        sector["share_pct"] = sector["value_usd"] / total * 100 if total else 0.0

        movers = pd.DataFrame(columns=["ticker", "issuer_name", "delta_usd", "delta_pct"])
        if not prev_s.empty:
            full = self.security_totals(quarter, managers=both).merge(prev_s[["security_id", "value_usd"]], on="security_id", how="outer",
                               suffixes=("", "_prev")).fillna({"value_usd": 0.0, "value_usd_prev": 0.0})
            full["delta_usd"] = full["value_usd"] - full["value_usd_prev"]
            full["delta_pct"] = full["delta_usd"] / full["value_usd_prev"].replace(0, np.nan) * 100
            full = self._label(full)
            buys = full[full["delta_usd"] > 0].nlargest(5, "delta_usd")
            sells = full[full["delta_usd"] < 0].nsmallest(5, "delta_usd")
            movers = pd.concat([buys, sells])[["ticker", "issuer_name", "delta_usd", "delta_pct"]]

        notes = []
        provisional = self.is_provisional(quarter)
        if provisional:
            notes.append("PROVISIONAL: this quarter ended less than 75 days ago and N-PORT filings are still "
                         "arriving; totals and QoQ will change as remaining funds file.")
        notes += [
            "Values are USD market value of IDX-listed equity reported by the tracked funds; "
            "QoQ deltas mix price moves and net flows.",
            "SEC N-PORT snapshots are fiscal-quarter-end dates snapped to the calendar quarter they fall in.",
            "Totals are per tracked fund vehicle, not each manager's firm-wide Indonesia exposure.",
        ]
        new = sorted(set(cur_m["manager_name"]) - set(mt.loc[comparable, "manager_name"]))
        if both and new:
            notes.append("QoQ change is like-for-like; excludes managers without prior-quarter data: "
                         + ", ".join(new) + ".")
        stale = mt.loc[mt["stale"].astype(bool), "manager_name"].tolist()
        if stale:
            notes.append("† Carried forward from an earlier snapshot (no filing in this quarter): "
                         + ", ".join(stale) + ".")
        missing = [m.name for m in self.scope if m.key not in set(mt["manager_key"])]
        if missing:
            notes.append("No usable data this quarter: " + ", ".join(missing) + ".")
        unmapped = top["ticker"].isna().sum()
        if unmapped:
            notes.append(f"{unmapped} top holding(s) shown by issuer name (no IDX ticker mapping).")

        return QuarterReport(
            quarter=quarter, as_of=q_end, total_idx_usd=total, total_idx_usd_prev=prev_total,
            total_comparable_usd=cur_comparable, n_securities=len(cur_s), provisional=provisional,
            n_managers_scope=len(self.scope),
            manager_table=mt, top_holdings=top, sector_table=sector,
            history=self.history(upto=quarter), movers=movers.reset_index(drop=True), notes=notes)
