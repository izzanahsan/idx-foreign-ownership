"""Synthetic 2020-2026 fixture + end-to-end tests (no network).

Run: .venv/bin/python -m pytest -q
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import storage  # noqa: E402
from analytics.tracker_engine import (TrackerEngine, all_quarters, prev_quarter,  # noqa: E402
                                      quarter_bounds, quarter_label)

# (ticker-resolvable issuer name, synthetic ISIN, base USD weight)
SECURITIES = [
    ("Bank Central Asia Tbk PT", "ID1000109507", 30), ("Bank Rakyat Indonesia Persero Tbk PT", "ID1000118201", 22),
    ("Bank Mandiri Persero Tbk PT", "ID1000095003", 18), ("Telkom Indonesia Persero Tbk PT", "ID1000129000", 12),
    ("Astra International Tbk PT", "ID1000122807", 10), ("Bank Negara Indonesia Persero Tbk PT", "ID1000096605", 7),
    ("GoTo Gojek Tokopedia Tbk PT", "ID1000166903", 5), ("Unilever Indonesia Tbk PT", "ID1000095706", 4),
    ("Indofood CBP Sukses Makmur Tbk PT", "ID1000116700", 4), ("Amman Mineral Internasional PT", "ID1000187209", 4),
    ("Kalbe Farma Tbk PT", "ID1000125107", 3), ("Sumber Alfaria Trijaya Tbk PT", "ID1000128705", 3),
    ("Merdeka Copper Gold Tbk PT", "ID1000134406", 3), ("Charoen Pokphand Indonesia Tbk PT", "ID1000117708", 2),
    ("Adaro Energy Indonesia Tbk PT", "ID1000111305", 2), ("United Tractors Tbk PT", "ID1000058407", 2),
    ("Vale Indonesia Tbk PT", "ID1000109309", 2), ("Bank Syariah Indonesia Tbk PT", "ID1000142904", 2),
    ("Ciputra Development Tbk PT", "ID1000115306", 1), ("Obscure Smallcap Tbk PT", "ID1000999999", 1),
]
# Fund-level scale (USD) roughly in line with real IDX allocations.
SCALE = {"vanguard": 1.7e9, "blackrock": 1.5e9, "fidelity": 2.0e8, "capital_group": 7.0e8, "state_street": 1.2e8,
         "jpmorgan": 9.0e7, "nbim": 2.0e9, "dfa": 1.6e8, "t_rowe": 6.0e7, "matthews": 8.0e7}
# Fiscal-quarter month offsets: N-PORT snapshot months differ by fund (e.g. EIDO Feb/May/Aug/Nov).
FISCAL_OFFSET = {"blackrock": -1, "capital_group": -2, "vanguard": 0}


def _month_end(y: int, m: int) -> date:
    return (pd.Timestamp(y, m, 1) + pd.offsets.MonthEnd(0)).date()


def build_synthetic_holdings(seed: int = 7, end: date = date(2026, 6, 30)) -> pd.DataFrame:
    """Deterministic holdings for all 10 managers, 2020Q1 to ``end``."""
    rng = np.random.default_rng(seed)
    rows = []
    market = 1.0
    market_path = {}
    for q in all_quarters():
        market *= float(np.exp(rng.normal(0.01, 0.08)))
        market_path[q] = market
    for m in config.MANAGERS:
        funds = m.funds or (config.FundSeries("NBIM", "GPFG (NBIM)", 0, "NBIM"),)
        if m.source == "NBIM":
            dates = [d for y in range(2019, 2027) for d in
                     ([date(y, 6, 30)] if y >= 2024 else []) + [date(y, 12, 31)]]
        else:
            off = FISCAL_OFFSET.get(m.key, 0)
            dates = [_month_end(y + (qm + off - 1) // 12, (qm + off - 1) % 12 + 1)
                     for y in range(2020, 2027) for qm in (3, 6, 9, 12)]
        # Matthews only starts reporting in 2021 -> exercises like-for-like QoQ.
        if m.key == "matthews":
            dates = [d for d in dates if d >= date(2021, 1, 1)]
        dates = [d for d in dates if d <= end]
        tilt = rng.uniform(0.6, 1.4, len(SECURITIES))
        for fund in funds:
            share = 1.0 / len(funds)
            for d in dates:
                q_mult = market_path.get(quarter_label(d), 1.0)
                for (name, isin, w), t in zip(SECURITIES, tilt):
                    if m.key in ("t_rowe", "matthews") and w < 3:
                        continue  # concentrated active books
                    val = SCALE[m.key] * share * q_mult * w * t / 140 * float(np.exp(rng.normal(0, 0.05)))
                    rows.append({
                        "manager_key": m.key, "fund_ticker": fund.ticker, "fund_name": fund.name,
                        "source": "SYNTHETIC" if m.source != "NBIM" else "NBIM",
                        "report_date": d.isoformat(), "filing_id": f"SYN-{fund.ticker}-{d}",
                        "isin": isin, "ticker": None, "issuer_name": name,
                        "shares": round(val / 0.25), "value_usd": val, "pct_of_fund": None,
                        "fund_net_assets_usd": None,
                    })
    df = pd.DataFrame(rows, columns=storage.HOLDING_COLUMNS)
    import idx_securities
    df["ticker"] = df["issuer_name"].map(lambda n: (idx_securities.resolve(n) or (None,))[0])
    return df


@pytest.fixture(scope="module")
def synthetic() -> pd.DataFrame:
    return build_synthetic_holdings()


@pytest.fixture(scope="module")
def engine(synthetic: pd.DataFrame) -> TrackerEngine:
    return TrackerEngine(holdings=synthetic)


# --------------------------------------------------------------------------- #
# Quarter math
# --------------------------------------------------------------------------- #
def test_quarter_helpers() -> None:
    assert quarter_label(date(2025, 11, 30)) == "2025Q4"
    assert quarter_bounds("2024Q1") == (date(2024, 1, 1), date(2024, 3, 31))
    assert quarter_bounds("2023Q4") == (date(2023, 10, 1), date(2023, 12, 31))
    assert prev_quarter("2021Q1") == "2020Q4"
    assert len(all_quarters()) == 28


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
def test_full_window_available(engine: TrackerEngine) -> None:
    qs = engine.available_quarters()
    assert qs[0] == "2020Q1" and qs[-1] == "2026Q2"
    assert len(qs) == 26


def test_fiscal_quarter_snapping(engine: TrackerEngine) -> None:
    snap = engine.snapshot("2025Q4")
    eido = snap[snap["fund_ticker"] == "EIDO"]
    assert set(eido["report_date"].dt.date) == {date(2025, 11, 30)}
    assert not eido["stale"].any()


def test_nbim_carry_forward_is_flagged(engine: TrackerEngine) -> None:
    snap = engine.snapshot("2021Q2")  # NBIM only year-end before 2024
    nb = snap[snap["manager_key"] == "nbim"]
    assert set(nb["report_date"].dt.date) == {date(2020, 12, 31)}
    assert nb["stale"].all()
    snap24 = engine.snapshot("2024Q3")  # half-year available from 2024
    assert set(snap24.loc[snap24["manager_key"] == "nbim", "report_date"].dt.date) == {date(2024, 6, 30)}


def test_report_shapes_and_ranking(engine: TrackerEngine) -> None:
    r = engine.build_report("2025Q2")
    assert len(r.manager_table) == 10
    assert r.manager_table["value_usd"].is_monotonic_decreasing
    assert len(r.top_holdings) == 15
    assert r.top_holdings.iloc[0]["ticker"] == "BBCA"
    assert r.top_holdings["n_holders"].max() == 10
    assert r.sector_table["share_pct"].sum() == pytest.approx(100.0)
    assert r.total_idx_usd == pytest.approx(r.manager_table["value_usd"].sum())
    assert len(r.movers) <= 10
    assert set(r.history["quarter"]) <= set(all_quarters())


def test_qoq_is_like_for_like(engine: TrackerEngine) -> None:
    r = engine.build_report("2021Q1")  # Matthews' first quarter
    mt = r.manager_table.set_index("manager_key")
    assert pd.isna(mt.loc["matthews", "value_usd_prev"])
    expected = mt.drop("matthews")["value_usd"].sum() - mt.drop("matthews")["value_usd_prev"].sum()
    assert r.qoq_delta_usd == pytest.approx(expected)
    assert any("like-for-like" in n for n in r.notes)


def test_first_quarter_compares_only_pre_window_baseline(engine: TrackerEngine) -> None:
    # Only NBIM has a pre-window (2019-12-31) snapshot, so 2020Q1 QoQ is NBIM-only.
    r = engine.build_report("2020Q1")
    mt = r.manager_table.set_index("manager_key")
    assert mt["value_usd_prev"].notna().sum() == 1 and pd.notna(mt.loc["nbim", "value_usd_prev"])
    assert r.qoq_delta_usd == pytest.approx(mt.loc["nbim", "delta_usd"])


def test_no_prior_data_means_no_qoq() -> None:
    df = build_synthetic_holdings()
    e = TrackerEngine(holdings=df[df["manager_key"] != "nbim"])
    r = e.build_report("2020Q1")
    assert r.manager_table["value_usd_prev"].isna().all()
    assert r.qoq_delta_usd is None and r.movers.empty


def test_drilldown_weights_sum_to_100(engine: TrackerEngine) -> None:
    d = engine.manager_holdings("2024Q4", "vanguard")
    assert d["weight_pct"].sum() == pytest.approx(100.0)


def test_empty_engine_is_safe() -> None:
    e = TrackerEngine(holdings=pd.DataFrame(columns=storage.HOLDING_COLUMNS))
    assert e.available_quarters() == []
    r = e.build_report("2024Q1")
    assert r.total_idx_usd == 0 and r.manager_table.empty


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def test_storage_roundtrip_and_amendment_replace(tmp_path: Path, synthetic: pd.DataFrame) -> None:
    db = tmp_path / "t.db"
    sample = synthetic[synthetic["fund_ticker"] == "EIDO"].head(40)
    assert storage.upsert_holdings(sample.to_dict("records"), db_path=db) == 40
    first_date = sample["report_date"].iloc[0]
    amended = sample[sample["report_date"] == first_date].head(3).to_dict("records")
    storage.replace_snapshot("EIDO", first_date, amended, db_path=db)
    out = storage.load_holdings(db)
    assert (out["report_date"].dt.strftime("%Y-%m-%d") == first_date).sum() == 3


# --------------------------------------------------------------------------- #
# Parsers (offline fixtures shaped like the real documents)
# --------------------------------------------------------------------------- #
NPORT_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/nport"><formData>
 <genInfo><seriesId>S000028553</seriesId><repPdDate>2025-11-30</repPdDate></genInfo>
 <fundInfo><netAssets>1000000.00</netAssets></fundInfo>
 <invstOrSecs>
  <invstOrSec><name>Bank Central Asia Tbk PT</name><identifiers><isin value="ID1000109507"/></identifiers>
   <balance>1000</balance><units>NS</units><valUSD>600000</valUSD><pctVal>60</pctVal><assetCat>EC</assetCat>
   <invCountry>ID</invCountry></invstOrSec>
  <invstOrSec><name>Bank Central Asia Tbk PT</name><identifiers><isin value="ID1000109507"/></identifiers>
   <balance>500</balance><units>NS</units><valUSD>300000</valUSD><pctVal>30</pctVal><assetCat>EC</assetCat>
   <invCountry>ID</invCountry></invstOrSec>
  <invstOrSec><name>Indonesia Govt Bond</name><identifiers><isin value="ID0000000001"/></identifiers>
   <balance>1</balance><units>PA</units><valUSD>50000</valUSD><assetCat>DBT</assetCat><invCountry>ID</invCountry></invstOrSec>
  <invstOrSec><name>INDONESIAN RUPIAH</name><identifiers><other otherDesc="CCY" value="IDR"/></identifiers>
   <balance>1</balance><units>NC</units><valUSD>70000</valUSD><invCountry>ID</invCountry></invstOrSec>
  <invstOrSec><name>Apple Inc</name><identifiers><isin value="US0378331005"/></identifiers>
   <balance>1</balance><units>NS</units><valUSD>50000</valUSD><assetCat>EC</assetCat><invCountry>US</invCountry></invstOrSec>
 </invstOrSecs></formData></edgarSubmission>"""

