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
from" decomposition. Convergence = stocks that drove alpha on BOTH sides.

Reads data/hedge/alpha_attribution.json (from backtest_13f) + data/performance.json +
data/rankings.json (congress). Congress's own landing lives at congress.html.

Usage:
  python src/generate_dashboard.py
"""

import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import DATA_DIR, load_json, setup_logging

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
  <div class="sub">Ranked by share of <em>total alpha produced</em> — where Congress and top hedge funds actually made their edge. Updated {{ generated }}</div>
  <nav class="top"><a class="here" href="index.html">💡 Dashboard</a><a href="congress.html">🏛️ Congress Trades</a><a href="hedge/index.html">📈 Hedge Fund 13Fs</a></nav>
</div></header>
<div class="wrap">

<h2>🎯 Convergence <span class="muted small">stocks that drove alpha for BOTH Congress out-performers and top hedge funds</span></h2>
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
</div>

<footer>Alpha attribution: each stock's summed contribution to the total alpha of the ranked funds /
  out-performing members. Congress: House &amp; Senate disclosures · Hedge: SEC EDGAR 13F · Prices: Polygon.io ·
  benchmark SPY. Educational use only — not investment advice.</footer>
</div></body></html>
"""


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


def run() -> None:
    from jinja2 import Template
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

    hedge_top = [s for s in sorted(hedge.values(), key=lambda s: s.get("share", 0), reverse=True)
                 if s.get("share", 0) > 0][:15]
    congress_top = sorted(({"ticker": t, **d} for t, d in congress.items() if d["share"] > 0),
                          key=lambda r: r["share"], reverse=True)[:15]

    # Unified stock pages at docs/stocks/ (hedge-featured or congress-traded tickers).
    sp_path = DATA_DIR / "hedge" / "stock_pages.json"
    page_tickers = set(load_json(sp_path).get("tickers", [])) if sp_path.exists() else set()
    if (DATA_DIR / "rankings.json").exists():
        page_tickers |= set(load_json(DATA_DIR / "rankings.json").get("stocks", {}).keys())

    def clink(ticker: str) -> str:
        return f'<a href="stocks/{ticker}.html">{ticker}</a>' if ticker in page_tickers else ticker

    def flink(cik, name: str) -> str:
        name = (name or "")[:30]
        if cik and (DOCS / "hedge" / "funds" / f"{cik}.html").exists():
            return f'<a href="hedge/funds/{cik}.html">{name}</a>'
        return name

    tmpl = Template(TEMPLATE)
    tmpl.globals["clink"] = clink
    tmpl.globals["flink"] = flink
    html = tmpl.render(generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                       convergence=convergence[:30], hedge_top=hedge_top, congress_top=congress_top,
                       n_hedge_funds=n_hedge_funds)
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "index.html").write_text(html)
    log.info("Dashboard: %d convergence (of %d hedge / %d congress alpha stocks) -> %s",
             len(convergence), len(hedge), len(congress), DOCS / "index.html")
    if convergence[:8]:
        log.info("--- top convergence (by combined alpha share) ---")
        for r in convergence[:8]:
            log.info("  %-6s hedge %.1f%% + congress %.1f%% | top fund %s",
                     r["ticker"], 100 * r["hedge_share"], 100 * r["congress_share"],
                     r["top_fund_name"][:28])


if __name__ == "__main__":
    run()
