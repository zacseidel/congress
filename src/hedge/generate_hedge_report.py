from __future__ import annotations

"""
Render per-fund pages for the Hedge report (docs/hedge/funds/{cik}.html).

Each page shows the manager's follow-the-filing performance (cumulative return vs
SPY, alpha, per-quarter returns) alongside its latest quarterly moves (new buys,
exits, adds/trims) and its largest current holdings. Stock tickers link to the
existing congressional stock pages so a name shows both signals — the shared-repo
payoff.

Reads fund_performance.json, changes.json, holdings.json, rankings.json.
Run after backtest_13f + rank_funds + diff_holdings.

Usage:
  python src/hedge/generate_hedge_report.py
"""

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils import DATA_DIR, load_json, load_json_gz, setup_logging
from resolve_cusip import cached as cusip_cached
from holdings_io import load_holdings

log = setup_logging("generate_hedge_report")

HEDGE_DIR = DATA_DIR / "hedge"
DOCS_DIR = Path(__file__).parent.parent.parent / "docs" / "hedge"
# Relative path from docs/hedge/funds/ back to the congress stock pages in docs/stocks/.
CONGRESS_STOCK_REL = "../../stocks"

STYLE = """
 :root{--bg:#f6f8fa;--card:#fff;--ink:#1f2328;--muted:#57606a;--line:#d0d7de;--green:#1a7f37;--red:#cf222e;--blue:#0969da;--accent:#0a3069;}
 *{box-sizing:border-box;} body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;}
 a{color:var(--blue);text-decoration:none;} a:hover{text-decoration:underline;}
 .wrap{max-width:1080px;margin:0 auto;padding:24px 20px 60px;}
 header.site{border-bottom:1px solid var(--line);background:var(--card);} header.site .wrap{padding:16px 20px;}
 header.site h1{font-size:18px;margin:0;} header.site a{color:inherit;}
 nav.top{margin-top:6px;} nav.top a{margin-right:18px;font-size:14px;font-weight:600;color:var(--blue);}
 h2{font-size:19px;margin:26px 0 10px;} .muted{color:var(--muted);} .small{font-size:13px;}
 .stat-row{display:flex;gap:28px;flex-wrap:wrap;margin:10px 0;}
 .stat .n{font-size:24px;font-weight:600;} .stat .l{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;}
 table{width:100%;border-collapse:collapse;font-size:14px;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden;margin:8px 0;}
 th,td{padding:7px 11px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap;}
 th{background:#f0f3f6;font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted);}
 td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;} tr:last-child td{border-bottom:none;}
 .pos{color:var(--green);} .neg{color:var(--red);}
 .cols{display:flex;gap:20px;flex-wrap:wrap;} .cols>div{flex:1 1 320px;}
 .scroll{overflow-x:auto;} h3{font-size:14px;margin:8px 0 6px;}
 table.tl td, table.tl th{padding:5px 9px;} table.tl td.new{background:#dafbe1;font-weight:600;}
 footer{color:var(--muted);font-size:12px;margin-top:40px;border-top:1px solid var(--line);padding-top:14px;}
"""

PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ p.name }} — Hedge 13F</title><style>{{ style }}</style></head><body>
<header class="site"><div class="wrap"><h1>📈 <a href="../index.html">Hedge — 13F Smart Money</a></h1>
  <nav class="top"><a href="../../index.html">💡 Dashboard</a><a href="../../congress.html">🏛️ Congress Trades</a><a href="../index.html">📈 Hedge Leaderboard</a></nav>
