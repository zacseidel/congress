from __future__ import annotations

"""
Discover the 13F filer universe from SEC EDGAR and rank it by portfolio value.

This is what scales the Hedge report past a hand-picked seed list to the real
candidate pool. Two stages:

  1. DISCOVER — SEC publishes a quarterly full-index listing every filing:
       https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx
     We scan the most recent few filing quarters, keep the 13F-HR (holdings) lines,
     and union to the set of currently-active managers (latest filing per CIK).

  2. RANK BY AUM — for each manager we fetch only the small primary_doc.xml of its
     latest filing (the cover page, ~5 KB) and read <tableValueTotal>. That avoids
     pulling every filer's full holdings just to rank them. Values are normalized to
     whole dollars (the 2023 scaling change plus non-compliant filers) using the
     average-position-size heuristic, the passive-manager blocklist is applied, and
     the top `candidate_pool_size` are written to candidate_pool.json.

The backtest pipeline then fetches full holdings (fetch_13f) only for the pool.

Usage:
  python src/hedge/discover_filers.py --discover                 # build filers.json (fast)
  python src/hedge/discover_filers.py --rank-aum [--limit N]     # build candidate_pool.json
  python src/hedge/discover_filers.py --all                      # both
"""

import argparse
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils import (DATA_DIR, EDGAR_CACHE, Progress, http_session, load_config,
                   load_json, load_json_gz, save_json, save_json_gz, setup_logging)

log = setup_logging("discover_filers")

HEDGE_DIR = DATA_DIR / "hedge"
FILERS_PATH = HEDGE_DIR / "filers.json"
POOL_PATH = HEDGE_DIR / "candidate_pool.json"
# Accession-keyed AUM/holdings index (one small file) instead of ~9k raw primary_doc
# XMLs (36 MB). Accessions are immutable, so repeat runs only fetch NEW filings.
AUM_INDEX_PATH = EDGAR_CACHE / "aum_index.json.gz"

FORM_IDX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx"
PRIMARY_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/primary_doc.xml"

# SEC allows ~10 req/s. A global min-interval, lock-guarded so it also bounds the
# concurrent primary_doc fetches, keeps us safely under that while overlapping the
# per-request latency across worker threads.
_EDGAR_MIN_INTERVAL = 0.11
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
    with _rate_lock:
        wait = _EDGAR_MIN_INTERVAL - (time.monotonic() - _last_req[0])
        if wait > 0:
            time.sleep(wait)
        _last_req[0] = time.monotonic()


def _polite_get(session, url: str, cache_path: Optional[Path] = None) -> Optional[str]:
    if cache_path and cache_path.exists():
        return cache_path.read_text()
    _throttle()
    resp = session.get(url, timeout=45)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(resp.text)
    return resp.text


def recent_filing_quarters(n: int, today: date = None) -> list:
    """The n most recent calendar quarters up to today, as (year, qtr) tuples."""
    today = today or date.today()
    y, q = today.year, (today.month - 1) // 3 + 1
    out = []
    for _ in range(n):
        out.append((y, q))
        q -= 1
        if q == 0:
            q = 4
            y -= 1
    return out


def parse_form_idx(text: str) -> list:
    """Extract 13F-HR / 13F-HR/A rows -> [(form, company, cik, date, accession)]."""
    rows = []
    for line in text.splitlines():
        if not line.startswith("13F-HR"):     # excludes 13F-NT (notices, no holdings)
            continue
        # Fields: Form  Company (spaces)  CIK  Date  FileName(path). rsplit from the right.
        try:
            left, cik, filed, path = line.rsplit(None, 3)
        except ValueError:
            continue
        if not cik.isdigit():
            continue
        form, company = left.split(None, 1)
        acc = path.rsplit("/", 1)[-1].replace(".txt", "")  # 0001193125-26-226661
        rows.append((form, company.strip(), int(cik), filed, acc))
    return rows


