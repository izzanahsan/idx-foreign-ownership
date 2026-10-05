"""Local web dashboard: JSON API over TrackerEngine + a single-page front end.

Stdlib only (http.server); intended for localhost use.

  GET  /                                   dashboard page
  GET  /api/meta                           quarters, managers, coverage, last update
  GET  /api/report?q=2026Q2[&m=a,b]        full quarter report (optionally filtered to managers)
  GET  /api/manager?q=..&m=key             one manager's positions
  GET  /api/compare?a=..&b=..[&m=a,b]      manager totals between two quarters
  GET  /api/export?q=..&fmt=png|pdf&theme=light|dark[&m=a,b]   rendered infographic file
  GET  /api/flows                          do tracked funds' net flows predict IDX returns?
  POST /api/refresh[?source=all|sec|nbim]  start downloading new filings (background)
  GET  /api/refresh                        refresh job status
"""
from __future__ import annotations

import json
import logging
import math
import threading
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

import config
import storage
from analytics.tracker_engine import QuarterReport, TrackerEngine, quarter_label
from visualization import chart_exporter

logger = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "static"


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def _clean(v: object) -> object:
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, (float, np.floating)):
        return None if math.isnan(v) else float(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, (pd.Timestamp, datetime, date)):
        return v.isoformat()[:10]
    return v


def records(df: pd.DataFrame, cols: list[str] | None = None) -> list[dict]:
    if df is None or df.empty:
        return []
    df = df[cols] if cols else df
    return [{k: _clean(v) for k, v in row.items()} for row in df.to_dict("records")]


def report_json(r: QuarterReport) -> dict:
    h = r.history
    hist: dict = {"quarters": [], "series": [], "total": []}
    if not h.empty:
        pv = h.pivot_table(index="quarter", columns="manager_key", values="value_usd", aggfunc="sum").sort_index()
        hist["quarters"] = list(pv.index)
        hist["total"] = [_clean(v) for v in pv.sum(axis=1).tolist()]
        for key in pv.columns:
            m = config.MANAGERS_BY_KEY.get(key)
            hist["series"].append({"key": key, "name": m.name if m else key, "rank": m.rank if m else 99,
                                   "values": [_clean(v) for v in pv[key].tolist()]})
    top = r.top_holdings
    total = r.total_idx_usd or 0.0
    top1 = top.iloc[0] if len(top) else None
    fin = r.sector_table.loc[r.sector_table["sector"] == "Financials", "share_pct"] if len(r.sector_table) else []
    return {
        "quarter": r.quarter, "as_of": r.as_of.isoformat(), "provisional": r.provisional,
        "total_usd": r.total_idx_usd, "prev_total_usd": _clean(r.total_idx_usd_prev),
        "qoq_usd": _clean(r.qoq_delta_usd), "qoq_pct": _clean(r.qoq_delta_pct),
        "n_securities": r.n_securities, "n_managers_tracked": r.n_managers_scope,
        "top10_share": _clean(top.head(10)["value_usd"].sum() / total * 100) if total else None,
        "top1": None if top1 is None else {"ticker": _clean(top1["ticker"]), "issuer_name": top1["issuer_name"],
                                           "share": _clean(top1["value_usd"] / total * 100) if total else None},
        "financials_share": _clean(float(fin.iloc[0])) if len(fin) else None,
        "managers": records(r.manager_table),
        "top": records(top),
        "sectors": records(r.sector_table),
        "movers": records(r.movers),
        "history": hist,
        "notes": r.notes,
    }


def flows_json(study) -> dict:
    t = study.table.reset_index().rename(columns={"index": "quarter", "q": "quarter"})
    return {
        "verdict": study.verdict,
        "oos_hit_rate": study.oos_hit_rate, "base_hit_rate": study.base_hit_rate,
        "tests": [{"label": x.label, "n": x.n, "corr": x.corr, "slope": x.slope, "t": x.t_stat, "r2": x.r2,
                   "hit": x.hit_rate, "significant": x.significant} for x in study.tests],
        "rows": records(t, ["quarter", "flow_usd", "flow_pct", "aum0", "n_funds", "ret", "ret_prev", "fwd1", "fwd2", "fwd4"]),
        "splits": records(study.splits, ["sid", "q", "factor", "n"]) if len(study.splits) else [],
        "notes": study.notes,
    }


