from __future__ import annotations

"""
Rank funds by realized follow-the-filing alpha and render the leaderboard.

Applies the config `hedge` gates (min_filings, min_coverage), ranks the survivors
by alpha (cumulative fund return - cumulative SPY over the same windows), selects
the curated watchlist (top `watchlist_size` + any pinned CIKs), writes rankings.json,
and renders the leaderboard as docs/hedge/index.html.

It also archives a dated snapshot per quarterly 13F filing wave under
docs/hedge/reports/{quarter}.html (keyed by the dominant filing quarter, so re-running
within a wave overwrites rather than piling up — ~4/yr). The live index lists the
archive; report_index.json tracks it.

Usage:
  python src/hedge/rank_funds.py
"""

import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils import DATA_DIR, load_config, load_json, load_json_gz, save_json, setup_logging

log = setup_logging("rank_funds")

HEDGE_DIR = DATA_DIR / "hedge"
PERFORMANCE_PATH = HEDGE_DIR / "fund_performance.json.gz"
RANKINGS_PATH = HEDGE_DIR / "rankings.json"
REPORT_INDEX = HEDGE_DIR / "report_index.json"
ATTRIBUTION_PATH = HEDGE_DIR / "alpha_attribution.json"
CHANGES_PATH = HEDGE_DIR / "changes.json.gz"
STOCK_PAGES_PATH = HEDGE_DIR / "stock_pages.json"
DOCS_DIR = Path(__file__).parent.parent.parent / "docs" / "hedge"

TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hedge — 13F Smart-Money Leaderboard{% if snapshot %} · {{ as_of }}{% endif %}</title>
{% if snapshot %}<base href="../">{% endif %}{# snapshot lives in reports/; resolve links from docs/hedge/ #}
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
 nav.top{margin-top:6px;} nav.top a{margin-right:18px;font-size:14px;font-weight:600;} nav.top a.here{color:var(--ink);}
 footer{color:var(--muted);font-size:12px;margin-top:40px;border-top:1px solid var(--line);padding-top:14px;}
</style></head><body>
<header class="site"><div class="wrap"><h1>📈 Hedge — 13F Smart-Money Leaderboard</h1>
  <nav class="top"><a href="../index.html">💡 Dashboard</a><a href="../congress.html">🏛️ Congress Trades</a><a class="here" href="index.html">📈 Hedge Fund 13Fs</a></nav>
</div></header>
<div class="wrap">
<p class="muted small">Generated {{ generated }} · {{ n_ranked }} ranked of {{ n_total }} funds ·
  &ldquo;Mirror the disclosed book, buy at each 13F&rsquo;s public filing date, rebalance at the next filing.&rdquo;
  Alpha = cumulative fund return &minus; SPY over the same windows.</p>
<div class="disclaimer">Coverage = share of book value we could price. Funds that beat SPY in &le;50% of their
  quarters, or with too few filings / low coverage, are gated out (kept in the data, not shown).
  Educational use only — not investment advice.</div>
{% if snapshot %}<div class="disclaimer" style="background:#ddf4ff;border-color:#54aeff;color:#0a3069">
  📌 Archived snapshot of the <b>{{ as_of }}</b> 13F filing wave. <a href="index.html">View the live leaderboard →</a></div>{% endif %}

<h2>📊 Stocks that drove out-performance <span class="muted small">share of the {{ n_ranked }} ranked funds&rsquo; total alpha</span></h2>
<table><thead><tr><th>Ticker</th><th>Issuer</th><th class="num">Alpha share</th><th class="num">Funds</th><th>Top contributing fund</th></tr></thead><tbody>
{% for s in drivers %}<tr><td>{{ tlink(s.ticker) }}</td><td class="small">{{ s.issuer[:30] }}</td>
  <td class="num pos">{{ '%.1f'|format(100*s.share) }}%</td><td class="num">{{ s.n_funds }}</td>
  <td class="small">{{ flink(s.top_fund_cik, s.top_fund_name) }}</td></tr>{% endfor %}
</tbody></table>

<h2>🛒 What out-performers are buying now <span class="muted small">new 13F positions, by skill-weighted conviction (alpha&times;consistency &times; position size)</span></h2>
<table><thead><tr><th>Ticker</th><th>Issuer</th><th class="num">Funds</th><th>Highest-conviction buyer (alpha · size)</th></tr></thead><tbody>
{% for b in buying_now %}<tr><td>{{ tlink(b.ticker) }}</td><td class="small">{{ b.issuer[:30] }}</td>
  <td class="num">{{ b.n_funds }}</td>
  <td class="small">{{ flink(b.top_cik, b.top_name) }} <span class="pos">+{{ '%.0f'|format(100*b.top_alpha) }}%</span> <span class="muted">· {{ '%.1f'|format(100*b.top_conv) }}% of book</span></td></tr>{% endfor %}
</tbody></table>

<h2>Alpha leaderboard <span class="muted small">clickable funds only; full ranking is in the data</span></h2>
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
{% if gated %}<details style="margin-top:24px"><summary style="cursor:pointer"><b>Gated out ({{ n_gated }})</b>
  <span class="muted small">&mdash; beat-rate &le; 50%, too few filings, or coverage &lt; {{ min_cov }}%; kept in the data, not clickable. Showing first {{ gated|length }}.</span></summary>
<table style="margin-top:10px"><thead><tr><th>Fund</th><th class="num">Alpha*</th><th class="num">Cov</th><th class="num">Qtrs</th><th>Reason</th></tr></thead><tbody>
{% for r in gated %}<tr><td>{{ r.name }}</td>
  <td class="num">{{ '%+.1f'|format(100*r.alpha) }}%</td>
  <td class="num">{{ '%.0f'|format(100*r.coverage) }}%</td>
  <td class="num">{{ r.n_periods }}</td><td class="small muted">{{ r.reason }}</td></tr>{% endfor %}
</tbody></table></details>{% endif %}
{% if archive and not snapshot %}<h2>🗓️ Snapshot archive <span class="muted small">one per quarterly 13F filing wave</span></h2>
<table><thead><tr><th>Filing wave</th><th class="num">Ranked funds</th><th>Top fund</th><th class="num">Top alpha</th><th>Generated</th></tr></thead><tbody>
{% for a in archive %}<tr><td><a href="reports/{{ a.quarter }}.html">{{ a.quarter }}</a></td>
  <td class="num">{{ a.n_ranked }}</td><td class="small">{{ (a.top_name or '')[:36] }}</td>
  <td class="num pos">{{ '%+.1f'|format(100*a.top_alpha) if a.top_alpha is not none else '—' }}%</td>
  <td class="small muted">{{ a.generated }}</td></tr>{% endfor %}
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
    leaderboard_size = cfg.get("leaderboard_size", 500)
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
    board = ranked[:leaderboard_size]        # only funds with pages are displayed/linked

    # leaderboard DATA keeps the full ranked set; the page only shows `board` (clickable).
    rankings = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "n_displayed": len(board),
        "leaderboard": [{"cik": r["cik"], "name": r["name"], "alpha": r["alpha"],
                         "cumulative_return": r["cumulative_return"], "spy_return": r["spy_return"],
                         "coverage": r["coverage"], "latest_book": r["latest_book"],
                         "n_periods": r["n_periods"], "hit_rate": r.get("hit_rate")} for r in ranked],
        "watchlist_ciks": sorted(watchlist),
    }
    save_json(RANKINGS_PATH, rankings)

    # "Stocks that drove out-performance" — reuse the backtest's alpha attribution.
    attr = load_json(ATTRIBUTION_PATH) if ATTRIBUTION_PATH.exists() else {"stocks": []}
    drivers = attr.get("stocks", [])[:15]

    # "What out-performers are buying now" — new positions in the latest 13Fs, scored by
    # SKILL-WEIGHTED CONVICTION: each ranked fund's vote for a new buy =
    #   skill (max(alpha,0) x hit_rate: magnitude x consistency, robust to lucky funds)
    #   x conviction (new position value / fund book: how hard they bet).
    # Summed across funds, so it rewards high-conviction bets by durable out-performers
    # over mega-caps everyone dabbles in or one lucky fund's pick.
    alpha_by_cik = {str(r["cik"]): r["alpha"] for r in ranked}
    skill_by_cik = {str(r["cik"]): max(r["alpha"], 0) * (r.get("hit_rate") or 0) for r in ranked}
    book_by_cik = {str(r["cik"]): (r.get("latest_book") or 0) for r in ranked}
    changes_funds = load_json_gz(CHANGES_PATH).get("funds", {}) if CHANGES_PATH.exists() else {}
    buys: dict = {}
    if changes_funds:
        for cik, d in changes_funds.items():
            skill, book = skill_by_cik.get(cik, 0), book_by_cik.get(cik, 0)
            if skill <= 0 or book <= 0:
                continue
            for row in d.get("new", []):
                t = row.get("ticker")
                if not t:
                    continue
                conv = (row.get("value", 0) or 0) / book
                vote = skill * conv
                b = buys.setdefault(t, {"ticker": t, "issuer": row.get("issuer", ""),
                                        "score": 0.0, "n_funds": 0, "top": None, "top_vote": -1.0})
                b["score"] += vote
                b["n_funds"] += 1
                if vote > b["top_vote"]:
                    b["top_vote"] = vote
                    b["top"] = (d["manager"], cik, alpha_by_cik[cik], conv)
    buying_now = sorted(buys.values(), key=lambda x: x["score"], reverse=True)[:15]
    for b in buying_now:
        b["top_name"], b["top_cik"], b["top_alpha"], b["top_conv"] = b["top"]

    # Unified stock pages live at docs/stocks/. A ticker has one if it's hedge-featured
    # or congress-traded.
    page_tickers = set(load_json(STOCK_PAGES_PATH).get("tickers", [])) if STOCK_PAGES_PATH.exists() else set()
    cr = DATA_DIR / "rankings.json"
    if cr.exists():
        page_tickers |= set(load_json(cr).get("stocks", {}).keys())

    def tlink(t):
        return f'<a href="../stocks/{t}.html">{t}</a>' if t in page_tickers else t

    def flink(cik, name):
        name = (name or "")[:30]
        return (f'<a href="funds/{cik}.html">{name}</a>'
                if cik and (DOCS_DIR / "funds" / f"{cik}.html").exists() else name)

    # Data vintage: 13F is quarterly, so key the snapshot by the DOMINANT filing wave
    # (mode of funds' latest filing quarter), not the run date. Re-running within a wave
    # overwrites the same snapshot; a new wave mints a new one — ~4/yr regardless of cadence.
    def _q(iso: str) -> str:
        return f"{iso[:4]}Q{(int(iso[5:7]) - 1) // 3 + 1}"
    waves = Counter(_q(f["latest_filing"]) for f in changes_funds.values() if f.get("latest_filing"))
    as_of = waves.most_common(1)[0][0] if waves else _q(datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    # Upsert this wave into the snapshot archive index (idempotent by quarter).
    top = board[0] if board else None
    idx = load_json(REPORT_INDEX) if REPORT_INDEX.exists() else []
    idx = [e for e in idx if e["quarter"] != as_of]
    idx.append({"quarter": as_of, "generated": rankings["generated"], "n_ranked": len(ranked),
                "top_name": top["name"] if top else None, "top_alpha": top["alpha"] if top else None})
    idx.sort(key=lambda e: e["quarter"], reverse=True)
    save_json(REPORT_INDEX, idx)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "reports").mkdir(parents=True, exist_ok=True)
    tmpl = Template(TEMPLATE)
    tmpl.globals.update(tlink=tlink, flink=flink)

    def render(snapshot: bool, archive) -> str:
        return tmpl.render(
            generated=rankings["generated"], leaderboard=board, gated=gated[:60], n_gated=len(gated),
            watchlist=watchlist, n_ranked=len(ranked), n_total=len(perf),
            drivers=drivers, buying_now=buying_now, min_cov=int(100 * min_cov),
            snapshot=snapshot, as_of=as_of, archive=archive)

    (DOCS_DIR / "index.html").write_text(render(False, idx))          # live: shows archive
    (DOCS_DIR / "reports" / f"{as_of}.html").write_text(render(True, None))   # dated snapshot
    log.info("Ranked %d funds (%d gated) -> %s, index.html, reports/%s.html",
             len(ranked), len(gated), RANKINGS_PATH.name, as_of)


if __name__ == "__main__":
    run()
