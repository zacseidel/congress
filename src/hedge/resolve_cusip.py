from __future__ import annotations

"""
CUSIP -> ticker resolution for the Hedge (13F) report.

13F holdings are keyed by CUSIP; the price layer is keyed by ticker. This module
bridges the two, with a persistent per-CUSIP cache (data/cache/cusip/{CUSIP}.json)
so each CUSIP is resolved from the network at most once.

Resolution chain (first hit wins). At scale the bulk resolver goes first:
  1. OpenFIGI /v3/mapping  ID_CUSIP -> ticker, constrained to US-listed equities
     (batched, ~200 CUSIPs/min unauth — far faster than Polygon's 5/min free tier)
  2. Polygon  /v3/reference/tickers?cusip=  (exact CUSIP match; reuses PolygonClient)
     as a fallback for the few CUSIPs OpenFIGI can't map

A definitive miss is cached as {"ticker": null} so we don't re-burn budget on the
long tail of private placements / foreign / fully-delisted names. Pass
--refresh-misses to retry those.

Usage:
  python src/hedge/resolve_cusip.py --cusip 037833100
  python src/hedge/resolve_cusip.py --from-holdings          # resolve every CUSIP in the ledger
"""

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests

sys.path.insert(0, str(Path(__file__).parent))            # src/hedge (sibling modules)
sys.path.insert(0, str(Path(__file__).parent.parent))     # src (utils)
from utils import (CUSIP_CACHE, PolygonClient, Progress, RateLimiter, load_config,
                   load_json, load_json_gz, save_json_gz, setup_logging)

log = setup_logging("resolve_cusip")

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

# One consolidated, gzipped CUSIP->record map instead of thousands of tiny per-CUSIP
# files (which wasted ~14 MB of 4 KB blocks for ~0.3 MB of data and slowed I/O). Loaded
# once into memory; cached() then does O(1) dict lookups. Legacy per-file caches are
# migrated in on first load, then removed.
CUSIP_MAP_PATH = CUSIP_CACHE / "cusip_map.json.gz"
_MAP: Optional[dict] = None


def _load_map() -> dict:
    global _MAP
    if _MAP is not None:
        return _MAP
    if CUSIP_MAP_PATH.exists():
        _MAP = load_json_gz(CUSIP_MAP_PATH)
        return _MAP
    _MAP = {}
    legacy = list(CUSIP_CACHE.glob("*.json"))     # migrate old per-file cache
    for p in legacy:
        try:
            rec = load_json(p)
            if rec.get("cusip"):
                _MAP[rec["cusip"]] = rec
        except Exception:
            continue
    if _MAP:
        CUSIP_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        save_json_gz(CUSIP_MAP_PATH, _MAP)
        for p in legacy:      # reclaim space only after the map is safely written
            p.unlink(missing_ok=True)
        log.info("Migrated %d CUSIPs from per-file cache into %s", len(_MAP), CUSIP_MAP_PATH.name)
    return _MAP


def _save_map() -> None:
    CUSIP_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    save_json_gz(CUSIP_MAP_PATH, _load_map())


def cached(cusip: str) -> Optional[dict]:
    return _load_map().get((cusip or "").strip().upper())


def _openfigi_headers() -> dict:
    h = {"Content-Type": "application/json"}
    key = os.environ.get("OPENFIGI_API_KEY")
    if key:
        h["X-OPENFIGI-APIKEY"] = key
    return h