# --------------------------------------------------------------------------- #
# Background refresh job
# --------------------------------------------------------------------------- #
class RefreshJob:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = {"running": False, "message": "", "started": None, "finished": None,
                      "results": None, "error": None}

    def status(self) -> dict:
        with self.lock:
            return dict(self.state)

    def _set(self, **kw: object) -> None:
        with self.lock:
            self.state.update(kw)

    def start(self, source: str, on_done) -> bool:
        with self.lock:
            if self.state["running"]:
                return False
            self.state.update(running=True, message="Starting…", started=datetime.now().isoformat(timespec="seconds"),
                              finished=None, results=None, error=None)
        threading.Thread(target=self._run, args=(source, on_done), daemon=True).start()
        return True

    def _run(self, source: str, on_done) -> None:
        from scrapers import nbim_scraper, sec_edgar

        results: dict[str, int] = {}
        try:
            say = lambda msg: self._set(message=msg)  # noqa: E731
            if source in ("sec", "all"):
                results |= sec_edgar.ingest_all(progress=say)
            if source in ("nbim", "all"):
                say("NBIM: checking for new snapshots…")
                results |= nbim_scraper.ingest_all(progress=say)
            on_done()
            new = sum(v for v in results.values() if v > 0)
            failed = [k for k, v in results.items() if v < 0]
            msg = f"Done: {new:,} new rows" + (f"; failed: {', '.join(failed)}" if failed else "")
            self._set(message=msg, results=results)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Refresh failed")
            self._set(error=str(exc), message=f"Failed: {exc}")
        finally:
            self._set(running=False, finished=datetime.now().isoformat(timespec="seconds"))


