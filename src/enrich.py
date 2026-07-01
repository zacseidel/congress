from __future__ import annotations

"""
Enrich traded tickers with Polygon company context:
description, sector, market cap, recent news, key financials, and a price summary.

The price summary (current price, 52-week high/low) is derived for free from the
close bars fetch_prices already built from grouped-daily snapshots — no per-ticker
price call is ever made here. API budget is spent only on:
  * new/stale company profiles — description recheck cadence is details_ttl_days
    (~6 months); the profile call also pulls financials + news.
  * financials-only refreshes — annual statements on their own financials_ttl_days
    (~90 days) cadence, without re-pulling the slow-changing description.

--focus outperformers restricts this API-spending work to the outperformer companies
in rankings.json (the standard, frequently-run pipeline); the default (all) is the
full refresh used by backfill.py. Price fields are refreshed for every ticker either
way, since that costs no API calls.

Usage:
  python src/enrich.py [--max N] [--focus all|outperformers]
"""

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import (AGGS_CACHE, DATA_DIR, FINANCIALS_CACHE, POLYGON_CACHE, Progress, PolygonClient,
                   fmt_duration, load_config, load_json, load_json_gz, save_json, setup_logging)

log = setup_logging("enrich")

LEDGER_PATH = DATA_DIR / "transactions.json"
COMPANY_INFO_PATH = DATA_DIR / "company_info.json"
RANKINGS_PATH = DATA_DIR / "rankings.json"


def outperformer_tickers() -> set[str]:
    """Tickers bought by at least one out-performing member, from rankings.json.
    Empty set if rankings haven't been computed yet."""
    if not RANKINGS_PATH.exists():
        return set()
    stocks = (load_json(RANKINGS_PATH) or {}).get("stocks", {})
    return {t for t, s in stocks.items() if (s or {}).get("n_outperformer_buyers", 0) > 0}


def _financials_summary(fin: dict | None) -> dict:
    if not fin:
        return {}
    f = fin.get("financials", {}) or {}
    inc = f.get("income_statement", {}) or {}
    bal = f.get("balance_sheet", {}) or {}

    def val(d, k):
        return (d.get(k) or {}).get("value")

    return {
        "fiscal_period": fin.get("fiscal_period"),
        "fiscal_year": fin.get("fiscal_year"),
        "revenues": val(inc, "revenues"),
        "net_income": val(inc, "net_income_loss"),
        "operating_income": val(inc, "operating_income_loss"),
        "diluted_eps": val(inc, "diluted_earnings_per_share"),
        "assets": val(bal, "assets"),
        "liabilities": val(bal, "liabilities"),
        "equity": val(bal, "equity"),
    }


def _has_profile(ticker: str, company_info: dict, details_ttl: int) -> bool:
    """A ticker is 'profiled' once its company_info record exists and its *details*
    cache is still fresh (details_ttl_days). Price and financials freshness are
    deliberately NOT part of this check — both are refreshed on their own cadence
    (prices for free from the close bars; financials via a separate cheap pass) so
    neither triggers a full re-enrichment of an existing profile."""
    return (ticker in company_info
            and PolygonClient._cache_fresh(POLYGON_CACHE / f"{ticker}.json", details_ttl))


def _price_summary(ticker: str) -> dict:
    """Current price and 52-week (window) high/low from the cached close bars that
    fetch_prices built from grouped-daily snapshots. Close-based (no intraday extremes)
    and free — no API call."""
    path = AGGS_CACHE / f"{ticker}.json.gz"
    bars = load_json_gz(path) if path.exists() else []
    closes = [b["c"] for b in bars if b.get("c") is not None]  # bars are sorted ascending
    return {
        "current_price": closes[-1] if closes else None,
        "week_52_high": max(closes) if closes else None,
        "week_52_low": min(closes) if closes else None,
        "has_prices": bool(closes),
    }


