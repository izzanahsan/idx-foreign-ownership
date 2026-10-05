"""Central configuration: managers, fund series, SEC access policy, IDX ISIN map.

Fund series are resolved by *ticker* against SEC's company_tickers_mf.json at
runtime; the CIKs below are the filing registrant (trust) CIKs verified against
that file, which is what N-PORT filings are keyed on. The manager-level CIKs are
kept for reference only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("INDO_TRACKER_DATA", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
DB_PATH = PROCESSED_DIR / "holdings.db"
REPORTS_DIR = Path(os.environ.get("INDO_TRACKER_REPORTS", ROOT / "reports"))

# --------------------------------------------------------------------------- #
# SEC EDGAR access policy (https://www.sec.gov/os/accessing-edgar-data)
# --------------------------------------------------------------------------- #
def _local_env(key: str) -> str | None:
    """Read KEY=value from the git-ignored .env.local (keeps personal contact details out of the repo)."""
    path = ROOT / ".env.local"
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip().strip('"') or None
    return None


# SEC requires a contact in the User-Agent, e.g. "Your Name you@example.com".
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT") or _local_env("SEC_USER_AGENT") or ""
SEC_MAX_RPS = 8  # SEC hard limit is 10 req/s; stay under it
SEC_TIMEOUT = 30
SEC_MF_TICKERS_URL = "https://www.sec.gov/files/company_tickers_mf.json"
SEC_BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data"

# --------------------------------------------------------------------------- #
# Coverage window
# --------------------------------------------------------------------------- #
START_YEAR = 2020
END_YEAR = 2026


@dataclass(frozen=True)
class FundSeries:
    ticker: str
    name: str
    registrant_cik: int  # trust CIK that files the N-PORT
    series_id: str


@dataclass(frozen=True)
class Manager:
    key: str
    name: str
    rank: int  # priority order by aggregate IDX equity allocation
    source: str  # "SEC_NPORT" | "NBIM"
    manager_cik: str | None = None  # as supplied in the brief (adviser-level)
    funds: tuple[FundSeries, ...] = field(default_factory=tuple)


MANAGERS: tuple[Manager, ...] = (
    Manager("vanguard", "Vanguard Group", 1, "SEC_NPORT", "0000857489", (
        FundSeries("VWO", "Vanguard Emerging Markets Stock Index (VWO/VEIEX)", 857489, "S000005786"),
    )),
    Manager("blackrock", "BlackRock / iShares", 2, "SEC_NPORT", "0001100663", (
        FundSeries("EIDO", "iShares MSCI Indonesia ETF", 1100663, "S000028553"),
        FundSeries("IEMG", "iShares Core MSCI Emerging Markets ETF", 930667, "S000038923"),
    )),
    Manager("fidelity", "Fidelity Investments (FMR)", 3, "SEC_NPORT", "0000035315", (
        FundSeries("FEMKX", "Fidelity Emerging Markets Fund", 744822, "S000007110"),
    )),
    Manager("capital_group", "Capital Group / American Funds", 4, "SEC_NPORT", "0000051931", (
        FundSeries("NEWFX", "American Funds New World Fund", 1073330, "S000009633"),
        FundSeries("AEPGX", "American Funds EuroPacific Growth Fund", 719603, "S000009618"),
    )),
    Manager("state_street", "State Street Global Advisors", 5, "SEC_NPORT", "0001064642", (
        FundSeries("SPEM", "SPDR Portfolio Emerging Markets ETF", 1168164, "S000014048"),
    )),
    Manager("jpmorgan", "JPMorgan Asset Management", 6, "SEC_NPORT", "0000763852", (
        FundSeries("JFAMX", "JPMorgan Emerging Markets Equity Fund", 1217286, "S000002615"),
    )),
    Manager("nbim", "Norges Bank Investment Management", 7, "NBIM", None, ()),
    Manager("dfa", "Dimensional Fund Advisors", 8, "SEC_NPORT", "0000354204", (
        FundSeries("DFCEX", "DFA Emerging Markets Core Equity Portfolio", 355437, "S000000978"),
    )),
    Manager("t_rowe", "T. Rowe Price", 9, "SEC_NPORT", "0000080249", (
        FundSeries("PRMSX", "T. Rowe Price Emerging Markets Stock Fund", 313212, "S000001496"),
        FundSeries("PRIJX", "T. Rowe Price Emerging Markets Discovery Stock Fund", 313212, "S000050390"),
    )),
    Manager("matthews", "Matthews International Capital", 10, "SEC_NPORT", "0000923184", (
        FundSeries("MAPTX", "Matthews Pacific Tiger Fund", 923184, "S000001029"),
        FundSeries("MPACX", "Matthews Asia Growth Fund", 923184, "S000001035"),
    )),
)

MANAGERS_BY_KEY = {m.key: m for m in MANAGERS}

# --------------------------------------------------------------------------- #
# NBIM holdings API (reverse-engineered from nbim.no/en/investments/all-investments)
# --------------------------------------------------------------------------- #
NBIM_INIT_URL = "https://www.nbim.no/api/investments/v2/init.json"
NBIM_REPORT_URL = "https://www.nbim.no/api/investments/v2/report/"  # ?assetType=eq&date=YYYY-MM-DD&fileType=csv
NBIM_USER_AGENT = "Mozilla/5.0 (compatible; IndoTracker/1.0)"
NBIM_COUNTRY = "Indonesia"
