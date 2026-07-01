from __future__ import annotations

"""
OCR scanned (paper) House PTRs from the review queue straight into the ledger.

Walks `data/unparsed_filings.json` (House paper filings only — they have direct PDF
links and a known form), OCRs each with `ocr_ptr`, and:

  - writes the extracted equity rows to `data/transactions.json`, tagged
    `entered_by="ocr"` (with a `match_score`) so they're distinguishable and revertible;
  - removes the filing from the queue;
  - if a filing yields no public stocks (cash/crypto/talent-firm payments, attachment
    sheets), records its DocID in `data/reviewed_filings.json` so it isn't re-flagged.

OCR is slow, so each run is capped (config `ocr.max_filings_per_run`) and resumable —
processed filings leave the queue, so re-running continues where it left off.

Usage:
  python src/ocr_scanned.py [--max-filings N] [--member SUBSTR]
"""

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ocr_ptr
from utils import (LEDGER_PATH, REVIEWED_PATH, UNPARSED_PATH, Progress, http_session,
                   load_config, load_json, save_json, setup_logging)

log = setup_logging("ocr_scanned")


def _download(url: str, session) -> bytes | None:
    try:
        r = session.get(url, timeout=60)
        if r.status_code == 200 and r.content:
            return r.content
        log.warning("download %s: HTTP %s", url, r.status_code)
    except Exception as e:
        log.warning("download %s failed: %s", url, e)
    return None


def run(max_filings: int | None = None, member_filter: str | None = None) -> None:
    cfg = load_config()
    ocfg = cfg.get("ocr", {})
    max_filings = max_filings if max_filings is not None else ocfg.get("max_filings_per_run", 20)
    dpi = ocfg.get("dpi", 300)
    run_stamp = date.today().isoformat()

    if not UNPARSED_PATH.exists():
        log.info("No unparsed queue yet — nothing to OCR.")
        return
    ledger = load_json(LEDGER_PATH) if LEDGER_PATH.exists() else {}
    unparsed = load_json(UNPARSED_PATH)
    reviewed = load_json(REVIEWED_PATH) if REVIEWED_PATH.exists() else []

    smap = ocr_ptr.build_stock_map()
    keys = list(smap)
    log.info("Stock dictionary: %d names", len(smap))

    # House paper filings only (direct PDF + known form). Group a member's filings
    # together (oldest first) for deterministic, resumable progress.
    queue = [(k, v) for k, v in unparsed.items() if v.get("chamber") == "house"
             and (not member_filter or member_filter.lower() in v["member"].lower())]
    queue.sort(key=lambda kv: (kv[1]["member"], kv[1]["disclosure_date"]))
    todo = queue[:max_filings] if max_filings else queue
    log.info("OCR queue: %d House paper filings pending, processing %d this run",
             len(queue), len(todo))

    session = http_session()
    prog = Progress(len(todo), "OCR filings", log, every=1)
    n_filings = n_rows = n_dismissed = 0

    for key, rec in todo:
        prog.step(f"{rec['member']} {rec['disclosure_date']}")
        pdf = _download(rec["source_url"], session)
        if not pdf:
            continue                       # transient — leave in queue, retry next run
        try:
            recs = ocr_ptr.extract_filing(pdf, smap, keys, rec["disclosure_date"], dpi=dpi)
        except Exception as e:
            log.warning("OCR failed for %s (%s): %s", rec["doc_id"], rec["member"], e)
            continue
        if not recs:
            if rec["doc_id"] not in reviewed:
                reviewed.append(rec["doc_id"])
            unparsed.pop(key, None)
            n_dismissed += 1
            continue
        for i, r in enumerate(recs):
            tx_id = f"{rec['chamber']}:{rec['doc_id']}:o{i}"
            ledger[tx_id] = {
                "tx_id": tx_id,
                "first_seen": (ledger.get(tx_id) or {}).get("first_seen") or run_stamp,
                "chamber": rec["chamber"],
                "member": rec["member"], "member_id": rec["member_id"], "party": "",
                "state": rec.get("state", ""), "district": rec.get("district", ""),
                "ticker": r["ticker"], "asset_name": r["asset_name"], "owner": "",
                "tx_type": r["tx_type"], "tx_date": r["tx_date"],
                "disclosure_date": rec["disclosure_date"],
                "amount_min": r["amount_min"], "amount_max": r["amount_max"],
                "amount_mid": (r["amount_min"] + r["amount_max"]) / 2,
                "doc_id": rec["doc_id"], "source_url": rec["source_url"],
                "entered_by": "ocr", "match_score": r["match_score"],
            }
            n_rows += 1
        unparsed.pop(key, None)
        n_filings += 1

    save_json(LEDGER_PATH, ledger)
    save_json(UNPARSED_PATH, unparsed)
    save_json(REVIEWED_PATH, sorted(set(reviewed)))
    log.info("OCR: %d filings -> %d equity rows; %d filings had no public stocks "
             "(dismissed). %d House paper filings still queued.",
             n_filings, n_rows, n_dismissed,
             sum(1 for v in unparsed.values() if v.get("chamber") == "house"))


def main() -> None:
    ap = argparse.ArgumentParser(description="OCR scanned House PTRs into the ledger.")
    ap.add_argument("--max-filings", type=int, default=None, help="cap filings this run")
    ap.add_argument("--member", help="only filings whose member name contains this text")
    args = ap.parse_args()
    run(args.max_filings, args.member)


if __name__ == "__main__":
    main()