def _quarter_rows(session, year: int, q: int, is_current: bool) -> list:
    """13F rows for a quarter. Completed quarters cache their PARSED rows (~1 MB gz)
    rather than the raw form.idx (~50 MB), and skip the download entirely on reuse."""
    parsed_cache = EDGAR_CACHE / "form-index" / f"{year}-QTR{q}-13f.json.gz"
    if not is_current and parsed_cache.exists():
        return load_json_gz(parsed_cache)
    text = _polite_get(session, FORM_IDX_URL.format(year=year, q=q))  # not stored raw
    if not text:
        return []
    rows = parse_form_idx(text)
    if not is_current:
        save_json_gz(parsed_cache, rows)
    return rows


def discover(lookback_quarters: int = 3) -> dict:
    """Union of 13F-HR filers across recent quarters; keep latest filing per CIK."""
    session = http_session()
    managers: dict = {}
    quarters = recent_filing_quarters(lookback_quarters)
    current = quarters[0]  # the in-progress quarter still accrues filings
    for (year, q) in quarters:
        rows = _quarter_rows(session, year, q, is_current=(year, q) == current)
        if not rows:
            log.info("  %d QTR%d form.idx not available yet — skipping", year, q)
            continue
        log.info("  %d QTR%d: %d 13F-HR rows", year, q, len(rows))
        for form, company, cik, filed, acc in rows:
            m = managers.get(cik)
            if m is None or filed > m["latest_date"]:
                managers[cik] = {"cik": cik, "name": company,
                                 "latest_date": filed, "latest_accession": acc}
    save_json(FILERS_PATH, managers)
    log.info("Discovered %d unique 13F filers -> %s", len(managers), FILERS_PATH)
    return managers


_VALUE_RE = re.compile(r"<[^>]*tableValueTotal>\s*([\d,]+)", re.I)
_ENTRY_RE = re.compile(r"<[^>]*tableEntryTotal>\s*(\d+)", re.I)


def _aum_from_primary_doc(text: str):
    """Return (whole-dollar AUM, n_holdings) from a primary_doc.xml.

    Scaling ($-thousands pre-2023 / non-compliant filers) is resolved via the 13F
    $100M regulatory floor: any manager that files is holding >= ~$100M, so a raw
    tableValueTotal below that must be in thousands. This is robust to the bogus
    holding counts some filers report (which broke an avg-position-size heuristic)."""
    mv = _VALUE_RE.search(text)
    if not mv:
        return None, 0
    value = float(mv.group(1).replace(",", ""))
    me = _ENTRY_RE.search(text)
    entries = int(me.group(1)) if me else 0
    scale = 1000 if value < 100_000_000 else 1
    return value * scale, entries


def _load_aum_index() -> dict:
    """{accession: [aum, n_holdings]}. Migrates any legacy per-filer primary_doc XMLs
    into the index (then removes them) so the 36 MB XML cache collapses to ~0.4 MB."""
    index = load_json_gz(AUM_INDEX_PATH) if AUM_INDEX_PATH.exists() else {}
    legacy_dir = EDGAR_CACHE / "primary_doc"
    if legacy_dir.exists():
        legacy = list(legacy_dir.glob("*.xml"))
        if legacy:
            for p in legacy:
                acc = p.stem
                if acc not in index:
                    aum, n = _aum_from_primary_doc(p.read_text())
                    index[acc] = [aum, n]
            AUM_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
            save_json_gz(AUM_INDEX_PATH, index)
            for p in legacy:
                p.unlink(missing_ok=True)
            try:
                legacy_dir.rmdir()
            except OSError:
                pass
            log.info("Migrated %d primary_doc cover pages into %s", len(legacy), AUM_INDEX_PATH.name)
    return index


