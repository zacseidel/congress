from __future__ import annotations

"""
Fetch institutional 13F-HR holdings from SEC EDGAR for the Hedge report.

For each manager (by CIK):
  1. Read the structured submissions feed
     https://data.sec.gov/submissions/CIK##########.json
  2. Keep 13F-HR / 13F-HR/A filings whose FilingDate is inside the trailing
     lookback window. The FilingDate is the *public* disclosure date — the day a
     retail investor could first act, and the entry point for the backtest.
  3. For each filing, read its directory index.json, locate the information-table
     XML (the holdings), and parse one row per <infoTable>.
  4. Normalize the reported `value` to whole dollars (pre-2023 filings report in
     $-thousands; SEC's amendment made filings on/after 2023-01-01 report whole
     dollars) and aggregate rows by CUSIP within a filing.
  5. Merge into data/hedge/holdings.json (keyed by "{cik}:{filing_date}:{cusip}")
     and update data/hedge/managers.json.

Raw EDGAR responses are cached under data/cache/edgar/ to stay polite (SEC asks
for a descriptive User-Agent — utils.http_session() already supplies one).

Usage:
  python src/hedge/fetch_13f.py --cik 1067983
  python src/hedge/fetch_13f.py --name "Pershing Square"
  python src/hedge/fetch_13f.py --all-seed [--lookback-days 730] [--limit N]
"""

import argparse
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))            # src/hedge
sys.path.insert(0, str(Path(__file__).parent.parent))     # src (utils)
import threading
from concurrent.futures import ThreadPoolExecutor

from utils import (DATA_DIR, EDGAR_CACHE, Progress, http_session, load_config,
                   load_json, load_json_gz, save_json, save_json_gz, setup_logging)
from holdings_io import load_holdings, save_holdings

log = setup_logging("fetch_13f")

HEDGE_DIR = DATA_DIR / "hedge"
HOLDINGS_PATH = HEDGE_DIR / "holdings.json.gz"   # gzipped: ~85 MB vs ~860 MB plain
MANAGERS_PATH = HEDGE_DIR / "managers.json"

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_DIR = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"

# The SEC 13F value-reporting change: filings on/after this date report holding
# `value` in whole dollars; earlier filings report in thousands of dollars.
WHOLE_DOLLAR_CUTOFF = "2023-01-01"

# A seed set of well-known active managers for the Phase-0 spike. CIKs are
# verified against the name each fetch returns from the submissions feed. This
# hardcoded map is a spike convenience only — the Phase-1 pipeline discovers the
# candidate pool from EDGAR's quarterly 13F index instead, which also avoids the
# "filer changed its CIK/entity" pitfall (e.g. Greenlight Capital stopped filing
# under CIK 1079114 after 2024-02 and moved to a new entity).
SEED_FUNDS = {
    "Berkshire Hathaway": 1067983,
    "Pershing Square": 1336528,
    "Scion Asset Management": 1649339,
    "Bridgewater Associates": 1350694,
    "Third Point": 1040273,
    "Duquesne Family Office": 1536411,
    "Appaloosa": 1656456,
    "Tiger Global Management": 1167483,
    "Himalaya Capital Management": 1709323,
}

_EDGAR_MIN_INTERVAL = 0.11  # ~9 req/s, under SEC's 10 req/s ceiling
_rate_lock = threading.Lock()
_last_req = [0.0]
_thread_local = threading.local()


def _thread_session():
    """One requests.Session per worker thread (Session isn't guaranteed thread-safe)."""
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = http_session()
        _thread_local.session = s
    return s


def _throttle() -> None:
    # Lock-guarded global spacing so concurrent worker threads together stay under
    # SEC's rate ceiling while overlapping per-request latency.
    with _rate_lock:
        wait = _EDGAR_MIN_INTERVAL - (time.monotonic() - _last_req[0])
        if wait > 0:
            time.sleep(wait)
        _last_req[0] = time.monotonic()


