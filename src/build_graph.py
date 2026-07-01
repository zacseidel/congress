from __future__ import annotations

"""
Build the Member–Industry–Committee graph and the composite "interestingness"
signal from already-computed data (no API calls).

Inputs : transactions.json, performance.json, committees.json, Polygon detail cache
Output : data/graph.json
  - ticker_class        ticker -> {industry, cap, market_cap, name}
  - matrix              member_id -> industry -> aggregated stats (the heatmap)
  - signal_feed         ranked "regulated bets" (one row per priced purchase)
  - industry_rollups    industry -> top members, most-bought stocks, aligned members
  - network             bounded node/edge list for the interactive graph

Usage:
  python src/build_graph.py
"""

import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from taxonomy import INDUSTRIES, SMALL_CAPS, cap_bucket, committee_to_industries, sic_to_industry
from utils import DATA_DIR, POLYGON_CACHE, load_config, load_json, save_json, setup_logging

log = setup_logging("build_graph")

OUT_PATH = DATA_DIR / "graph.json"

DEFAULT_SIGNAL = {
    "cap_multiplier": {"micro": 3.0, "small": 2.5, "mid": 1.5, "large": 1.1, "mega": 1.0, "unknown": 1.0},
    "committee_boost": 3.0,
    "consensus_step": 0.5,
}


def classify_tickers(tickers: set[str], cap_bounds: dict | None) -> dict:
    """Read sic_code/market_cap from the Polygon detail cache for each ticker."""
    out = {}
    for t in tickers:
        cache = POLYGON_CACHE / f"{t}.json"
        if not cache.exists():
            continue
        d = load_json(cache)
        sic, mc = d.get("sic_code"), d.get("market_cap")
        out[t] = {"industry": sic_to_industry(sic), "cap": cap_bucket(mc, cap_bounds),
                  "market_cap": mc, "name": d.get("name") or t}
    return out