def _openfigi_batch(cusips: list, limiter: RateLimiter,
                    session: requests.Session) -> dict:
    """Map a batch of CUSIPs via OpenFIGI, constrained to US listings.
    Returns {cusip: ticker or None}. Batch capped at 10 (unauth) / 100 (keyed)."""
    out: dict = {}
    if not cusips:
        return out
    jobs = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in cusips]
    limiter.wait()
    try:
        resp = session.post(OPENFIGI_URL, json=jobs, headers=_openfigi_headers(), timeout=30)
        if resp.status_code == 429:
            log.warning("OpenFIGI rate limited; sleeping 20s")
            time.sleep(20)
            resp = session.post(OPENFIGI_URL, json=jobs, headers=_openfigi_headers(), timeout=30)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        log.warning("OpenFIGI batch failed (%d cusips): %s", len(cusips), e)
        return {c: None for c in cusips}
    for c, item in zip(cusips, payload):
        data = item.get("data") if isinstance(item, dict) else None
        ticker = None
        if data:
            # Prefer an equity common share; else first US row. Strip trailing '*'/'/…'.
            best = next((d for d in data if d.get("securityType") == "Common Stock"), data[0])
            ticker = (best.get("ticker") or "").split("*")[0].strip() or None
        out[c] = ticker
    return out


def resolve(cusip: str, poly: PolygonClient, limiter: RateLimiter,
            session: requests.Session, refresh_misses: bool = False) -> dict:
    """Resolve one CUSIP -> {cusip, ticker, source, name}. Cached persistently."""
    cusip = (cusip or "").strip().upper()
    hit = cached(cusip)
    if hit is not None and not (refresh_misses and not hit.get("ticker")):
        return hit

    # 1) Polygon exact CUSIP match
    pres = poly.cusip_lookup(cusip)
    if pres and pres.get("ticker"):
        rec = {"cusip": cusip, "ticker": pres["ticker"].upper(),
               "source": "polygon", "name": pres.get("name")}
        _load_map()[cusip] = rec
        _save_map()
        return rec

    # 2) OpenFIGI fallback
    fig = _openfigi_batch([cusip], limiter, session).get(cusip)
    rec = {"cusip": cusip, "ticker": fig.upper() if fig else None,
           "source": "openfigi" if fig else None, "name": None}
    _load_map()[cusip] = rec
    _save_map()
    return rec


