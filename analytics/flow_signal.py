"""Do tracked foreign funds' net flows predict IDX returns?

1. Net flows: for each SEC fund and each pair of consecutive snapshots, flow =
   Σ Δshares × average price (new positions at current price, exits at last
   price). This strips out price moves, which dominate raw USD value changes.
   NBIM is excluded (no share counts). ADR lines are excluded (ADR share units
   aren't comparable with local shares).
2. Stock splits: a split multiplies every holder's share count at once, while
   real buying differs fund by fund. A split event is confirmed when several fund
   intervals for the same security around the same quarter show a share jump
   with a matching price drop; the factor is the standard ratio nearest the
   median share jump. Without this, BBCA's 2021 1:5 split alone reads as ~$1B of
   fake inflow.
3. Signal: quarterly net flow as % of the funds' prior IDX holdings, against
   the IDX Composite's USD return in the same quarter, the prior quarter
   (do funds chase returns?) and the next 1, 2 and 4 quarters (do they lead?).
   Overlapping multi-quarter windows use Newey-West standard errors.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from analytics.tracker_engine import quarter_label

SPLIT_FACTORS = (2, 3, 4, 5, 8, 10, 20, 25, 50, 100)
MAX_GAP_DAYS = 120  # consecutive snapshots further apart than this are skipped
HORIZONS = (1, 2, 4)


# --------------------------------------------------------------------------- #
# Flows
# --------------------------------------------------------------------------- #
def _position_panel(holdings: pd.DataFrame) -> pd.DataFrame:
    sec = holdings[(holdings["source"] == "SEC_NPORT") & holdings["shares"].gt(0)
                   & holdings["isin"].astype(str).str.startswith("ID")].copy()
    sec["sid"] = sec["ticker"].where(sec["ticker"].notna(), sec["isin"])
    sec = (sec.groupby(["fund_ticker", "manager_key", "report_date", "sid"], as_index=False)
              .agg(shares=("shares", "sum"), value_usd=("value_usd", "sum")))
    sec["px"] = sec["value_usd"] / sec["shares"]
    return sec


def _intervals(panel: pd.DataFrame) -> pd.DataFrame:
    """One row per (fund, consecutive snapshot pair, security) with both ends."""
    out = []
    for fund, f in panel.groupby("fund_ticker"):
        dates = sorted(f["report_date"].unique())
        for d0, d1 in zip(dates, dates[1:]):
            if (pd.Timestamp(d1) - pd.Timestamp(d0)).days > MAX_GAP_DAYS:
                continue
            a = f[f["report_date"] == d0].set_index("sid")[["shares", "px", "value_usd"]]
            b = f[f["report_date"] == d1].set_index("sid")[["shares", "px", "value_usd"]]
            j = a.join(b, how="outer", lsuffix="0", rsuffix="1").fillna(0.0)
            j["fund_ticker"], j["manager_key"] = fund, f["manager_key"].iloc[0]
            j["d0"], j["d1"] = pd.Timestamp(d0), pd.Timestamp(d1)
            j["aum0"] = a["value_usd"].sum()
            out.append(j.reset_index())
    cols = ["sid", "shares0", "px0", "value_usd0", "shares1", "px1", "value_usd1",
            "fund_ticker", "manager_key", "d0", "d1", "aum0"]
    return pd.concat(out, ignore_index=True)[cols] if out else pd.DataFrame(columns=cols)


def detect_splits(iv: pd.DataFrame, min_confirm: int = 2) -> pd.DataFrame:
    """Return confirmed split events: sid, quarter (of d1), factor, n_confirming.

    A fund interval is a split candidate when shares rose >=1.8x while price fell
    to <=0.6x. Candidates for the same security within adjacent quarters form one
    event; the factor is the standard ratio nearest their median share jump.
    """
    both = iv[(iv["shares0"] > 0) & (iv["shares1"] > 0)].copy()
    both["s_ratio"] = both["shares1"] / both["shares0"]
    both["p_ratio"] = both["px1"] / both["px0"]
    cand = both[(both["s_ratio"] >= 1.8) & (both["p_ratio"] <= 0.6)].copy()
    if cand.empty:
        return pd.DataFrame(columns=["sid", "q", "factor", "n"])
    cand["q"] = cand["d1"].map(lambda d: quarter_label(d.date()))
    cand["qi"] = cand["q"].map(lambda q: int(q[:4]) * 4 + int(q[-1]))
    events = []
    for sid, g in cand.groupby("sid"):
        g = g.sort_values("qi")
        cluster = [g.iloc[0]]
        for _, row in list(g.iterrows())[1:] + [(None, None)]:
            if row is not None and row["qi"] - cluster[-1]["qi"] <= 1:
                cluster.append(row)
                continue
            c = pd.DataFrame(cluster)
            if len(c) >= min_confirm:
                med = float(np.median(c["s_ratio"]))
                factor = min(SPLIT_FACTORS, key=lambda k: abs(math.log(med / k)))
                events.append({"sid": sid, "q": c["q"].iloc[0], "qi_lo": c["qi"].min(),
                               "qi_hi": c["qi"].max(), "factor": factor, "n": len(c)})
            if row is not None:
                cluster = [row]
    return pd.DataFrame(events)


def estimate_fund_flows(holdings: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per fund interval net flow (USD), split-adjusted. Returns (flows, split_events)."""
    iv = _intervals(_position_panel(holdings))
    if iv.empty:
        return pd.DataFrame(columns=["fund_ticker", "manager_key", "d1", "q", "flow_usd", "aum0"]), pd.DataFrame()
    splits = detect_splits(iv)
    iv["qi"] = iv["d1"].map(lambda d: d.year * 4 + (d.month - 1) // 3 + 1)
    for ev in splits.itertuples():
        hit = ((iv["sid"] == ev.sid) & iv["qi"].between(ev.qi_lo, ev.qi_hi)
               & (iv["shares0"] > 0) & (iv["shares1"] / iv["shares0"].replace(0, np.nan) >= 1.8)
               & (iv["px1"] / iv["px0"].replace(0, np.nan) <= 0.6))
        iv.loc[hit, "shares0"] *= ev.factor
        iv.loc[hit, "px0"] /= ev.factor
    held_both = (iv["shares0"] > 0) & (iv["shares1"] > 0)
    iv["flow_usd"] = np.where(
        held_both, (iv["shares1"] - iv["shares0"]) * (iv["px0"] + iv["px1"]) / 2,
        np.where(iv["shares1"] > 0, iv["value_usd1"], -iv["value_usd0"]))
    flows = (iv.groupby(["fund_ticker", "manager_key", "d1"], as_index=False)
               .agg(flow_usd=("flow_usd", "sum"), aum0=("aum0", "first")))
    flows["q"] = flows["d1"].map(lambda d: quarter_label(d.date()))
    return flows, splits


def quarterly_flows(flows: pd.DataFrame) -> pd.DataFrame:
    q = flows.groupby("q").agg(flow_usd=("flow_usd", "sum"), aum0=("aum0", "sum"),
                               n_funds=("fund_ticker", "nunique")).sort_index()
    q["flow_pct"] = q["flow_usd"] / q["aum0"] * 100
    return q


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def ols_hac(y: np.ndarray, x: np.ndarray, lags: int = 0) -> tuple[float, float, float]:
    """Slope, Newey-West t-stat, R² for y = a + b x."""
    X = np.column_stack([np.ones_like(x), x])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    n = len(y)
    xtx_inv = np.linalg.inv(X.T @ X)
    S = (X * resid[:, None]).T @ (X * resid[:, None])
    for L in range(1, lags + 1):
        w = 1 - L / (lags + 1)
        g = (X[L:] * resid[L:, None]).T @ (X[:-L] * resid[:-L, None])
        S += w * (g + g.T)
    cov = xtx_inv @ S @ xtx_inv * n / max(n - 2, 1)
    t = beta[1] / math.sqrt(cov[1, 1]) if cov[1, 1] > 0 else float("nan")
    r2 = 1 - (resid @ resid) / ((y - y.mean()) @ (y - y.mean())) if n > 2 else float("nan")
    return float(beta[1]), float(t), float(r2)


@dataclass
class TestResult:
    label: str
    n: int
    corr: float
    slope: float
    t_stat: float
    r2: float
    hit_rate: float  # share of quarters where flow sign == return sign

    @property
    def significant(self) -> bool:
        return abs(self.t_stat) >= 2.0


@dataclass
class FlowStudy:
    table: pd.DataFrame            # quarter: flow_usd, flow_pct, ret, fwd1.., ret_prev
    tests: list[TestResult]
    splits: pd.DataFrame
    oos_hit_rate: float | None     # out-of-sample sign accuracy of an expanding-window model
    base_hit_rate: float | None    # "always predict the historical majority sign" baseline
    notes: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        fwd = [t for t in self.tests if t.label.startswith("Next")]
        if any(t.significant and t.slope > 0 for t in fwd):
            return "predictive"
        if any(t.significant and t.slope < 0 for t in fwd):
            return "contrarian"
        return "not predictive"


def run_study(holdings: pd.DataFrame, market: pd.DataFrame, exclude: set[str] | None = None) -> FlowStudy:
    """``exclude``: quarters to leave out (e.g. provisional ones still awaiting filings)."""
    flows, splits = estimate_fund_flows(holdings)
    qf = quarterly_flows(flows)
    # Quarters where most funds haven't reported yet would understate flows.
    qf = qf[(qf["n_funds"] >= 0.75 * qf["n_funds"].max()) & ~qf.index.isin(exclude or set())]
    m = market.copy()
    for k in HORIZONS:
        m[f"fwd{k}"] = (m["jci_usd"].shift(-k) / m["jci_usd"] - 1) * 100
    m["ret_prev"] = m["ret"].shift(1)
    tab = qf.join(m[["jci", "jci_usd", "ret", "ret_prev"] + [f"fwd{k}" for k in HORIZONS]], how="inner")

    specs = [("Same quarter", "ret", 0), ("Prior quarter (chasing?)", "ret_prev", 0)] + \
            [(f"Next {k} quarter{'s' if k > 1 else ''}", f"fwd{k}", k - 1) for k in HORIZONS]
    tests = []
    for label, col, lags in specs:
        d = tab[["flow_pct", col]].dropna()
        if len(d) < 6:
            continue
        x, y = d["flow_pct"].to_numpy(), d[col].to_numpy()
        slope, t, r2 = ols_hac(y, x, lags)
        tests.append(TestResult(label, len(d), float(np.corrcoef(x, y)[0, 1]), slope, t, r2,
                                float((np.sign(x) == np.sign(y)).mean())))

    # Out-of-sample: at each quarter, fit on the past only, predict next-quarter sign.
    d = tab[["flow_pct", "fwd1"]].dropna()
    hits, base = [], []
    for i in range(8, len(d)):
        past = d.iloc[:i]
        b = np.polyfit(past["flow_pct"], past["fwd1"], 1)
        pred = np.polyval(b, d["flow_pct"].iloc[i])
        actual = d["fwd1"].iloc[i]
        hits.append(np.sign(pred) == np.sign(actual))
        base.append(np.sign(np.sign(past["fwd1"]).sum() or 1) == np.sign(actual))
    notes = [
        f"{len(qf)} quarters of net flows from {flows['fund_ticker'].nunique()} SEC-reporting funds "
        f"(NBIM excluded: no share counts).",
        f"{len(splits)} stock-split events detected and adjusted"
        + (f" (e.g. {', '.join(f'{r.sid} 1:{r.factor} in {r.q}' for r in splits.head(3).itertuples())})." if len(splits) else "."),
        "Returns are IDX Composite price returns converted to USD (^JKSE / USD-IDR, Yahoo Finance).",
        "Small sample: ~25 quarters. Treat any single result with caution; |t| ≥ 2 is the usual bar.",
    ]
    return FlowStudy(tab, tests, splits,
                     float(np.mean(hits)) if hits else None, float(np.mean(base)) if base else None, notes)
