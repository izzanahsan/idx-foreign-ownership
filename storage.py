"""SQLite persistence for normalized IDX holdings snapshots.

Every scraper emits rows matching HOLDING_COLUMNS; one row = one Indonesian
security held by one fund at one report date.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

import pandas as pd

import config

HOLDING_COLUMNS = [
    "manager_key",     # config.Manager.key
    "fund_ticker",     # e.g. "EIDO"; "NBIM" for Norges
    "fund_name",
    "source",          # "SEC_NPORT" | "NBIM" | "SYNTHETIC"
    "report_date",     # ISO date of the portfolio snapshot (N-PORT repPdDate)
    "filing_id",       # SEC accession number or NBIM file name
    "isin",
    "ticker",          # IDX ticker if known, else None
    "issuer_name",
    "shares",          # float, may be None
    "value_usd",       # float
    "pct_of_fund",     # % of fund net assets, may be None
    "fund_net_assets_usd",
]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS holdings (
    manager_key TEXT NOT NULL,
    fund_ticker TEXT NOT NULL,
    fund_name TEXT,
    source TEXT NOT NULL,
    report_date TEXT NOT NULL,
    filing_id TEXT NOT NULL,
    isin TEXT NOT NULL,
    ticker TEXT,
    issuer_name TEXT,
    shares REAL,
    value_usd REAL NOT NULL,
    pct_of_fund REAL,
    fund_net_assets_usd REAL,
    PRIMARY KEY (fund_ticker, report_date, isin)
);
CREATE INDEX IF NOT EXISTS ix_holdings_date ON holdings(report_date);
CREATE INDEX IF NOT EXISTS ix_holdings_mgr ON holdings(manager_key);

CREATE TABLE IF NOT EXISTS ingest_log (
    filing_id TEXT PRIMARY KEY,
    fund_ticker TEXT NOT NULL,
    report_date TEXT,
    n_idx_rows INTEGER,
    ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_holdings(rows: Iterable[dict], db_path: Path | None = None) -> int:
    """Insert or replace holdings rows; returns the number written."""
    rows = list(rows)
    if not rows:
        return 0
    placeholders = ",".join("?" * len(HOLDING_COLUMNS))
    sql = f"INSERT OR REPLACE INTO holdings ({','.join(HOLDING_COLUMNS)}) VALUES ({placeholders})"
    with connect(db_path) as conn:
        conn.executemany(sql, [tuple(r.get(c) for c in HOLDING_COLUMNS) for r in rows])
    return len(rows)


def replace_snapshot(fund_ticker: str, report_date: str, rows: list[dict],
                     db_path: Path | None = None) -> int:
    """Atomically swap one fund's snapshot (so amendments drop stale positions)."""
    placeholders = ",".join("?" * len(HOLDING_COLUMNS))
    sql = f"INSERT OR REPLACE INTO holdings ({','.join(HOLDING_COLUMNS)}) VALUES ({placeholders})"
    with connect(db_path) as conn:
        conn.execute("DELETE FROM holdings WHERE fund_ticker=? AND report_date=?",
                     (fund_ticker, report_date))
        conn.executemany(sql, [tuple(r.get(c) for c in HOLDING_COLUMNS) for r in rows])
    return len(rows)


def mark_ingested(filing_id: str, fund_ticker: str, report_date: str | None,
                  n_rows: int, db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ingest_log (filing_id, fund_ticker, report_date, n_idx_rows) "
            "VALUES (?,?,?,?)", (filing_id, fund_ticker, report_date, n_rows))


def ingested_filing_ids(db_path: Path | None = None) -> set[str]:
    with connect(db_path) as conn:
        return {r[0] for r in conn.execute("SELECT filing_id FROM ingest_log")}


def load_holdings(db_path: Path | None = None) -> pd.DataFrame:
    with connect(db_path) as conn:
        df = pd.read_sql_query("SELECT * FROM holdings", conn)
    if not df.empty:
        df["report_date"] = pd.to_datetime(df["report_date"])
    return df


def export_parquet(db_path: Path | None = None, out: Path | None = None) -> Path:
    out = Path(out or config.PROCESSED_DIR / "holdings.parquet")
    load_holdings(db_path).to_parquet(out, index=False)
    return out
