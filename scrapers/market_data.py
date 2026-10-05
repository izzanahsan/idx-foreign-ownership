"""Benchmark market data: IDX Composite (^JKSE) and USD/IDR, quarterly, cached.

Source: Yahoo Finance chart API (monthly bars). Used only for the flow-signal
study; the holdings pipeline does not depend on it.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date

import pandas as pd
import requests

import config
from analytics.tracker_engine import quarter_label

logger = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
SYMBOLS = {"jci": "^JKSE", "usdidr": "IDR=X"}
CACHE_TTL = 24 * 3600


def _fetch(symbol: str, refresh: bool = False) -> dict:
    cache = config.RAW_DIR / "market" / f"{symbol.replace('^', '').replace('=', '_')}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists() and not refresh and time.time() - cache.stat().st_mtime < CACHE_TTL:
        return json.loads(cache.read_text())
    start = int(time.mktime(date(config.START_YEAR - 1, 1, 1).timetuple()))
    resp = requests.get(CHART_URL.format(symbol=symbol), timeout=30,
                        params={"period1": start, "period2": int(time.time()), "interval": "1mo"},
                        headers={"User-Agent": "Mozilla/5.0 (compatible; IndoTracker/1.0)"})
    resp.raise_for_status()
    cache.write_text(resp.text)
    return resp.json()


def _quarterly_close(payload: dict) -> pd.Series:
    res = payload["chart"]["result"][0]
    ind = res["indicators"]
    closes = (ind.get("adjclose") or [{}])[0].get("adjclose") or ind["quote"][0]["close"]
    s = pd.Series(closes, index=pd.to_datetime(res["timestamp"], unit="s")).dropna()
    s.index = [quarter_label(t.date()) for t in s.index]
    return s.groupby(level=0).last()


def quarterly_market(refresh: bool = False) -> pd.DataFrame:
    """Quarter-end IDX Composite in IDR and USD, with USD returns (percent).

    Columns: jci, usdidr, jci_usd, ret (same-quarter USD return).
    The latest quarter may be partial (last available month).
    """
    jci = _quarterly_close(_fetch(SYMBOLS["jci"], refresh))
    fx = _quarterly_close(_fetch(SYMBOLS["usdidr"], refresh))
    m = pd.DataFrame({"jci": jci, "usdidr": fx}).dropna().sort_index()
    m["jci_usd"] = m["jci"] / m["usdidr"]
    m["ret"] = m["jci_usd"].pct_change() * 100
    return m
