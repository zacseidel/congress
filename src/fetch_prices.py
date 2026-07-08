from __future__ import annotations

"""
Build every traded ticker's 2-year daily close series from Polygon grouped-daily
snapshots.

One grouped-daily call prices *all* tickers for a given day, so the entire price
history costs at most one call per trading day (~500 for two years) regardless of
how many tickers there are — versus one call per ticker (re-pulled every refresh)
for the per-ticker aggregates endpoint. Snapshots are permanently cached, so a cold
start fills only the missing days and steady state is a single call for the latest
session.

Output: per-ticker {t, c[, v]} bars written to AGGS_CACHE — the shape fetch_charts,
compute_momentum, and compute_performance read. "v" (volume) is carried only on days
whose snapshot has it, feeding compute_momentum's up-vs-down-volume dot.

Usage:
  python src/fetch_prices.py
"""

import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import (AGGS_CACHE, DATA_DIR, GROUPED_CACHE, PolygonClient, Progress, fmt_duration,
                   load_config, load_json, load_json_gz, most_recent_trading_day, save_json_gz,
                   setup_logging)

log = setup_logging("fetch_prices")

LEDGER_PATH = DATA_DIR / "transactions.json"


def _weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def _epoch_ms(d: date) -> int:
    """Midnight-UTC epoch ms; datetime.utcfromtimestamp(t/1000).date() recovers d."""
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def run(refresh_days: int = 0) -> None:
    cfg = load_config()
    pcfg = cfg["polygon"]
    benchmark = cfg["pipeline"]["benchmark_ticker"]

    if not LEDGER_PATH.exists():
        log.error("No ledger at %s — run fetch_house/fetch_senate first", LEDGER_PATH)
        return
    rows = list(load_json(LEDGER_PATH).values())
    keep = {r["ticker"] for r in rows} | {benchmark}

    today = most_recent_trading_day()
    start = today - timedelta(days=pcfg["chart_lookback_days"])
    days = list(_weekdays(start, today))

    # 1) Ensure a grouped snapshot exists for every trading day in the window. Each
    #    call prices ALL tickers, so cost is bounded by uncached days, not tickers.
    api_key = os.environ.get("POLYGON_API_KEY", "")
    if api_key:
        poly = PolygonClient(api_key, pcfg)
        uncached = [d for d in days if not (GROUPED_CACHE / f"{d.isoformat()}.json.gz").exists()]
        log.info("Grouped-daily coverage: %d window days, %d uncached (~%s @ %d/min)",
                 len(days), len(uncached), fmt_duration(len(uncached) * 12),
                 pcfg["rate_limit_calls_per_min"])
        prog = Progress(len(uncached), "grouped days fetched", log, every=5)
        for d in uncached:
            poly.grouped_daily(d, keep=keep)  # permanently cached; holidays marked empty
            prog.step(d.isoformat())

        # One-time backfill: re-fetch the most recent `refresh_days` trading days so their
        # snapshots carry volume (older close-only days feed the momentum volume dot). New
        # days already store volume, so this is only needed once to seed the recent window.
        if refresh_days:
            recent = [d for d in days if (GROUPED_CACHE / f"{d.isoformat()}.json.gz").exists()][-refresh_days:]
            log.info("Refreshing %d recent snapshots to backfill volume", len(recent))
            rprog = Progress(len(recent), "snapshots refreshed", log, every=5)
            for d in recent:
                poly.grouped_daily(d, keep=keep, force=True)
                rprog.step(d.isoformat())
    else:
        log.warning("POLYGON_API_KEY not set — building bars from cached snapshots only")

    # 2) Reconstruct per-ticker close series from every in-window cached snapshot.
    lo, hi = start.isoformat(), today.isoformat()
    by_ticker: dict[str, list[tuple[int, float]]] = defaultdict(list)
    n_days = 0
    for f in sorted(GROUPED_CACHE.glob("*.json.gz")):
        iso = f.name[:-len(".json.gz")]
        if iso < lo or iso > hi:
            continue
        snap = load_json_gz(f)
        if not snap:
            continue  # empty file == non-trading-day marker
        n_days += 1
        t = _epoch_ms(date.fromisoformat(iso))
        for ticker, val in snap.items():
            # Snapshots are [close, volume] (v2) or a bare close (v1, no volume).
            close, vol = (val[0], val[1] if len(val) > 1 else 0) if isinstance(val, list) else (val, 0)
            if close is not None:
                by_ticker[ticker].append((t, close, vol))

    written = 0
    for ticker, series in by_ticker.items():
        series.sort()
        # Keep bars minimal: only carry "v" when we actually have volume (recent v2 days).
        bars = [({"t": t, "c": c, "v": v} if v else {"t": t, "c": c}) for t, c, v in series]
        save_json_gz(AGGS_CACHE / f"{ticker}.json.gz", bars)
        written += 1
    log.info("Built close series for %d tickers from %d trading days", written, n_days)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-days", type=int, default=0,
                    help="re-fetch the last N cached trading days to backfill volume (one-time)")
    args = ap.parse_args()
    run(refresh_days=args.refresh_days)


if __name__ == "__main__":
    main()
