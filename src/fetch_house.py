from __future__ import annotations

"""
Fetch House of Representatives Periodic Transaction Reports (PTRs).

  1. Download the Clerk's annual index ZIP (<YEAR>FD.zip → <YEAR>FD.xml).
  2. Keep filings with FilingType == "P" (PTR) whose FilingDate is inside the
     trailing lookback window. The index FilingDate is the public disclosure
     date — the signal date we trade on.
  3. Download each PTR PDF (cached), extract text, parse the transaction rows.
  4. Merge normalized rows into data/transactions.json (keyed by tx_id).

Usage:
  python src/fetch_house.py [--lookback-days N] [--year YYYY] [--limit N]
"""

import argparse
import io
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

import requests
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).parent))
from utils import (DATA_DIR, REVIEWED_PATH, UNPARSED_PATH, Progress, http_session,
                   load_config, load_json, parse_date, save_json, setup_logging, slugify)

log = setup_logging("fetch_house")

INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
LEDGER_PATH = DATA_DIR / "transactions.json"
SEEN_PATH = DATA_DIR / "seen_filings.json"


def _is_electronic_doc(doc_id: str) -> bool:
    """Electronic House PTRs have an 8-digit DocID starting 1/2 and yield text PDFs.
    Paper (scanned) filings use the 7-digit 8xxxxxx/9xxxxxx series — image-only PDFs
    that pypdf cannot read (e.g. Khanna, McCaul, Hal Rogers)."""
    return len(doc_id) == 8 and doc_id[:1] in "12"

# A transaction row anchored on "(TICKER) [CODE] TYPE DATE DATE $min - $max".
# Asset-type codes we keep are filtered separately; we capture all and select [ST].
ROW_RE = re.compile(
    r"\(([A-Z][A-Z.]{0,5})\)\s*"            # ticker in parens
    r"\[([A-Z]{2})\]\s*"                      # asset type code, e.g. ST, GS, OP
    r"(P|S|E)\b(?:\s*\(partial\))?\s*"       # transaction type
    r"(\d{2}/\d{2}/\d{4})\s*"                # transaction date
    r"(\d{2}/\d{2}/\d{4})\s*"                # notification date (glued in source)
    r"\$([\d,]+)\s*-\s*\$([\d,]+)"           # amount range
)

# Boundary markers that separate a transaction row from preceding clutter.
_ASSET_BOUNDARIES = ("$200?", ": New", "(partial)", "Filing ID")


def _to_int(s: str) -> int:
    return int(s.replace(",", ""))


_JUNK_MARKERS = (">", " O:", " O :", "Securities", "Notification", "Amount", "Subholding")


def _extract_owner_asset(pre: str) -> tuple[str, str]:
    """From the text preceding a ticker, isolate the owner code and asset name."""
    seg = pre.replace("\x00", "").replace("\n", " ")
    # Owner code (SP/JT/DC) sits immediately before the asset name when present.
    owners = list(re.finditer(r"\b(SP|JT|DC)\s+", seg))
    if owners:
        owner = owners[-1].group(1)
        asset = seg[owners[-1].end():]
    else:
        owner = ""
        asset = seg
        for b in _ASSET_BOUNDARIES:
            idx = asset.rfind(b)
            if idx != -1:
                asset = asset[idx + len(b):]
    asset = asset.strip(" .,-")
    if any(j in asset for j in _JUNK_MARKERS):
        asset = ""  # leftover clutter — let Polygon supply the display name
    return owner, asset[:80]


def fetch_index(year: int, session: requests.Session) -> list[dict]:
    """Return list of PTR filings {last, first, prefix, suffix, state_dst, filing_date, doc_id}."""
    url = INDEX_URL.format(year=year)
    log.info("Downloading index %s", url)
    resp = session.get(url, timeout=60)
    if resp.status_code == 404:
        log.warning("No index for %d (404)", year)
        return []
    resp.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(resp.content))
    xml_name = next(n for n in z.namelist() if n.lower().endswith(".xml"))
    root = ET.fromstring(z.read(xml_name).decode("utf-8", "replace"))
    out = []
    for m in root.findall("Member"):
        if m.findtext("FilingType") != "P":
            continue
        out.append({
            "last": (m.findtext("Last") or "").strip(),
            "first": (m.findtext("First") or "").strip(),
            "prefix": (m.findtext("Prefix") or "").strip(),
            "suffix": (m.findtext("Suffix") or "").strip(),
            "state_dst": (m.findtext("StateDst") or "").strip(),
            "filing_date": (m.findtext("FilingDate") or "").strip(),
            "doc_id": (m.findtext("DocID") or "").strip(),
        })
    log.info("Year %d: %d PTR filings in index", year, len(out))
    return out