ATOM = b"""<?xml version="1.0" encoding="ISO-8859-1" ?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><content type="text/xml"><accession-number>0001410368-26-006900</accession-number>
<filing-date>2026-01-23</filing-date><filing-href>https://www.sec.gov/Archives/edgar/data/1100663/000141036826006900/0001410368-26-006900-index.htm</filing-href>
<filing-type>NPORT-P</filing-type></content></entry>
<entry><content type="text/xml"><accession-number>0000000000-26-000001</accession-number>
<filing-date>2026-01-24</filing-date><filing-href>x</filing-href><filing-type>N-CEN</filing-type></content></entry>
</feed>"""


def test_nport_parse_and_normalize() -> None:
    from scrapers import sec_edgar

    snap = sec_edgar.parse_nport_xml(NPORT_XML)
    assert snap.series_id == "S000028553" and snap.report_date == date(2025, 11, 30)
    assert len(snap.holdings) == 5
    m = config.MANAGERS_BY_KEY["blackrock"]
    rows = sec_edgar.normalize_rows(m, m.funds[0], snap, "ACC")
    assert len(rows) == 1  # bond, IDR cash, US stock dropped; duplicate ISIN aggregated
    r = rows[0]
    assert r["ticker"] == "BBCA" and r["value_usd"] == 900000 and r["shares"] == 1500 and r["pct_of_fund"] == 90