def _polite_get(session, url: str, cache_path: Optional[Path] = None,
                parse_json: bool = False, retries: int = 4):
    """GET with a cache + a polite, thread-safe fixed interval. Returns text or JSON.

    Retries transient failures (connection resets, timeouts, 429/5xx) with backoff so
    one network blip doesn't abort a multi-hour full run; 4xx (e.g. 404) raise at once."""
    if cache_path and cache_path.exists():
        return load_json(cache_path) if parse_json else cache_path.read_text()
    import requests as _rq
    last = None
    for attempt in range(retries):
        _throttle()
        try:
            resp = session.get(url, timeout=30)
        except _rq.exceptions.RequestException as e:
            last = e
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = f"HTTP {resp.status_code}"
            time.sleep(2 * (attempt + 1))
            continue
        resp.raise_for_status()      # 4xx (incl. 404) — definitive, let caller handle
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(resp.text)
        return resp.json() if parse_json else resp.text
    raise RuntimeError(f"GET failed after {retries} attempts ({last}): {url}")


def _local(tag: str) -> str:
    return tag.split("}")[-1]


SUBMISSIONS_TTL_HOURS = 18   # short TTL: reuse within a run/restart, refresh on scheduled runs
# One consolidated cache of the EXTRACTED 13F filing lists (not the full submissions
# feed) for all funds — {cik: {t, name, filings}} — instead of 5,177 tiny per-fund
# files (which wasted ~12 MB to 4 KB-block rounding and cached each fund's entire
# unrelated filing history). Loaded once per run; new fetches merged + saved at the end.
SUBS_PATH = EDGAR_CACHE / "submissions_13f.json.gz"
_SUBS: dict = {}
_SUBS_NEW: dict = {}
_SUBS_LOCK = threading.Lock()


def _load_subs() -> None:
    global _SUBS, _SUBS_NEW
    _SUBS = load_json_gz(SUBS_PATH) if SUBS_PATH.exists() else {}
    _SUBS_NEW = {}


def _save_subs() -> None:
    if _SUBS_NEW:
        _SUBS.update(_SUBS_NEW)
        save_json_gz(SUBS_PATH, _SUBS)


def _extract_13f(data: dict) -> list:
    """Pull the 13F-HR filings out of a submissions payload (drops all other forms)."""
    rec = data["filings"]["recent"]
    out = []
    for i, form in enumerate(rec["form"]):
        if not form.startswith("13F-HR"):
            continue
        out.append({"form": form, "accession": rec["accessionNumber"][i],
                    "filing_date": rec["filingDate"][i], "report_date": rec["reportDate"][i],
                    "is_amendment": form.endswith("/A")})
    return out


def list_13f_filings(session, cik: int, lookback_days: int) -> tuple:
    """Return (manager_name, [in-window 13F-HR filing dicts]) using the consolidated
    18h-TTL cache; only funds whose entry is missing/stale hit the submissions feed."""
    key = str(cik)
    now = time.time()
    entry = _SUBS.get(key) or _SUBS_NEW.get(key)
    if entry and (now - entry["t"]) / 3600 < SUBMISSIONS_TTL_HOURS:
        name, all_filings = entry["name"], entry["filings"]
    else:
        data = _polite_get(session, SUBMISSIONS_URL.format(cik=cik), None, parse_json=True)
        name = data.get("name", f"CIK {cik}")
        all_filings = _extract_13f(data)
        with _SUBS_LOCK:                       # distinct cik per thread; lock is belt-and-suspenders
            _SUBS_NEW[key] = {"t": now, "name": name, "filings": all_filings}
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    filings = sorted((f for f in all_filings if f["filing_date"] >= cutoff),
                     key=lambda f: f["filing_date"])
    return name, filings


def _info_table_url(session, cik: int, accession: str) -> Optional[str]:
    acc = accession.replace("-", "")
    diru = ARCHIVE_DIR.format(cik=cik, acc=acc)
    # Not cached: only fetched for accessions whose parsed holdings aren't cached yet.
    idx = _polite_get(session, diru + "index.json", None, parse_json=True)
    xmls = [it["name"] for it in idx["directory"]["item"]
            if it["name"].endswith(".xml") and "primary_doc" not in it["name"]]
    if not xmls:
        return None
    # Usually exactly one; if several, the info table is the largest .xml.
    if len(xmls) > 1:
        sizes = {it["name"]: int(it.get("size") or 0) for it in idx["directory"]["item"]}
        xmls.sort(key=lambda n: sizes.get(n, 0), reverse=True)
    return diru + xmls[0]


