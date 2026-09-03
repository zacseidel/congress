from __future__ import annotations

"""
Top-level "Smart Money" dashboard — the combined home page (docs/index.html) above the
congressional-trades tracker and the 13F Hedge report.

It ranks stocks by their SHARE OF TOTAL ALPHA, not by how many people hold them:
  * Hedge total alpha  = sum of alpha across every ranked fund (passed the skill gates).
    A stock's share    = sum of its per-fund alpha contributions / total alpha.
  * Congress total alpha = sum of alpha across the out-performing members. A stock's
    share = sum of (position weight x position alpha) over out-performers / total.
Both sides' per-stock shares ~sum to 100%, so this is a true "where did the edge come
from" decomposition. Alpha convergence = stocks that drove alpha on BOTH sides.

A second convergence joins the two "buying now" feeds: new 13F positions from the
top ranked hedge funds (not pinned specialists) and recent purchases by congressional
out-performers. Windows/cuts live in config `dashboard`.

Reads data/hedge/alpha_attribution.json (from backtest_13f) + data/performance.json +
data/rankings.json (congress) + data/hedge/rankings.json + data/hedge/changes.json.gz.
Congress's own landing lives at congress.html.

Usage:
  python src/generate_dashboard.py
"""

import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import DATA_DIR, load_config, load_json, load_json_gz, parse_date, setup_logging

log = setup_logging("generate_dashboard")

DOCS = Path(__file__).parent.parent / "docs"