def run(max_override: int | None = None, focus: str | None = None) -> None:
    cfg = load_config()
    pcfg = cfg["polygon"]
    max_new = max_override if max_override is not None else pcfg["max_enrichment_tickers"]
    details_ttl = pcfg.get("details_ttl_days", 180)
    financials_ttl = pcfg.get("financials_ttl_days", 90)

    if not LEDGER_PATH.exists():
        log.error("No ledger at %s — run fetch_house/fetch_senate first", LEDGER_PATH)
        return
    ledger = load_json(LEDGER_PATH)
    rows = list(ledger.values())

    # Unique tickers, ranked by total disclosed dollar volume (enrich the big ones first).
    dollar = defaultdict(float)
    for r in rows:
        dollar[r["ticker"]] += r.get("amount_mid", 0) or 0
    tickers = sorted(dollar, key=lambda t: dollar[t], reverse=True)
    log.info("%d unique tickers in ledger", len(tickers))

    api_key = os.environ.get("POLYGON_API_KEY", "")
    company_info = load_json(COMPANY_INFO_PATH) if COMPANY_INFO_PATH.exists() else {}

    # Price fields come (free) from the close bars fetch_prices built, so refresh them
    # for every already-profiled ticker each run — even ones whose profile is untouched.
    for ticker in tickers:
        if ticker in company_info:
            company_info[ticker].update(_price_summary(ticker))

    if not api_key:
        log.warning("POLYGON_API_KEY not set — keeping existing company_info (%d)", len(company_info))
        save_json(COMPANY_INFO_PATH, company_info)
        return

    poly = PolygonClient(api_key, pcfg)

    # Scope the API-spending work. The standard pipeline focuses on the out-performer
    # companies (deep dives that matter); the full refresh (default) covers everything.
    scope = tickers
    if focus == "outperformers":
        op = outperformer_tickers()
        scope = [t for t in tickers if t in op]
        log.info("Focus=outperformers — %d of %d tickers in scope", len(scope), len(tickers))
        if not scope:
            log.warning("No out-performer tickers (rankings.json missing/empty?) — nothing to enrich")
            save_json(COMPANY_INFO_PATH, company_info)
            return

    # 1) Build profiles for scoped tickers whose record is missing or whose description
    #    (details) cache has gone stale. ~3 calls each (details + financials + news).
    needs_profile = [t for t in scope if not _has_profile(t, company_info, details_ttl)]
    planned = min(len(needs_profile), max_new)
    log.info("%d in scope | %d profiled & fresh | up to %d new/stale profiles via API (~%s)",
             len(scope), len(scope) - len(needs_profile), planned, fmt_duration(planned * 3 * 12))
    prog = Progress(planned, "profiles (API)", log)

    new_enriched = 0
    for ticker in needs_profile:
        if new_enriched >= max_new:
            break  # API budget spent this run; pick up the rest next run
        new_enriched += 1
        prog.step(ticker)

        details = poly.ticker_details(ticker) or {}
        fin = poly.financials(ticker)
        news = poly.ticker_news(ticker)

        company_info[ticker] = {
            "ticker": ticker,
            "name": details.get("name"),
            "description": (details.get("description") or "")[:cfg["report"]["description_max_chars"]],
            "sic_code": details.get("sic_code"),
            "sic_description": details.get("sic_description"),
            "market_cap": details.get("market_cap"),
            "total_employees": details.get("total_employees"),
            "homepage_url": details.get("homepage_url"),
            "icon_url": (details.get("branding") or {}).get("icon_url"),
            **_price_summary(ticker),
            "recent_news": [
                {"title": n.get("title"), "article_url": n.get("article_url"),
                 "publisher": (n.get("publisher") or {}).get("name"),
                 "published_utc": n.get("published_utc")}
                for n in news
            ],
            "financials": _financials_summary(fin),
        }

    # 2) Financials-only refresh: for already-profiled scoped tickers whose annual
    #    statements are older than financials_ttl_days, refresh just the financials
    #    (1 call) without re-pulling the slow-changing description.
    fin_refreshed = 0
    for ticker in scope:
        if fin_refreshed >= max_new:
            break  # keep the free-tier budget bounded; the rest refresh next run
        if not (ticker in company_info and _has_profile(ticker, company_info, details_ttl)):
            continue  # missing or just (re)built above — financials already current
        if PolygonClient._cache_fresh(FINANCIALS_CACHE / f"{ticker}.json", financials_ttl):
            continue  # financials still fresh
        company_info[ticker]["financials"] = _financials_summary(poly.financials(ticker))
        fin_refreshed += 1

    save_json(COMPANY_INFO_PATH, company_info)
    log.info("Enriched %d new/stale profiles, refreshed financials for %d; company_info has %d tickers",
             new_enriched, fin_refreshed, len(company_info))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=None, help="override max new tickers this run")
    ap.add_argument("--focus", choices=["all", "outperformers"], default="all",
                    help="'outperformers' restricts API work to out-performer companies (standard pipeline)")
    args = ap.parse_args()
    run(args.max, focus=args.focus)


if __name__ == "__main__":
    main()
