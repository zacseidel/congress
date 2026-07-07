from __future__ import annotations

"""
The mirror-portfolio backtest: "what would a retail investor have earned by
copying this manager's disclosed book, entering the day each 13F went public?"

For each fund:
  * Order its 13F-HR filings by filing (public) date. Each filing opens a holding
    period that runs to the next filing's date (the last runs to the most recent
    trading day) — i.e. we hold the disclosed book until the next disclosure, then
    rebalance to the new book.
  * Value the long-equity book (share positions; options excluded in v1) at the
    close on the entry date and again at the exit date, weighting by the reported
    market value. Positions we can't price (unresolved CUSIP, or no close on a
    date) are dropped and the surviving weights renormalized; the dropped fraction
    is tracked as (1 - coverage).
  * Period return = value-weighted return of the priced book. Chain the periods to
    a cumulative fund return, and benchmark against SPY over the identical dates.
    alpha = cumulative fund return - cumulative SPY return.

Reuses: resolve_cusip cache (CUSIP -> ticker) and price_hedge.build_price_map
(grouped-daily closes on the needed filing dates). Writes fund_performance.json.

Usage:
  python src/hedge/backtest_13f.py [--cik 1067983]
"""

import argparse
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils import (DATA_DIR, Progress, load_config, load_json_gz, most_recent_trading_day,
                   save_json_gz, setup_logging)
from resolve_cusip import cached as cusip_cached
from price_hedge import build_price_map
from holdings_io import load_holdings

log = setup_logging("backtest_13f")

HEDGE_DIR = DATA_DIR / "hedge"
HOLDINGS_PATH = HEDGE_DIR / "holdings.json.gz"
PERFORMANCE_PATH = HEDGE_DIR / "fund_performance.json.gz"


def _ticker_for(cusip: str):
    rec = cusip_cached(cusip)
    return rec.get("ticker") if rec else None


def _period_return(holdings: list, price_map: dict, entry_iso: str, exit_iso: str) -> dict:
    """Value-weighted return of the priced long book over [entry, exit].
    Returns {return, total_value, priced_value, n_priced, n_total}."""
    entry_px = price_map.get(entry_iso, {})
    exit_px = price_map.get(exit_iso, {})
    total_value = 0.0
    priced = []  # (ticker, issuer, value, ret)
    for h in holdings:
        if h.get("put_call"):        # v1: long shares only, skip options
            continue
        total_value += h["value"]
        ticker = _ticker_for(h["cusip"])
        if not ticker:
            continue
        e, x = entry_px.get(ticker), exit_px.get(ticker)
        if not e or not x or e <= 0:
            continue
        priced.append((ticker, h.get("issuer"), h["value"], x / e - 1.0))
    priced_value = sum(p[2] for p in priced)
    if priced_value <= 0:
        return {"return": None, "total_value": total_value, "priced_value": 0.0,
                "n_priced": 0, "n_total": len(holdings), "holdings": []}
    ret = sum((v / priced_value) * r for _, _, v, r in priced)
    return {"return": ret, "total_value": total_value, "priced_value": priced_value,
            "n_priced": len(priced), "n_total": len(holdings), "holdings": priced}


