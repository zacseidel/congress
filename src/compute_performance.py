from __future__ import annotations

"""
The "follow them into the trade" engine.

For each (member, ticker): walk disclosures in date order. A purchase opens a
lot priced at the close on/after its disclosure date. A sale closes the member's
*entire* open position in that ticker at the sale's disclosure-date close. Lots
still open at the end are held to today. Each position is benchmarked against SPY
over its own holding window to produce an alpha.

Member leaderboard score = dollar-weighted (by disclosed amount-range midpoint)
mean return across all of that member's positions.

Cost: one grouped-daily Polygon call per unique disclosure date + today, all
permanently cached.

Usage:
  python src/compute_performance.py [--date YYYY-MM-DD]
"""

import argparse
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from datetime import datetime

from utils import (AGGS_CACHE, DATA_DIR, GROUPED_CACHE, PolygonClient, Progress, fmt_duration,
                   load_config, load_json, load_json_gz, most_recent_trading_day, parse_date,
                   save_json, setup_logging)

log = setup_logging("compute_performance")

LEDGER_PATH = DATA_DIR / "transactions.json"
PERFORMANCE_PATH = DATA_DIR / "performance.json"


def _safe_pct(end, start):
    if start and end and start > 0:
        return (end / start - 1) * 100
    return None


