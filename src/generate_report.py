from __future__ import annotations

"""
Render the static site from rankings.json / performance.json / company_info.json:

  docs/reports/<date>.html   dated leaderboard + new-disclosure digest + driver/recent-buy tables
  docs/members/<id>.html     per-member positions + stats
  docs/stocks/<TICKER>.html  per-stock deep dive (chart, news, financials, buyers)
  docs/index.html            landing page + dated report archive

Usage:
  python src/generate_report.py [--date YYYY-MM-DD]
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, str(Path(__file__).parent))
from taxonomy import committee_to_industries
from utils import (DATA_DIR, ROOT, UNPARSED_PATH, load_config, load_json, parse_date,
                   save_json, setup_logging, slugify)

log = setup_logging("generate_report")

TEMPLATES_DIR = ROOT / "templates"
DOCS = ROOT / "docs"
CHARTS_DIR = DOCS / "assets" / "charts"
REPORT_INDEX = DATA_DIR / "report_index.json"
RUNS_PATH = DATA_DIR / "runs.json"
OUTPERF_HISTORY = DATA_DIR / "outperformer_history.json"
_TYPE_LABEL = {"P": "Buy", "S": "Sell", "E": "Exchange"}


def _outperformer_changes(report_date: str, current_ids: set, members: dict):
    """Snapshot the out-performer set per run and diff against the previous run.
    Returns (added, removed, since_date). The set shifts both when new trades are
    disclosed and as prices move between runs — every run recomputes returns/alpha."""
    history = load_json(OUTPERF_HISTORY) if OUTPERF_HISTORY.exists() else {}
    prior_dates = sorted(d for d in history if d < report_date)
    prev = set(history[prior_dates[-1]]) if prior_dates else None
    history[report_date] = sorted(current_ids)
    save_json(OUTPERF_HISTORY, history)
    if prev is None:
        return [], [], None             # first snapshot — nothing to diff against

    def info(mid):
        m = members.get(mid, {})
        return {"member_id": mid, "member": m.get("member", mid),
                "chamber": m.get("chamber", ""), "dw_return_pct": m.get("dw_return_pct"),
                "dw_alpha": m.get("dw_alpha")}

    key = lambda m: m["dw_alpha"] if m["dw_alpha"] is not None else -1e9
    added = sorted((info(i) for i in current_ids - prev), key=key, reverse=True)
    removed = sorted((info(i) for i in prev - current_ids), key=key, reverse=True)
    return added, removed, prior_dates[-1]


# --- display helpers (registered as Jinja globals) ------------------------- #
def money(v):
    if v is None:
        return "—"
    v = float(v)
    a = abs(v)
    if a >= 1e9:
        return f"${v/1e9:.1f}B"
    if a >= 1e6:
        return f"${v/1e6:.1f}M"
    if a >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:,.0f}"


def usd(v):
    return "—" if v is None else f"${float(v):,.2f}"


def pct(v):
    return "—" if v is None else f"{v:+.1f}%"


def cls(v):
    if v is None:
        return ""
    return "pos" if v > 0 else ("neg" if v < 0 else "")


def heatcolor(ret):
    """Background color for a heatmap cell: green for gains, red for losses."""
    if ret is None:
        return "transparent"
    mag = min(abs(ret) / 40.0, 1.0)
    alpha = 0.12 + 0.6 * mag
    return f"rgba(26,127,55,{alpha:.2f})" if ret >= 0 else f"rgba(207,34,46,{alpha:.2f})"


def _build_digest(ledger, cutoff, members, ticker_class, cmembers, member_industries):
    """Transactions with a public disclosure date on/after `cutoff`, enriched with
    context, most interesting first. Window-based (not run-based) so recent trades
    surface regardless of how often the pipeline runs or when one was first scraped."""
    digest = []
    for tx in ledger.values():
        dd = parse_date(tx.get("disclosure_date"))
        if not dd or dd < cutoff:
            continue
        mid, ticker = tx["member_id"], tx["ticker"]
        cls = ticker_class.get(ticker) or {}
        ind = cls.get("industry")
        aligned = bool(ind and ind in member_industries.get(mid, set()))
        committees = []
        if aligned:
            for c in cmembers.get(mid, {}).get("committees", []):
                if ind in committee_to_industries(c["id"], c["name"]):
                    committees.append(c["name"].split(":")[0])
        m = members.get(mid, {})
        digest.append({
            "member": tx["member"], "member_id": mid, "chamber": tx["chamber"],
            "ticker": ticker, "tx_type": tx["tx_type"], "type_label": _TYPE_LABEL.get(tx["tx_type"], tx["tx_type"]),
            "tx_date": tx.get("tx_date"), "disclosure_date": tx["disclosure_date"],
            "amount_mid": tx.get("amount_mid"), "industry": ind, "cap": cls.get("cap"),
            "aligned": aligned, "committees": sorted(set(committees)),
            "member_return": m.get("dw_return_pct"),
        })
    # Most recently disclosed first; within a day, committee-aligned and larger bets on top.
    digest.sort(key=lambda d: (d["disclosure_date"] or "", d["aligned"], d["amount_mid"] or 0),
                reverse=True)
    return digest


def _build_unparsed(report_date: str, lookback_days: int):
    """Scanned/paper filings inside the window, grouped by member (most filings first).
    These are image-only PDFs we can't machine-read — surfaced so the data gap is
    transparent and the filings can be transcribed manually or with Claude."""
    if not UNPARSED_PATH.exists():
        return [], 0
    cutoff = date.fromisoformat(report_date) - timedelta(days=lookback_days)
    rows = [u for u in load_json(UNPARSED_PATH).values()
            if (parse_date(u.get("disclosure_date")) or date.min) >= cutoff]
    by_member = defaultdict(list)
    for u in rows:
        by_member[(u["member"], u["chamber"], u.get("state", ""), u.get("member_id", ""))].append(u)
    members = []
    for (member, chamber, state, mid), items in by_member.items():
        items.sort(key=lambda x: x["disclosure_date"], reverse=True)
        members.append({"member": member, "chamber": chamber, "state": state,
                        "member_id": mid, "count": len(items),
                        "latest": items[0]["disclosure_date"], "filings": items})
    members.sort(key=lambda m: (m["count"], m["latest"]), reverse=True)
    return members, len(rows)


def _prune_stale(dir_path, keep_stems) -> int:
    """Delete *.html in dir_path whose stem isn't in keep_stems (orphans from
    merged/removed members, stocks, or industries). Does not touch the report archive."""
    if not dir_path.exists():
        return 0
    removed = 0
    for f in dir_path.glob("*.html"):
        if f.stem not in keep_stems:
            f.unlink()
            removed += 1
    return removed


def _env():
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
    env.globals.update(money=money, usd=usd, pct=pct, cls=cls, heatcolor=heatcolor, slug=slugify)
    return env


def run(today: date | None = None) -> None:
    cfg = load_config()
    report_date = (today or date.today()).isoformat()
    benchmark = cfg["pipeline"]["benchmark_ticker"]
    lookback_years = round(cfg["pipeline"]["lookback_days"] / 365)

    if not (DATA_DIR / "rankings.json").exists():
        log.error("No rankings.json; run score_and_rank.py first")
        return
    rankings = load_json(DATA_DIR / "rankings.json")
    perf = load_json(DATA_DIR / "performance.json")
    info = load_json(DATA_DIR / "company_info.json") if (DATA_DIR / "company_info.json").exists() else {}
    ledger = list(load_json(DATA_DIR / "transactions.json").values())

    members = perf["members"]
    positions = perf["positions"]
    stocks = rankings["stocks"]
    outperformer_ids = set(rankings["outperformer_ids"])

    # Optional graph + committee layer (degrade gracefully if not built yet).
    graph = load_json(DATA_DIR / "graph.json") if (DATA_DIR / "graph.json").exists() else None
    committees = load_json(DATA_DIR / "committees.json") if (DATA_DIR / "committees.json").exists() else {"members": {}}
    cmembers = committees.get("members", {})
    member_industries = {mid: set(e.get("industries", [])) for mid, e in cmembers.items()}
    ticker_class = (graph or {}).get("ticker_class", {})
    ticker_perf = (graph or {}).get("ticker_perf", {})
    momentum = load_json(DATA_DIR / "momentum.json") if (DATA_DIR / "momentum.json").exists() else {}
    benchmark_period = (graph or {}).get("benchmark_period") or perf.get("benchmark_period")
    signals_by_member = defaultdict(list)
    for r in (graph or {}).get("signal_feed", []):
        signals_by_member[r["member_id"]].append(r)

    # "What's new" digest: trades disclosed within a trailing window (config-driven),
    # so recent buys/sells surface regardless of run cadence or when first scraped.
    whats_new_days = cfg["report"].get("whats_new_window_days", 14)
    digest_cutoff = date.fromisoformat(report_date) - timedelta(days=whats_new_days)
    runs = load_json(RUNS_PATH) if RUNS_PATH.exists() else []
    ledger_by_id = load_json(DATA_DIR / "transactions.json")
    digest = _build_digest(ledger_by_id, digest_cutoff, members, ticker_class,
                           cmembers, member_industries)
    log.info("Digest: %d disclosures in the last %d days (since %s)",
             len(digest), whats_new_days, digest_cutoff.isoformat())

    # Focus view: new BUYS by out-performers, one row per member, deduped to one entry
    # per stock (amounts summed). Everything else (out-performer sells, all trades by
    # non-out-performers) goes to a compact secondary list.
    op_buys_by_member: dict = {}
    digest_other = []
    for d in digest:
        if d["tx_type"] == "P" and d["member_id"] in outperformer_ids:
            e = op_buys_by_member.setdefault(d["member_id"], {
                "member": d["member"], "member_id": d["member_id"],
                "chamber": d["chamber"], "member_return": d["member_return"], "buys": {}})
            b = e["buys"].setdefault(d["ticker"], {"ticker": d["ticker"], "amount": 0.0,
                                                   "aligned": False, "committees": set()})
            b["amount"] += d["amount_mid"] or 0
            b["aligned"] = b["aligned"] or d["aligned"]
            b["committees"].update(d["committees"])
        else:
            digest_other.append(d)
    op_buys = []
    for e in op_buys_by_member.values():
        buys = sorted(e["buys"].values(), key=lambda b: (b["aligned"], b["amount"]), reverse=True)
        for b in buys:
            b["committees"] = sorted(b["committees"])
        op_buys.append({"member": e["member"], "member_id": e["member_id"],
                        "chamber": e["chamber"], "member_return": e["member_return"], "buys": buys})
    op_buys.sort(key=lambda e: (e["member_return"] if e["member_return"] is not None else -1e9),
                 reverse=True)

    op_added, op_removed, op_changes_since = _outperformer_changes(
        report_date, outperformer_ids, members)
    if op_added or op_removed:
        log.info("Out-performer changes since %s: +%d joined, -%d dropped",
                 op_changes_since, len(op_added), len(op_removed))

    unparsed_members, unparsed_total = _build_unparsed(report_date, cfg["pipeline"]["lookback_days"])
    log.info("Unparsed filings flagged for review: %d across %d members",
             unparsed_total, len(unparsed_members))

    env = _env()

    from markupsafe import Markup, escape

    def mdots(ticker):
        m = momentum.get((ticker or "").upper())
        if not m:
            return ""
        spans = "".join(
            f'<span class="mdot {d["state"]}" title="{escape(d["label"])}: {escape(d["detail"])}"></span>'
            for d in m["dots"])
        return Markup(f'<span class="mdots" title="momentum as of {escape(m.get("as_of") or "")}">{spans}</span>')

    env.globals["mdots"] = mdots

    common = dict(root="../", benchmark=benchmark, latest_report=report_date,
                  outperformer_ids=outperformer_ids)

    # --- dated report ------------------------------------------------------ #
    tmpl = env.get_template("report.html")
    (DOCS / "reports").mkdir(parents=True, exist_ok=True)
    (DOCS / "reports" / f"{report_date}.html").write_text(tmpl.render(
        report_date=report_date, lookback_years=lookback_years,
        leaderboard=rankings["leaderboard"],
        drivers=rankings["drivers"], recent_buys=rankings["recent_buys"],
        view_b_window_days=rankings.get("view_b_window_days", 30),
        n_rankable=rankings["n_rankable"], n_positions=len(positions),
        n_stocks=len(stocks), n_outperformers=len(outperformer_ids),
        min_positions=cfg["scoring"]["min_positions_to_rank"],
        benchmark_period=benchmark_period,
        op_buys=op_buys, digest_other=digest_other, digest_window_days=whats_new_days,
        op_added=op_added, op_removed=op_removed, op_changes_since=op_changes_since,
        unparsed_members=unparsed_members, unparsed_total=unparsed_total, **common,
    ), encoding="utf-8")

    # "What's new" standalone page (docs root): out-performer trades first, then everyone else.
    digest_op = [d for d in digest if d["member_id"] in outperformer_ids]
    digest_rest = [d for d in digest if d["member_id"] not in outperformer_ids]
    (DOCS / "latest.html").write_text(env.get_template("latest.html").render(
        digest=digest, digest_op=digest_op, digest_rest=digest_rest,
        digest_window_days=whats_new_days, report_date=report_date,
        op_added=op_added, op_removed=op_removed, op_changes_since=op_changes_since,
        root="", benchmark=benchmark, latest_report=report_date,
        outperformer_ids=outperformer_ids), encoding="utf-8")

    # --- member pages (every member who appears in the ledger) ------------- #
    pos_by_member = defaultdict(list)
    for p in positions:
        if p["return_pct"] is not None:
            pos_by_member[p["member_id"]].append(p)
    ledger_meta = {}
    for r in ledger:
        ledger_meta.setdefault(r["member_id"], {"member": r["member"], "chamber": r["chamber"],
                                                "state": r.get("state", ""), "party": r.get("party", "")})

    mtmpl = env.get_template("member.html")
    (DOCS / "members").mkdir(parents=True, exist_ok=True)
    member_ids = set(ledger_meta) | set(members)
    for mid in member_ids:
        m = members.get(mid)
        if not m:  # member with no priced positions yet — stub stats
            meta = ledger_meta.get(mid, {"member": mid, "chamber": "", "state": "", "party": ""})
            m = {"member_id": mid, "member": meta["member"], "chamber": meta["chamber"],
                 "state": meta["state"], "party": meta["party"], "n_positions": 0, "n_open": 0,
                 "n_closed": 0, "total_dollars": 0, "dw_return_pct": None, "ew_return_pct": None,
                 "dw_alpha": None, "win_rate": 0, "rankable": False}
        # Newest entry first; tie-break by larger disclosed $.
        mp = sorted(pos_by_member.get(mid, []),
                    key=lambda p: (p["entry_date"], p["weight"]), reverse=True)
        ce = cmembers.get(mid, {})
        (DOCS / "members" / f"{mid}.html").write_text(
            mtmpl.render(m=m, positions=mp, committees=ce.get("committees", []),
                         member_industries=ce.get("industries", []),
                         member_signals=signals_by_member.get(mid, [])[:15], **common),
            encoding="utf-8")
    # Redirect stubs for merged member ids so historical report links still resolve.
    aliases = load_json(DATA_DIR / "member_aliases.json") if (DATA_DIR / "member_aliases.json").exists() else {}
    alias_stems = set()
    for variant, canonical in aliases.items():
        if variant in member_ids:
            continue
        if (DOCS / "members" / f"{canonical}.html").exists():
            (DOCS / "members" / f"{variant}.html").write_text(
                f'<!doctype html><meta charset="utf-8">'
                f'<meta http-equiv="refresh" content="0; url={canonical}.html">'
                f'<p>Merged member — see <a href="{canonical}.html">{canonical}</a>.</p>',
                encoding="utf-8")
            alias_stems.add(variant)
    _prune_stale(DOCS / "members", member_ids | alias_stems)

    # --- stock pages ------------------------------------------------------- #
    stmpl = env.get_template("stock.html")
    (DOCS / "stocks").mkdir(parents=True, exist_ok=True)
    for ticker, s in stocks.items():
        ci = info.get(ticker, {})
        has_chart = (CHARTS_DIR / f"{ticker}.png").exists()
        tcls = ticker_class.get(ticker)
        jur_buyers = set()
        if tcls:
            for b in s.get("buyers", []):
                if tcls["industry"] in member_industries.get(b["member_id"], set()):
                    jur_buyers.add(b["member_id"])
        (DOCS / "stocks" / f"{ticker}.html").write_text(
            stmpl.render(s=s, info=ci, has_chart=has_chart, tclass=tcls,
                         tperf=ticker_perf.get(ticker),
                         jurisdiction_buyer_ids=jur_buyers,
                         n_jurisdiction_buyers=len(jur_buyers), **common),
            encoding="utf-8")
    _prune_stale(DOCS / "stocks", set(stocks.keys()))

    # --- graph layer: skill map, network, industry pages ------------------- #
    if graph:
        top_common = dict(root="", benchmark=benchmark, latest_report=report_date,
                          outperformer_ids=outperformer_ids)
        matrix = graph["matrix"]
        industries = graph["industries_present"]
        # Heatmap rows: rankable members first, by dollar-weighted return.
        rows = sorted(matrix.items(),
                      key=lambda kv: (kv[1]["rankable"], kv[1]["dw_return"] or -1e9), reverse=True)
        (DOCS / "map.html").write_text(env.get_template("map.html").render(
            rows=rows, industries=industries, feed=graph["signal_feed"],
            coverage=graph["coverage"], benchmark_period=benchmark_period,
            **top_common), encoding="utf-8")

        (DOCS / "graph.html").write_text(env.get_template("graph.html").render(
            network=graph["network"], network_json=json.dumps(graph["network"]),
            node_detail_json=json.dumps(graph.get("node_detail", {})),
            committee_detail_json=json.dumps(graph.get("committee_detail", {})),
            benchmark_period=benchmark_period, **top_common), encoding="utf-8")

        itmpl_ind = env.get_template("industry.html")
        (DOCS / "industries").mkdir(parents=True, exist_ok=True)
        for ind, roll in graph["industry_rollups"].items():
            (DOCS / "industries" / f"{slugify(ind)}.html").write_text(
                itmpl_ind.render(industry=ind, roll=roll, root="../", benchmark=benchmark,
                                 latest_report=report_date, outperformer_ids=outperformer_ids),
                encoding="utf-8")
        _prune_stale(DOCS / "industries", {slugify(i) for i in graph["industry_rollups"]})

    # --- archive index ----------------------------------------------------- #
    index = load_json(REPORT_INDEX) if REPORT_INDEX.exists() else []
    index = [e for e in index if e["date"] != report_date]
    top = rankings["leaderboard"][0] if rankings["leaderboard"] else None
    index.append({
        "date": report_date,
        "n_rankable": rankings["n_rankable"],
        "n_positions": len(positions),
        "n_new": len(digest),
        "top_member": top["member"] if top else None,
        "top_return": top["dw_return_pct"] if top else None,
    })
    index.sort(key=lambda e: e["date"], reverse=True)
    save_json(REPORT_INDEX, index)

    # Record this run so the next run can diff against it.
    runs_set = sorted(set(runs) | {report_date})
    save_json(RUNS_PATH, runs_set)

    itmpl = env.get_template("index.html")
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "index.html").write_text(itmpl.render(
        reports=index, root="", benchmark=benchmark,
        latest_report=report_date, outperformer_ids=outperformer_ids,
    ), encoding="utf-8")

    log.info("Rendered report %s, %d member pages, %d stock pages",
             report_date, len(set(ledger_meta) | set(members)), len(stocks))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="report date (YYYY-MM-DD, default today)")
    args = ap.parse_args()
    run(date.fromisoformat(args.date) if args.date else None)


if __name__ == "__main__":
    main()