def test_atom_parse_filters_form_type() -> None:
    from scrapers import sec_edgar

    refs = sec_edgar.parse_atom(ATOM, default_cik=1)
    assert len(refs) == 1
    assert refs[0].registrant_cik == 1100663
    assert refs[0].doc_url.endswith("/1100663/000141036826006900/primary_doc.xml")


def test_nbim_csv_parse() -> None:
    from scrapers import nbim_scraper

    csv = ("Region;Country;Name;Industry;Market Value(NOK);Market Value(USD);Voting;Ownership;Incorporation Country\n"
           "Asia;Indonesia;Bank Central Asia Tbk PT;Financials;100;10;0.3%;0.3%;Indonesia\n"
           "Asia;Indonesia;Unknown Co Tbk PT;Industrials;50;5;1.1%;1.1%;Indonesia\n"
           "Asia;Japan;Toyota Motor Corp;Consumer Discretionary;999;99;0.1%;0.1%;Japan\n").encode("utf-16")
    idn = nbim_scraper.parse_snapshot(csv)
    assert len(idn) == 2 and idn["value_usd"].sum() == 15
    rows = nbim_scraper.normalize_rows(idn, date(2025, 12, 31))
    keys = {r["isin"] for r in rows}
    assert "TICKER:BBCA" in keys and any(k.startswith("NAME:unknown co") for k in keys)


