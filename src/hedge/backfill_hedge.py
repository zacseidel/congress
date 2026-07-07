from __future__ import annotations

"""
Orchestrate the Hedge (13F) pipeline end to end, mirroring src/backfill.py.

Stages (each module is also runnable standalone):
  1. discover_filers  — 13F filer universe from EDGAR, ranked by AUM -> candidate_pool.json
  2. fetch_13f        — full holdings for the top-N pool CIKs -> holdings.json
  3. resolve_cusip    — CUSIP -> ticker for every held CUSIP (cached)
  4. backtest_13f     — mirror-portfolio alpha vs SPY -> fund_performance.json
  5. diff_holdings    — Q/Q new buys / exits / sizing -> changes.json
  6. rank_funds       — leaderboard + watchlist + docs/hedge/index.html
  7. generate_hedge_report — per-fund pages w/ congress cross-links -> docs/hedge/funds/

Usage:
  python src/hedge/backfill_hedge.py --top-n 50            # use existing pool, run 5 funds..N
  python src/hedge/backfill_hedge.py --discover --top-n 500
  python src/hedge/backfill_hedge.py --seed                # run the 9 hand-picked seed funds
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils import load_config, load_json, setup_logging

import discover_filers
import fetch_13f
import resolve_cusip
import backtest_13f
import rank_funds
import diff_holdings
import generate_hedge_report

log = setup_logging("backfill_hedge")

POOL_PATH = discover_filers.POOL_PATH


def _pool_ciks(top_n: int) -> list:
    if not POOL_PATH.exists():
        log.error("No candidate_pool.json — run with --discover first")
        return []
    pool = load_json(POOL_PATH)["pool"]
    return [p["cik"] for p in pool[:top_n]]


def run(top_n: int = None, do_discover: bool = False, seed: bool = False,
        discover_quarters: int = 3) -> None:
    cfg = load_config().get("hedge", {})
    top_n = top_n or cfg.get("candidate_pool_size", 1000)

    if do_discover:
        log.info("[1/5] Discovering + ranking filer universe")
        managers = discover_filers.discover(discover_quarters)
        discover_filers.rank_by_aum(managers)

    if seed:
        ciks = list(fetch_13f.SEED_FUNDS.values())
        log.info("Running %d seed funds", len(ciks))
    else:
        ciks = _pool_ciks(top_n)
        if not ciks:
            return
        log.info("Running top %d funds from candidate pool", len(ciks))

    log.info("[2/5] Fetching 13F holdings for %d funds", len(ciks))
    fetch_13f.run(ciks)

    log.info("[3/5] Resolving CUSIPs")
    resolve_cusip.run()

    log.info("[4/7] Backtesting mirror portfolios")
    backtest_13f.run()

    log.info("[5/7] Diffing quarter-over-quarter holdings")
    diff_holdings.run()

    log.info("[6/7] Ranking + rendering leaderboard")
    rank_funds.run()

    log.info("[7/7] Rendering per-fund pages")
    generate_hedge_report.run()
    log.info("Hedge pipeline complete.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, help="number of top-AUM funds to run")
    ap.add_argument("--discover", action="store_true", help="rebuild the filer universe + AUM ranking first")
    ap.add_argument("--seed", action="store_true", help="run the 9 hand-picked seed funds instead of the pool")
    ap.add_argument("--quarters", type=int, default=3)
    args = ap.parse_args()
    run(top_n=args.top_n, do_discover=args.discover, seed=args.seed,
        discover_quarters=args.quarters)


if __name__ == "__main__":
    main()
