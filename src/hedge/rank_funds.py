from __future__ import annotations

"""
Rank funds by realized follow-the-filing alpha and render the leaderboard.

Applies the config `hedge` gates (min_filings, min_coverage), ranks the survivors
by alpha (cumulative fund return - cumulative SPY over the same windows), selects
the curated watchlist (top `watchlist_size` + any pinned CIKs), writes
rankings.json, and renders a self-contained docs/hedge/index.html to eyeball.

(Phase 2 will replace this bare page with base.html-integrated fund/stock pages
and congress cross-links.)

Usage:
  python src/hedge/rank_funds.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils import DATA_DIR, load_config, load_json_gz, save_json, setup_logging

log = setup_logging("rank_funds")

HEDGE_DIR = DATA_DIR / "hedge"
PERFORMANCE_PATH = HEDGE_DIR / "fund_performance.json.gz"
RANKINGS_PATH = HEDGE_DIR / "rankings.json"
DOCS_DIR = Path(__file__).parent.parent.parent / "docs" / "hedge"

TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hedge — 13F Smart-Money Leaderboard</title>
<style>
 :root{--bg:#f6f8fa;--card:#fff;--ink:#1f2328;--muted:#57606a;--line:#d0d7de;--green:#1a7f37;--red:#cf222e;--blue:#0969da;--accent:#0a3069;}
 *{box-sizing:border-box;} body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;}
 a{color:var(--blue);text-decoration:none;} a:hover{text-decoration:underline;}
 .wrap{max-width:1080px;margin:0 auto;padding:24px 20px 60px;}
 header.site{border-bottom:1px solid var(--line);background:var(--card);} header.site .wrap{padding:16px 20px;}
 header.site h1{font-size:18px;margin:0;}
 h2{font-size:20px;margin:28px 0 10px;} .muted{color:var(--muted);} .small{font-size:13px;}
 table{width:100%;border-collapse:collapse;font-size:14px;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden;}
 th,td{padding:8px 11px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap;}
 th{background:#f0f3f6;font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted);}
 td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;} tr:last-child td{border-bottom:none;}
 tbody tr:hover{background:#f6f8fa;} .pos{color:var(--green);} .neg{color:var(--red);}
 .pill{display:inline-block;font-size:11px;padding:1px 7px;border-radius:10px;background:#dafbe1;color:var(--green);}
 .disclaimer{background:#fff8c5;border:1px solid #d4a72c;border-radius:8px;padding:12px 16px;font-size:13px;color:#54470f;margin:16px 0;}
 footer{color:var(--muted);font-size:12px;margin-top:40px;border-top:1px solid var(--line);padding-top:14px;}
</style></head><body>
<header class="site"><div class="wrap"><h1>📈 Hedge — 13F Smart-Money Leaderboard</h1></div></header>
<div class="wrap">
<p class="muted small">Generated {{ generated }} · {{ n_ranked }} ranked of {{ n_total }} funds ·
  &ldquo;Mirror the disclosed book, buy at each 13F&rsquo;s public filing date, rebalance at the next filing.&rdquo;
  Alpha = cumulative fund return &minus; SPY over the same windows.</p>
<div class="disclaimer">Spike preview (seed funds only). Coverage = share of book value we could price;
  low-coverage funds are gated out. Educational use only — not investment advice.</div>
<h2>Alpha leaderboard</h2>
<table><thead><tr>
  <th>#</th><th>Fund</th><th class="num">Alpha</th><th class="num">Fund ret</th>
  <th class="num">SPY ret</th><th class="num">Beat SPY</th><th class="num">Book</th><th class="num">Cov</th>
  <th class="num">Qtrs</th><th></th></tr></thead><tbody>
{% for r in leaderboard %}<tr>
  <td class="num">{{ loop.index }}</td>
  <td><a href="funds/{{ r.cik }}.html">{{ r.name }}</a>{% if r.cik in watchlist %} <span class="pill">watchlist</span>{% endif %}</td>
  <td class="num {{ 'pos' if r.alpha>=0 else 'neg' }}">{{ '%+.1f'|format(100*r.alpha) }}%</td>
  <td class="num {{ 'pos' if r.cumulative_return>=0 else 'neg' }}">{{ '%+.1f'|format(100*r.cumulative_return) }}%</td>
  <td class="num">{{ '%+.1f'|format(100*r.spy_return) }}%</td>
  <td class="num" title="quarters beating SPY">{{ ('%.0f/%d'|format(r.hit_rate*r.n_periods, r.n_periods)) if r.hit_rate is not none else '—' }}</td>
  <td class="num">${{ '%.1f'|format(r.latest_book/1e9) }}B</td>
  <td class="num">{{ '%.0f'|format(100*r.coverage) }}%</td>
  <td class="num">{{ r.n_periods }}</td>
  <td class="small muted">CIK {{ r.cik }}</td>
</tr>{% endfor %}
</tbody></table>
{% if gated %}<h2>Gated out <span class="muted small">(too few filings or coverage &lt; {{ min_cov }}%)</span></h2>
<table><thead><tr><th>Fund</th><th class="num">Alpha*</th><th class="num">Cov</th><th class="num">Qtrs</th><th>Reason</th></tr></thead><tbody>
{% for r in gated %}<tr><td>{{ r.name }}</td>
  <td class="num">{{ '%+.1f'|format(100*r.alpha) }}%</td>
  <td class="num">{{ '%.0f'|format(100*r.coverage) }}%</td>
  <td class="num">{{ r.n_periods }}</td><td class="small muted">{{ r.reason }}</td></tr>{% endfor %}
</tbody></table>{% endif %}
<footer>13F holdings: SEC EDGAR · Prices: Polygon.io grouped-daily · Benchmark SPY.
  Options positions excluded; entry at each filing&rsquo;s public date.</footer>
</div></body></html>
"""