def run() -> None:
    cfg = load_config()
    gcfg = cfg.get("graph", {}) or {}
    scfg = {**DEFAULT_SIGNAL, **(gcfg.get("signal") or {})}
    cap_mult = {**DEFAULT_SIGNAL["cap_multiplier"], **(scfg.get("cap_multiplier") or {})}
    cap_bounds = gcfg.get("cap_buckets")
    max_net_members = gcfg.get("network", {}).get("max_members", 25)
    edges_per_member = gcfg.get("network", {}).get("edges_per_member", 3)
    feed_size = gcfg.get("feed_size", 150)

    perf = load_json(DATA_DIR / "performance.json")
    positions = perf["positions"]
    members = perf["members"]
    committees = load_json(DATA_DIR / "committees.json") if (DATA_DIR / "committees.json").exists() else {"members": {}, "committee_ref": {}}
    cmembers = committees.get("members", {})
    rankings = load_json(DATA_DIR / "rankings.json") if (DATA_DIR / "rankings.json").exists() else {}
    outperformer_ids = set(rankings.get("outperformer_ids", []))

    tickers = {p["ticker"] for p in positions}
    ticker_class = classify_tickers(tickers, cap_bounds)

    # Consensus: how many distinct members bought each ticker (purchases = positions).
    buyers_by_ticker = defaultdict(set)
    for p in positions:
        buyers_by_ticker[p["ticker"]].add(p["member_id"])

    # Member jurisdiction industries (set) for alignment checks.
    member_industries = {mid: set(e.get("industries", [])) for mid, e in cmembers.items()}

    # --- Member × Industry matrix ---------------------------------------- #
    matrix: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(
        lambda: {"w": 0.0, "wr": 0.0, "wa": 0.0, "n": 0, "wins": 0,
                 "smallcap_dollars": 0.0, "largecap_dollars": 0.0}))
    signal_rows = []

    for p in positions:
        if p["return_pct"] is None or p["weight"] <= 0:
            continue
        cls = ticker_class.get(p["ticker"])
        if not cls:
            continue
        ind, cap = cls["industry"], cls["cap"]
        mid = p["member_id"]
        w = p["weight"]
        cell = matrix[mid][ind]
        cell["w"] += w
        cell["wr"] += w * p["return_pct"]
        if p["alpha"] is not None:
            cell["wa"] += w * p["alpha"]
        cell["n"] += 1
        cell["wins"] += 1 if p["return_pct"] > 0 else 0
        if cap in SMALL_CAPS:
            cell["smallcap_dollars"] += w
        else:
            cell["largecap_dollars"] += w

        # Signal feed row (one per priced purchase position).
        aligned = ind in member_industries.get(mid, set())
        alpha = p["alpha"] if p["alpha"] is not None else p["return_pct"]
        perf_comp = max(alpha, 0.0)
        dollar_factor = 1.0 + math.log10(max(w, 1.0)) / 6.0
        cmult = cap_mult.get(cap, 1.0)
        boost = scfg["committee_boost"] if aligned else 1.0
        n_consensus = len(buyers_by_ticker[p["ticker"]])
        consensus = 1.0 + scfg["consensus_step"] * (n_consensus - 1)
        signal = perf_comp * dollar_factor * cmult * boost * consensus
        jur_committees = []
        if aligned:
            for c in cmembers.get(mid, {}).get("committees", []):
                if ind in committee_to_industries(c["id"], c["name"]):
                    jur_committees.append(c["name"].split(":")[0])
        signal_rows.append({
            "member_id": mid, "member": p["member"], "chamber": p["chamber"],
            "ticker": p["ticker"], "name": cls["name"], "industry": ind, "cap": cap,
            "return_pct": p["return_pct"], "alpha": p["alpha"], "weight": round(w),
            "aligned": aligned, "n_consensus": n_consensus,
            "committees": sorted(set(jur_committees)),
            "signal": round(signal, 2),
        })

    # Finalize matrix cells.
    matrix_out: dict[str, dict] = {}
    for mid, inds in matrix.items():
        m = members.get(mid, {})
        row = {}
        for ind, c in inds.items():
            if c["w"] <= 0:
                continue
            row[ind] = {
                "dw_return": round(c["wr"] / c["w"], 1),
                "dw_alpha": round(c["wa"] / c["w"], 1) if c["w"] else None,
                "dollars": round(c["w"]),
                "n": c["n"],
                "win_rate": round(100 * c["wins"] / c["n"], 0),
                "smallcap_dollars": round(c["smallcap_dollars"]),
                "largecap_dollars": round(c["largecap_dollars"]),
                "aligned": ind in member_industries.get(mid, set()),
            }
        if row:
            matrix_out[mid] = {
                "member": m.get("member") or cmembers.get(mid, {}).get("matched_name") or mid,
                "chamber": m.get("chamber", ""),
                "dw_return": m.get("dw_return_pct"),
                "rankable": m.get("rankable", False),
                "n_committees": len(cmembers.get(mid, {}).get("committees", [])),
                "cells": row,
            }

    signal_rows.sort(key=lambda r: r["signal"], reverse=True)

    # --- Industry rollups ------------------------------------------------- #
    industry_rollups = {}
    industries_present = sorted({c["industry"] for c in ticker_class.values()})
    # Roll up every industry in the taxonomy so jurisdiction links always resolve
    # (industries with no classified trades render as empty "no trades yet" pages).
    for ind in INDUSTRIES:
        mem_rows = []
        for mid, mdata in matrix_out.items():
            if ind in mdata["cells"]:
                cell = mdata["cells"][ind]
                mem_rows.append({"member_id": mid, "member": mdata["member"],
                                 "chamber": mdata["chamber"], "dw_return": cell["dw_return"],
                                 "dollars": cell["dollars"], "n": cell["n"],
                                 "aligned": cell["aligned"]})
        mem_rows.sort(key=lambda x: x["dw_return"], reverse=True)
        # Most-bought stocks in this industry.
        stock_acc = defaultdict(lambda: {"dollars": 0.0, "buyers": set(), "cap": None, "name": None})
        for p in positions:
            cls = ticker_class.get(p["ticker"])
            if not cls or cls["industry"] != ind or p["weight"] <= 0:
                continue
            s = stock_acc[p["ticker"]]
            s["dollars"] += p["weight"]
            s["buyers"].add(p["member_id"])
            s["cap"] = cls["cap"]
            s["name"] = cls["name"]
        stocks = [{"ticker": t, "name": s["name"], "cap": s["cap"],
                   "dollars": round(s["dollars"]), "n_buyers": len(s["buyers"])}
                  for t, s in stock_acc.items()]
        stocks.sort(key=lambda x: x["dollars"], reverse=True)
        industry_rollups[ind] = {
            "members": mem_rows,
            "stocks": stocks,
            "n_aligned_members": sum(1 for m in mem_rows if m["aligned"]),
        }

    # --- Network (bipartite) + per-node drill-down + committee sidebar ---- #
    network = _build_network(matrix_out, outperformer_ids, max_net_members, edges_per_member)
    node_detail = _node_details(network, positions, ticker_class, cmembers)
    committee_detail = _committee_details(network, matrix_out, positions, ticker_class,
                                          cmembers, committees.get("committee_ref", {}),
                                          outperformer_ids)
    ticker_perf = ticker_perf_table(positions, ticker_class)

    coverage = {
        "n_positions": len(positions),
        "n_positions_classified": sum(1 for p in positions if p["ticker"] in ticker_class),
        "n_tickers": len(tickers),
        "n_tickers_classified": len(ticker_class),
    }

    save_json(OUT_PATH, {
        "generated": perf["generated"],
        "benchmark_period": perf.get("benchmark_period"),
        "coverage": coverage,
        "ticker_class": ticker_class,
        "ticker_perf": ticker_perf,
        "matrix": matrix_out,
        "industries_present": industries_present,
        "signal_feed": signal_rows[:feed_size],
        "industry_rollups": industry_rollups,
        "network": network,
        "node_detail": node_detail,
        "committee_detail": committee_detail,
    })
    log.info("Graph: %d/%d positions classified, %d members in matrix, %d signal rows, %d industries",
             coverage["n_positions_classified"], coverage["n_positions"],
             len(matrix_out), len(signal_rows), len(industries_present))