TEMPLATE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Smart Money Dashboard</title>
<style>
 :root{--bg:#f6f8fa;--card:#fff;--ink:#1f2328;--muted:#57606a;--line:#d0d7de;--green:#1a7f37;--red:#cf222e;--blue:#0969da;--accent:#0a3069;}
 *{box-sizing:border-box;} body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;}
 a{color:var(--blue);text-decoration:none;} a:hover{text-decoration:underline;}
 .wrap{max-width:1080px;margin:0 auto;padding:24px 20px 60px;}
 header.site{border-bottom:1px solid var(--line);background:var(--card);} header.site .wrap{padding:18px 20px;}
 header.site h1{font-size:20px;margin:0;} .sub{color:var(--muted);font-size:14px;margin-top:3px;}
 nav.top{margin-top:8px;} nav.top a{margin-right:18px;font-size:14px;font-weight:600;} nav.top a.here{color:var(--ink);}
 h2{font-size:19px;margin:30px 0 6px;} .muted{color:var(--muted);} .small{font-size:13px;}
 table{width:100%;border-collapse:collapse;font-size:14px;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden;margin:10px 0;}
 th,td{padding:8px 11px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap;} tr:last-child td{border-bottom:none;}
 th{background:#f0f3f6;font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted);}
 td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;} tbody tr:hover{background:#f6f8fa;}
 .conv{border:2px solid #1a7f37;} .badge{display:inline-block;font-size:11px;font-weight:600;padding:1px 7px;border-radius:10px;background:#dafbe1;color:var(--green);}
 .pos{color:var(--green);}
 .cols{display:flex;gap:20px;flex-wrap:wrap;} .cols>div{flex:1 1 340px;}
 .cards{display:flex;gap:16px;flex-wrap:wrap;margin:12px 0;}
 .cardlink{flex:1 1 300px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;}
 .cardlink h3{margin:0 0 6px;font-size:16px;} .cardlink a{margin-right:12px;font-size:14px;}
 .lead{background:#f0f7ff;border:1px solid #cfe4ff;border-radius:8px;padding:12px 16px;font-size:14px;margin:12px 0;}
 .bar{display:inline-block;height:9px;background:#1a7f37;border-radius:2px;vertical-align:middle;margin-left:6px;}
 footer{color:var(--muted);font-size:12px;margin-top:40px;border-top:1px solid var(--line);padding-top:14px;}
</style></head><body>
<header class="site"><div class="wrap">
  <h1>💡 Smart Money Dashboard</h1>
  <div class="sub">Ranked by share of <em>total alpha produced</em> — where Congress and top hedge funds actually made their edge. Updated {{ generated }}{% if hedge_wave %} · latest 13F filing wave <b>{{ hedge_wave }}</b>{% endif %}</div>
  <nav class="top"><a class="here" href="index.html">💡 Dashboard</a><a href="congress.html">🏛️ Congress Trades</a><a href="hedge/index.html">📈 Hedge Fund 13Fs</a><a href="decisions.html">🎯 Decisions</a></nav>
</div></header>
<div class="wrap">

<h2>🎯 Alpha convergence <span class="muted small">stocks that drove alpha for BOTH Congress out-performers and top hedge funds</span></h2>
{% if convergence %}
<div class="lead">These {{ convergence|length }} names produced a meaningful share of the excess return on
  <em>both</em> sides — the strongest signal in the dataset. Share = % of each side's total alpha.</div>
<table class="conv"><thead><tr>
  <th>Ticker</th><th class="num">Hedge α-share</th><th class="num">Congress α-share</th>
  <th>Top fund (its alpha)</th><th>Top member</th></tr></thead><tbody>
{% for r in convergence %}<tr>
  <td>{{ clink(r.ticker) }} <span class="badge">both</span></td>
  <td class="num pos">{{ '%.1f'|format(100*r.hedge_share) }}%</td>
  <td class="num pos">{{ '%.1f'|format(100*r.congress_share) }}%</td>
  <td class="small">{{ flink(r.top_fund_cik, r.top_fund_name) }}</td>
  <td class="small">{{ r.top_member }}</td>
</tr>{% endfor %}
</tbody></table>
{% else %}<p class="muted small">No overlap yet — refresh both reports to populate.</p>{% endif %}

<h2>🛒 Buying-now convergence <span class="muted small">new positions on BOTH sides — top {{ top_funds_n }} ranked hedge funds this 13F wave, Congress out-performers in the last {{ congress_window_days }} days</span></h2>
{% if buying_now %}
<div class="lead">These {{ buying_now|length }} names are being bought by out-performers on both sides —
  where the smart money is <em>going</em>, to complement the backward-looking alpha view above.
  Ranked the same way as alpha convergence: each name&rsquo;s share of that side&rsquo;s total
  buying-now score, then added. Hedge score = skill-weighted new-buy conviction
  (alpha × consistency × position/book) among the top ranked funds; Congress score = share of
  out-performer portfolios flowing into the name. Pinned specialists are excluded.</div>
<table class="conv"><thead><tr>
  <th>Ticker</th><th class="num">Hedge buy-share</th><th class="num">Congress buy-share</th>
  <th>Top fund</th><th>Top member</th></tr></thead><tbody>
{% for r in buying_now %}<tr>
  <td>{{ clink(r.ticker) }} <span class="badge">both</span></td>
  <td class="num pos" title="{{ r.hedge_n }} ranked fund{{ '' if r.hedge_n == 1 else 's' }} newly buying">{{ '%.1f'|format(100*r.hedge_share) }}%</td>
  <td class="num pos" title="{{ r.congress_n }} out-performer{{ '' if r.congress_n == 1 else 's' }} buying">{{ '%.1f'|format(100*r.congress_share) }}%</td>
  <td class="small">{{ flink(r.top_fund_cik, r.top_fund_name) }}</td>
  <td class="small">{{ mlink(r.top_member_id, r.top_member) }}</td>
</tr>{% endfor %}
</tbody></table>
{% else %}<p class="muted small">No overlapping new buys this window — refresh both reports to populate.</p>{% endif %}

<div class="cols">
<div><h2>📈 Where hedge-fund alpha came from <span class="muted small">{{ n_hedge_funds }} ranked funds</span></h2>
<table><thead><tr><th>Ticker</th><th class="num">α-share</th><th>Top fund</th></tr></thead><tbody>
{% for r in hedge_top %}<tr><td>{{ clink(r.ticker) }}</td>
  <td class="num">{{ '%.1f'|format(100*r.share) }}%</td>
  <td class="small">{{ flink(r.top_fund_cik, r.top_fund_name) }}</td></tr>{% endfor %}
</tbody></table></div>
<div><h2>🏛️ Where Congress alpha came from <span class="muted small">out-performers</span></h2>
<table><thead><tr><th>Ticker</th><th class="num">α-share</th><th>Top member</th></tr></thead><tbody>
{% for r in congress_top %}<tr><td>{{ clink(r.ticker) }}</td>
  <td class="num">{{ '%.1f'|format(100*r.share) }}%</td><td class="small">{{ r.top_member }}</td></tr>{% endfor %}
</tbody></table></div>
</div>

<h2>Explore the reports</h2>
<div class="cards">
  <div class="cardlink"><h3>🏛️ Congressional Trades</h3>
    <div class="small muted">Members ranked by buy-the-disclosure alpha vs SPY</div>
    <div style="margin-top:8px"><a href="congress.html">Leaderboard &amp; archive</a><a href="graph.html">Network</a><a href="map.html">Skill map</a></div></div>
  <div class="cardlink"><h3>📈 Hedge Fund 13Fs</h3>
    <div class="small muted">{{ n_hedge_funds }} funds ranked · mirror-portfolio alpha vs SPY</div>
    <div style="margin-top:8px"><a href="hedge/index.html">Leaderboard</a><a href="hedge/index.html">Top funds &amp; drivers</a></div></div>
  <div class="cardlink"><h3>🎯 Decisions</h3>
    <div class="small muted">My own open &amp; closed positions, priced vs SPY</div>
    <div style="margin-top:8px"><a href="decisions.html">Position tracker</a></div></div>
</div>

<footer>Alpha attribution: each stock's summed contribution to the total alpha of the ranked funds /
  out-performing members. Buying now: new 13F positions of the top ranked funds that congressional
  out-performers also bought recently. Congress: House &amp; Senate disclosures · Hedge: SEC EDGAR 13F ·
  Prices: Polygon.io · benchmark SPY. Educational use only — not investment advice.</footer>
</div></body></html>
"""


def select_top_funds(leaderboard: list, top_n: int, min_alpha: float,
                     exclude_ciks) -> list:
    """Top ranked funds by alpha, dropping pinned specialists and funds below min_alpha."""
    exclude = {int(c) for c in exclude_ciks}
    selected = []
    for record in leaderboard:
        if int(record["cik"]) in exclude:
            continue
        if (record.get("alpha") or 0) < min_alpha:
            continue
        selected.append(record)
        if len(selected) >= top_n:
            break
    return selected


def hedge_buying_from_changes(funds: list, changes: dict) -> dict:
    """Skill-weighted new 13F buys for `funds` (already filtered to the ranked top-N).

    Score is the same quantity the hedge leaderboard ranks by: Σ over funds of
    max(alpha,0) × hit-rate × (new position / book). Share-of-total happens later.
    """
    alpha_by = {str(record["cik"]): record["alpha"] for record in funds}
    skill_by = {str(record["cik"]): max(record["alpha"], 0) * (record.get("hit_rate") or 0)
                for record in funds}
    book_by = {str(record["cik"]): (record.get("latest_book") or 0) for record in funds}
    buys: dict = {}
    for record in funds:
        cik = str(record["cik"])
        delta = changes.get(cik)
        if not delta:
            continue
        skill, book = skill_by.get(cik, 0), book_by.get(cik, 0)
        if skill <= 0 or book <= 0:
            continue
        for row in delta.get("new", []):
            ticker = row.get("ticker")
            if not ticker:
                continue
            conv = (row.get("value", 0) or 0) / book
            vote = skill * conv
            entry = buys.setdefault(ticker, {"score": 0.0, "n_funds": 0,
                                             "top": None, "top_vote": -1.0})
            entry["score"] += vote
            entry["n_funds"] += 1
            if vote > entry["top_vote"]:
                entry["top_vote"] = vote
                entry["top"] = (delta.get("manager"), record["cik"], alpha_by[cik], conv)
    out = {}
    for ticker, entry in buys.items():
        name, cik, alpha, conv = entry["top"]
        out[ticker] = {
            "score": entry["score"],
            "n_funds": entry["n_funds"],
            "top_name": name,
            "top_cik": cik,
            "top_alpha": alpha,
            "top_conv": conv,
        }
    return out


def congress_buying_from_positions(positions: list, members: dict,
                                   outperformer_ids: set, cutoff) -> dict:
    """Out-performer purchases disclosed on/after `cutoff`, scored like View B.

    score = Σ (position $ / member total $) across out-performers — the share of
    proven portfolios flowing into the name. Share-of-total happens later.
    """
    acc: dict = defaultdict(lambda: {"score": 0.0, "buyers": {}})
    for position in positions:
        member_id = position.get("member_id")
        if member_id not in outperformer_ids or (position.get("weight") or 0) <= 0:
            continue
        entry_date = parse_date(position.get("entry_date"))
        if not entry_date or entry_date < cutoff:
            continue
        total = (members.get(member_id) or {}).get("total_dollars") or 0
        if total <= 0:
            continue
        alloc = position["weight"] / total
        row = acc[position["ticker"]]
        row["score"] += alloc
        buyer = row["buyers"].setdefault(member_id, {
            "member": position.get("member") or member_id,
            "member_id": member_id,
            "alloc": 0.0,
        })
        buyer["alloc"] += alloc
    out = {}
    for ticker, row in acc.items():
        buyers = sorted(row["buyers"].values(), key=lambda b: b["alloc"], reverse=True)
        top = buyers[0]
        out[ticker] = {
            "score": row["score"],
            "n_outperformers": len(buyers),
            "top_member": top["member"],
            "top_member_id": top["member_id"],
        }
    return out


def buying_now_convergence(hedge_buys: dict, congress_buys: dict) -> list:
    """Names newly bought on both sides, ranked like alpha convergence.

    Each side's raw buying-now score (hedge: skill-weighted conviction; congress:
    out-performer portfolio share) is turned into a share of that side's total,
    then the two shares are added. Stocks not on both lists are dropped.
    """
    if not hedge_buys or not congress_buys:
        return []
    hedge_total = sum(row["score"] for row in hedge_buys.values())
    congress_total = sum(row["score"] for row in congress_buys.values())
    if hedge_total <= 0 or congress_total <= 0:
        return []
    rows = []
    for ticker in set(hedge_buys) & set(congress_buys):
        hedge = hedge_buys[ticker]
        congress = congress_buys[ticker]
        hedge_share = hedge["score"] / hedge_total
        congress_share = congress["score"] / congress_total
        if hedge_share <= 0 or congress_share <= 0:
            continue
        rows.append({
            "ticker": ticker,
            "hedge_share": hedge_share,
            "congress_share": congress_share,
            "hedge_n": hedge["n_funds"],
            "top_fund_cik": hedge["top_cik"],
            "top_fund_name": hedge["top_name"],
            "congress_n": congress["n_outperformers"],
            "top_member": congress["top_member"],
            "top_member_id": congress["top_member_id"],
            "combined": hedge_share + congress_share,
        })
    rows.sort(key=lambda row: row["combined"], reverse=True)
    return rows


def _hedge_alpha() -> tuple:
    """(total_alpha, {ticker: stock_dict}) from the backtest's alpha_attribution.json."""
    path = DATA_DIR / "hedge" / "alpha_attribution.json"
    if not path.exists():
        return 0.0, {}
    a = load_json(path)
    return a.get("total_alpha", 0.0), {s["ticker"]: s for s in a.get("stocks", [])}, a.get("n_funds", 0)


def _congress_alpha() -> tuple:
    """(total_alpha, {ticker: {share, contribution, n_members, top_member}}) — REUSES the
    congress tracker's own alpha attribution: rankings.json `drivers` already computes each
    stock's signed contribution to the out-performers' alpha (score = Σ weight·alpha over
    out-performers, a member's contributions summing to their alpha). We just normalize by
    the true out-performer total (Σ their dw_alpha) to get shares comparable to the hedge side."""
    rank_path = DATA_DIR / "rankings.json"
    perf_path = DATA_DIR / "performance.json"
    if not rank_path.exists() or not perf_path.exists():
        return 0.0, {}
    rank = load_json(rank_path)
    op = set(rank.get("outperformer_ids", []))
    members = load_json(perf_path).get("members", {})
    total = sum(m.get("dw_alpha", 0) or 0 for mid, m in members.items() if mid in op)
    stocks = {d["ticker"]: {"contribution": d["score"],
                            "share": d["score"] / total if total else 0,
                            "n_members": d.get("n_outperformers", 0),
                            "top_member": d.get("top_contributor", "")}
              for d in rank.get("drivers", [])}
    return total, stocks


def _load_buying_now(cfg: dict) -> tuple[list, int, int]:
    """Join ranked-fund new 13F buys with congressional out-performer purchases."""
    dcfg = cfg.get("dashboard", {})
    top_n = int(dcfg.get("top_funds_n", 200))
    min_alpha = float(dcfg.get("min_fund_alpha", 0.0))
    window_days = int(dcfg.get("congress_window_days", 120))

    hedge_rank = load_json(DATA_DIR / "hedge" / "rankings.json") \
        if (DATA_DIR / "hedge" / "rankings.json").exists() else {}
    changes = load_json_gz(DATA_DIR / "hedge" / "changes.json.gz").get("funds", {}) \
        if (DATA_DIR / "hedge" / "changes.json.gz").exists() else {}
    exclude = {s["cik"] for s in hedge_rank.get("specialists", [])}
    funds = select_top_funds(hedge_rank.get("leaderboard", []), top_n, min_alpha, exclude)
    hedge_buys = hedge_buying_from_changes(funds, changes)

    rank = load_json(DATA_DIR / "rankings.json") if (DATA_DIR / "rankings.json").exists() else {}
    perf = load_json(DATA_DIR / "performance.json") if (DATA_DIR / "performance.json").exists() else {}
    generated = parse_date(perf.get("generated")) or datetime.now(timezone.utc).date()
    cutoff = generated - timedelta(days=window_days)
    congress_buys = congress_buying_from_positions(
        perf.get("positions", []), perf.get("members", {}),
        set(rank.get("outperformer_ids", [])), cutoff)

    rows = buying_now_convergence(hedge_buys, congress_buys)
    return rows, top_n, window_days


def run() -> None:
    from jinja2 import Template
    cfg = load_config()
    h_total, hedge, n_hedge_funds = _hedge_alpha()
    c_total, congress = _congress_alpha()

    # Convergence: stocks that produced positive alpha share on BOTH sides.
    convergence = []
    for t in set(hedge) & set(congress):
        hs, cs = hedge[t].get("share", 0), congress[t].get("share", 0)
        if hs <= 0 or cs <= 0:
            continue
        convergence.append({"ticker": t, "hedge_share": hs, "congress_share": cs,
                            "combined": hs + cs, "top_fund_cik": hedge[t].get("top_fund_cik"),
                            "top_fund_name": hedge[t].get("top_fund_name", ""),
                            "top_member": congress[t].get("top_member", "")})
    convergence.sort(key=lambda r: r["combined"], reverse=True)

    buying_now, top_funds_n, congress_window_days = _load_buying_now(cfg)

    hedge_top = [s for s in sorted(hedge.values(), key=lambda s: s.get("share", 0), reverse=True)
                 if s.get("share", 0) > 0][:15]
    congress_top = sorted(({"ticker": t, **d} for t, d in congress.items() if d["share"] > 0),
                          key=lambda r: r["share"], reverse=True)[:15]

    # Unified stock pages at docs/stocks/ (hedge-featured or congress-traded tickers).
    sp_path = DATA_DIR / "hedge" / "stock_pages.json"
    page_tickers = set(load_json(sp_path).get("tickers", [])) if sp_path.exists() else set()
    if (DATA_DIR / "rankings.json").exists():
        page_tickers |= set(load_json(DATA_DIR / "rankings.json").get("stocks", {}).keys())

    # Latest 13F filing wave (e.g. "2025Q4"), from the hedge snapshot index — stamps the
    # dashboard so the vintage of the hedge signal is clear, not just the render time.
    hedge_wave = None
    ri_path = DATA_DIR / "hedge" / "report_index.json"
    if ri_path.exists():
        ri = load_json(ri_path)
        if ri:
            hedge_wave = ri[0].get("quarter")

    def clink(ticker: str) -> str:
        return f'<a href="stocks/{ticker}.html">{ticker}</a>' if ticker in page_tickers else ticker

    def flink(cik, name: str) -> str:
        name = (name or "")[:30]
        if cik and (DOCS / "hedge" / "funds" / f"{cik}.html").exists():
            return f'<a href="hedge/funds/{cik}.html">{name}</a>'
        return name

    def mlink(member_id, name: str) -> str:
        name = name or ""
        if member_id and (DOCS / "members" / f"{member_id}.html").exists():
            return f'<a href="members/{member_id}.html">{name}</a>'
        return name

    tmpl = Template(TEMPLATE)
    tmpl.globals["clink"] = clink
    tmpl.globals["flink"] = flink
    tmpl.globals["mlink"] = mlink
    html = tmpl.render(generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                       convergence=convergence[:30], buying_now=buying_now[:30],
                       hedge_top=hedge_top, congress_top=congress_top,
                       n_hedge_funds=n_hedge_funds, hedge_wave=hedge_wave,
                       top_funds_n=top_funds_n, congress_window_days=congress_window_days)
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "index.html").write_text(html)
    log.info("Dashboard: %d alpha-convergence (of %d hedge / %d congress) · "
             "%d buying-now convergence -> %s",
             len(convergence), len(hedge), len(congress), len(buying_now), DOCS / "index.html")
    if convergence[:8]:
        log.info("--- top alpha convergence (by combined alpha share) ---")
        for r in convergence[:8]:
            log.info("  %-6s hedge %.1f%% + congress %.1f%% | top fund %s",
                     r["ticker"], 100 * r["hedge_share"], 100 * r["congress_share"],
                     r["top_fund_name"][:28])
    if buying_now[:8]:
        log.info("--- top buying-now convergence ---")
        for r in buying_now[:8]:
            log.info("  %-6s hedge %.1f%% + congress %.1f%% | %s / %s",
                     r["ticker"], 100 * r["hedge_share"], 100 * r["congress_share"],
                     (r["top_fund_name"] or "")[:24], r["top_member"])


if __name__ == "__main__":
    run()