def download_pdf(year: int, doc_id: str, session: requests.Session) -> bytes | None:
    """Fetch a PTR PDF. Parsed in memory and discarded — `seen_filings.json` is the
    durable 'already processed' record, so we never persist the raw PDF."""
    url = PTR_URL.format(year=year, doc_id=doc_id)
    try:
        resp = session.get(url, timeout=60)
        if resp.status_code != 200 or not resp.content:
            log.warning("PTR PDF %s: HTTP %s", doc_id, resp.status_code)
            return None
        return resp.content
    except Exception as e:
        log.warning("PTR PDF %s download failed: %s", doc_id, e)
        return None


def parse_ptr_text(text: str) -> list[dict]:
    """Extract equity transaction rows from PTR text. Returns raw row dicts."""
    rows = []
    for i, mt in enumerate(ROW_RE.finditer(text)):
        ticker, code, tx_type, tx_d, notif_d, amt_min, amt_max = mt.groups()
        # Asset name + owner = text immediately preceding the ticker on this row.
        pre = text[max(0, mt.start() - 160):mt.start()]
        owner, asset = _extract_owner_asset(pre)
        rows.append({
            "row_index": i,
            "owner": owner,
            "asset_name": asset[-80:],
            "ticker": ticker.replace(".", ""),
            "asset_code": code,
            "tx_type": tx_type,
            "tx_date": tx_d,
            "notification_date": notif_d,
            "amount_min": _to_int(amt_min),
            "amount_max": _to_int(amt_max),
        })
    return rows