def run(today: date | None = None) -> None:
    cfg = load_config()
    benchmark = cfg["pipeline"]["benchmark_ticker"]
    min_positions = cfg["scoring"]["min_positions_to_rank"]

    if not LEDGER_PATH.exists():
        log.error("No ledger; run fetchers first")
        return
    rows = list(load_json(LEDGER_PATH).values())

    today = today or most_recent_trading_day()
    api_key = os.environ.get("POLYGON_API_KEY", "")
    if not api_key:
        log.warning("POLYGON_API_KEY not set — cannot price positions; skipping")
        return
    poly = PolygonClient(api_key, cfg["polygon"])

    # Grouped snapshots are pruned to the tickers we read (everything in the ledger
    # plus the benchmark). The keep-set only grows, so re-pruning a date later always
    # keeps a superset.
    keep = {r["ticker"] for r in rows} | {benchmark}

    # Build a price map for every unique disclosure date + today (grouped daily, cached).
    needed = sorted({r["disclosure_date"] for r in rows} | {today.isoformat()})
    n_uncached = sum(1 for ds in needed if not (GROUPED_CACHE / f"{ds}.json.gz").exists())
    log.info("Pricing %d unique dates (grouped daily) | %d uncached (~%s of API calls @ 5/min)",
             len(needed), n_uncached, fmt_duration(n_uncached * 12))
    prog = Progress(len(needed), "priced dates", log, every=5)
    price_map: dict[str, dict[str, float]] = {}
    for ds in needed:
        d = parse_date(ds)
        if not d:
            prog.step()
            continue
        price_map[ds] = poly.grouped_daily(d, keep=keep)
        prog.step(ds)
    today_prices = price_map.get(today.isoformat(), {})

    # Fallback: a ticker missing from a pruned grouped snapshot (a newly-added name,
    # or one absent from the grouped feed) is priced from its own 2-year aggs cache,
    # which enrichment already fetched. No grouped re-fetch is ever needed.
    _aggs_series_cache: dict[str, dict] = {}

    def _aggs_series(ticker: str) -> dict:
        if ticker not in _aggs_series_cache:
            path = AGGS_CACHE / f"{ticker}.json.gz"
            series: dict[str, float] = {}
            if path.exists():
                try:
                    for b in load_json_gz(path):
                        if b.get("t") and b.get("c") is not None:
                            iso = datetime.utcfromtimestamp(b["t"] / 1000).date().isoformat()
                            series[iso] = b["c"]
                except Exception:
                    pass
            _aggs_series_cache[ticker] = series
        return _aggs_series_cache[ticker]

    def price(date_iso: str, ticker: str):
        v = (price_map.get(date_iso) or {}).get(ticker)
        if v is not None:
            return v
        series = _aggs_series(ticker)
        if not series:
            return None
        if date_iso in series:
            return series[date_iso]
        prior = [k for k in series if k <= date_iso]
        return series[max(prior)] if prior else None

    # Group ledger rows by member + ticker.
    by_mt: dict[tuple, list] = defaultdict(list)
    member_name: dict[str, str] = {}
    member_meta: dict[str, dict] = {}
    for r in rows:
        by_mt[(r["member_id"], r["ticker"])].append(r)
        member_name[r["member_id"]] = r["member"]
        member_meta.setdefault(r["member_id"], {"chamber": r["chamber"], "state": r.get("state", ""),
                                                "party": r.get("party", "")})

    positions: list[dict] = []
    for (member_id, ticker), trs in by_mt.items():
        trs.sort(key=lambda x: x["disclosure_date"])
        open_lots: list[dict] = []

        def close_lots(exit_iso: str):
            exit_px = price(exit_iso, ticker)
            spy_exit = price(exit_iso, benchmark)
            for lot in open_lots:
                _record(lot, exit_iso, exit_px, spy_exit, "closed")
            open_lots.clear()

        def _record(lot, exit_iso, exit_px, spy_exit, status):
            ret = _safe_pct(exit_px, lot["entry_price"])
            spy_ret = _safe_pct(spy_exit, lot["spy_entry"])
            alpha = (ret - spy_ret) if (ret is not None and spy_ret is not None) else None
            positions.append({
                "member_id": member_id, "member": member_name[member_id],
                "chamber": member_meta[member_id]["chamber"],
                "ticker": ticker,
                "entry_date": lot["entry_date"], "exit_date": exit_iso,
                "entry_price": lot["entry_price"], "exit_price": exit_px,
                "weight": lot["weight"], "status": status,
                "return_pct": round(ret, 2) if ret is not None else None,
                "spy_return_pct": round(spy_ret, 2) if spy_ret is not None else None,
                "alpha": round(alpha, 2) if alpha is not None else None,
            })

        for r in trs:
            ds = r["disclosure_date"]
            if r["tx_type"] == "P":
                open_lots.append({
                    "entry_date": ds,
                    "entry_price": price(ds, ticker),
                    "spy_entry": price(ds, benchmark),
                    "weight": r.get("amount_mid", 0) or 0,
                })
            elif r["tx_type"] == "S":
                close_lots(ds)
            # Exchanges (E) are ignored.

        # Remaining lots are held to today.
        today_iso = today.isoformat()
        exit_px = price(today_iso, ticker)
        spy_exit = price(today_iso, benchmark)
        for lot in open_lots:
            _record(lot, today_iso, exit_px, spy_exit, "open")

    # Keep only priced positions for stats.
    valid = [p for p in positions if p["return_pct"] is not None and p["weight"] > 0]
    log.info("Built %d positions (%d priced)", len(positions), len(valid))

    # Aggregate per member (dollar-weighted).
    members: dict[str, dict] = {}
    by_member = defaultdict(list)
    for p in valid:
        by_member[p["member_id"]].append(p)

    for member_id, ps in by_member.items():
        wsum = sum(p["weight"] for p in ps)
        if wsum <= 0:
            continue
        dw_return = sum(p["weight"] * p["return_pct"] for p in ps) / wsum
        ew_return = sum(p["return_pct"] for p in ps) / len(ps)
        alpha_ps = [p for p in ps if p["alpha"] is not None]
        wa = sum(p["weight"] for p in alpha_ps)
        dw_alpha = (sum(p["weight"] * p["alpha"] for p in alpha_ps) / wa) if wa else None
        members[member_id] = {
            "member_id": member_id,
            "member": member_name[member_id],
            "chamber": member_meta[member_id]["chamber"],
            "state": member_meta[member_id]["state"],
            "party": member_meta[member_id]["party"],
            "n_positions": len(ps),
            "n_open": sum(1 for p in ps if p["status"] == "open"),
            "n_closed": sum(1 for p in ps if p["status"] == "closed"),
            "total_dollars": round(wsum),
            "dw_return_pct": round(dw_return, 2),
            "ew_return_pct": round(ew_return, 2),
            "dw_alpha": round(dw_alpha, 2) if dw_alpha is not None else None,
            "win_rate": round(100 * sum(1 for p in ps if p["return_pct"] > 0) / len(ps), 1),
            "rankable": len(ps) >= min_positions,
        }

    # Trailing-window benchmark: S&P 500 (via SPY) return over the full window.
    start_iso = needed[0] if needed else today.isoformat()
    spy_start = (price_map.get(start_iso) or {}).get(benchmark)
    spy_now = today_prices.get(benchmark)
    benchmark_period = {
        "ticker": benchmark, "start_date": start_iso, "end_date": today.isoformat(),
        "start_price": spy_start, "end_price": spy_now,
        "return_pct": round((spy_now / spy_start - 1) * 100, 1)
        if (spy_start and spy_now and spy_start > 0) else None,
    }

    save_json(PERFORMANCE_PATH, {
        "generated": today.isoformat(),
        "benchmark": benchmark,
        "benchmark_period": benchmark_period,
        "positions": positions,
        "members": members,
    })
    log.info("Saved performance: %d members, %d positions | S&P 500 over window: %s%%",
             len(members), len(positions), benchmark_period["return_pct"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="treat this date as 'today' (YYYY-MM-DD)")
    args = ap.parse_args()
    run(parse_date(args.date) if args.date else None)


if __name__ == "__main__":
    main()
