from __future__ import annotations

"""
Top-level "Smart Money" dashboard — the combined home page (docs/index.html) that
sits above the congressional-trades tracker and the 13F Hedge report.

Three modules:
  1. Convergence — tickers BOTH sides are buying now: congressional members bought it
     in the last `congress_window_days`, AND top (watchlist) hedge funds newly disclosed
     it in their latest 13F. The shared-repo payoff: "Congress and top funds both bought X."
  2. Recent changes — congress's most-bought names + the biggest new bets from top funds.
  3. Section links into the Congress and Hedge reports.

Reads data/transactions.json (congress) + data/hedge/{changes,rankings}.json. Congress's
own landing moves to congress.html so this can own index.html.

Usage:
  python src/generate_dashboard.py
"""

import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import DATA_DIR, load_config, load_json, load_json_gz, parse_date, setup_logging

log = setup_logging("generate_dashboard")

DOCS = Path(__file__).parent.parent / "docs"
CONGRESS_WINDOW_DAYS = 120     # a congressional purchase counts as "recent" within this window

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
 footer{color:var(--muted);font-size:12px;margin-top:40px;border-top:1px solid var(--line);padding-top:14px;}
</style></head><body>
<header class="site"><div class="wrap">
  <h1>💡 Smart Money Dashboard</h1>
  <div class="sub">Where U.S. Congress and top 13F hedge funds are putting money — updated {{ generated }}</div>
</div></header>
<div class="wrap">

<h2>🎯 Convergence <span class="muted small">stocks BOTH Congress and top hedge funds are buying now</span></h2>
{% if convergence %}
<div class="lead">These {{ convergence|length }} names show rare agreement: bought by members of Congress in the
  last {{ window }} days <em>and</em> newly added by a market-beating hedge fund in its latest 13F.
  Ranked by the buying fund&rsquo;s alpha — skill, not crowd.</div>
<table class="conv"><thead><tr>
  <th>Ticker</th><th class="num">Congress buyers</th><th>Top fund buying (its alpha vs SPY)</th>
  <th class="num">#&nbsp;top&nbsp;funds</th></tr></thead><tbody>
{% for r in convergence %}<tr>
  <td>{{ clink(r.ticker) }} <span class="badge">both</span></td>
  <td class="num">{{ r.n_congress }}</td>
  <td class="small">{{ r.top_fund }} <span class="pos">+{{ '%.0f'|format(100*r.top_alpha) }}%</span></td>
  <td class="num">{{ r.n_funds }}</td>
</tr>{% endfor %}
</tbody></table>
{% else %}<p class="muted small">No overlap in the current window — refresh both reports to populate.</p>{% endif %}

<div class="cols">
<div><h2>🏛️ Congress is buying <span class="muted small">last {{ window }}d</span></h2>
<table><thead><tr><th>Ticker</th><th class="num">Members</th></tr></thead><tbody>
{% for r in congress_top %}<tr><td>{{ clink(r.ticker) }}</td><td class="num">{{ r.n }}</td></tr>{% endfor %}
</tbody></table></div>
<div><h2>📈 Top funds are buying <span class="muted small">best performers&rsquo; latest new bets</span></h2>
<table><thead><tr><th>Ticker</th><th>Bought by (alpha)</th></tr></thead><tbody>
{% for r in hedge_top %}<tr><td>{{ clink(r.ticker) }}</td>
  <td class="small">{{ r.top_fund }} <span class="pos">+{{ '%.0f'|format(100*r.top_alpha) }}%</span>{% if r.n > 1 %} <span class="muted">+{{ r.n-1 }}</span>{% endif %}</td></tr>{% endfor %}
</tbody></table></div>
</div>

<h2>Explore the reports</h2>
<div class="cards">
  <div class="cardlink"><h3>🏛️ Congressional Trades</h3>
    <div class="small muted">{{ n_members }} members ranked · buy-the-disclosure alpha vs SPY</div>
    <div style="margin-top:8px"><a href="congress.html">Leaderboard &amp; archive</a><a href="graph.html">Network</a><a href="map.html">Skill map</a></div></div>
  <div class="cardlink"><h3>📈 Hedge Fund 13Fs</h3>
    <div class="small muted">{{ n_funds_ranked }} funds ranked · mirror-portfolio alpha vs SPY</div>
    <div style="margin-top:8px"><a href="hedge/index.html">Leaderboard</a><a href="hedge/index.html">Top funds &amp; drivers</a></div></div>
</div>

<footer>Congress: U.S. House Clerk &amp; Senate eFD · Hedge: SEC EDGAR 13F · Prices: Polygon.io ·
  Both benchmarked vs SPY. Educational use only — not investment advice.</footer>
