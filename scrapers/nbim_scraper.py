"""NBIM (Norway GPFG) holdings: discover snapshot dates and ingest Indonesian equities.

Endpoints (the same ones nbim.no/en/investments/all-investments calls):
  init.json                              -> list of published snapshot dates
  report/?assetType=eq&date=YYYY-MM-DD&fileType=csv
                                         -> full equity holdings, UTF-16 ';'-separated,
                                            columns Region;Country;Name;Industry;
                                            Market Value(NOK);Market Value(USD);Voting;
                                            Ownership;Incorporation Country
Year-end files exist for every year; half-year (06-30) files exist from 2024.
The CSV has no ISIN, so positions are keyed by IDX ticker when the issuer name
resolves, else by a normalized-name pseudo key ("NAME:...").
"""
from __future__ import annotations

import io
import logging
import time
from datetime import date
from pathlib import Path
from typing import Callable

import pandas as pd
import requests

import config
import idx_securities
import storage

logger = logging.getLogger(__name__)

Progress = Callable[[str], None] | None
FUND_TICKER = "NBIM"
FUND_NAME = "Government Pension Fund Global (NBIM)"


class NbimError(RuntimeError):
    """Raised when NBIM data cannot be fetched or parsed."""


def _get(url: str, params: dict | None = None, attempts: int = 4) -> bytes:
    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, params=params, timeout=60,
                                headers={"User-Agent": config.NBIM_USER_AGENT})
            if resp.status_code == 200:
                return resp.content
            if resp.status_code == 404:
                raise NbimError(f"404 Not Found: {resp.url}")
            err: Exception = NbimError(f"HTTP {resp.status_code}: {resp.url}")
        except requests.RequestException as exc:
            err = exc
        logger.warning("NBIM request failed (%d/%d): %s", attempt, attempts, err)
        if attempt < attempts:
            time.sleep(delay)
            delay *= 2
    raise NbimError(f"Giving up on {url}: {err}")


def available_dates(refresh: bool = False) -> list[date]:
    """Published snapshot dates within the configured window, oldest first."""
    cache = config.RAW_DIR / "nbim" / "init.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if refresh or not cache.exists() or time.time() - cache.stat().st_mtime > 86400:
        cache.write_bytes(_get(config.NBIM_INIT_URL))
    import json

    details = json.loads(cache.read_bytes())["data"]["details"]
    out = sorted({date.fromisoformat(d["date"][:10]) for d in details})
    inside = [d for d in out if config.START_YEAR <= d.year <= config.END_YEAR]
    # Keep the last pre-window snapshot so early quarters can carry it forward.
    before = [d for d in out if d.year < config.START_YEAR]
    return before[-1:] + inside


def _raw_path(snapshot: date) -> Path:
    return config.RAW_DIR / "nbim" / f"EQ_{snapshot.isoformat()}.csv"


def download_snapshot(snapshot: date, refresh: bool = False) -> Path:
    """Download (and cache) the full equity CSV for one snapshot date."""
    path = _raw_path(snapshot)
    if path.exists() and not refresh:
        return path
    body = _get(config.NBIM_REPORT_URL, params={
        "assetType": "eq", "date": snapshot.isoformat(), "fileType": "csv"})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _decode(raw: bytes) -> str:
    for enc in ("utf-16", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise NbimError("Unrecognized NBIM CSV encoding")


def _pct(v: object) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(str(v).replace("%", "").replace(",", ".").strip())
    except ValueError:
        return None


def parse_snapshot(raw: bytes) -> pd.DataFrame:
    """Full holdings file -> Indonesian rows with columns name, industry, value_usd, ownership_pct."""
    df = pd.read_csv(io.StringIO(_decode(raw)), sep=";", dtype=str)
    df.columns = [c.strip() for c in df.columns]
    required = {"Country", "Name", "Market Value(USD)"}
    if not required <= set(df.columns):
        raise NbimError(f"Unexpected NBIM columns: {list(df.columns)}")
    idn = df[df["Country"].str.strip().str.casefold() == config.NBIM_COUNTRY.casefold()].copy()
    return pd.DataFrame({
        "name": idn["Name"].str.strip(),
        "industry": idn.get("Industry"),
        "value_usd": pd.to_numeric(idn["Market Value(USD)"].str.replace(",", ""), errors="coerce"),
        "ownership_pct": idn.get("Ownership", pd.Series(index=idn.index, dtype=str)).map(_pct),
    }).dropna(subset=["value_usd"])


def normalize_rows(idn: pd.DataFrame, snapshot: date) -> list[dict]:
    rows: dict[str, dict] = {}
    for rec in idn.itertuples(index=False):
        sec = idx_securities.resolve(rec.name)
        key = f"TICKER:{sec[0]}" if sec else "NAME:" + idx_securities.normalize_name(rec.name)
        if key in rows:  # multiple share lines for one issuer
            rows[key]["value_usd"] += float(rec.value_usd)
            continue
        rows[key] = {
            "manager_key": "nbim", "fund_ticker": FUND_TICKER, "fund_name": FUND_NAME,
            "source": "NBIM", "report_date": snapshot.isoformat(),
            "filing_id": f"NBIM_EQ_{snapshot.isoformat()}", "isin": key,
            "ticker": sec[0] if sec else None, "issuer_name": rec.name, "shares": None,
            "value_usd": float(rec.value_usd), "pct_of_fund": None, "fund_net_assets_usd": None,
        }
    return list(rows.values())


def ingest_all(refresh: bool = False, progress: Progress = None) -> dict[str, int]:
    """Download every in-window NBIM snapshot and store its Indonesian rows."""
    say = progress or (lambda _m: None)
    done = set() if refresh else storage.ingested_filing_ids()
    written = 0
    for snap in available_dates(refresh=refresh):
        filing_id = f"NBIM_EQ_{snap.isoformat()}"
        if filing_id in done:
            continue
        try:
            raw = download_snapshot(snap, refresh=refresh).read_bytes()
            rows = normalize_rows(parse_snapshot(raw), snap)
        except NbimError as exc:
            logger.warning("NBIM %s skipped: %s", snap, exc)
            continue
        written += storage.replace_snapshot(FUND_TICKER, snap.isoformat(), rows)
        storage.mark_ingested(filing_id, FUND_TICKER, snap.isoformat(), len(rows))
        say(f"NBIM {snap}: {len(rows)} Indonesian positions")
    return {FUND_TICKER: written}
