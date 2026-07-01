from __future__ import annotations

"""
Turn performance.json into the rankings that drive the report.

Member leaderboard + the out-performer set: rankable members in the top quartile
by cumulative 2-year dollar-weighted alpha, with alpha > 0.

Two stock views, both on a "person-percent" basis (each member weighted by their
own percentage performance, never by dollar size):

  drivers      Which stocks drove the out-performers' out-performance.
               score = Σ over out-performers of (stock $ / member total $) ×
                       (stock return − S&P over the same window)
               i.e. each stock's signed contribution, in person-alpha-points.
               (A member's contributions sum to their total alpha, so a +300%
               member counts ~30× a +10% member. No agreement floor.)

  recent_buys  What out-performers are buying now (purchases disclosed in the
               last N days), ranked by Σ (stock $ / member total $) — the share
               of proven portfolios flowing into the name (return not yet known).

Usage:
  python src/score_and_rank.py
"""

import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from taxonomy import cap_bucket, sic_to_industry
from utils import DATA_DIR, load_config, load_json, parse_date, save_json, setup_logging

log = setup_logging("score_and_rank")

LEDGER_PATH = DATA_DIR / "transactions.json"
PERFORMANCE_PATH = DATA_DIR / "performance.json"
RANKINGS_PATH = DATA_DIR / "rankings.json"