def backtest_fund(cik: int, holdings_by_filing: dict, price_map: dict,
                  benchmark: str, today_iso: str) -> dict:
    """Chain holding-period returns for one fund. holdings_by_filing: {filing_date: [rows]}."""
    filing_dates = sorted(holdings_by_filing)
    name = holdings_by_filing[filing_dates[0]][0]["manager"]
    periods = []
    cum_fund, cum_spy = 1.0, 1.0
    cov_num, cov_den = 0.0, 0.0
    # Per-ticker attribution of the fund's ALPHA (excess over SPY): each period a
    # position contributes (growth before p) × weight × (position return − SPY return).
    # This measures how much each name helped the fund BEAT the benchmark, which is what
    # the alpha ranking is about — a position that merely tracked SPY contributes ~0.
    contrib: dict = defaultdict(lambda: {"issuer": None, "contribution": 0.0,
                                         "weight_sum": 0.0, "quarters": 0, "best": None})
    for i, fd in enumerate(filing_dates):
        exit_iso = filing_dates[i + 1] if i + 1 < len(filing_dates) else today_iso
        if exit_iso <= fd:
            continue
        pr = _period_return(holdings_by_filing[fd], price_map, fd, exit_iso)
        if pr["return"] is None:
            continue
        # SPY over the same window
        spy_e = price_map.get(fd, {}).get(benchmark)
        spy_x = price_map.get(exit_iso, {}).get(benchmark)
        spy_ret = (spy_x / spy_e - 1.0) if (spy_e and spy_x and spy_e > 0) else None
        c_before = cum_fund      # cumulative growth factor entering this period
        pv = pr["priced_value"]
        active = spy_ret if spy_ret is not None else 0.0   # benchmark to beat this period
        for ticker, issuer, value, r in pr["holdings"]:
            w = value / pv
            c = contrib[ticker]
            c["issuer"] = issuer or c["issuer"]
            c["contribution"] += c_before * w * (r - active)   # contribution to ALPHA (vs SPY)
            c["weight_sum"] += w
            c["quarters"] += 1
            if c["best"] is None or (r - active) > c["best"]:
                c["best"] = r - active
        cum_fund *= (1 + pr["return"])
        if spy_ret is not None:
            cum_spy *= (1 + spy_ret)
        coverage = pr["priced_value"] / pr["total_value"] if pr["total_value"] else 0.0
        cov_num += coverage * pr["total_value"]
        cov_den += pr["total_value"]
        periods.append({
            "entry": fd, "exit": exit_iso,
            "return": round(pr["return"], 4),
            "spy_return": round(spy_ret, 4) if spy_ret is not None else None,
            "coverage": round(coverage, 3),
            "n_priced": pr["n_priced"], "n_total": pr["n_total"],
            "book_value": round(pr["total_value"], 0),
        })
    cumulative_return = cum_fund - 1.0
    spy_return = cum_spy - 1.0
    avg_coverage = cov_num / cov_den if cov_den else 0.0
    # Consistency: share of quarters the fund beat SPY. A robustness signal shown next
    # to alpha so a single lucky quarter is distinguishable from durable outperformance
    # (small concentrated funds can post huge cumulative alpha off one bet).
    beats = [p for p in periods if p["spy_return"] is not None]
    hit_rate = round(sum(1 for p in beats if p["return"] > p["spy_return"]) / len(beats), 3) if beats else None

    # Rank positions by their contribution to the fund's cumulative return; keep the
    # biggest winners and losers (the drivers). Contributions are in return-fraction
    # units and sum to cumulative_return.
    drivers_all = sorted(
        ({"ticker": t, "issuer": d["issuer"], "contribution": round(d["contribution"], 4),
          "avg_weight": round(d["weight_sum"] / d["quarters"], 4) if d["quarters"] else 0,
          "quarters": d["quarters"], "best_period_return": round(d["best"], 4) if d["best"] is not None else None}
         for t, d in contrib.items()),
        key=lambda x: x["contribution"], reverse=True)
    drivers = [d for d in drivers_all if d["contribution"] > 0][:15]
    detractors = [d for d in drivers_all if d["contribution"] < 0][-10:][::-1]
    return {
        "cik": cik, "name": name,
        "n_filings": len(filing_dates),
        "n_periods": len(periods),
        "cumulative_return": round(cumulative_return, 4),
        "spy_return": round(spy_return, 4),
        "alpha": round(cumulative_return - spy_return, 4),
        "hit_rate": hit_rate,
        "coverage": round(avg_coverage, 3),
        "latest_book": round(sum(h["value"] for h in holdings_by_filing[filing_dates[-1]]
                                 if not h.get("put_call")), 0),
        "periods": periods,
        "drivers": drivers,
        "detractors": detractors,
    }


def run(ciks=None, today: date = None) -> None:
    cfg = load_config()
    benchmark = cfg["pipeline"]["benchmark_ticker"]
    today_iso = (today or most_recent_trading_day()).isoformat()

    if not HOLDINGS_PATH.exists():
        log.error("No holdings at %s; run fetch_13f.py first", HOLDINGS_PATH)
        return
    holdings = load_holdings(HOLDINGS_PATH)

    # Group holdings by fund, then by filing date.
    by_fund: dict = defaultdict(lambda: defaultdict(list))
    for h in holdings.values():
        if ciks and h["cik"] not in ciks:
            continue
        by_fund[h["cik"]][h["filing_date"]].append(h)

    # Price every filing date (+ today) once, across all funds. Prune snapshots to the
    # union of tickers actually held over the window + benchmark, so the price cache
    # never stores the ~4k market names no fund holds.
    needed_dates = {fd for f in by_fund.values() for fd in f} | {today_iso}
    keep = {t for h in holdings.values() if not h.get("put_call")
            for t in [_ticker_for(h["cusip"])] if t} | {benchmark}
    log.info("Pricing %d filing dates (+today) for %d funds | keep-set %d tickers",
             len(needed_dates), len(by_fund), len(keep))
    price_map = build_price_map(needed_dates, keep=keep)
    if benchmark not in price_map.get(today_iso, {}):
        log.warning("Benchmark %s not found on %s — SPY returns may be incomplete",
                    benchmark, today_iso)

    results = {}
    prog = Progress(len(by_fund), "funds backtested", log, every=5)
    for cik, hbf in by_fund.items():
        try:
            results[str(cik)] = backtest_fund(cik, hbf, price_map, benchmark, today_iso)
        except Exception as e:
            log.warning("backtest failed for CIK %s: %s", cik, e)
        prog.step()
    prog.done()

    save_json_gz(PERFORMANCE_PATH, results)
    log.info("Wrote %d fund performances -> %s", len(results), PERFORMANCE_PATH)
    # Quick leaderboard preview to stderr.
    ranked = sorted(results.values(), key=lambda r: r["alpha"], reverse=True)
    log.info("--- alpha leaderboard (preview) ---")
    for r in ranked:
        log.info("  %-42s alpha %+6.1f%%  (fund %+6.1f%% vs SPY %+6.1f%%)  cov %.0f%%  %dq",
                 r["name"][:41], 100 * r["alpha"], 100 * r["cumulative_return"],
                 100 * r["spy_return"], 100 * r["coverage"], r["n_periods"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cik", type=int, help="Backtest a single fund by CIK")
    args = ap.parse_args()
    run(ciks=[args.cik] if args.cik else None)


if __name__ == "__main__":
    main()