def rank_by_aum(managers: dict = None, limit: Optional[int] = None) -> list:
    cfg = load_config().get("hedge", {})
    pool_size = cfg.get("candidate_pool_size", 6000)
    blocklist = [b.upper() for b in cfg.get("passive_blocklist", [])]
    min_aum = cfg.get("min_aum", 100_000_000)
    min_holdings = cfg.get("min_holdings", 5)
    max_holdings = cfg.get("max_holdings", 150)
    pins = {int(c) for c in cfg.get("pool_pins", [])}
    if managers is None:
        if not FILERS_PATH.exists():
            log.error("No filers.json; run --discover first")
            return []
        managers = load_json(FILERS_PATH)
    items = list(managers.values() if isinstance(managers, dict) else managers)
    if limit:
        items = items[:limit]
    # Blocklisted custodians/index shops never enter the pool — skip before fetching.
    items = [m for m in items if not any(b in m["name"].upper() for b in blocklist)]

    index = _load_aum_index()

    # Only fetch cover pages for accessions not already in the index. Accessions are
    # immutable, so steady-state (non-filing-season) runs fetch nothing.
    todo = [m for m in items if m["latest_accession"] not in index]
    log.info("AUM index: %d cached, %d to fetch", len(items) - len(todo), len(todo))

    def _fetch(m):
        acc = m["latest_accession"]
        url = PRIMARY_DOC_URL.format(cik=m["cik"], acc=acc.replace("-", ""))
        try:
            text = _polite_get(_thread_session(), url)   # fetched, parsed, NOT stored raw
            return acc, _aum_from_primary_doc(text) if text else (None, 0)
        except Exception as e:
            log.debug("primary_doc failed for %s: %s", m["cik"], e)
            return acc, (None, 0)

    if todo:
        prog = Progress(len(todo), "AUM probes", log, every=200)
        # Concurrent within SEC's 10 req/s (the shared _throttle bounds the global rate;
        # workers just overlap the per-request latency). ~3x faster than serial.
        with ThreadPoolExecutor(max_workers=8) as ex:
            for acc, (aum, n) in ex.map(_fetch, todo):
                index[acc] = [aum, n]
                prog.step()
        prog.done()
        save_json_gz(AUM_INDEX_PATH, index)

    ranked = []
    for m in items:
        aum, n_holdings = index.get(m["latest_accession"], [None, 0])
        if not aum:
            continue
        pinned = m["cik"] in pins
        # Select CONCENTRATED, meaningfully-sized funds (likely active stock-pickers),
        # not the biggest. Pinned CIKs bypass the band entirely.
        in_band = aum >= min_aum and min_holdings <= n_holdings <= max_holdings
        if pinned or in_band:
            ranked.append({"cik": m["cik"], "name": m["name"], "aum": round(aum, 0),
                           "n_holdings": n_holdings, "latest_date": m["latest_date"],
                           "pinned": pinned})

    ranked.sort(key=lambda r: r["aum"], reverse=True)
    pool = ranked[:pool_size]
    # Ensure pinned funds survive the safety cap even if tiny.
    pinned_out = [r for r in ranked[pool_size:] if r["pinned"]]
    pool.extend(pinned_out)
    save_json(POOL_PATH, {"generated": date.today().isoformat(),
                          "selection": {"min_aum": min_aum, "min_holdings": min_holdings,
                                        "max_holdings": max_holdings, "pins": sorted(pins)},
                          "n_discovered": len(items), "n_selected": len(pool),
                          "pool": pool})
    log.info("Selected %d concentrated funds (AUM>=$%.0fM, holdings %d-%d) + %d pins -> %s",
             len(pool), min_aum / 1e6, min_holdings, max_holdings, len(pins), POOL_PATH)
    log.info("--- largest 12 in the pool ---")
    for r in pool[:12]:
        log.info("  %-44s $%6.1fB  %3d holdings%s", r["name"][:43], r["aum"] / 1e9,
                 r["n_holdings"], "  [pinned]" if r["pinned"] else "")
    return pool


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--rank-aum", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--quarters", type=int, default=3, help="lookback filing quarters")
    ap.add_argument("--limit", type=int, help="cap filers probed for AUM (testing)")
    args = ap.parse_args()

    managers = None
    if args.discover or args.all:
        managers = discover(args.quarters)
    if args.rank_aum or args.all:
        rank_by_aum(managers, limit=args.limit)
    if not (args.discover or args.rank_aum or args.all):
        ap.print_help()


if __name__ == "__main__":
    main()
