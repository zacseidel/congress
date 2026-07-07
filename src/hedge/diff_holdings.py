from __future__ import annotations

"""
Quarter-over-quarter position changes per fund: new buys, exits, and sizing moves.

This produces one of the two core Hedge signals (the other is holding performance):
what did each manager newly buy, fully sell, or materially add to / trim between its
two most recent 13F filings. For the watchlist funds this is the "smart money is
moving into X" feed.

For each fund we compare its latest filing's long book against the prior filing's,
keyed by CUSIP:
  * new       — held now, absent last quarter
  * exited    — held last quarter, absent now
  * increased — share count up by more than `change_threshold`
  * decreased — share count down by more than `change_threshold`
Each row carries the resolved ticker (when known) and current position value.

Writes data/hedge/changes.json.

Usage:
  python src/hedge/diff_holdings.py
"""

import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils import DATA_DIR, load_config, load_json_gz, save_json_gz, setup_logging
from resolve_cusip import cached as cusip_cached
from holdings_io import load_holdings

log = setup_logging("diff_holdings")

HEDGE_DIR = DATA_DIR / "hedge"
HOLDINGS_PATH = HEDGE_DIR / "holdings.json.gz"
CHANGES_PATH = HEDGE_DIR / "changes.json.gz"


def _ticker(cusip: str):
    rec = cusip_cached(cusip)
    return rec.get("ticker") if rec else None


def _book(rows: list) -> dict:
    """Collapse a filing's long rows to {cusip: {shares, value, issuer}}."""
    book: dict = {}
    for h in rows:
        if h.get("put_call"):
            continue
        b = book.setdefault(h["cusip"], {"shares": 0.0, "value": 0.0, "issuer": h["issuer"]})
        b["shares"] += h.get("shares", 0.0)
        b["value"] += h.get("value", 0.0)
    return book


def _row(cusip: str, b: dict, extra: dict = None) -> dict:
    r = {"cusip": cusip, "ticker": _ticker(cusip), "issuer": b["issuer"],
         "value": round(b["value"], 0), "shares": b["shares"]}
    if extra:
        r.update(extra)
    return r


def diff_fund(filings: dict) -> dict:
    """filings: {filing_date: [rows]}. Compare the two most recent filings."""
    dates = sorted(filings)
    latest = dates[-1]
    cur = _book(filings[latest])
    name = filings[latest][0]["manager"]
    if len(dates) < 2:
        return {"manager": name, "latest_filing": latest, "prior_filing": None,
                "new": [], "exited": [], "increased": [], "decreased": []}
    prev = _book(filings[dates[-2]])
    threshold = load_config().get("hedge", {}).get("change_threshold", 0.15)

    new, increased, decreased = [], [], []
    for cusip, b in cur.items():
        if cusip not in prev:
            new.append(_row(cusip, b))
        else:
            ps = prev[cusip]["shares"]
            if ps > 0:
                delta = b["shares"] / ps - 1.0
                if delta > threshold:
                    increased.append(_row(cusip, b, {"delta_pct": round(100 * delta, 1)}))
                elif delta < -threshold:
                    decreased.append(_row(cusip, b, {"delta_pct": round(100 * delta, 1)}))
    exited = [_row(cusip, b) for cusip, b in prev.items() if cusip not in cur]

    for lst in (new, exited, increased, decreased):
        lst.sort(key=lambda r: r["value"], reverse=True)
    return {"manager": name, "latest_filing": latest, "prior_filing": dates[-2],
            "new": new, "exited": exited, "increased": increased, "decreased": decreased}


def run(ciks=None) -> None:
    if not HOLDINGS_PATH.exists():
        log.error("No holdings at %s; run fetch_13f.py first", HOLDINGS_PATH)
        return
    holdings = load_holdings(HOLDINGS_PATH)
    by_fund: dict = defaultdict(lambda: defaultdict(list))
    for h in holdings.values():
        if ciks and h["cik"] not in ciks:
            continue
        by_fund[h["cik"]][h["filing_date"]].append(h)

    changes = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
               "funds": {}}
    for cik, filings in by_fund.items():
        changes["funds"][str(cik)] = diff_fund(filings)
    save_json_gz(CHANGES_PATH, changes)

    # Preview: biggest new buys across funds this quarter.
    all_new = []
    for cik, d in changes["funds"].items():
        for r in d["new"]:
            all_new.append((d["manager"], r))
    all_new.sort(key=lambda x: x[1]["value"], reverse=True)
    log.info("Wrote changes for %d funds -> %s", len(changes["funds"]), CHANGES_PATH)
    log.info("--- biggest new buys this quarter (across funds) ---")
    for mgr, r in all_new[:12]:
        log.info("  %-34s %-6s %-26s $%.0fM", mgr[:33], r["ticker"] or r["cusip"][:6],
                 (r["issuer"] or "")[:25], r["value"] / 1e6)


if __name__ == "__main__":
    run()
