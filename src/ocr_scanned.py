from __future__ import annotations

"""
OCR scanned (paper) PTRs from the review queue straight into the ledger.

Walks `data/unparsed_filings.json` and OCRs each paper filing with `ocr_ptr`:
  - House filings are direct PDFs on the grid form -> ocr_ptr.extract_filing;
  - Senate filings are page-image GIFs behind the eFD viewer, on a typed
    "Stock Act Transaction" list -> ocr_ptr.extract_senate_pages.

For each filing it then:
  - writes the extracted equity rows to `data/transactions.json`, tagged
    `entered_by="ocr"` (with a `match_score`) so they're distinguishable and revertible;
  - removes the filing from the queue;
  - if a filing yields no public stocks (cash/crypto/private LLC/trust interests,
    attachment sheets), records its DocID in `data/reviewed_filings.json` so it isn't
    re-flagged.

OCR is slow, so each run is capped (config `ocr.max_filings_per_run`) and resumable —
processed filings leave the queue, so re-running continues where it left off. Pass
`--max-filings 0` to drain the whole queue (used by local runs).

Usage:
  python src/ocr_scanned.py [--max-filings N] [--member SUBSTR]
"""

import argparse
import io
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import fetch_senate
import ocr_ptr
from utils import (LEDGER_PATH, OCR_STATE_PATH, REVIEWED_PATH, UNPARSED_PATH, Progress,
                   http_session, load_config, load_json, save_json, setup_logging)

log = setup_logging("ocr_scanned")

_local = threading.local()

# Senate paper filings serve one scanned GIF per page from efd-media-public, linked from
# the /search/view/paper/ page (which needs an accepted eFD site agreement to load).
_SEN_GIF_RE = re.compile(r"https://efd-media-public[^\"']+\.gif", re.I)


def _session():
    """One requests.Session per worker thread (Session isn't guaranteed thread-safe)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = _local.session = http_session()
    return s


def _senate_session():
    """Per-thread eFD session with the site agreement already accepted (required before
    the Senate viewer will serve paper-filing pages)."""
    s = getattr(_local, "sen_session", None)
    if s is None:
        s = _local.sen_session = http_session()
        try:
            fetch_senate.accept_agreement(s)
        except Exception as e:
            log.warning("Senate eFD agreement failed: %s", e)
    return s


def _download(url: str, session) -> bytes | None:
    try:
        r = session.get(url, timeout=60)
        if r.status_code == 200 and r.content:
            return r.content
        log.warning("download %s: HTTP %s", url, r.status_code)
    except Exception as e:
        log.warning("download %s failed: %s", url, e)
    return None


def _senate_pages(view_url: str) -> list[np.ndarray] | None:
    """Resolve a Senate paper filing's page-image GIFs and decode them to grayscale
    arrays. Returns None (leave queued for retry) if the page or images won't load."""
    s = _senate_session()
    try:
        html = s.get(view_url, timeout=30).text
    except Exception as e:
        log.warning("senate view %s failed: %s", view_url, e)
        return None
    urls = list(dict.fromkeys(_SEN_GIF_RE.findall(html)))
    if not urls:
        return None                        # couldn't resolve images — retry next run
    pages = []
    for u in urls:
        content = _download(u, s)
        if content:
            try:
                pages.append(np.array(Image.open(io.BytesIO(content)).convert("L")))
            except Exception as e:
                log.warning("senate gif %s decode failed: %s", u, e)
    return pages or None


def _process(rec, smap, keys, dpi, valid_tickers):
    """Download + OCR one filing (runs in a worker thread; no shared-state writes).
    Returns the extracted rows, [] to dismiss (no public stocks), or None to leave the
    filing queued for retry. House filings are direct PDFs; Senate filings are page-image
    GIFs parsed by the typed-list extractor."""
    if rec.get("chamber") == "senate":
        pages = _senate_pages(rec["source_url"])
        if pages is None:
            return None                    # transient — leave in queue, retry next run
        try:
            return ocr_ptr.extract_senate_pages(pages, smap, keys, rec["disclosure_date"],
                                                valid_tickers)
        except Exception as e:
            log.warning("Senate OCR failed for %s (%s): %s", rec["doc_id"], rec["member"], e)
            return None
    pdf = _download(rec["source_url"], _session())
    if not pdf:
        return None                        # transient — leave in queue, retry next run
    try:
        return ocr_ptr.extract_filing(pdf, smap, keys, rec["disclosure_date"], dpi=dpi)
    except Exception as e:
        log.warning("OCR failed for %s (%s): %s", rec["doc_id"], rec["member"], e)
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
    valid_tickers = set(smap.values())     # Senate parenthesised-symbol validation set
    log.info("Stock dictionary: %d names, %d tickers", len(smap), len(valid_tickers))

    # House paper filings are direct PDFs (grid form); Senate paper filings are page-image
    # GIFs (typed-list form). Both are handled — group a member's filings together (oldest
    # first) for deterministic, resumable progress.
    queue = [(k, v) for k, v in unparsed.items()
             if v.get("chamber") in ("house", "senate")
             and (not member_filter or member_filter.lower() in v["member"].lower())]
    queue.sort(key=lambda kv: (kv[1]["member"], kv[1]["disclosure_date"]))
    todo = queue[:max_filings] if max_filings else queue
    log.info("OCR queue: %d paper filings pending (House PDF + Senate GIF), processing %d this run",
             len(queue), len(todo))

    # OCR is CPU-bound and shells out to tesseract/poppler (which release the GIL), so
    # process filings concurrently. Download + OCR happen in worker threads; all ledger /
    # queue mutations stay on the main thread as results arrive, keeping writes race-free.
    workers = ocfg.get("workers", 4)
    prog = Progress(len(todo), "OCR filings", log, every=1)
    n_filings = n_rows = n_dismissed = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_process, rec, smap, keys, dpi, valid_tickers): (key, rec)
                for key, rec in todo}
        for fut in as_completed(futs):
            key, rec = futs[fut]
            prog.step(f"{rec['member']} {rec['disclosure_date']}")
            recs = fut.result()
            if recs is None:
                continue                   # download/OCR failed — leave queued for retry
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

    remaining = sum(1 for v in unparsed.values() if v.get("chamber") in ("house", "senate"))
    save_json(LEDGER_PATH, ledger)
    save_json(UNPARSED_PATH, unparsed)
    save_json(REVIEWED_PATH, sorted(set(reviewed)))
    # Freshness stamp for the report banner: when OCR last ran and what's left in the
    # queue. Written every run (even a no-op) so the report can tell "up to date" from
    # "backlog, last processed N days ago". See utils.OCR_STATE_PATH.
    save_json(OCR_STATE_PATH, {
        "last_run": run_stamp,
        "processed": n_filings,
        "rows_added": n_rows,
        "dismissed": n_dismissed,
        "queue_remaining": remaining,
    })
    log.info("OCR: %d filings -> %d equity rows; %d filings had no public stocks "
             "(dismissed). %d paper filings still queued.",
             n_filings, n_rows, n_dismissed, remaining)


def main() -> None:
    ap = argparse.ArgumentParser(description="OCR scanned House + Senate PTRs into the ledger.")
    ap.add_argument("--max-filings", type=int, default=None,
                    help="cap filings this run (0 = drain the whole queue)")
    ap.add_argument("--member", help="only filings whose member name contains this text")
    args = ap.parse_args()
    run(args.max_filings, args.member)


if __name__ == "__main__":
    main()
