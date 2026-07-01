from __future__ import annotations

"""
Refresh recent-news for the *relevant* tickers whose cached headlines have gone
stale — decoupled from the heavy price/details enrichment so news stays current
without re-enriching everything.

Relevant = disclosed in the last `news_relevant_days` OR shown in the report's
driver / recent-buy tables, and already enriched (has a stock page). Only stale
ones (news cache older than `news_ttl_days`) are refreshed, capped per run and
ordered by recency. Updates company_info so the stock pages reflect it.

Cost: ~1 Polygon call per refreshed ticker (<= news_refresh_max).

Usage:
  python src/refresh_news.py [--max N]
"""

import argparse
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import (DATA_DIR, NEWS_CACHE, PolygonClient, load_config, load_json,
                   save_json, setup_logging)

log = setup_logging("refresh_news")

LEDGER_PATH = DATA_DIR / "transactions.json"
COMPANY_INFO_PATH = DATA_DIR / "company_info.json"
RANKINGS_PATH = DATA_DIR / "rankings.json"


def run(max_override: int | None = None) -> None:
    cfg = load_config()
    pcfg = cfg["polygon"]
    ttl = pcfg.get("news_ttl_days", 14)
    cap = max_override if max_override is not None else pcfg.get("news_refresh_max", 40)
    relevant_days = cfg["report"].get("news_relevant_days", 120)

    if not COMPANY_INFO_PATH.exists():
        log.info("No company_info yet — nothing to refresh")
        return
    company_info = load_json(COMPANY_INFO_PATH)
    ledger = load_json(LEDGER_PATH).values() if LEDGER_PATH.exists() else []
    rankings = load_json(RANKINGS_PATH) if RANKINGS_PATH.exists() else {}

    # Relevance inputs.
    latest_disc: dict[str, str] = {}
    dollar: dict[str, float] = defaultdict(float)
    for tx in ledger:
        t = tx["ticker"]
        latest_disc[t] = max(latest_disc.get(t, ""), tx["disclosure_date"])
        dollar[t] += tx.get("amount_mid", 0) or 0
    # Out-performer companies (any ticker an out-performing member bought) are always
    # relevant; so is anything disclosed within news_relevant_days.
    outperformer = {t for t, s in rankings.get("stocks", {}).items()
                    if (s or {}).get("n_outperformer_buyers", 0) > 0}

    cutoff = (date.today() - timedelta(days=relevant_days)).isoformat()
    candidates = [t for t in company_info
                  if t in outperformer or latest_disc.get(t, "") >= cutoff]
    # Most relevant first: out-performer, then most-recent disclosure, then dollar volume.
    candidates.sort(key=lambda t: (t in outperformer, latest_disc.get(t, ""), dollar.get(t, 0)),
                    reverse=True)

    api_key = os.environ.get("POLYGON_API_KEY", "")
    if not api_key:
        log.warning("POLYGON_API_KEY not set — skipping news refresh")
        return
    poly = PolygonClient(api_key, pcfg)

    refreshed = 0
    for t in candidates:
        if refreshed >= cap:
            break
        if PolygonClient._cache_fresh(NEWS_CACHE / f"{t}.json", ttl):
            continue                               # still fresh — no call
        news = poly.ticker_news(t)                 # fetch + cache (1 API call)
        company_info[t]["recent_news"] = [
            {"title": n.get("title"), "article_url": n.get("article_url"),
             "publisher": (n.get("publisher") or {}).get("name"),
             "published_utc": n.get("published_utc")}
            for n in news
        ]
        refreshed += 1

    save_json(COMPANY_INFO_PATH, company_info)
    log.info("Refreshed news for %d tickers (cap %d, %d relevant candidates)",
             refreshed, cap, len(candidates))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=None, help="override max refreshes this run")
    args = ap.parse_args()
    run(args.max)


if __name__ == "__main__":
    main()