def _build_network(matrix_out: dict, outperformer_ids: set, max_members: int,
                   edges_per_member: int) -> dict:
    """Bipartite members ↔ industries. Members are dual-encoded: size = total $,
    fill = return, gold ring = out-performer (rendered in the template)."""
    def member_dollars(md):
        return sum(c["dollars"] for c in md["cells"].values())

    # Show the top performers, but always include the biggest traders too.
    by_return = sorted(matrix_out.items(),
                       key=lambda kv: (kv[1]["rankable"], kv[1]["dw_return"] or -1e9), reverse=True)
    chosen = [mid for mid, _ in by_return[:max_members]]
    by_dollars = sorted(matrix_out.items(), key=lambda kv: member_dollars(kv[1]), reverse=True)
    for mid, _ in by_dollars[:8]:
        if mid not in chosen:
            chosen.append(mid)

    nodes, edges = [], []
    industries_used = set()
    for mid in chosen:
        md = matrix_out[mid]
        nodes.append({
            "id": f"m:{mid}", "label": md["member"], "group": "member",
            "value": round(member_dollars(md)),          # drives node size
            "ret": md["dw_return"],                        # drives fill color
            "alpha": md.get("dw_return"),                  # (kept for tooltip)
            "outperformer": mid in outperformer_ids,       # drives gold ring
        })
        # Each member links only to their top industries by dollars.
        top = sorted(md["cells"].items(), key=lambda kv: kv[1]["dollars"], reverse=True)
        for ind, cell in top[:edges_per_member]:
            industries_used.add(ind)
            edges.append({"from": f"m:{mid}", "to": f"i:{ind}",
                          "value": max(1.0, math.log10(max(cell["dollars"], 10))),
                          "ret": cell["dw_return"], "aligned": cell["aligned"]})

    for ind in sorted(industries_used):
        nodes.append({"id": f"i:{ind}", "label": ind, "group": "industry"})
    return {"nodes": nodes, "edges": edges}


def _new_acc():
    # w/wr = dollars & dollar·return; sw/swr = dollars & dollar·SPY (where SPY known)
    return {"w": 0.0, "wr": 0.0, "sw": 0.0, "swr": 0.0, "buyers": set()}


def _accumulate(a, w, ret, spy):
    a["w"] += w
    a["wr"] += w * ret
    if spy is not None:
        a["sw"] += w
        a["swr"] += w * spy


