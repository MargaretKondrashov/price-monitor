"""Command line:

  python -m monitor run                 # full daily run: collect, compare, email, save
  python -m monitor run --max-products 30   # quick trial on 30 products
  python -m monitor calibrate           # small run that saves HTML snapshots of every site (debug/)
  python -m monitor test-email          # sends a sample report to check the email settings
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import yaml

from . import mailer
from .pipeline import RunResult, run, save
from .report import build_email_html, build_excel

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text("utf-8"))


def deliver(result: RunResult, cfg: dict, out_dir: Path, send_email: bool) -> Path:
    tname = cfg["traklin"].get("name", "טרקלין חשמל")
    xlsx = build_excel(result, out_dir / f"price_report_{result.date}.xlsx", tname)
    subject, body = build_email_html(result, tname, int(cfg.get("report", {}).get("top_rows_in_email", 15)))
    (out_dir / "email_preview.html").write_text(body, "utf-8")
    logging.info("Report written: %s", xlsx)
    if send_email:
        if mailer.configured():
            mailer.send(subject, body, [xlsx])
        else:
            logging.warning("Email settings missing (SMTP_USER / SMTP_PASSWORD / EMAIL_TO) – email not sent")
    return xlsx


def main(argv=None):
    ap = argparse.ArgumentParser(prog="monitor")
    ap.add_argument("command", choices=["run", "calibrate", "test-email"])
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--out", default=str(ROOT / "output"))
    ap.add_argument("--debug-dir", default=str(ROOT / "debug"))
    ap.add_argument("--max-products", type=int, default=None)
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--no-save", action="store_true", help="don't update data/ (for trial runs)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("curl_cffi", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = load_config(Path(a.config))
    data, out, dbg = Path(a.data), Path(a.out), Path(a.debug_dir)
    out.mkdir(parents=True, exist_ok=True)

    if a.command == "test-email":
        from .sample import sample_result
        deliver(sample_result(cfg), cfg, out, send_email=True)
        return 0

    calibrate = a.command == "calibrate"
    max_products = a.max_products if a.max_products is not None else (5 if calibrate else None)
    result = asyncio.run(run(cfg, data, dbg, max_products=max_products, calibrate=calibrate))

    if calibrate:
        # per-site HTML snapshots were already written by each store; add the run summary
        (dbg / "health.json").parent.mkdir(parents=True, exist_ok=True)
        (dbg / "health.json").write_text(json.dumps(result.health, ensure_ascii=False, indent=1), "utf-8")

    if not result.products:
        # Traklin itself could not be read: alert instead of sending an empty report
        logging.error("No Traklin products collected – sending an alert instead of a report")
        if not a.no_email and mailer.configured() and not calibrate:
            mailer.send("⚠ מעקב המחירים: לא הצלחתי לקרוא את אתר טרקלין היום",
                        '<div dir="rtl" style="font-family:Arial">הריצה היומית לא הצליחה לקרוא את אתר טרקלין חשמל '
                        '(ייתכן שהאתר היה למטה או שינה מבנה). הנתונים מאתמול נשמרו. אם זה חוזר על עצמו – '
                        'כדאי להוריד את קובץ האבחון מהריצה ב-GitHub ולבדוק.</div>')
        return 2

    deliver(result, cfg, out, send_email=not (a.no_email or calibrate))
    if not (a.no_save or calibrate or a.max_products):
        save(result, data)

    # one-line summary for the GitHub log
    summary = {s["name"]: f'{s["found"]}/{s["searches"]}' + (" (לא עבד)" if s["broken"] else "")
               for s in result.health.get("stores", [])}
    logging.info("Traklin products: %d | per store found/searched: %s", len(result.products),
                 json.dumps(summary, ensure_ascii=False))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(f"### ריצה {result.date}\n\nמוצרים בטרקלין: **{len(result.products)}**\n\n"
                    "| חנות | נמצאו / חיפושים | מצב |\n|---|---|---|\n")
            for s in result.health.get("stores", []):
                f.write(f'| {s["name"]} | {s["found"]} / {s["searches"]} | {"⚠ לא עבד" if s["broken"] else "✓"} |\n')
    return 0


if __name__ == "__main__":
    sys.exit(main())