def test_sec_and_nbim_rows_join_on_ticker() -> None:
    sec = {"manager_key": "blackrock", "fund_ticker": "EIDO", "source": "SEC_NPORT", "report_date": "2025-11-30",
           "filing_id": "a", "isin": "ID1000109507", "ticker": "BBCA", "issuer_name": "Bank Central Asia Tbk PT",
           "value_usd": 1.0}
    nb = sec | {"manager_key": "nbim", "fund_ticker": "NBIM", "source": "NBIM", "report_date": "2025-12-31",
                "isin": "TICKER:BBCA", "value_usd": 2.0}
    e = TrackerEngine(holdings=pd.DataFrame([sec, nb], columns=storage.HOLDING_COLUMNS))
    top = e.build_report("2025Q4").top_holdings
    assert len(top) == 1 and top.iloc[0]["value_usd"] == 3.0 and top.iloc[0]["n_holders"] == 2
    assert top.iloc[0]["isin"] == "ID1000109507"


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
def test_chart_export_png_pdf(tmp_path: Path, engine: TrackerEngine) -> None:
    from pypdf import PdfReader  # type: ignore[import-not-found]
    from visualization import chart_exporter

    paths = chart_exporter.render_report(engine.build_report("2024Q4"), tmp_path, ("png", "pdf"))
    png, pdf = paths
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and png.stat().st_size > 200_000
    assert len(PdfReader(pdf).pages) == 2


