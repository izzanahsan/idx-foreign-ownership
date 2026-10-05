"""SEC Form N-PORT ingestion: list filings per fund series, download, keep IDX rows.

Flow per fund series:
  browse-edgar Atom feed (by series id) -> accession list
  -> Archives/.../primary_doc.xml (cached forever; filings are immutable)
  -> parse invstOrSec rows -> keep Indonesian equity -> storage.upsert_holdings
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

import requests
from lxml import etree

import config
import idx_securities
import storage

logger = logging.getLogger(__name__)

Progress = Callable[[str], None] | None
ATOM_TTL_SECONDS = 24 * 3600
EQUITY_ASSET_CATS = {"EC", "EP"}


class SecEdgarError(RuntimeError):
    """Raised when EDGAR cannot be reached after retries."""


class RateLimiter:
    """Thread-safe minimum-interval limiter (SEC fair access: <= 10 req/s)."""

    def __init__(self, max_per_second: float) -> None:
        self._interval = 1.0 / max_per_second
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = self._next
            self._next = now + self._interval


_limiter = RateLimiter(config.SEC_MAX_RPS)
_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if not config.SEC_USER_AGENT:
        raise SecEdgarError('SEC requires a contact: set SEC_USER_AGENT="Your Name you@example.com" '
                            "(environment variable or .env.local)")
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": config.SEC_USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
        })
    return _session


def _get(url: str, params: dict | None = None, attempts: int = 5) -> bytes:
    """GET with rate limiting and exponential backoff on 429/5xx/network errors."""
    delay = 1.0
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        _limiter.wait()
        try:
            resp = _get_session().get(url, params=params, timeout=config.SEC_TIMEOUT)
        except requests.RequestException as exc:
            last_err = exc
        else:
            if resp.status_code == 200:
                return resp.content
            if resp.status_code == 404:
                raise SecEdgarError(f"404 Not Found: {resp.url}")
            last_err = SecEdgarError(f"HTTP {resp.status_code}: {resp.url}")
            if resp.status_code in (429, 503) and resp.headers.get("Retry-After", "").isdigit():
                delay = max(delay, float(resp.headers["Retry-After"]))
            elif resp.status_code < 500 and resp.status_code != 429:
                raise last_err
        logger.warning("EDGAR request failed (attempt %d/%d): %s", attempt, attempts, last_err)
        if attempt < attempts:
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise SecEdgarError(f"Giving up on {url}: {last_err}")


# --------------------------------------------------------------------------- #
# Filing index
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FilingRef:
    accession: str
    filing_date: date
    form_type: str
    registrant_cik: int
    index_url: str

    @property
    def doc_url(self) -> str:
        return f"{config.SEC_ARCHIVES_URL}/{self.registrant_cik}/{self.accession.replace('-', '')}/primary_doc.xml"


def _local(tag: object) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _child_text(el: etree._Element, name: str) -> str | None:
    for child in el.iter():
        if _local(child.tag) == name:
            return (child.text or "").strip() or None
    return None


def _cik_from_href(href: str) -> int | None:
    parts = href.split("/edgar/data/")
    if len(parts) < 2:
        return None
    head = parts[1].split("/", 1)[0]
    return int(head) if head.isdigit() else None


def parse_atom(xml_bytes: bytes, default_cik: int) -> list[FilingRef]:
    root = etree.fromstring(xml_bytes)
    refs: list[FilingRef] = []
    for entry in root.iter():
        if _local(entry.tag) != "entry":
            continue
        acc = _child_text(entry, "accession-number") or _child_text(entry, "accession-nunber")
        fdate = _child_text(entry, "filing-date")
        ftype = _child_text(entry, "filing-type") or ""
        href = _child_text(entry, "filing-href") or ""
        if not acc or not fdate or not ftype.startswith("NPORT-P"):
            continue
        refs.append(FilingRef(acc, date.fromisoformat(fdate), ftype,
                              _cik_from_href(href) or default_cik, href))
    return refs


def list_nport_filings(series_id: str, registrant_cik: int, refresh: bool = False) -> list[FilingRef]:
    """All NPORT-P(/A) filings for a series within the configured window."""
    cache_dir = config.RAW_DIR / "edgar" / "atom"
    cache_dir.mkdir(parents=True, exist_ok=True)
    refs: list[FilingRef] = []
    start = 0
    while True:
        cache = cache_dir / f"{series_id}_{start}.xml"
        fresh = cache.exists() and (time.time() - cache.stat().st_mtime) < ATOM_TTL_SECONDS
        if fresh and not refresh:
            body = cache.read_bytes()
        else:
            body = _get(config.SEC_BROWSE_URL, params={
                "action": "getcompany", "CIK": series_id, "type": "NPORT-P", "dateb": "",
                "owner": "include", "count": 100, "start": start, "output": "atom"})
            cache.write_bytes(body)
        page = parse_atom(body, registrant_cik)
        refs.extend(page)
        if len(page) < 100:
            break
        start += 100
    lo, hi = date(config.START_YEAR, 1, 1), date(config.END_YEAR + 1, 12, 31)
    return [r for r in refs if lo <= r.filing_date <= hi]


# --------------------------------------------------------------------------- #
# N-PORT document
# --------------------------------------------------------------------------- #
@dataclass
class NportSnapshot:
    series_id: str | None
    report_date: date | None
    net_assets: float | None
    holdings: list[dict] = field(default_factory=list)


def _num(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _first(el: etree._Element, path: str) -> etree._Element | None:
    found = el.xpath(path)
    return found[0] if found else None


def _text(el: etree._Element, path: str) -> str | None:
    node = _first(el, path)
    if node is None:
        return None
    raw = node.text if isinstance(node, etree._Element) else str(node)
    return (raw or "").strip() or None


def parse_nport_xml(xml_bytes: bytes) -> NportSnapshot:
    """Parse an N-PORT primary_doc.xml into a snapshot of all positions."""
    root = etree.fromstring(xml_bytes)
    gen = _first(root, ".//*[local-name()='genInfo']")
    fund = _first(root, ".//*[local-name()='fundInfo']")
    series_id = _text(gen, "./*[local-name()='seriesId']") if gen is not None else None
    rep = _text(gen, "./*[local-name()='repPdDate']") if gen is not None else None
    net = _num(_text(fund, "./*[local-name()='netAssets']")) if fund is not None else None

    holdings = []
    for sec in root.xpath(".//*[local-name()='invstOrSec']"):
        holdings.append({
            "issuer_name": _text(sec, "./*[local-name()='name']") or _text(sec, "./*[local-name()='title']"),
            "isin": _text(sec, "./*[local-name()='identifiers']/*[local-name()='isin']/@value"),
            "shares": _num(_text(sec, "./*[local-name()='balance']")),
            "units": _text(sec, "./*[local-name()='units']"),
            "value_usd": _num(_text(sec, "./*[local-name()='valUSD']")),
            "pct_of_fund": _num(_text(sec, "./*[local-name()='pctVal']")),
            "asset_cat": _text(sec, "./*[local-name()='assetCat']"),
            "country": _text(sec, "./*[local-name()='invCountry']"),
        })
    return NportSnapshot(series_id, date.fromisoformat(rep) if rep else None, net, holdings)


def is_indonesian(h: dict) -> bool:
    """Indonesian equity position with a usable USD value.

    Uncategorized rows count only with an IDX equity ISIN (ID1...): this keeps
    cash/FX lines ("INDONESIAN RUPIAH") and rights out. Indonesian ADRs (e.g.
    TLKM's US-ISIN ADR) are categorized EC with invCountry ID and are kept.
    """
    if h.get("value_usd") is None:
        return False
    isin = h.get("isin") or ""
    cat = h.get("asset_cat")
    if cat in EQUITY_ASSET_CATS:
        return h.get("country") == "ID" or isin.startswith("ID")
    return not cat and isin.startswith("ID1")


def _doc_cache(ref: FilingRef) -> Path:
    return config.RAW_DIR / "edgar" / str(ref.registrant_cik) / f"{ref.accession}.xml"


def fetch_filing(ref: FilingRef) -> bytes:
    path = _doc_cache(ref)
    if path.exists():
        return path.read_bytes()
    body = _get(ref.doc_url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return body


def normalize_rows(manager: config.Manager, fund: config.FundSeries,
                   snap: NportSnapshot, accession: str) -> list[dict]:
    """IDX rows in storage schema, duplicate ISINs within a snapshot aggregated."""
    agg: dict[str, dict] = {}
    for h in filter(is_indonesian, snap.holdings):
        key = h["isin"] or "NAME:" + idx_securities.normalize_name(h["issuer_name"])
        row = agg.get(key)
        if row is None:
            sec = idx_securities.resolve(h["issuer_name"])
            agg[key] = {
                "manager_key": manager.key, "fund_ticker": fund.ticker, "fund_name": fund.name,
                "source": "SEC_NPORT", "report_date": snap.report_date.isoformat(),
                "filing_id": accession, "isin": key, "ticker": sec[0] if sec else None,
                "issuer_name": h["issuer_name"],
                "shares": h["shares"] if h.get("units") == "NS" else None,
                "value_usd": h["value_usd"], "pct_of_fund": h["pct_of_fund"],
                "fund_net_assets_usd": snap.net_assets,
            }
        else:
            row["value_usd"] += h["value_usd"]
            if h.get("units") == "NS" and h["shares"] is not None:
                row["shares"] = (row["shares"] or 0) + h["shares"]
            if h["pct_of_fund"] is not None:
                row["pct_of_fund"] = (row["pct_of_fund"] or 0) + h["pct_of_fund"]
    return list(agg.values())


def ingest_fund(manager: config.Manager, fund: config.FundSeries,
                refresh: bool = False, progress: Progress = None) -> int:
    """Ingest every in-window N-PORT for one fund series; returns rows written."""
    say = progress or (lambda _m: None)
    refs = list_nport_filings(fund.series_id, fund.registrant_cik, refresh=refresh)
    done = set() if refresh else storage.ingested_filing_ids()
    todo = [r for r in refs if r.accession not in done]
    say(f"{fund.ticker}: {len(refs)} filings, {len(todo)} new")

    # Parse first, then keep only the latest filing per report date (amendments win).
    by_date: dict[date, list[tuple[FilingRef, NportSnapshot]]] = defaultdict(list)
    for i, ref in enumerate(sorted(todo, key=lambda r: r.filing_date), 1):
        try:
            snap = parse_nport_xml(fetch_filing(ref))
        except (SecEdgarError, etree.XMLSyntaxError) as exc:
            logger.warning("%s %s: skipped (%s)", fund.ticker, ref.accession, exc)
            continue
        if snap.series_id and snap.series_id != fund.series_id:
            logger.warning("%s %s: series %s != %s, skipped", fund.ticker, ref.accession,
                           snap.series_id, fund.series_id)
            storage.mark_ingested(ref.accession, fund.ticker, None, 0)
            continue
        if snap.report_date is None:
            continue
        by_date[snap.report_date].append((ref, snap))
        say(f"{fund.ticker}: parsed {i}/{len(todo)} ({snap.report_date})")

    written = 0
    for rep_date, items in sorted(by_date.items()):
        items.sort(key=lambda t: (t[0].filing_date, t[0].form_type.endswith("/A")))
        ref, snap = items[-1]
        rows = normalize_rows(manager, fund, snap, ref.accession)
        written += storage.replace_snapshot(fund.ticker, rep_date.isoformat(), rows)
        for r, _s in items:
            storage.mark_ingested(r.accession, fund.ticker, rep_date.isoformat(),
                                  len(rows) if r is ref else 0)
    return written


def ingest_all(manager_keys: list[str] | None = None, refresh: bool = False,
               progress: Progress = None) -> dict[str, int]:
    """Ingest all configured SEC managers; one fund's failure doesn't stop the run."""
    results: dict[str, int] = {}
    for m in config.MANAGERS:
        if m.source != "SEC_NPORT" or (manager_keys and m.key not in manager_keys):
            continue
        for fund in m.funds:
            try:
                results[fund.ticker] = ingest_fund(m, fund, refresh=refresh, progress=progress)
            except Exception:  # noqa: BLE001 - isolate per-fund failures
                logger.exception("Ingest failed for %s (%s)", fund.ticker, m.name)
                results[fund.ticker] = -1
    return results