def run() -> None:
    from jinja2 import Template
    cfg = load_config().get("hedge", {})
    min_filings = cfg.get("min_filings", 4)
    min_cov = cfg.get("min_coverage", 0.90)
    min_hit = cfg.get("min_hit_rate", 0.50)
    watchlist_size = cfg.get("watchlist_size", 40)
    pins = set(str(c) for c in cfg.get("watchlist_pins", []))

    if not PERFORMANCE_PATH.exists():
        log.error("No fund_performance.json; run backtest_13f.py first")
        return
    perf = load_json_gz(PERFORMANCE_PATH)

    ranked, gated = [], []
    for cik, r in perf.items():
        reasons = []
        if r["n_periods"] < min_filings - 1:   # N filings -> N-1 completed periods
            reasons.append(f"only {r['n_periods']} periods")
        if r["coverage"] < min_cov:
            reasons.append(f"coverage {100*r['coverage']:.0f}%")
        hitr = r.get("hit_rate")
        if hitr is None or hitr <= min_hit:    # must beat SPY in MORE than half its quarters
            reasons.append(f"beat-rate {100*(hitr or 0):.0f}%")
        (gated if reasons else ranked).append({**r, "reason": "; ".join(reasons)})

    ranked.sort(key=lambda r: r["alpha"], reverse=True)
    gated.sort(key=lambda r: r["alpha"], reverse=True)
    watchlist = {r["cik"] for r in ranked[:watchlist_size]} | {int(c) for c in pins if c.isdigit()}

    rankings = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "leaderboard": [{"cik": r["cik"], "name": r["name"], "alpha": r["alpha"],
                         "cumulative_return": r["cumulative_return"], "spy_return": r["spy_return"],
                         "coverage": r["coverage"], "latest_book": r["latest_book"],
                         "n_periods": r["n_periods"], "hit_rate": r.get("hit_rate")} for r in ranked],
        "watchlist_ciks": sorted(watchlist),
    }
    save_json(RANKINGS_PATH, rankings)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    html = Template(TEMPLATE).render(
        generated=rankings["generated"], leaderboard=ranked, gated=gated,
        watchlist=watchlist, n_ranked=len(ranked), n_total=len(perf),
        min_cov=int(100 * min_cov))
    (DOCS_DIR / "index.html").write_text(html)
    log.info("Ranked %d funds (%d gated) -> %s and %s",
             len(ranked), len(gated), RANKINGS_PATH, DOCS_DIR / "index.html")


if __name__ == "__main__":
    run()