</div></body></html>
"""


def _congress_recent_buys(window_days: int) -> dict:
    """{ticker: {members:set, n:int}} for congressional purchases in the window."""
    path = DATA_DIR / "transactions.json"
    if not path.exists():
        return {}
    rows = load_json(path).values()
    buys = [r for r in rows if r.get("tx_type") == "P" and r.get("ticker") and r.get("disclosure_date")]
    if not buys:
        return {}
    latest = max(r["disclosure_date"] for r in buys)
    cutoff = (date.fromisoformat(latest) - timedelta(days=window_days)).isoformat()
    out: dict = defaultdict(lambda: {"members": set(), "n": 0})
    for r in buys:
        if r["disclosure_date"] >= cutoff:
            out[r["ticker"]]["members"].add(r.get("member", ""))
    for t, d in out.items():
        d["n"] = len(d["members"])
    return out


def _top_fund_buys(top_n: int, min_alpha: float) -> dict:
    """New buys made by TOP-TIER funds only — funds actually beating the market.

    We are NOT interested in consensus (many funds owning a name); we want what the
    best performers are buying. So we take the top `top_n` funds by alpha (that also
    clear `min_alpha`), and key each new buy to its buyer's alpha. A ticker's signal
    is its BEST buyer's alpha, so a lone buy by a market-crushing fund outranks a
    crowded buy by mediocre ones.

    Returns {ticker: {buyers:[{name,alpha,value}], best_alpha, issuer, value}}.
    """
    changes_path = DATA_DIR / "hedge" / "changes.json.gz"
    rank_path = DATA_DIR / "hedge" / "rankings.json"
    if not changes_path.exists() or not rank_path.exists():
        return {}
    funds = load_json_gz(changes_path).get("funds", {})
    leaderboard = load_json(rank_path).get("leaderboard", [])   # sorted by alpha desc
    top = [f for f in leaderboard if f.get("alpha", 0) > min_alpha][:top_n]
    alpha_of = {str(f["cik"]): f["alpha"] for f in top}

    out: dict = defaultdict(lambda: {"buyers": [], "best_alpha": None, "issuer": "", "value": 0.0})
    for cik, d in funds.items():
        if cik not in alpha_of:
            continue
        a = alpha_of[cik]
        for r in d.get("new", []):
            t = r.get("ticker")
            if not t:
                continue
            o = out[t]
            o["buyers"].append({"name": d["manager"], "alpha": a, "value": r.get("value", 0)})
            o["issuer"] = r.get("issuer") or o["issuer"]
            o["value"] += r.get("value", 0)
            o["best_alpha"] = a if o["best_alpha"] is None else max(o["best_alpha"], a)
    for t, o in out.items():
        o["buyers"].sort(key=lambda x: x["alpha"], reverse=True)
    return out


def run() -> None:
    from jinja2 import Template
    cfg = load_config()
    dcfg = cfg.get("dashboard", {})
    window = dcfg.get("congress_window_days", CONGRESS_WINDOW_DAYS)
    top_funds_n = dcfg.get("top_funds_n", 50)
    min_alpha = dcfg.get("min_fund_alpha", 0.0)

    congress = _congress_recent_buys(window)
    hedge = _top_fund_buys(top_funds_n, min_alpha)

    def _best(o):
        b = o["buyers"][0]
        return {"name": b["name"], "alpha": b["alpha"], "n": len(set(x["name"] for x in o["buyers"]))}

    # Convergence: bought recently by Congress AND by a top-tier (market-beating) fund.
    # Ranked by the BEST buyer's alpha — fund QUALITY, not how many funds piled in.
    convergence = []
    for t in set(congress) & set(hedge):
        b = _best(hedge[t])
        convergence.append({"ticker": t, "n_congress": congress[t]["n"], "issuer": hedge[t]["issuer"],
                            "top_fund": b["name"], "top_alpha": b["alpha"], "n_funds": b["n"]})
    convergence.sort(key=lambda r: (r["top_alpha"], r["n_congress"]), reverse=True)

    congress_top = sorted(({"ticker": t, "n": d["n"]} for t, d in congress.items()),
                          key=lambda r: r["n"], reverse=True)[:15]
    hedge_top = []
    for t, o in hedge.items():
        b = _best(o)
        hedge_top.append({"ticker": t, "issuer": o["issuer"], "top_fund": b["name"],
                          "top_alpha": b["alpha"], "n": b["n"]})
    hedge_top.sort(key=lambda r: r["top_alpha"], reverse=True)
    hedge_top = hedge_top[:15]

    def clink(ticker: str) -> str:
        if (DOCS / "stocks" / f"{ticker}.html").exists():
            return f'<a href="stocks/{ticker}.html">{ticker}</a>'
        return ticker

    n_members = len(load_json(DATA_DIR / "rankings.json").get("leaderboard", [])) if (DATA_DIR / "rankings.json").exists() else 0
    hr = load_json(DATA_DIR / "hedge" / "rankings.json") if (DATA_DIR / "hedge" / "rankings.json").exists() else {}
    n_funds_ranked = len(hr.get("leaderboard", []))

    tmpl = Template(TEMPLATE)
    tmpl.globals["clink"] = clink
    html = tmpl.render(generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                       window=window, convergence=convergence[:30],
                       congress_top=congress_top, hedge_top=hedge_top,
                       n_members=n_members, n_funds_ranked=n_funds_ranked)
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "index.html").write_text(html)
    log.info("Dashboard: %d convergence names (of %d congress / %d hedge tickers) -> %s",
             len(convergence), len(congress), len(hedge), DOCS / "index.html")
    if convergence[:8]:
        log.info("--- top convergence (ranked by buying fund's alpha) ---")
        for r in convergence[:8]:
            log.info("  %-6s  %d congress buyers + top fund %s (+%.0f%% alpha)", r["ticker"],
                     r["n_congress"], r["top_fund"][:28], 100 * r["top_alpha"])


if __name__ == "__main__":
    run()
