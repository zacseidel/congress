from __future__ import annotations

"""
Price the Hedge (13F) universe on the dates the backtest actually needs.

The mirror-portfolio backtest values each fund's book at the close on each 13F's
filing date (entry) and the next filing date (exit). Those dates cluster on the
quarterly filing calendar, so across ALL funds there are only a few dozen distinct
dates. One Polygon grouped-daily call prices *every* US ticker for one day, so we
price the whole universe in a few dozen calls — versus one call per ticker.

We keep an UNPRUNED grouped snapshot per needed date in data/cache/hedge_grouped/
(the shared congress GROUPED_CACHE is pruned to congress tickers, so it can't serve
hedge-only names — hence a separate cache here). Weekends/holidays step back to the
prior trading day, and the result is stored under the requested date key so a
filing-date lookup always lands on a real close.
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils import (CACHE_DIR, TICKER_ALIASES_REV, PolygonClient, Progress, load_config,
                   load_json_gz, parse_date, save_json_gz, setup_logging)

log = setup_logging("price_hedge")

HEDGE_GROUPED = CACHE_DIR / "hedge_grouped"   # {ticker: close} per date, pruned to held tickers


def _grouped_snapshot(poly: PolygonClient, day: date, keep: Optional[set] = None) -> dict:
    """{ticker: close} for `day`. Steps back up to 4 days for holidays. Permanently
    cached; an empty file marks a known non-trading day. When `keep` is given, the
    saved snapshot is pruned to those tickers — the union of tickers our funds hold
    over the whole window + benchmark — dropping the ~4k never-held market names.
    Safe because a ticker only needs a price on dates it's held, and those dates are
    always fetched with a keep-set that includes it."""
    for delta in range(5):
        target = day - timedelta(days=delta)
        cp = HEDGE_GROUPED / f"{target.isoformat()}.json.gz"
        if cp.exists():
            data = load_json_gz(cp)
            if data:
                return data
            continue  # empty == non-trading day marker; try the prior day
        url = f"{poly.BASE}/v2/aggs/grouped/locale/us/market/stocks/{target.isoformat()}"
        try:
            payload = poly._get(url, {"adjusted": "true"})
        except Exception as e:
            log.debug("grouped %s failed: %s", target, poly._safe_err(e))
            continue
        results = payload.get("results") or []
        if not results:
            save_json_gz(cp, {})
            continue
        prices = {}
        for r in results:
            if "T" in r and "c" in r:
                t = TICKER_ALIASES_REV.get(r["T"], r["T"])  # BRK.B -> BRKB
                if keep is None or t in keep:
                    prices[t] = r["c"]
        save_json_gz(cp, prices)
        log.info("Priced %s: %d tickers", target, len(prices))
        return prices
    return {}


def build_price_map(date_isos, poly: Optional[PolygonClient] = None,
                    keep: Optional[set] = None) -> dict:
    """{date_iso: {ticker: close}} for each requested filing date. `keep` prunes newly
    fetched snapshots to the held-ticker set (see _grouped_snapshot)."""
    if poly is None:
        import os
        cfg = load_config()
        poly = PolygonClient(os.environ.get("POLYGON_API_KEY", ""), cfg["polygon"])
    HEDGE_GROUPED.mkdir(parents=True, exist_ok=True)
    dates = sorted({d for d in date_isos if d})
    pm: dict = {}
    prog = Progress(len(dates), "priced dates", log, every=5)
    for iso in dates:
        d = parse_date(iso)
        pm[iso] = _grouped_snapshot(poly, d, keep) if d else {}
        prog.step(iso)
    prog.done()
    return pm


def prune_cache(keep: set) -> None:
    """One-time: re-prune existing snapshots to the held-ticker keep-set, dropping the
    never-held market names. Zero coverage loss (dropped tickers are held by no fund)."""
    n_files = n_before = n_after = 0
    for f in sorted(HEDGE_GROUPED.glob("*.json.gz")):
        data = load_json_gz(f)
        if not data:
            continue
        pruned = {t: c for t, c in data.items() if t in keep}
        if len(pruned) < len(data):
            save_json_gz(f, pruned)
            n_files += 1
            n_before += len(data)
            n_after += len(pruned)
    log.info("Pruned %d snapshots: %d -> %d tickers each (avg)", n_files,
             n_before // max(n_files, 1), n_after // max(n_files, 1))