def test_chart_export_dark_and_empty_quarter(tmp_path: Path) -> None:
    from visualization import chart_exporter

    e = TrackerEngine(holdings=pd.DataFrame(columns=storage.HOLDING_COLUMNS))
    out = chart_exporter.render_report(e.build_report("2020Q1"), tmp_path, ("png",), theme="dark")
    assert out[0].name.endswith("_dark.png") and out[0].exists()


def test_terminal_dashboard_renders(engine: TrackerEngine) -> None:
    from rich.console import Console
    from visualization import terminal_ui

    terminal_ui.console = Console(record=True, width=160, file=open("/dev/null", "w"))
    terminal_ui.render_dashboard(engine.build_report("2026Q2"))
    terminal_ui.render_manager_drilldown(engine, "2026Q2", "nbim")
    terminal_ui.render_comparison(engine, "2020Q1", "2026Q2")
    text = terminal_ui.console.export_text()
    assert "Top 15 IDX holdings" in text and "BBCA" in text and "Norges" in text


def test_fmt_helpers() -> None:
    from visualization.chart_exporter import fmt_pct, fmt_usd

    assert fmt_usd(1.234e9) == "$1.23B" and fmt_usd(-4.5e8) == "-$450.0M" and fmt_usd(None) == "—"
    assert fmt_pct(3.14159) == "+3.1%" and fmt_pct(float("nan")) == "—"


def test_provisional_quarter_detection(engine: TrackerEngine) -> None:
    assert engine.is_provisional("2026Q2", today=date(2026, 8, 1))
    assert not engine.is_provisional("2026Q2", today=date(2026, 9, 20))
    assert engine.latest_complete_quarter(today=date(2026, 8, 1)) == "2026Q1"
    assert engine.latest_complete_quarter(today=date(2026, 10, 5)) == "2026Q2"


def test_web_report_json_is_strict_json(engine: TrackerEngine) -> None:
    import json
    from web.server import report_json

    payload = report_json(engine.build_report("2025Q1"))
    text = json.dumps(payload, allow_nan=False)  # NaN would break the browser's JSON.parse
    assert payload["quarter"] == "2025Q1" and len(payload["top"]) == 15
    assert len(payload["history"]["series"]) == 10 and '"provisional"' in text


# --------------------------------------------------------------------------- #
# Flow signal
# --------------------------------------------------------------------------- #
def _flow_rows(fund: str, d: str, positions: dict[str, tuple[float, float]]) -> list[dict]:
    return [{"manager_key": "vanguard", "fund_ticker": fund, "source": "SEC_NPORT", "report_date": d,
             "filing_id": f"{fund}{d}", "isin": "ID" + sid, "ticker": sid, "issuer_name": sid,
             "shares": sh, "value_usd": sh * px} for sid, (sh, px) in positions.items()]


