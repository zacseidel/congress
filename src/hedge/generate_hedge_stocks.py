from __future__ import annotations

"""
Per-stock HEDGE DATA for the unified stock pages.

Stock pages are rendered once, by the congress side (generate_report.py -> docs/stocks/
{ticker}.html), so a single page shows whichever is relevant: congressional trades and/or
the top hedge funds. This module produces the hedge half of that:

  stock_holders.json.gz : {ticker: {issuer, alpha_share, holders[], buyers[]}} for every
                          ticker held by a ranked fund — the "top hedge funds holding /
                          buying" section, looked up per ticker by generate_report.
  stock_pages.json      : the FEATURED hedge tickers (top alpha contributors + top
                          skill-weighted new buys) that should get a stock page even if
                          Congress never traded them (e.g. TXG, SMH). Congress renders the
                          union of its own traded tickers + this list.

Runs after backtest/diff, before the congress render. Computes the ranked-fund set itself
(same gates as rank_funds), so it doesn't depend on rankings.json.

Usage:
  python src/hedge/generate_hedge_stocks.py
"""

import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils import DATA_DIR, load_config, load_json, load_json_gz, save_json, save_json_gz, setup_logging
from resolve_cusip import cached as cusip_cached
from holdings_io import load_holdings

log = setup_logging("generate_hedge_stocks")

HEDGE_DIR = DATA_DIR / "hedge"
STOCK_HOLDERS_PATH = HEDGE_DIR / "stock_holders.json.gz"   # per-ticker hedge data (all held tickers)
STOCK_PAGES_PATH = HEDGE_DIR / "stock_pages.json"          # featured tickers to render even if congress-untraded
TOP_HOLDERS = 15
TOP_BUYERS = 15


def _ticker(cusip):
    rec = cusip_cached(cusip)
    return rec.get("ticker") if rec else None


def run() -> None:
    hcfg = load_config().get("hedge", {})
    min_f, min_cov, min_hit = hcfg.get("min_filings", 4), hcfg.get("min_coverage", 0.90), hcfg.get("min_hit_rate", 0.50)
    n_drivers = hcfg.get("stock_pages_drivers", 120)
    n_buys = hcfg.get("stock_pages_buys", 120)

    perf_path = HEDGE_DIR / "fund_performance.json.gz"
    if not perf_path.exists():
        log.error("No fund_performance; run backtest_13f first")
        return
    perf = load_json_gz(perf_path)
    board_size = hcfg.get("leaderboard_size", 500)
    passing = []
    for cik, r in perf.items():
        if (r["n_periods"] >= min_f - 1 and r["coverage"] >= min_cov
                and r.get("hit_rate") is not None and r["hit_rate"] > min_hit):
            passing.append((int(cik), {"alpha": r["alpha"], "hit": r.get("hit_rate") or 0,
                                       "book": r.get("latest_book") or 0, "name": r["name"]}))
    # Only the top-`leaderboard_size` ranked funds get fund pages, so only link those.
    passing.sort(key=lambda kv: kv[1]["alpha"], reverse=True)
    ranked = dict(passing[:board_size])

    attr = load_json(HEDGE_DIR / "alpha_attribution.json") if (HEDGE_DIR / "alpha_attribution.json").exists() else {"stocks": []}
    share_by_t = {s["ticker"]: s.get("share", 0) for s in attr.get("stocks", [])}
    changes = load_json_gz(HEDGE_DIR / "changes.json.gz").get("funds", {}) if (HEDGE_DIR / "changes.json.gz").exists() else {}

    # holders: ranked funds' latest-filing positions, per ticker
    holdings = load_holdings(HEDGE_DIR / "holdings.json.gz")
    latest_date: dict = {}
    for h in holdings.values():
        latest_date[h["cik"]] = max(latest_date.get(h["cik"], ""), h["filing_date"])
    holders: dict = defaultdict(list)
    issuers: dict = {}
    for h in holdings.values():
        cik = h["cik"]
        rc = ranked.get(cik)
        if not rc or h.get("put_call") or h["filing_date"] != latest_date[cik]:
            continue
        t = _ticker(h["cusip"])
        if not t:
            continue
        issuers.setdefault(t, h["issuer"])
        holders[t].append({"name": rc["name"], "cik": cik, "alpha": rc["alpha"],
                           "value": round(h["value"], 0), "conv": (h["value"] / rc["book"]) if rc["book"] else 0})

    # buyers: ranked funds' new positions this quarter, per ticker
    buyers: dict = defaultdict(list)
    buy_score: dict = defaultdict(float)
    for cik, d in changes.items():
        rc = ranked.get(int(cik))
        if not rc or rc["book"] <= 0:
            continue
        skill = max(rc["alpha"], 0) * rc["hit"]
        for row in d.get("new", []):
            t = row.get("ticker")
            if not t:
                continue
            v = row.get("value", 0) or 0
            conv = v / rc["book"]
            issuers.setdefault(t, row.get("issuer", ""))
            buyers[t].append({"name": rc["name"], "cik": int(cik), "alpha": rc["alpha"],
                              "value": round(v, 0), "conv": conv})
            buy_score[t] += skill * conv

    tickers = set(holders) | set(buyers)
    out = {}
    for t in tickers:
        hs = sorted(holders.get(t, []), key=lambda x: x["value"], reverse=True)[:TOP_HOLDERS]
        bs = sorted(buyers.get(t, []), key=lambda x: x["conv"], reverse=True)[:TOP_BUYERS]
        out[t] = {"issuer": issuers.get(t, ""), "alpha_share": round(share_by_t.get(t, 0), 5),
                  "n_holders": len(holders.get(t, [])), "n_buyers": len(buyers.get(t, [])),
                  "holders": hs, "buyers": bs}
    save_json_gz(STOCK_HOLDERS_PATH, out)

    # featured: top-N alpha contributors + top-N skill-weighted new buys (standalone pages
    # even if Congress never traded them).
    featured = {s["ticker"] for s in attr.get("stocks", [])[:n_drivers]}
    featured |= set(sorted(buy_score, key=buy_score.get, reverse=True)[:n_buys])
    featured &= tickers    # only tickers we actually have hedge data for
    save_json(STOCK_PAGES_PATH, {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                                 "tickers": sorted(featured)})
    log.info("Hedge stock data: %d tickers held/bought by ranked funds; %d featured -> %s",
             len(out), len(featured), STOCK_HOLDERS_PATH.name)


if __name__ == "__main__":
    run()
