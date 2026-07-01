from __future__ import annotations

"""
Fetch Senate Periodic Transaction Reports from efdsearch.senate.gov.

  1. Accept the site agreement (CSRF token + POST) to get a usable session.
  2. Page the DataTables report search for report_type 11 (PTR), filer_type 1
     (senators) within the trailing window.
  3. For each *electronic* PTR (/search/view/ptr/<uuid>/), fetch and parse the
     HTML transaction table. Paper/scanned reports are skipped (counted).
  4. Merge normalized equity rows into data/transactions.json (keyed by tx_id).

The search row's submitted date is the public disclosure date — the signal date.

Usage:
  python src/fetch_senate.py [--lookback-days N] [--limit N]
"""

import argparse
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from utils import (DATA_DIR, REVIEWED_PATH, UNPARSED_PATH, Progress, http_session, load_config,
                   load_json, parse_date, save_json, setup_logging, slugify)

log = setup_logging("fetch_senate")

LANDING = "https://efdsearch.senate.gov/search/home/"
SEARCH = "https://efdsearch.senate.gov/search/"
REPORTS = "https://efdsearch.senate.gov/search/report/data/"
VIEW_BASE = "https://efdsearch.senate.gov"
LEDGER_PATH = DATA_DIR / "transactions.json"
SEEN_PATH = DATA_DIR / "seen_filings.json"

PTR_HREF_RE = re.compile(r"/search/view/ptr/([0-9a-f-]+)/")
# Any report-view link (paper reports use /paper/ instead of /ptr/).
VIEW_HREF_RE = re.compile(r"/search/view/\w+/([0-9a-f-]+)/")
AMOUNT_RE = re.compile(r"\$([\d,]+)\s*-\s*\$([\d,]+)")

TYPE_MAP = {"purchase": "P", "sale": "S", "exchange": "E"}


def _amount(s: str) -> tuple[int, int] | None:
    m = AMOUNT_RE.search(s)
    if not m:
        return None
    return int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))


def accept_agreement(session):
    session.get(LANDING, timeout=30)
    tok = session.cookies.get("csrftoken")
    session.post(LANDING, data={"prohibition_agreement": "1", "csrfmiddlewaretoken": tok},
                 headers={"Referer": LANDING}, timeout=30)
    session.get(SEARCH, timeout=30)
    return session.cookies.get("csrftoken")


def search_reports(session, tok, start_date: date, limit: int | None):
    """Yield search rows [first, last, office, report_link_html, submitted_date]."""
    collected = 0
    start = 0
    page_len = 100
    while True:
        payload = {
            "draw": 1, "start": start, "length": page_len,
            "report_types": "[11]", "filer_types": "[1]",
            "submitted_start_date": start_date.strftime("%m/%d/%Y 00:00:00"),
            "submitted_end_date": "",
            "candidate_state": "", "senator_state": "", "office_id": "",
            "first_name": "", "last_name": "",
            "csrfmiddlewaretoken": tok,
            "order[0][column]": "1", "order[0][dir]": "desc",
            "search[value]": "",
        }
        r = session.post(REPORTS, data=payload,
                         headers={"Referer": SEARCH, "X-CSRFToken": tok,
                                  "X-Requested-With": "XMLHttpRequest"}, timeout=30)
        r.raise_for_status()
        data = r.json()
        rows = data.get("data", [])
        if not rows:
            break
        for row in rows:
            yield row
            collected += 1
            if limit and collected >= limit:
                return
        start += page_len
        if start >= data.get("recordsTotal", 0):
            break


def parse_report(session, url: str) -> list[dict]:
    """Parse the electronic PTR HTML transaction table into raw rows."""
    r = session.get(url, timeout=30)
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    tbl = soup.find("table")
    if not tbl or not tbl.find("tbody"):
        return []
    out = []
    for i, tr in enumerate(tbl.find("tbody").find_all("tr")):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 8:
            continue
        # #, Transaction Date, Owner, Ticker, Asset Name, Asset Type, Type, Amount, [Comment]
        _, tx_date, owner, ticker, asset_name, asset_type, ttype, amount = cells[:8]
        out.append({
            "row_index": i,
            "owner": owner,
            "asset_name": asset_name,
            "ticker": ticker,
            "asset_type": asset_type,
            "ttype": ttype,
            "tx_date": tx_date,
            "amount": amount,
        })
    return out