def _detect_scale(raw: list, filing_date: str) -> int:
    """Return 1 (values in whole $) or 1000 (values in $-thousands).

    The 2023 SEC rule moved 13F `value` to whole dollars, but some filers kept
    reporting in thousands, so the filing-date cutoff alone is unreliable. Detect
    from the data: for share rows, value/shares is an implied per-share price —
    ~tens/hundreds for whole-dollar filings, ~hundredths for thousands. The median
    across the book cleanly separates the two (threshold $1). Fall back to the date
    rule only when a filing has no usable share rows (e.g. all options/bonds)."""
    implied = [r["value_raw"] / r["shares"] for r in raw
               if r["ssh_type"] == "SH" and r["put_call"] is None
               and r["shares"] > 0 and r["value_raw"] > 0]
    if implied:
        implied.sort()
        median = implied[len(implied) // 2]
        return 1000 if median < 1.0 else 1
    return 1000 if filing_date < WHOLE_DOLLAR_CUTOFF else 1


def parse_info_table(xml_text: str, filing_date: str) -> tuple:
    """Parse an information-table XML into (holdings aggregated by CUSIP, scale).

    Values are normalized to whole dollars; the applied scale is returned for logging.
    """
    root = ET.fromstring(xml_text)
    raw: list = []
    for it in (e for e in root.iter() if _local(e.tag) == "infoTable"):
        cusip = issuer = put_call = None
        ssh_type = "SH"
        value = shares = 0.0
        for c in it.iter():
            lt = _local(c.tag)
            txt = (c.text or "").strip()
            if lt == "cusip":
                cusip = txt.upper()
            elif lt == "nameOfIssuer":
                issuer = txt
            elif lt == "value" and txt:
                try:
                    value = float(txt.replace(",", ""))
                except ValueError:
                    pass
            elif lt == "sshPrnamt" and txt:
                try:
                    shares = float(txt.replace(",", ""))
                except ValueError:
                    pass
            elif lt == "sshPrnamtType" and txt:
                ssh_type = txt.upper()
            elif lt == "putCall" and txt:
                put_call = txt.upper()
        if not cusip:
            continue
        raw.append({"cusip": cusip, "issuer": issuer, "value_raw": value,
                    "shares": shares, "ssh_type": ssh_type, "put_call": put_call})

    scale = _detect_scale(raw, filing_date)
    agg: dict = {}
    for r in raw:
        # Aggregate multiple rows for the same CUSIP+putCall (different managers/accounts).
        key = (r["cusip"], r["put_call"])
        if key not in agg:
            agg[key] = {"cusip": r["cusip"], "issuer": r["issuer"], "value": 0.0,
                        "shares": 0.0, "put_call": r["put_call"]}
        agg[key]["value"] += r["value_raw"] * scale
        agg[key]["shares"] += r["shares"]
    return list(agg.values()), scale


def fetch_manager(cik: int, lookback_days: int, cached_by_acc: dict) -> Optional[tuple]:
    """Fetch one manager's in-window 13F holdings. Pure: returns
    (fund_holdings, manager_record) or None, so it's safe to run concurrently.

    `cached_by_acc` maps accession -> already-parsed holding rows (from the existing
    holdings ledger). Accessions are immutable, so any filing we already have is reused
    with no network call — the ledger (holdings.json.gz) IS the fetch cache. Only new
    filings hit EDGAR: index.json + information-table XML + parse."""
    session = _thread_session()
    try:
        name, filings = list_13f_filings(session, cik, lookback_days)
    except Exception as e:
        log.warning("  submissions failed for CIK %s (skipped): %s", cik, e)
        return None
    if not filings:
        log.warning("  %s (CIK %d): no 13F-HR in window", name, cik)
        return None
    total_value_latest = 0.0
    fund_holdings: dict = {}
    for f in filings:
        try:
            acc = f["accession"]
            if acc in cached_by_acc:
                # Reuse already-parsed rows from the ledger — no fetch, no parse.
                for row in cached_by_acc[acc]:
                    key = f"{cik}:{f['filing_date']}:{row['cusip']}"
                    if row.get("put_call"):
                        key += f":{row['put_call']}"
                    fund_holdings[key] = row
                fval = sum(r["value"] for r in cached_by_acc[acc])
            else:
                url = _info_table_url(session, cik, acc)
                if not url:
                    log.warning("  no info table for %s %s", name, acc)
                    continue
                xml_text = _polite_get(session, url)
                rows, scale = parse_info_table(xml_text, f["filing_date"])
                fval = sum(r["value"] for r in rows)
                for r in rows:
                    key = f"{cik}:{f['filing_date']}:{r['cusip']}"
                    if r["put_call"]:
                        key += f":{r['put_call']}"
                    fund_holdings[key] = {
                        "cik": cik, "manager": name,
                        "filing_date": f["filing_date"], "report_date": f["report_date"],
                        "form": f["form"], "accession": acc,
                        "cusip": r["cusip"], "issuer": r["issuer"],
                        "value": round(r["value"], 2), "shares": r["shares"],
                        "put_call": r["put_call"],
                    }
            if f["filing_date"] == filings[-1]["filing_date"]:
                total_value_latest = fval
        except Exception as e:
            log.warning("  failed %s %s: %s", name, f["accession"], e)
    manager_rec = {
        "cik": cik, "name": name,
        "filing_count": len(filings),
        "latest_value": round(total_value_latest, 2),
        "latest_filing": filings[-1]["filing_date"],
        "first_seen": filings[0]["filing_date"],
    }
    return fund_holdings, manager_rec


def run(ciks: list, lookback_days: Optional[int] = None) -> None:
    cfg = load_config()
    lookback_days = lookback_days or cfg["pipeline"]["lookback_days"]
    workers = cfg.get("hedge", {}).get("fetch_workers", 8)
    holdings = load_holdings(HOLDINGS_PATH) if HOLDINGS_PATH.exists() else {}
    managers = load_json(MANAGERS_PATH) if MANAGERS_PATH.exists() else {}
    _load_subs()   # consolidated 13F-list cache (one file, read by all fetch threads)

    # The ledger itself is the fetch cache: index existing rows by accession so any
    # filing we already have is reused instead of re-downloaded (no separate infotables
    # cache). Built before the drop below so refetched funds' prior rows stay reusable.
    from collections import defaultdict
    cached_by_acc: dict = defaultdict(list)
    for v in holdings.values():
        cached_by_acc[v["accession"]].append(v)

    # Drop the refetched funds' rows so each ends up with exactly its current window
    # (no stale filings linger when the lookback window slides forward); fetch_manager
    # returns each fund's full current set (reused + any newly-fetched filings).
    fetch_set = {int(c) for c in ciks}
    holdings = {k: v for k, v in holdings.items() if v["cik"] not in fetch_set}

    # Concurrent across funds: the shared thread-safe _throttle keeps the aggregate
    # request rate under SEC's ceiling while overlapping per-request latency (~3x faster).
    prog = Progress(len(ciks), "funds fetched", log, every=25)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(lambda c: fetch_manager(int(c), lookback_days, cached_by_acc), ciks):
            if res:
                fund_holdings, mrec = res
                holdings.update(fund_holdings)
                managers[str(mrec["cik"])] = mrec
            prog.step()
    prog.done()

    _save_subs()   # merge newly-fetched 13F lists into the consolidated cache
    save_holdings(HOLDINGS_PATH, holdings)
    save_json(MANAGERS_PATH, managers)
    log.info("Saved %d holdings across %d managers -> %s",
             len(holdings), len(managers), HOLDINGS_PATH)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cik", type=int, help="Fetch a single manager by CIK")
    ap.add_argument("--name", help="Fetch a single seed manager by (partial) name")
    ap.add_argument("--all-seed", action="store_true", help="Fetch all seed funds")
    ap.add_argument("--lookback-days", type=int)
    ap.add_argument("--limit", type=int, help="Cap number of seed funds (with --all-seed)")
    args = ap.parse_args()

    if args.cik:
        ciks = [args.cik]
    elif args.name:
        matches = [c for n, c in SEED_FUNDS.items() if args.name.lower() in n.lower()]
        if not matches:
            log.error("No seed fund matches %r", args.name)
            return
        ciks = matches
    elif args.all_seed:
        ciks = list(SEED_FUNDS.values())
        if args.limit:
            ciks = ciks[:args.limit]
    else:
        ap.print_help()
        return
    run(ciks, args.lookback_days)


if __name__ == "__main__":
    main()
