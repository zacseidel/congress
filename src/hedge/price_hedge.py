from __future__ import annotations

"""
Price the Hedge (13F) universe on the dates the backtest actually needs.

The mirror-portfolio backtest values each fund's book at the close on each 13F's
filing date (entry) and the next filing date (exit). Those dates cluster on the
quarterly filing calendar, so across ALL funds there are only a few dozen distinct
dates. One Polygon grouped-daily call prices *every* US ticker for one day, so we
price the whole universe in a few dozen calls — versus one call per ticker.

We keep a hedge-specific, held-ticker-pruned grouped snapshot per needed date in
data/cache/hedge_grouped/. Weekends/holidays step back to the prior trading day.
If a CUSIP is resolved after a snapshot was pruned, one per-ticker aggregate-history
request fills all of that ticker's missing dates without re-fetching hundreds of
grouped market days.
"""

import sys
from bisect import bisect_right
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils import (AGGS_CACHE, CACHE_DIR, CUSIP_CACHE, DATA_DIR, TICKER_ALIASES_REV,
                   PolygonClient, Progress, load_config, load_json, load_json_gz,
                   parse_date, save_json_gz, setup_logging)

log = setup_logging("price_hedge")

HEDGE_GROUPED = CACHE_DIR / "hedge_grouped"   # {ticker: close} per date, pruned to held tickers
PENDING_PRICE_TICKERS_PATH = CUSIP_CACHE / "pending_price_tickers.json.gz"
CUSIP_OVERRIDES_PATH = DATA_DIR / "hedge" / "cusip_overrides.json"


def _pending_price_tickers() -> set[str]:
    pending = set()
    if PENDING_PRICE_TICKERS_PATH.exists():
        data = load_json_gz(PENDING_PRICE_TICKERS_PATH)
        pending |= set(data.get("tickers", []))
    if CUSIP_OVERRIDES_PATH.exists():
        for value in load_json(CUSIP_OVERRIDES_PATH).values():
            ticker = value if isinstance(value, str) else value.get("ticker")
            if ticker:
                pending.add(ticker.upper())
    return pending


def _grouped_snapshot(poly: PolygonClient, day: date, keep: Optional[set] = None) -> dict:
    """{ticker: close} for `day`. Steps back up to 4 days for holidays. Permanently
    cached; an empty file marks a known non-trading day. When `keep` is given, the
    saved snapshot is pruned to those tickers — the union of tickers our funds hold
    over the whole window + benchmark — dropping the ~4k never-held market names.
    Newly resolved tickers missing from old pruned snapshots are filled from their
    aggregate history by ``build_price_map``."""
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


def _backfill_pending_prices(pm: dict, dates: list[str], keep: Optional[set],
                             keep_by_date: Optional[dict[str, set]],
                             poly: PolygonClient) -> None:
    """Fill newly resolved tickers with one aggregate-history request per ticker."""
    pending = _pending_price_tickers()
    for ticker in sorted(pending):
        missing_dates = [
            iso for iso in dates
            if ticker in (keep_by_date.get(iso, set()) if keep_by_date is not None else (keep or set()))
            and ticker not in pm.get(iso, {})
        ]
        if not missing_dates:
            continue
        first = date.fromisoformat(min(missing_dates)) - timedelta(days=4)
        last = date.fromisoformat(max(missing_dates))
        path = AGGS_CACHE / f"{ticker}.json.gz"
        bars = load_json_gz(path) if path.exists() else []
        valid = [bar for bar in bars if bar.get("t") is not None and bar.get("c") is not None]
        valid.sort(key=lambda bar: bar["t"])
        if not valid or datetime.utcfromtimestamp(valid[0]["t"] / 1000).date() > first:
            valid = poly.aggregates(
                ticker, first.isoformat(), last.isoformat(), use_cache=False) or []
            valid = [bar for bar in valid if bar.get("t") is not None and bar.get("c") is not None]
        valid.sort(key=lambda bar: bar["t"])
        bar_days = [datetime.utcfromtimestamp(bar["t"] / 1000).date() for bar in valid]
        closes = [bar["c"] for bar in valid]
        filled = 0
        for iso in missing_dates:
            target = date.fromisoformat(iso)
            index = bisect_right(bar_days, target) - 1
            if index >= 0 and (target - bar_days[index]).days <= 4:
                pm[iso][ticker] = closes[index]
                filled += 1
        log.info("Aggregate backfill %s: priced %d/%d needed dates",
                 ticker, filled, len(missing_dates))


def build_price_map(date_isos, poly: Optional[PolygonClient] = None,
                    keep: Optional[set] = None,
                    keep_by_date: Optional[dict[str, set]] = None) -> dict:
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
        date_keep = keep_by_date.get(iso, set()) if keep_by_date is not None else keep
        pm[iso] = _grouped_snapshot(poly, d, date_keep) if d else {}
        prog.step(iso)
    prog.done()
    _backfill_pending_prices(pm, dates, keep, keep_by_date, poly)
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