def test_flows_strip_price_moves_and_detect_splits() -> None:
    from analytics.flow_signal import estimate_fund_flows

    rows = []
    for fund, buy in (("F1", 0), ("F2", 0), ("F3", 100)):
        rows += _flow_rows(fund, "2021-09-30", {"BBCA": (1000, 10.0), "TLKM": (500, 4.0)})
        # BBCA 1:5 split (shares x5, price /5 then +10%); TLKM price doubles; F3 also buys 100 TLKM
        rows += _flow_rows(fund, "2021-12-31", {"BBCA": (5000, 2.2), "TLKM": (500 + buy, 8.0)})
    df = pd.DataFrame(rows)
    df["report_date"] = pd.to_datetime(df["report_date"])
    flows, splits = estimate_fund_flows(df)
    assert splits.iloc[0]["sid"] == "BBCA" and splits.iloc[0]["factor"] == 5
    by_fund = flows.set_index("fund_ticker")["flow_usd"]
    assert by_fund["F1"] == pytest.approx(0.0) and by_fund["F2"] == pytest.approx(0.0)  # price moves only
    assert by_fund["F3"] == pytest.approx(100 * (4.0 + 8.0) / 2)                      # real buying only


def test_single_fund_share_jump_is_not_a_split() -> None:
    from analytics.flow_signal import estimate_fund_flows

    rows = _flow_rows("F1", "2022-03-31", {"XXXX": (1000, 10.0)}) + _flow_rows("F1", "2022-06-30", {"XXXX": (3000, 5.0)})
    df = pd.DataFrame(rows)
    df["report_date"] = pd.to_datetime(df["report_date"])
    flows, splits = estimate_fund_flows(df)
    assert splits.empty  # one fund alone can't confirm a split
    assert flows["flow_usd"].iloc[0] == pytest.approx(2000 * 7.5)


def test_hac_regression_recovers_slope() -> None:
    from analytics.flow_signal import ols_hac

    rng = np.random.default_rng(0)
    x = rng.normal(size=200)
    y = 2.0 * x + rng.normal(scale=0.5, size=200)
    slope, t, r2 = ols_hac(y, x, lags=2)
    assert slope == pytest.approx(2.0, abs=0.1) and t > 10 and r2 > 0.9


def test_study_on_synthetic_runs() -> None:
    from analytics.flow_signal import run_study

    df = build_synthetic_holdings()
    df.loc[df["source"] == "SYNTHETIC", "source"] = "SEC_NPORT"
    qs = all_quarters()[:26]
    jci = pd.Series(np.linspace(6000, 7000, len(qs)), index=qs)
    market = pd.DataFrame({"jci": jci, "usdidr": 15000.0})
    market["jci_usd"] = market["jci"] / market["usdidr"]
    market["ret"] = market["jci_usd"].pct_change() * 100
    st = run_study(df, market)
    assert len(st.tests) == 5 and st.verdict in {"predictive", "contrarian", "not predictive"}
    assert not st.table.empty


def test_flows_json_has_period_fields() -> None:
    from analytics.flow_signal import run_study
    from web.server import flows_json

    df = build_synthetic_holdings()
    df.loc[df["source"] == "SYNTHETIC", "source"] = "SEC_NPORT"
    qs = all_quarters()[:26]
    market = pd.DataFrame({"jci": np.linspace(6000, 7000, len(qs)), "usdidr": 15000.0}, index=qs)
    market["jci_usd"] = market["jci"] / market["usdidr"]
    market["ret"] = market["jci_usd"].pct_change() * 100
    rows = flows_json(run_study(df, market))["rows"]
    assert rows and {"quarter", "flow_usd", "aum0", "ret", "fwd1", "fwd4"} <= set(rows[0])
    assert all(r["aum0"] > 0 for r in rows)
