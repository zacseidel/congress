from __future__ import annotations

"""
One-shot orchestrator to build the whole site from scratch (or refresh it).

Runs every stage in order. On the Polygon free tier the enrichment stage is the
slow part (~4 calls/ticker at 5 calls/min); it is resumable — caching means each
run only spends calls on tickers not yet enriched — so for a cold start you can
run this script several times until `enrich` reports 0 new tickers.

Usage:
  python src/backfill.py [--lookback-days N] [--enrich-max N]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fetch_house
import fetch_senate
import fetch_committees
import ocr_scanned
import fetch_prices
import enrich
import refresh_news
import fetch_charts
import compute_momentum
import compute_performance
import score_and_rank
import build_graph
import generate_report
from utils import load_config, setup_logging

log = setup_logging("backfill")


def run(lookback_days: int, enrich_max: int) -> None:
    # Phase 1 — produce the leaderboard fast. Returns come from grouped-daily
    # pricing and need no Polygon enrichment, so render a complete report first.
    log.info("[1/10] fetch House ===========================================")
    fetch_house.run(lookback_days)
    log.info("[2/10] fetch Senate ==========================================")
    fetch_senate.run(lookback_days)
    log.info("[3/10] fetch committees ======================================")
    fetch_committees.run()
    log.info("[3.5/10] OCR scanned House PTRs (capped, resumable) ==========")
    ocr_scanned.run()
    log.info("[3.7/10] prices (grouped-daily → per-ticker close bars) =======")
    fetch_prices.run()
    log.info("[4/10] performance (pricing all disclosure dates) ============")
    compute_performance.run()
    log.info("[5/10] rankings ==============================================")
    score_and_rank.run()
    log.info("[6/10] graph =================================================")
    build_graph.run()
    log.info("[7/10] report (leaderboard + map ready) ======================")
    generate_report.run()
    log.info(">>> Leaderboard, skill map and network are live in docs/ — open them while enrichment runs.")

    # Phase 2 — slow on the free tier (~4 Polygon calls/ticker at 5/min). This
    # adds company descriptions, news, financials, and price charts to the stock
    # detail pages. Resumable: re-run backfill to continue where caching left off.
    log.info("[8/10] enrich (max %d new tickers) ==========================", enrich_max)
    enrich.run(enrich_max)
    log.info("[8/10] refresh news (relevant tickers) ======================")
    refresh_news.run()
    log.info("[9/10] charts ===============================================")
    fetch_charts.run()
    log.info("[9/10] momentum =============================================")
    compute_momentum.run()
    log.info("[9/10] graph (with enrichment) ==============================")
    build_graph.run()
    log.info("[10/10] report (with enrichment) ============================")
    generate_report.run()
    log.info(">>> Backfill complete.")


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback-days", type=int, default=cfg["pipeline"]["lookback_days"])
    ap.add_argument("--enrich-max", type=int, default=10_000,
                    help="max new tickers to enrich this pass (default: effectively all)")
    args = ap.parse_args()
    run(args.lookback_days, args.enrich_max)


if __name__ == "__main__":
    main()