def _summarize(a):
    ret = round(a["wr"] / a["w"], 1) if a["w"] else None
    spy = round(a["swr"] / a["sw"], 1) if a["sw"] else None
    alpha = round(ret - spy, 1) if (ret is not None and spy is not None) else None
    return ret, spy, alpha


def ticker_perf_table(positions: list, ticker_class: dict) -> dict:
    """Global per-ticker dollar-weighted return, same-window S&P return, and alpha."""
    acc = defaultdict(_new_acc)
    for p in positions:
        if p["return_pct"] is None or p["weight"] <= 0 or p["ticker"] not in ticker_class:
            continue
        a = acc[p["ticker"]]
        _accumulate(a, p["weight"], p["return_pct"], p.get("spy_return_pct"))
        a["buyers"].add(p["member_id"])
    out = {}
    for t, a in acc.items():
        ret, spy, alpha = _summarize(a)
        out[t] = {"dw_return": ret, "dw_spy": spy, "alpha": alpha,
                  "dollars": round(a["w"]), "n_buyers": len(a["buyers"])}
    return out


def _node_details(network: dict, positions: list, ticker_class: dict, cmembers: dict,
                  per_node_limit: int = 40) -> dict:
    """For each network node, the relevant stocks with returns vs S&P + #members investing."""
    tg_acc = defaultdict(_new_acc)               # global per ticker
    mt_acc = defaultdict(_new_acc)               # per (member, ticker)
    member_tickers = defaultdict(set)
    for p in positions:
        if p["return_pct"] is None or p["weight"] <= 0 or p["ticker"] not in ticker_class:
            continue
        t, mid, w, spy = p["ticker"], p["member_id"], p["weight"], p.get("spy_return_pct")
        _accumulate(tg_acc[t], w, p["return_pct"], spy); tg_acc[t]["buyers"].add(mid)
        _accumulate(mt_acc[(mid, t)], w, p["return_pct"], spy)
        member_tickers[mid].add(t)

    def row_from(t, acc):
        c = ticker_class[t]
        ret, spy, alpha = _summarize(acc)
        return {"ticker": t, "name": c["name"], "industry": c["industry"], "cap": c["cap"],
                "return_pct": ret, "spy_return_pct": spy, "alpha": alpha,
                "dollars": round(acc["w"]), "n_buyers": len(tg_acc[t]["buyers"])}

    def node_summary(stocks):
        a = _new_acc()
        for r in stocks:
            if r["return_pct"] is None:
                continue
            a["w"] += r["dollars"]; a["wr"] += r["dollars"] * r["return_pct"]
            if r["spy_return_pct"] is not None:
                a["sw"] += r["dollars"]; a["swr"] += r["dollars"] * r["spy_return_pct"]
        ret, spy, alpha = _summarize(a)
        return {"dw_return": ret, "dw_spy": spy, "alpha": alpha}

    by_industry = defaultdict(list)
    for t in tg_acc:
        by_industry[ticker_class[t]["industry"]].append(t)

    comm_members = defaultdict(set)
    for mid, e in cmembers.items():
        for c in e.get("committees", []):
            comm_members[c["id"][:4]].add(mid)

    detail = {}
    for node in network["nodes"]:
        nid, typ, label = node["id"], node["group"], node["label"]
        stocks, n_members, committees = [], 0, []
        if typ == "member":
            mid = nid[2:]
            stocks = [row_from(t, mt_acc[(mid, t)]) for t in member_tickers.get(mid, ())]
            n_members = len({m for r in stocks for m in tg_acc[r["ticker"]]["buyers"]})
            # Parent-level committees with jurisdiction (chips for the sidebar).
            seen_c = set()
            for c in cmembers.get(mid, {}).get("committees", []):
                cid = c["id"][:4]
                if cid in seen_c:
                    continue
                inds = committee_to_industries(cid, c["name"])
                if not inds:
                    continue
                seen_c.add(cid)
                committees.append({"id": cid, "name": c["name"].split(":")[0],
                                   "industries": sorted(inds)})
        elif typ == "industry":
            ind = nid[2:]
            stocks = [row_from(t, tg_acc[t]) for t in by_industry.get(ind, [])]
            n_members = len({m for t in by_industry.get(ind, []) for m in tg_acc[t]["buyers"]})
        elif typ == "committee":
            cid = nid[2:]
            members = comm_members.get(cid, set())
            juris = committee_to_industries(cid, label)
            acc = defaultdict(_new_acc)
            for p in positions:
                if (p["member_id"] in members and p["return_pct"] is not None
                        and p["weight"] > 0 and p["ticker"] in ticker_class
                        and ticker_class[p["ticker"]]["industry"] in juris):
                    _accumulate(acc[p["ticker"]], p["weight"], p["return_pct"], p.get("spy_return_pct"))
                    acc[p["ticker"]]["buyers"].add(p["member_id"])
            for t, a in acc.items():
                c = ticker_class[t]
                ret, spy, alpha = _summarize(a)
                stocks.append({"ticker": t, "name": c["name"], "industry": c["industry"],
                               "cap": c["cap"], "return_pct": ret, "spy_return_pct": spy,
                               "alpha": alpha, "dollars": round(a["w"]), "n_buyers": len(a["buyers"])})
            n_members = len({m for a in acc.values() for m in a["buyers"]})

        stocks.sort(key=lambda r: r["dollars"], reverse=True)
        detail[nid] = {"label": label, "type": typ, "n_stocks": len(stocks),
                       "n_members": n_members, "summary": node_summary(stocks),
                       "stocks": stocks[:per_node_limit], "committees": committees}
    return detail


