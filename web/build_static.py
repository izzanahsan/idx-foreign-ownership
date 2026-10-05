"""Build a static, server-free snapshot of the web dashboard for hosting.

  python -m web.build_static [--out dist]

Writes dist/index.html (the live page in static mode), dist/data.json (every
quarter for All and US-funds-only, manager drill-downs, flow study) and
dist/reports/*.pdf (one infographic per complete quarter).
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import config
from visualization import chart_exporter
from web.server import STATIC, App

DRILL_TOP_N = 60  # positions per manager per quarter in the snapshot


def _strip_document(html: str) -> str:
    """For hosts that supply their own <html>/<head>/<body> skeleton."""
    import re

    html = re.sub(r"<!doctype html>\s*", "", html, flags=re.I)
    html = re.sub(r"</?(html|head|body)(\s[^>]*)?>\s*", "", html, flags=re.I)
    return re.sub(r'<meta (charset|name="viewport")[^>]*>\s*', "", html)


def build(out: Path, db_path: Path | None = None, pdfs: bool = True, artifact: bool = False) -> Path:
    app = App(db_path)
    out.mkdir(parents=True, exist_ok=True)
    us = frozenset(m.key for m in config.MANAGERS if m.key != "nbim")

    data: dict = {"meta": app.meta(), "reports": {"all": {}, "us": {}}, "drill": {}, "pdfs": []}
    for q in app.quarters:
        data["reports"]["all"][q] = app.report(q, None)
        data["reports"]["us"][q] = app.report(q, us)
        data["drill"][q] = {}
        for m in data["reports"]["all"][q]["managers"]:
            d = app.manager(q, m["manager_key"])
            d["positions"] = d["positions"][:DRILL_TOP_N]
            data["drill"][q][m["manager_key"]] = d
    data["flows"] = app.flows()

    if pdfs:
        (out / "reports").mkdir(exist_ok=True)
        for q in app.quarters:
            if app.engine.is_provisional(q):
                continue
            chart_exporter.render_report(app.build(q, None), out / "reports", ("pdf",), "light")
            data["pdfs"].append(q)

    (out / "data.json").write_text(json.dumps(data, allow_nan=False, separators=(",", ":")))
    html = (STATIC / "index.html").read_text()
    html = html.replace("<script>\nconst $ =", '<script>window.STATIC_DATA_URL = "data.json";</script>\n<script>\nconst $ =', 1)
    if artifact:
        html = _strip_document(html)
    (out / "index.html").write_text(html)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=config.ROOT / "dist")
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--artifact", action="store_true",
                    help="page fragment for hosts that add their own document skeleton (implies --no-pdf)")
    args = ap.parse_args()
    if args.out.exists():
        shutil.rmtree(args.out)
    out = build(args.out, pdfs=not (args.no_pdf or args.artifact), artifact=args.artifact)
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"Static site written to {out} ({size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