</div></header>
<div class="wrap">
<p class="small muted"><a href="../index.html">&larr; Leaderboard</a> · CIK {{ p.cik }} · latest filing {{ ch.latest_filing or '—' }}</p>
<h2>{{ p.name }}</h2>
<div class="stat-row">
  <div class="stat"><div class="n {{ 'pos' if p.alpha>=0 else 'neg' }}">{{ '%+.1f'|format(100*p.alpha) }}%</div><div class="l">Alpha vs SPY</div></div>
  <div class="stat"><div class="n">{{ '%+.1f'|format(100*p.cumulative_return) }}%</div><div class="l">Follow return</div></div>
  <div class="stat"><div class="n">{{ '%+.1f'|format(100*p.spy_return) }}%</div><div class="l">SPY, same windows</div></div>
  <div class="stat"><div class="n">${{ '%.1f'|format(p.latest_book/1e9) }}B</div><div class="l">Latest book</div></div>
  <div class="stat"><div class="n">{{ '%.0f'|format(100*p.coverage) }}%</div><div class="l">Priced coverage</div></div>
</div>

<h2>What drove alpha <span class="muted small">each position&rsquo;s contribution to the {{ '%+.0f'|format(100*p.alpha) }}% alpha vs SPY (weight &times; excess return)</span></h2>
<div class="cols">
<div><h3 class="muted small">Top contributors</h3>
<table><thead><tr><th>Ticker</th><th>Issuer</th><th class="num">Contribution</th><th class="num">Avg wt</th><th class="num">Qtrs</th></tr></thead><tbody>
{% for d in p.drivers %}<tr><td>{{ tlink(d) }}</td><td class="small">{{ d.issuer }}</td>
  <td class="num pos">+{{ '%.1f'|format(100*d.contribution) }}pp</td>
  <td class="num">{{ '%.0f'|format(100*d.avg_weight) }}%</td><td class="num">{{ d.quarters }}</td></tr>{% endfor %}
</tbody></table></div>
<div><h3 class="muted small">Top detractors</h3>
{% if p.detractors %}<table><thead><tr><th>Ticker</th><th>Issuer</th><th class="num">Contribution</th><th class="num">Avg wt</th><th class="num">Qtrs</th></tr></thead><tbody>
{% for d in p.detractors %}<tr><td>{{ tlink(d) }}</td><td class="small">{{ d.issuer }}</td>
  <td class="num neg">{{ '%.1f'|format(100*d.contribution) }}pp</td>
  <td class="num">{{ '%.0f'|format(100*d.avg_weight) }}%</td><td class="num">{{ d.quarters }}</td></tr>{% endfor %}
</tbody></table>{% else %}<p class="muted small">None.</p>{% endif %}</div>
</div>

<div class="cols">
<div><h2>New buys <span class="muted small">{{ ch.prior_filing }} → {{ ch.latest_filing }}</span></h2>
{% if ch.new %}<table><thead><tr><th>Ticker</th><th>Issuer</th><th class="num">Value</th></tr></thead><tbody>
{% for r in ch.new[:15] %}<tr><td>{{ tlink(r) }}</td><td class="small">{{ r.issuer }}</td><td class="num">${{ '%.0f'|format(r.value/1e6) }}M</td></tr>{% endfor %}
</tbody></table>{% else %}<p class="muted small">None.</p>{% endif %}</div>
<div><h2>Exits</h2>
{% if ch.exited %}<table><thead><tr><th>Ticker</th><th>Issuer</th><th class="num">Was</th></tr></thead><tbody>
{% for r in ch.exited[:15] %}<tr><td>{{ tlink(r) }}</td><td class="small">{{ r.issuer }}</td><td class="num">${{ '%.0f'|format(r.value/1e6) }}M</td></tr>{% endfor %}
</tbody></table>{% else %}<p class="muted small">None.</p>{% endif %}</div>
</div>
<h2>Largest holdings <span class="muted small">latest filing</span></h2>
<table><thead><tr><th>Ticker</th><th>Issuer</th><th class="num">Value</th><th class="num">% book</th></tr></thead><tbody>
{% for r in top_holdings %}<tr><td>{{ tlink(r) }}</td><td class="small">{{ r.issuer }}</td>
  <td class="num">${{ '%.0f'|format(r.value/1e6) }}M</td><td class="num">{{ '%.1f'|format(r.pct) }}%</td></tr>{% endfor %}