def run() -> None:
    cfg = load_config()
    scfg = cfg["scoring"]
    rcfg = cfg["report"]
    window_days = rcfg.get("view_b_window_days", 30)

    if not PERFORMANCE_PATH.exists():
        log.error("No performance.json; run compute_performance.py first")
        return
    perf = load_json(PERFORMANCE_PATH)
    members = perf["members"]
    positions = perf["positions"]
    ledger = list(load_json(LEDGER_PATH).values()) if LEDGER_PATH.exists() else []
    info = load_json(DATA_DIR / "company_info.json") if (DATA_DIR / "company_info.json").exists() else {}

    rankable = [m for m in members.values() if m["rankable"]]
    # Sort by dollar-weighted alpha (market-adjusted) so out-performers rise to the
    # top; members with no computable alpha fall to the bottom.
    leaderboard = sorted(
        rankable,
        key=lambda m: (m["dw_alpha"] is not None, m["dw_alpha"] if m["dw_alpha"] is not None else 0),
        reverse=True,
    )

    # Out-performer set: top quartile by cumulative alpha AND alpha > 0.
    with_alpha = sorted([m for m in rankable if m["dw_alpha"] is not None],
                        key=lambda m: m["dw_alpha"], reverse=True)
    cut = max(1, math.ceil(len(with_alpha) * scfg["outperformer_top_quantile"]))
    outperformers = {m["member_id"] for m in with_alpha[:cut]
                     if m["dw_alpha"] > scfg["outperformer_min_alpha"]}

    def classify(ticker):
        ci = info.get(ticker) or {}
        return sic_to_industry(ci.get("sic_code")), cap_bucket(ci.get("market_cap"))

    def name_of(ticker):
        return (info.get(ticker) or {}).get("name")

    # --- View A: stocks that drove out-performance ------------------------ #
    # contribution(member, stock) = Σ_position weight·alpha / member total $
    drivers_acc: dict[str, dict] = defaultdict(lambda: {"score": 0.0, "contributors": {}})
    for p in positions:
        mid = p["member_id"]
        if mid not in outperformers or p["alpha"] is None or p["weight"] <= 0:
            continue
        wm = members[mid]["total_dollars"] or 0
        if wm <= 0:
            continue
        contrib = p["weight"] * p["alpha"] / wm           # percentage points of member's portfolio
        d = drivers_acc[p["ticker"]]
        d["score"] += contrib
        c = d["contributors"].setdefault(mid, {"member": p["member"], "member_id": mid, "contribution": 0.0})
        c["contribution"] += contrib

    drivers = []
    for ticker, d in drivers_acc.items():
        ind, cap = classify(ticker)
        contributors = sorted(d["contributors"].values(), key=lambda c: c["contribution"], reverse=True)
        for c in contributors:
            c["contribution"] = round(c["contribution"], 1)
        drivers.append({
            "ticker": ticker, "name": name_of(ticker), "industry": ind, "cap": cap,
            "score": round(d["score"], 1), "n_outperformers": len(contributors),
            "top_contributor": contributors[0]["member"] if contributors else None,
            "contributors": contributors[:10],
        })
    drivers.sort(key=lambda s: s["score"], reverse=True)

    # --- View B: what out-performers are buying now ----------------------- #
    ref_date = parse_date(perf["generated"]) or date.today()
    cutoff_b = ref_date - timedelta(days=window_days)
    recent_acc: dict[str, dict] = defaultdict(lambda: {"score": 0.0, "buyers": {}})
    for p in positions:
        mid = p["member_id"]
        if mid not in outperformers or p["weight"] <= 0:
            continue
        ed = parse_date(p["entry_date"])                  # entry_date == purchase disclosure date
        if not ed or ed < cutoff_b:
            continue
        wm = members[mid]["total_dollars"] or 0
        if wm <= 0:
            continue
        alloc = p["weight"] / wm
        r = recent_acc[p["ticker"]]
        r["score"] += alloc
        b = r["buyers"].setdefault(mid, {"member": p["member"], "member_id": mid,
                                         "alloc": 0.0, "dollars": 0.0, "latest": p["entry_date"]})
        b["alloc"] += alloc
        b["dollars"] += p["weight"]
        b["latest"] = max(b["latest"], p["entry_date"])

    recent_buys = []
    for ticker, r in recent_acc.items():
        ind, cap = classify(ticker)
        buyers = sorted(r["buyers"].values(), key=lambda b: b["alloc"], reverse=True)
        for b in buyers:
            b["alloc_pct"] = round(b["alloc"] * 100, 1)
            b["dollars"] = round(b["dollars"])
        recent_buys.append({
            "ticker": ticker, "name": name_of(ticker), "industry": ind, "cap": cap,
            "score_pct": round(r["score"] * 100, 1), "n_outperformers": len(buyers),
            "latest": max(b["latest"] for b in buyers),
            "buyers": buyers[:10],
        })
    recent_buys.sort(key=lambda s: s["score_pct"], reverse=True)

    # --- Per-stock rollup (still used by the stock detail pages) ---------- #
    stocks: dict[str, dict] = {}
    buys_by_ticker = defaultdict(list)
    for r in ledger:
        if r["tx_type"] == "P":
            buys_by_ticker[r["ticker"]].append(r)
    pair_acc = defaultdict(lambda: {"w": 0.0, "wr": 0.0})
    for p in positions:
        if p["weight"] > 0 and p["return_pct"] is not None:
            a = pair_acc[(p["member_id"], p["ticker"])]
            a["w"] += p["weight"]
            a["wr"] += p["weight"] * p["return_pct"]
    for ticker, buys in buys_by_ticker.items():
        buyers = {}
        for r in buys:
            mid = r["member_id"]
            b = buyers.setdefault(mid, {"member_id": mid, "member": r["member"], "chamber": r["chamber"],
                                        "dollars": 0.0, "is_outperformer": mid in outperformers})
            b["dollars"] += r.get("amount_mid", 0) or 0
        for mid, b in buyers.items():
            b["dollars"] = round(b["dollars"])
            a = pair_acc.get((mid, ticker))
            b["return_pct"] = round(a["wr"] / a["w"], 2) if (a and a["w"]) else None
        op_buyers = [b for b in buyers.values() if b["is_outperformer"]]
        ci = info.get(ticker) or {}
        stocks[ticker] = {
            "ticker": ticker, "name": ci.get("name"),
            "n_buyers": len(buyers), "n_outperformer_buyers": len(op_buyers),
            "total_buy_dollars": round(sum(b["dollars"] for b in buyers.values())),
            "outperformer_dollars": round(sum(b["dollars"] for b in op_buyers)),
            "buyers": sorted(buyers.values(), key=lambda b: b["dollars"], reverse=True),
            "has_detail": ci.get("has_prices", False),
        }

    save_json(RANKINGS_PATH, {
        "generated": perf["generated"],
        "benchmark": perf["benchmark"],
        "leaderboard": leaderboard,
        "outperformer_ids": sorted(outperformers),
        "n_rankable": len(rankable),
        "n_members_total": len(members),
        "view_b_window_days": window_days,
        "drivers": drivers[:rcfg.get("drivers_size", 40)],
        "recent_buys": recent_buys[:rcfg.get("recent_buys_size", 40)],
        "stocks": stocks,
    })
    log.info("Rankings: %d rankable, %d out-performers (top %.0f%% by alpha) | %d driver stocks, %d recent-buy stocks",
             len(rankable), len(outperformers), scfg["outperformer_top_quantile"] * 100,
             len(drivers), len(recent_buys))


if __name__ == "__main__":
    run()