def resolve_many(cusips: list, refresh_misses: bool = False,
                 poly_fallback: bool = True) -> dict:
    """Resolve a list of CUSIPs -> {cusip: record}. OpenFIGI batched (fast) first;
    then, when `poly_fallback`, a Polygon per-CUSIP pass over the OpenFIGI misses.

    The Polygon pass is capped at the free tier's 5/min, so for large bulk runs
    (hundreds of foreign-CINS misses) it dominates wall-clock for marginal coverage.
    Bulk callers pass poly_fallback=False (OpenFIGI-only) and let the remaining names
    fall to the deferred CINS override map; the misses stay uncached for a later retry."""
    cfg = load_config()
    CUSIP_CACHE.mkdir(parents=True, exist_ok=True)
    api_key = os.environ.get("POLYGON_API_KEY", "")
    poly = PolygonClient(api_key, cfg["polygon"]) if api_key else None
    figi_cfg = cfg.get("openfigi", {})
    # An OpenFIGI API key raises limits from ~20 req/min x 10 jobs to ~250 x 100
    # (~125x throughput), turning a ~90 min cold resolve of the full universe into ~1 min.
    if os.environ.get("OPENFIGI_API_KEY"):
        rate = figi_cfg.get("keyed_rate_calls_per_min", 250)
        batch_size = figi_cfg.get("keyed_batch_size", 100)
    else:
        rate = figi_cfg.get("rate_limit_calls_per_min", 20)
        batch_size = figi_cfg.get("batch_size", 10)
    limiter = RateLimiter(rate)
    session = requests.Session()

    uniq = sorted({(c or "").strip().upper() for c in cusips if c})
    cmap = _load_map()
    results: dict = {}
    uncached: list = []

    # Pass 1: serve from the persistent cache.
    for c in uniq:
        hit = cmap.get(c)
        if hit is not None and not (refresh_misses and not hit.get("ticker")):
            results[c] = hit
        else:
            uncached.append(c)

    # Pass 2: OpenFIGI batched — the fast bulk resolver (~200 CUSIPs/min unauth vs
    # Polygon's 5/min free-tier cap), so it goes first at scale.
    figi_misses: list = []
    if uncached:
        log.info("OpenFIGI resolving %d uncached CUSIPs (batch=%d)", len(uncached), batch_size)
        prog = Progress(len(uncached), "cusips (openfigi)", log, every=50)
        for i in range(0, len(uncached), batch_size):
            chunk = uncached[i:i + batch_size]
            mapped = _openfigi_batch(chunk, limiter, session)
            for c in chunk:
                t = mapped.get(c)
                if t:
                    rec = {"cusip": c, "ticker": t.upper(), "source": "openfigi", "name": None}
                    cmap[c] = rec
                    results[c] = rec
                else:
                    figi_misses.append(c)
                prog.step()
            if i % (batch_size * 50) == 0:   # periodic flush for crash-safety on long runs
                _save_map()
        prog.done()
        _save_map()

    # Pass 3: Polygon fallback for the (few) OpenFIGI misses — exact CUSIP match
    # catches names OpenFIGI lacks; small volume keeps us within the 5/min budget.
    if figi_misses and poly is not None and poly_fallback:
        log.info("Polygon fallback for %d OpenFIGI misses", len(figi_misses))
        prog = Progress(len(figi_misses), "cusips (polygon)", log, every=10)
        for c in figi_misses:
            pres = poly.cusip_lookup(c)
            ticker = pres.get("ticker") if pres else None
            rec = {"cusip": c, "ticker": ticker.upper() if ticker else None,
                   "source": "polygon" if ticker else None,
                   "name": pres.get("name") if pres else None}
            cmap[c] = rec
            results[c] = rec
            prog.step()
        prog.done()
        _save_map()
    elif figi_misses:
        # OpenFIGI-only mode: leave misses UNCACHED so a later --refresh-misses /
        # Polygon pass can still recover them; count them as unresolved for now.
        log.info("Skipping Polygon fallback for %d OpenFIGI misses (poly_fallback=off)",
                 len(figi_misses))

    resolved = sum(1 for r in results.values() if r.get("ticker"))
    log.info("Resolved %d/%d CUSIPs (%.1f%%)", resolved, len(uniq),
             100 * resolved / len(uniq) if uniq else 0)
    return results


def run(refresh_misses: bool = False, poly_fallback: bool = False) -> None:
    """Resolve every CUSIP present in the holdings ledger. Bulk default is
    OpenFIGI-only (poly_fallback=False) to avoid the slow 5/min Polygon tail."""
    from utils import DATA_DIR
    holdings_path = DATA_DIR / "hedge" / "holdings.json.gz"
    if not holdings_path.exists():
        log.error("No holdings ledger at %s; run fetch_13f.py first", holdings_path)
        return
    from holdings_io import load_holdings
    holdings = load_holdings(holdings_path)
    cusips = {h["cusip"] for h in holdings.values() if h.get("cusip")}
    resolve_many(list(cusips), refresh_misses=refresh_misses, poly_fallback=poly_fallback)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cusip", help="Resolve a single CUSIP and print the result")
    ap.add_argument("--from-holdings", action="store_true",
                    help="Resolve every CUSIP in data/hedge/holdings.json")
    ap.add_argument("--refresh-misses", action="store_true",
                    help="Retry CUSIPs previously cached as unresolved")
    args = ap.parse_args()

    if args.cusip:
        cfg = load_config()
        CUSIP_CACHE.mkdir(parents=True, exist_ok=True)
        poly = PolygonClient(os.environ.get("POLYGON_API_KEY", ""), cfg["polygon"])
        limiter = RateLimiter(cfg.get("openfigi", {}).get("rate_limit_calls_per_min", 20))
        rec = resolve(args.cusip, poly, limiter, requests.Session(),
                      refresh_misses=args.refresh_misses)
        print(rec)
    elif args.from_holdings:
        run(refresh_misses=args.refresh_misses)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