</tbody></table>
<h2>Position timeline <span class="muted small">position value ($M) by 13F, quarter over quarter</span></h2>
<div class="scroll"><table class="tl"><thead><tr><th>Ticker</th>
{% for c in timeline_cols %}<th class="num">{{ c }}</th>{% endfor %}</tr></thead><tbody>
{% for row in timeline %}<tr><td>{{ tlink(row) }} <span class="small muted">{{ (row.issuer or '')[:22] }}</span></td>
{% for cell in row.cells %}<td class="num {{ cell.cls }}" title="{{ cell.note }}">{{ cell.txt }}</td>{% endfor %}
</tr>{% endfor %}
</tbody></table></div>
<p class="small muted">Green = first disclosed (new buy) · brighter = larger position · blank = not held that quarter.</p>

<h2>Quarter-by-quarter <span class="muted small">follow-the-filing</span></h2>
<table><thead><tr><th>Entry</th><th>Exit</th><th class="num">Fund</th><th class="num">SPY</th><th class="num">Cov</th><th class="num">Names</th></tr></thead><tbody>
{% for q in p.periods %}<tr><td>{{ q.entry }}</td><td>{{ q.exit }}</td>
  <td class="num {{ 'pos' if q.return>=0 else 'neg' }}">{{ '%+.1f'|format(100*q.return) }}%</td>
  <td class="num">{{ '%+.1f'|format(100*q.spy_return) if q.spy_return is not none else '—' }}%</td>
  <td class="num">{{ '%.0f'|format(100*q.coverage) }}%</td>
  <td class="num">{{ q.n_priced }}/{{ q.n_total }}</td></tr>{% endfor %}
</tbody></table>
<footer>13F holdings: SEC EDGAR · Prices: Polygon.io · Benchmark SPY · entry at each filing&rsquo;s public date, options excluded.
  Tickers link to congressional trading pages where available. Educational use only — not investment advice.</footer>