# --------------------------------------------------------------------------- #
# App state
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self.refresh_job = RefreshJob()
        self.reload()

    def reload(self) -> None:
        with self._lock:
            self.holdings = storage.load_holdings(self.db_path)
            self._engines: dict[frozenset | None, TrackerEngine] = {}
            self._reports: dict[tuple, QuarterReport] = {}
            self.engine = self.engine_for(None)
            self.quarters = self.engine.available_quarters()
            self._flows: dict | None = None

    def engine_for(self, managers: frozenset | None) -> TrackerEngine:
        with self._lock:
            if managers not in self._engines:
                self._engines[managers] = TrackerEngine(holdings=self.holdings, managers=managers)
            return self._engines[managers]

    @staticmethod
    def parse_managers(raw: str | None) -> frozenset | None:
        if not raw:
            return None
        keys = frozenset(k for k in raw.split(",") if k)
        unknown = keys - set(config.MANAGERS_BY_KEY)
        if unknown:
            raise ValueError(f"Unknown manager(s): {', '.join(sorted(unknown))}")
        if not keys:
            raise ValueError("Select at least one manager")
        return None if keys == frozenset(config.MANAGERS_BY_KEY) else keys

    def build(self, q: str, managers: frozenset | None) -> QuarterReport:
        key = (q, managers)
        with self._lock:
            if key not in self._reports:
                self._reports[key] = self.engine_for(managers).build_report(q)
            return self._reports[key]

    def quarter(self, q: str | None, d: str | None = None) -> str:
        if d:
            q = quarter_label(date.fromisoformat(d))
        q = (q or self.engine.latest_complete_quarter() or "").upper()
        if q not in self.quarters:
            raise ValueError(f"No data for {q or 'any quarter'}")
        return q

    def last_updated(self) -> str | None:
        try:
            with storage.connect(self.db_path) as conn:
                row = conn.execute("SELECT MAX(ingested_at) FROM ingest_log").fetchone()
            return row[0]
        except Exception:  # noqa: BLE001
            return None

    def meta(self) -> dict:
        cov = self.engine.coverage()
        coverage = []
        for m in config.MANAGERS:
            c = cov[cov["fund_ticker"].isin([f.ticker for f in m.funds] or ["NBIM"])]
            coverage.append({"key": m.key, "funds": [f.ticker for f in m.funds] or ["NBIM"],
                             "source": "SEC N-PORT" if m.source == "SEC_NPORT" else "NBIM",
                             "last": _clean(c["last"].max()) if len(c) else None})
        return {"quarters": self.quarters, "latest": self.engine.latest_complete_quarter(),
                "provisional": [q for q in self.quarters if self.engine.is_provisional(q)],
                "managers": [{"key": m.key, "name": m.name, "rank": m.rank} for m in config.MANAGERS],
                "coverage": coverage, "last_updated": self.last_updated(),
                "rows": int(len(self.holdings))}

    def flows(self) -> dict:
        """Flow-signal study (cached; needs benchmark data from Yahoo Finance)."""
        with self._lock:
            if self._flows is None:
                from analytics.flow_signal import run_study
                from scrapers.market_data import quarterly_market

                provisional = {q for q in self.quarters if self.engine.is_provisional(q)}
                self._flows = flows_json(run_study(self.holdings, quarterly_market(), exclude=provisional))
            return self._flows

    def report(self, q: str, managers: frozenset | None) -> dict:
        return report_json(self.build(q, managers))

    def manager(self, q: str, key: str) -> dict:
        if key not in config.MANAGERS_BY_KEY:
            raise ValueError(f"Unknown manager {key}")
        df = self.engine.manager_holdings(q, key)
        df = df[(df["value_usd"] > 0) | (df["value_usd_prev"] > 0)]
        return {"quarter": q, "manager": config.MANAGERS_BY_KEY[key].name, "key": key,
                "total_usd": float(df["value_usd"].sum()),
                "positions": records(df, ["ticker", "issuer_name", "sector", "isin", "value_usd",
                                          "value_usd_prev", "delta_usd", "weight_pct"])}

    def compare(self, a: str, b: str, managers: frozenset | None) -> dict:
        eng = self.engine_for(managers)
        ta, tb = eng.manager_totals(a), eng.manager_totals(b)
        m = ta.merge(tb, on=["manager_key", "manager_name"], how="outer", suffixes=("_a", "_b"))
        m["delta_usd"] = m["value_usd_b"] - m["value_usd_a"]
        m["delta_pct"] = m["delta_usd"] / m["value_usd_a"] * 100
        m = m.sort_values("value_usd_b", ascending=False)
        return {"a": a, "b": b, "rows": records(m, ["manager_key", "manager_name", "value_usd_a",
                                                     "value_usd_b", "delta_usd", "delta_pct"])}

    def export(self, q: str, fmt: str, theme: str, managers: frozenset | None) -> Path:
        if fmt not in ("png", "pdf") or theme not in ("light", "dark"):
            raise ValueError("fmt must be png|pdf and theme light|dark")
        with self._lock:  # matplotlib is not thread-safe
            paths = chart_exporter.render_report(self.build(q, managers), config.REPORTS_DIR, (fmt,), theme)
        return paths[0]


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "IndoTracker/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: object, status: int = 200) -> None:
            self._send(status, json.dumps(obj, allow_nan=False).encode(), "application/json")

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                mg = App.parse_managers(qs.get("m")) if url.path != "/api/manager" else None
                if method == "POST" and url.path == "/api/refresh":
                    source = qs.get("source", "all")
                    if source not in ("all", "sec", "nbim"):
                        raise ValueError("source must be all|sec|nbim")
                    started = app.refresh_job.start(source, app.reload)
                    self._json({**app.refresh_job.status(), "accepted": started},
                               HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT)
                elif method != "GET":
                    self._json({"error": "method not allowed"}, HTTPStatus.METHOD_NOT_ALLOWED)
                elif url.path in ("/", "/index.html"):
                    self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/api/meta":
                    self._json(app.meta())
                elif url.path == "/api/refresh":
                    self._json(app.refresh_job.status())
                elif url.path == "/api/flows":
                    self._json(app.flows())
                elif url.path == "/api/report":
                    self._json(app.report(app.quarter(qs.get("q"), qs.get("date")), mg))
                elif url.path == "/api/manager":
                    self._json(app.manager(app.quarter(qs.get("q")), qs.get("m", "")))
                elif url.path == "/api/compare":
                    self._json(app.compare(app.quarter(qs.get("a")), app.quarter(qs.get("b")), mg))
                elif url.path == "/api/export":
                    path = app.export(app.quarter(qs.get("q")), qs.get("fmt", "png"), qs.get("theme", "light"), mg)
                    ctype = "application/pdf" if path.suffix == ".pdf" else "image/png"
                    self._send(200, path.read_bytes(), ctype,
                               {"Content-Disposition": f'attachment; filename="{path.name}"'})
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Request failed: %s", self.path)
                self._json({"error": f"server error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8050, db_path: Path | None = None,
          open_browser: bool = False) -> None:
    app = App(db_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
    print(f"IDX Foreign Ownership dashboard running at {url}  (Ctrl+C to stop)")
    if open_browser:
        import webbrowser

        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