def _committee_details(network, matrix_out, positions, ticker_class, cmembers,
                       committee_ref, outperformer_ids, per_node_limit: int = 40) -> dict:
    """Sidebar drill-down per committee: its members (shown ones), the industries it
    regulates, and the in-jurisdiction stocks those members bought."""
    shown = {n["id"][2:] for n in network["nodes"] if n["group"] == "member"}

    # Parent-committee id -> set of member_ids on it (full committee or any subcommittee).
    comm_members = defaultdict(set)
    comm_name = {}
    for mid, e in cmembers.items():
        for c in e.get("committees", []):
            cid = c["id"][:4]
            comm_members[cid].add(mid)
            comm_name.setdefault(cid, c["name"].split(":")[0])

    # Which committees do we need? Only those a shown member sits on (with jurisdiction).
    needed = set()
    for mid in shown:
        for c in cmembers.get(mid, {}).get("committees", []):
            cid = c["id"][:4]
            if committee_to_industries(cid, c["name"]):
                needed.add(cid)

    detail = {}
    for cid in needed:
        name = comm_name.get(cid, cid)
        juris = committee_to_industries(cid, name)
        members = comm_members.get(cid, set())
        # Members on this committee (rank by dollar-weighted return; flag shown ones).
        mrows = []
        for mid in members:
            md = matrix_out.get(mid)
            if not md:
                continue
            mrows.append({"member_id": mid, "member": md["member"],
                          "dw_return": md["dw_return"],
                          "dollars": round(sum(c["dollars"] for c in md["cells"].values())),
                          "outperformer": mid in outperformer_ids,
                          "shown": mid in shown})
        mrows.sort(key=lambda r: (r["dw_return"] if r["dw_return"] is not None else -1e9),
                   reverse=True)
        # In-jurisdiction stocks bought by this committee's members.
        acc = defaultdict(_new_acc)
        for p in positions:
            if (p["member_id"] in members and p["return_pct"] is not None and p["weight"] > 0
                    and p["ticker"] in ticker_class
                    and ticker_class[p["ticker"]]["industry"] in juris):
                _accumulate(acc[p["ticker"]], p["weight"], p["return_pct"], p.get("spy_return_pct"))
                acc[p["ticker"]]["buyers"].add(p["member_id"])
        stocks = []
        for t, a in acc.items():
            c = ticker_class[t]
            ret, spy, alpha = _summarize(a)
            stocks.append({"ticker": t, "name": c["name"], "industry": c["industry"], "cap": c["cap"],
                           "return_pct": ret, "spy_return_pct": spy, "alpha": alpha,
                           "dollars": round(a["w"]), "n_buyers": len(a["buyers"])})
        stocks.sort(key=lambda r: r["dollars"], reverse=True)
        detail[cid] = {"name": name, "industries": sorted(juris),
                       "members": mrows[:per_node_limit], "stocks": stocks[:per_node_limit]}
    return detail


if __name__ == "__main__":
    run()