</div></body></html>
"""


def _ticker(cusip: str):
    rec = cusip_cached(cusip)
    return rec.get("ticker") if rec else None


def run() -> None:
    from jinja2 import Template
    perf_path = HEDGE_DIR / "fund_performance.json.gz"
    if not perf_path.exists():
        log.error("No fund_performance.json.gz; run backtest_13f.py first")
        return
    perf = load_json_gz(perf_path)
    changes = load_json_gz(HEDGE_DIR / "changes.json.gz").get("funds", {}) if (HEDGE_DIR / "changes.json.gz").exists() else {}
    holdings = load_holdings(HEDGE_DIR / "holdings.json.gz")
    rankings = load_json(HEDGE_DIR / "rankings.json") if (HEDGE_DIR / "rankings.json").exists() else {"watchlist_ciks": []}
    watchlist = set(rankings.get("watchlist_ciks", []))

    # Latest-filing holdings per fund, for the "largest holdings" table.
    latest_by_fund: dict = defaultdict(list)
    latest_date: dict = {}
    for h in holdings.values():
        latest_date[h["cik"]] = max(latest_date.get(h["cik"], ""), h["filing_date"])
    for h in holdings.values():
        if h["filing_date"] == latest_date[h["cik"]] and not h.get("put_call"):
            latest_by_fund[h["cik"]].append(h)

    # Unified stock pages at docs/stocks/ (hedge-featured or congress-traded tickers).
    page_tickers = set(load_json(HEDGE_DIR / "stock_pages.json").get("tickers", [])) \
        if (HEDGE_DIR / "stock_pages.json").exists() else set()
    if (DATA_DIR / "rankings.json").exists():
        page_tickers |= set(load_json(DATA_DIR / "rankings.json").get("stocks", {}).keys())

    def tlink(r):
        t = r.get("ticker")
        if not t:
            return f'<span class="muted">{r["cusip"][:6]}…</span>'
        return f'<a href="../../stocks/{t}.html">{t}</a>' if t in page_tickers else t

    tmpl = Template(PAGE)
    tmpl.globals["tlink"] = tlink
    funds_dir = DOCS_DIR / "funds"
    funds_dir.mkdir(parents=True, exist_ok=True)
    # Clear stale pages from prior runs (the ranked set changes each run), so no
    # orphaned pages linger.
    for old in funds_dir.glob("*.html"):
        old.unlink()

    # Render a page for each fund DISPLAYED on the leaderboard (top leaderboard_size),
    # so every displayed/linked row resolves. The full ranked set stays in the data.
    from utils import load_config
    board_size = load_config().get("hedge", {}).get("leaderboard_size", 500)
    ranked_ciks = [str(r["cik"]) for r in rankings.get("leaderboard", [])][:board_size]
    targets = [c for c in ranked_ciks if c in perf] or list(perf)
    target_ciks = {int(c) for c in targets}

    # Per-ticker position value by filing date, for the "position timeline" (buys by
    # date). Built once for the target funds.
    series: dict = defaultdict(lambda: defaultdict(dict))   # cik -> ticker -> {date: value}
    issuer_of: dict = {}
    report_of: dict = {}
    fund_dates: dict = defaultdict(set)
    for h in holdings.values():
        if h["cik"] not in target_ciks or h.get("put_call"):
            continue
        t = _ticker(h["cusip"])
        if not t:
            continue
        series[h["cik"]][t][h["filing_date"]] = series[h["cik"]][t].get(h["filing_date"], 0) + h["value"]
        issuer_of[(h["cik"], t)] = h["issuer"]
        report_of[h["filing_date"]] = h.get("report_date", h["filing_date"])
        fund_dates[h["cik"]].add(h["filing_date"])

    def qlabel(report_date: str) -> str:
        try:
            y, m, _ = report_date.split("-")
            return "Q%d'%s" % ((int(m) - 1) // 3 + 1, y[2:])
        except Exception:
            return report_date

    def build_timeline(cik_i: int):
        dates = sorted(fund_dates.get(cik_i, []))
        cols = [qlabel(report_of[d]) for d in dates]
        tmax = {t: max(v.values()) for t, v in series[cik_i].items()}
        top_t = sorted(tmax, key=tmax.get, reverse=True)[:22]
        rows = []
        for t in top_t:
            vals = series[cik_i][t]
            first = min(vals)
            cells = []
            for d in dates:
                if d in vals:
                    v = vals[d]
                    cells.append({"txt": "%.0f" % (v / 1e6), "cls": "new" if d == first else "",
                                  "note": "%s: $%.0fM" % (d, v / 1e6)})
                else:
                    cells.append({"txt": "", "cls": "", "note": ""})
            rows.append({"ticker": t, "issuer": issuer_of.get((cik_i, t)), "cells": cells})
        return cols, rows

    n = 0
    for cik in targets:
        p = perf[cik]
        rows = sorted(latest_by_fund.get(int(cik), []), key=lambda x: x["value"], reverse=True)
        book = sum(r["value"] for r in rows) or 1
        top = [{"ticker": _ticker(r["cusip"]), "cusip": r["cusip"], "issuer": r["issuer"],
                "value": r["value"], "pct": 100 * r["value"] / book} for r in rows[:20]]
        ch = changes.get(cik, {"latest_filing": None, "prior_filing": None,
                               "new": [], "exited": [], "increased": [], "decreased": []})
        tl_cols, tl_rows = build_timeline(int(cik))
        html = tmpl.render(p=p, ch=ch, top_holdings=top, style=STYLE,
                           timeline=tl_rows, timeline_cols=tl_cols)
        (funds_dir / f"{cik}.html").write_text(html)
        n += 1
    log.info("Rendered %d fund pages -> %s", n, funds_dir)


if __name__ == "__main__":
    run()