def run(lookback_days: int, only_year: int | None = None, limit: int | None = None) -> None:
    cfg = load_config()
    delay = cfg["http"]["rate_limit_delay_ms"] / 1000.0
    cutoff = date.today() - timedelta(days=lookback_days)
    run_stamp = date.today().isoformat()

    session = http_session()
    # Every calendar year touched by the window, inclusive. The previous set-based
    # form ({cutoff.year, today.year, ...}) silently dropped any middle year — e.g.
    # a mid-2026 run over a 2-year window skipped all of 2025.
    years = [only_year] if only_year else list(range(cutoff.year, date.today().year + 1))

    ledger: dict = load_json(LEDGER_PATH) if LEDGER_PATH.exists() else {}
    seen_all = load_json(SEEN_PATH) if SEEN_PATH.exists() else {}
    seen = set(seen_all.get("house", []))
    unparsed: dict = load_json(UNPARSED_PATH) if UNPARSED_PATH.exists() else {}
    reviewed = set(load_json(REVIEWED_PATH)) if REVIEWED_PATH.exists() else set()
    # doc_ids we already have data for (transcribed) or dismissed as not applicable.
    resolved_doc_ids = {v["doc_id"] for v in ledger.values()} | reviewed
    n_filings = n_rows = n_unparsed = n_skipped = 0

    def flag(f, fd, year, reason):
        key = f"house:{f['doc_id']}"
        unparsed[key] = {
            "chamber": "house",
            "member": f"{f['first']} {f['last']}".strip(),
            "member_id": slugify(f"{f['last']}-{f['first']}"),
            "state": f["state_dst"][:2] if f["state_dst"] else "",
            "district": f["state_dst"][2:] if len(f["state_dst"]) > 2 else "",
            "disclosure_date": fd.isoformat(),
            "doc_id": f["doc_id"],
            "source_url": PTR_URL.format(year=year, doc_id=f["doc_id"]),
            "reason": reason,
            "first_seen": (unparsed.get(key) or {}).get("first_seen") or run_stamp,
        }

    for year in years:
        filings = fetch_index(year, session)
        # Keep filings inside the window that we haven't already processed.
        windowed = []
        for f in filings:
            fd = parse_date(f["filing_date"])
            if fd and fd >= cutoff:
                windowed.append((f, fd))
        if limit:
            windowed = windowed[:limit]
        # Flag scanned/paper filings (image PDFs we can't read) for manual/Claude
        # review. Done from the index by DocID format so already-seen filings — like
        # Khanna's, processed in earlier runs — are caught without re-downloading.
        for f, fd in windowed:
            if not _is_electronic_doc(f["doc_id"]) and f["doc_id"] not in resolved_doc_ids:
                flag(f, fd, year, "scanned/paper filing (image PDF — not machine-readable)")
        todo = [(f, fd) for f, fd in windowed if f["doc_id"] not in seen]
        n_skipped += len(windowed) - len(todo)
        log.info("Year %d: %d PTRs in window, %d already processed, %d to fetch",
                 year, len(windowed), len(windowed) - len(todo), len(todo))
        prog = Progress(len(todo), f"house {year} PTRs", log, every=25)

        for f, fd in todo:
            prog.step()
            content = download_pdf(year, f["doc_id"], session)
            if not content:
                continue                       # download failed — retry next run
            time.sleep(delay)                  # polite delay between fetches
            seen.add(f["doc_id"])              # mark processed (even if 0 equity rows)
            n_filings += 1
            try:
                reader = PdfReader(io.BytesIO(content))
                text = "\n".join(p.extract_text() or "" for p in reader.pages).replace("\x00", "")
            except Exception as e:
                log.warning("PDF parse failed %s: %s", f["doc_id"], e)
                n_unparsed += 1
                flag(f, fd, year, "PDF could not be read")
                continue
            rows = parse_ptr_text(text)
            if not rows and len(text.strip()) < 200:
                n_unparsed += 1  # likely a scanned image PTR
                flag(f, fd, year, "scanned image PDF (no extractable text)")
                continue

            full = f"{f['first']} {f['last']}".strip()
            member_id = slugify(f"{f['last']}-{f['first']}")
            state = f["state_dst"][:2] if f["state_dst"] else ""
            district = f["state_dst"][2:] if len(f["state_dst"]) > 2 else ""
            for r in rows:
                if r["asset_code"] != "ST":      # equities only
                    continue
                tx_id = f"house:{f['doc_id']}:{r['row_index']}"
                first_seen = (ledger.get(tx_id) or {}).get("first_seen") or run_stamp
                ledger[tx_id] = {
                    "tx_id": tx_id,
                    "first_seen": first_seen,
                    "chamber": "house",
                    "member": full,
                    "member_id": member_id,
                    "party": "",
                    "state": state,
                    "district": district,
                    "ticker": r["ticker"].upper(),
                    "asset_name": r["asset_name"],
                    "owner": r["owner"],
                    "tx_type": r["tx_type"],
                    "tx_date": (parse_date(r["tx_date"]) or "").isoformat() if parse_date(r["tx_date"]) else r["tx_date"],
                    "disclosure_date": fd.isoformat(),
                    "amount_min": r["amount_min"],
                    "amount_max": r["amount_max"],
                    "amount_mid": (r["amount_min"] + r["amount_max"]) / 2,
                    "doc_id": f["doc_id"],
                    "source_url": PTR_URL.format(year=year, doc_id=f["doc_id"]),
                }
                n_rows += 1

    # Drop any flagged filing that's since been resolved (transcribed or dismissed).
    unparsed = {k: v for k, v in unparsed.items() if v["doc_id"] not in resolved_doc_ids}
    save_json(LEDGER_PATH, ledger)
    seen_all["house"] = sorted(seen)
    save_json(SEEN_PATH, seen_all)
    save_json(UNPARSED_PATH, unparsed)
    n_house_unparsed = sum(1 for v in unparsed.values() if v["chamber"] == "house")
    log.info("House: %d new filings parsed, %d equity rows, %d unparsed/scanned, %d skipped (already seen). "
             "Ledger now %d rows; %d scanned filings flagged for review.",
             n_filings, n_rows, n_unparsed, n_skipped, len(ledger), n_house_unparsed)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback-days", type=int, default=None)
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None, help="cap filings per year (testing)")
    args = ap.parse_args()
    lookback = args.lookback_days or load_config()["pipeline"]["lookback_days"]
    run(lookback, args.year, args.limit)


if __name__ == "__main__":
    main()