def run(lookback_days: int, limit: int | None = None) -> None:
    cfg = load_config()
    delay = cfg["http"]["rate_limit_delay_ms"] / 1000.0
    cutoff = date.today() - timedelta(days=lookback_days)
    run_stamp = date.today().isoformat()

    session = http_session()
    tok = accept_agreement(session)

    ledger: dict = load_json(LEDGER_PATH) if LEDGER_PATH.exists() else {}
    seen_all = load_json(SEEN_PATH) if SEEN_PATH.exists() else {}
    seen = set(seen_all.get("senate", []))
    unparsed: dict = load_json(UNPARSED_PATH) if UNPARSED_PATH.exists() else {}
    reviewed = set(load_json(REVIEWED_PATH)) if REVIEWED_PATH.exists() else set()
    # doc_ids we already have data for (transcribed) or dismissed as not applicable.
    resolved_doc_ids = {v["doc_id"] for v in ledger.values()} | reviewed
    n_reports = n_rows = n_paper = n_skipped = 0

    rows = list(search_reports(session, tok, cutoff, limit))
    log.info("Senate: %d PTR filings in window", len(rows))
    prog = Progress(len(rows), "senate reports", log, every=10)

    for srow in rows:
        prog.step()
        first, last = srow[0].strip(), srow[1].strip()
        link_html, submitted = srow[3], srow[4]
        m = PTR_HREF_RE.search(link_html)
        disc = parse_date(submitted)
        if not m:
            n_paper += 1   # paper/scanned PTR — no electronic table; flag for review
            vm = VIEW_HREF_RE.search(link_html)
            doc_id = vm.group(1) if vm else slugify(f"{last}-{first}-{submitted}")
            if doc_id in resolved_doc_ids:
                continue   # already transcribed or dismissed as N/A — don't re-flag
            key = f"senate:{doc_id}"
            unparsed[key] = {
                "chamber": "senate",
                "member": f"{first} {last}".strip(),
                "member_id": slugify(f"{last}-{first}"),
                "state": "",
                "district": "",
                "disclosure_date": disc.isoformat() if disc else submitted,
                "doc_id": doc_id,
                "source_url": (VIEW_BASE + vm.group(0)) if vm else SEARCH,
                "reason": "paper/scanned filing (no electronic table)",
                "first_seen": (unparsed.get(key) or {}).get("first_seen") or run_stamp,
            }
            continue
        uuid = m.group(1)
        if uuid in seen:
            n_skipped += 1            # immutable report already parsed in a prior run
            continue
        url = f"{VIEW_BASE}/search/view/ptr/{uuid}/"
        try:
            txs = parse_report(session, url)
        except Exception as e:
            log.warning("Report %s parse failed: %s", uuid, e)
            txs = []
        time.sleep(delay)
        seen.add(uuid)
        n_reports += 1

        full = f"{first} {last}".strip()
        member_id = slugify(f"{last}-{first}")
        for r in txs:
            if r["asset_type"].strip().lower() not in ("stock", "stock option"):
                continue
            ticker = r["ticker"].strip().upper()
            if not ticker.isalpha() or len(ticker) > 5:
                continue
            amt = _amount(r["amount"])
            if not amt:
                continue
            tx_type = TYPE_MAP.get(r["ttype"].split()[0].lower())
            if not tx_type:
                continue
            txd = parse_date(r["tx_date"])
            tx_id = f"senate:{uuid}:{r['row_index']}"
            first_seen = (ledger.get(tx_id) or {}).get("first_seen") or run_stamp
            ledger[tx_id] = {
                "tx_id": tx_id,
                "first_seen": first_seen,
                "chamber": "senate",
                "member": full,
                "member_id": member_id,
                "party": "",
                "state": "",
                "district": "",
                "ticker": ticker,
                "asset_name": r["asset_name"],
                "owner": r["owner"],
                "tx_type": tx_type,
                "tx_date": txd.isoformat() if txd else r["tx_date"],
                "disclosure_date": disc.isoformat() if disc else submitted,
                "amount_min": amt[0],
                "amount_max": amt[1],
                "amount_mid": (amt[0] + amt[1]) / 2,
                "doc_id": uuid,
                "source_url": url,
            }
            n_rows += 1

    # Drop any flagged filing that's since been resolved (transcribed or dismissed).
    unparsed = {k: v for k, v in unparsed.items() if v["doc_id"] not in resolved_doc_ids}
    save_json(LEDGER_PATH, ledger)
    seen_all["senate"] = sorted(seen)
    save_json(SEEN_PATH, seen_all)
    save_json(UNPARSED_PATH, unparsed)
    n_senate_unparsed = sum(1 for v in unparsed.values() if v["chamber"] == "senate")
    log.info("Senate: %d new reports parsed, %d equity rows, %d paper, %d skipped (already seen). "
             "Ledger now %d rows; %d paper filings flagged for review.",
             n_reports, n_rows, n_paper, n_skipped, len(ledger), n_senate_unparsed)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback-days", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None, help="cap filings (testing)")
    args = ap.parse_args()
    lookback = args.lookback_days or load_config()["pipeline"]["lookback_days"]
    run(lookback, args.limit)


if __name__ == "__main__":
    main()
