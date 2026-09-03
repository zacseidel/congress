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
from utils import DATA_DIR, EDGAR_CACHE, load_config, load_json, load_json_gz, save_json, setup_logging
from specialist_funds import configured_specialists, configured_watchlist

log = setup_logging("rank_funds")

HEDGE_DIR = DATA_DIR / "hedge"
PERFORMANCE_PATH = HEDGE_DIR / "fund_performance.json.gz"
RANKINGS_PATH = HEDGE_DIR / "rankings.json"
REPORT_INDEX = HEDGE_DIR / "report_index.json"
ATTRIBUTION_PATH = HEDGE_DIR / "alpha_attribution.json"
CHANGES_PATH = HEDGE_DIR / "changes.json.gz"
STOCK_PAGES_PATH = HEDGE_DIR / "stock_pages.json"
SPECIALIST_HOLDINGS_PATH = HEDGE_DIR / "specialist_holdings.json"
DOCS_DIR = Path(__file__).parent.parent.parent / "docs" / "hedge"
LEADERBOARD_PREVIEW = 30   # rows shown before the expandable remainder

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
 h2{font-size:20px;margin:28px 0 10px;} h3{font-size:16px;margin:22px 0 8px;} .muted{color:var(--muted);} .small{font-size:13px;}
 table{width:100%;border-collapse:collapse;font-size:14px;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden;}
 th,td{padding:8px 11px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap;}
 th{background:#f0f3f6;font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted);}
 td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;} tr:last-child td{border-bottom:none;}
 tbody tr:hover{background:#f6f8fa;} .pos{color:var(--green);} .neg{color:var(--red);}
 .pill{display:inline-block;font-size:11px;padding:1px 7px;border-radius:10px;background:#dafbe1;color:var(--green);}
 .pill.warn{background:#fff8c5;color:#7d4e00;}
 .pill.hl{background:#dafbe1;color:var(--green);}
 tr.hl td{background:#f0fff4;}
 .ctrack{display:inline-block;width:56px;height:9px;background:#eaeef2;border-radius:2px;vertical-align:middle;overflow:hidden;margin-right:7px;}
 .cbar{display:block;height:100%;background:var(--green);border-radius:2px;}
 .wave{background:#ddf4ff;border:1px solid #54aeff;color:#0a3069;border-radius:8px;padding:10px 14px;font-size:14px;margin:14px 0 4px;}
 .disclaimer{background:#fff8c5;border:1px solid #d4a72c;border-radius:8px;padding:12px 16px;font-size:13px;color:#54470f;margin:16px 0;}
 nav.top{margin-top:6px;} nav.top a{margin-right:18px;font-size:14px;font-weight:600;} nav.top a.here{color:var(--ink);}
 footer{color:var(--muted);font-size:12px;margin-top:40px;border-top:1px solid var(--line);padding-top:14px;}
 details{margin-top:20px;} details summary{cursor:pointer;}
</style></head><body>
<header class="site"><div class="wrap"><h1>📈 Hedge — 13F Smart-Money Leaderboard</h1>
  <nav class="top"><a href="../index.html">💡 Dashboard</a><a href="../congress.html">🏛️ Congress Trades</a><a class="here" href="index.html">📈 Hedge Fund 13Fs</a></nav>
</div></header>
<div class="wrap">
{% if not snapshot %}<p class="wave">📅 Latest 13F filing wave: <b>{{ as_of }}</b>
  <span class="muted small">— the New buys &amp; Conviction below reflect this quarter&rsquo;s filings. Prior waves are archived at the bottom.</span></p>{% endif %}
<p class="muted small">Generated {{ generated }} · {{ n_ranked }} ranked of {{ n_total }} funds ·
  &ldquo;Mirror the disclosed book, buy at each 13F&rsquo;s public filing date, rebalance at the next filing.&rdquo;
  Alpha = cumulative fund return &minus; SPY over the same windows.</p>
<div class="disclaimer">Coverage = share of book value we could price. Funds that beat SPY in &le;50% of their
  quarters, or with too few filings / low coverage, are gated out of leaderboard ranking.
  Curated specialists may still be shown separately with an unranked label.
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
<table><thead><tr><th>Ticker</th><th>Issuer</th><th class="num">Funds</th><th class="num">Conviction</th><th>Highest-conviction buyer (alpha · size)</th></tr></thead><tbody>
{% for b in buying_now %}<tr><td>{{ tlink(b.ticker) }}</td><td class="small">{{ b.issuer[:30] }}</td>
  <td class="num">{{ b.n_funds }}</td>
  <td class="num" title="Skill-weighted conviction, indexed to the strongest buy this wave (=100). Σ over buyers of max(alpha,0)×hit-rate × position/book."><span class="ctrack"><span class="cbar" style="width:{{ b.conviction }}%"></span></span><b>{{ b.conviction }}</b></td>
  <td class="small">{{ flink(b.top_cik, b.top_name) }} <span class="pos">+{{ '%.0f'|format(100*b.top_alpha) }}%</span> <span class="muted">· {{ '%.1f'|format(100*b.top_conv) }}% of book</span></td></tr>{% endfor %}
</tbody></table>

<h2>Alpha leaderboard <span class="muted small">top {{ leaderboard_top|length }} of {{ n_displayed }} clickable funds</span></h2>
<table><thead><tr>
  <th>#</th><th>Fund</th><th class="num">Alpha</th><th class="num">Fund ret</th>
  <th class="num">SPY ret</th><th class="num">Beat SPY</th><th class="num">Book</th><th class="num">Cov</th>
  <th class="num">Qtrs</th><th></th></tr></thead><tbody>
{% for r in leaderboard_top %}<tr>
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
{% if leaderboard_rest %}
<details><summary><b>Ranks {{ leaderboard_preview + 1 }}–{{ n_displayed }}</b>
  <span class="muted small">&mdash; {{ leaderboard_rest|length }} more ranked funds</span></summary>
<table style="margin-top:10px"><thead><tr>
  <th>#</th><th>Fund</th><th class="num">Alpha</th><th class="num">Fund ret</th>
  <th class="num">SPY ret</th><th class="num">Beat SPY</th><th class="num">Book</th><th class="num">Cov</th>
  <th class="num">Qtrs</th><th></th></tr></thead><tbody>
{% for r in leaderboard_rest %}<tr>
  <td class="num">{{ leaderboard_preview + loop.index }}</td>
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
</details>
{% endif %}

{% if watchlist_pinned %}
<h2 id="watchlist">📌 Watchlist <span class="muted small">general high-conviction pins — not mixed into the healthcare specialist overlap</span></h2>
<table><thead><tr><th>Fund</th><th class="num">Rank</th><th class="num">Alpha*</th>
  <th class="num">Beat SPY</th><th class="num">Book</th><th class="num">Cov</th><th>Status</th></tr></thead><tbody>
{% for s in watchlist_pinned %}<tr>
  <td>{% if s.status != 'absent' %}<a href="funds/{{ s.cik }}.html">{{ s.label }}</a>
    {% if s.label != s.name %}<span class="small muted">· {{ s.name }}</span>{% endif %}
    {% else %}{{ s.label }}{% if s.label != s.name %} <span class="small muted">· {{ s.name }}</span>{% endif %}{% endif %}</td>
  <td class="num">{% if s.rank %}#{{ s.rank }}{% else %}—{% endif %}</td>
  <td class="num{% if s.alpha is not none %} {{ 'pos' if s.alpha>=0 else 'neg' }}{% endif %}">{% if s.alpha is not none %}{{ '%+.1f'|format(100*s.alpha) }}%{% else %}—{% endif %}</td>
  <td class="num">{{ ('%.0f%%'|format(100*s.hit_rate)) if s.hit_rate is not none else '—' }}</td>
  <td class="num">{% if s.latest_book %}{{ '$%.1f'|format(s.latest_book/1e9) }}B{% else %}—{% endif %}</td>
  <td class="num">{% if s.coverage is not none %}{{ '%.0f'|format(100*s.coverage) }}%{% else %}—{% endif %}</td>
  <td>{% if s.status == 'ranked' %}<span class="pill">ranked</span>
    {% elif s.status == 'unranked' %}<span class="pill warn">unranked</span> <span class="small muted">{{ s.reason }}</span>
    {% else %}<span class="pill warn">not in universe</span> <span class="small muted">{{ s.reason }}</span>{% endif %}</td>
</tr>{% endfor %}
</tbody></table>
<p class="small muted">*Modeled results for unranked names are provisional and do not affect the leaderboard.
  <span class="pill hl">universe</span> marks a name that also appears in the broader alpha-contributor,
  buying-now, or Congress/hedge convergence lists above.</p>
{% if watchlist_drivers %}
<h3>Watchlist — stocks that drove alpha <span class="muted small">share of these funds&rsquo; combined alpha</span></h3>
<table><thead><tr><th>Ticker</th><th>Issuer</th><th class="num">Alpha share</th><th class="num">Funds</th><th>Top contributing fund</th></tr></thead><tbody>
{% for s in watchlist_drivers %}<tr class="{{ 'hl' if s.ticker in highlight_tickers else '' }}">
  <td>{{ tlink(s.ticker) }}{% if s.ticker in highlight_tickers %} <span class="pill hl">universe</span>{% endif %}</td>
  <td class="small">{{ (s.issuer or '')[:30] }}</td>
  <td class="num pos">{{ '%.1f'|format(100*s.share) }}%</td><td class="num">{{ s.n_funds }}</td>
  <td class="small">{{ flink(s.top_fund_cik, s.top_fund_name) }}</td></tr>{% endfor %}
</tbody></table>
{% endif %}
{% if watchlist_buys %}
<h3>Watchlist — what they&rsquo;re buying now <span class="muted small">new 13F positions, skill-weighted conviction</span></h3>
<table><thead><tr><th>Ticker</th><th>Issuer</th><th class="num">Funds</th><th class="num">Conviction</th><th>Highest-conviction buyer</th></tr></thead><tbody>
{% for b in watchlist_buys %}<tr class="{{ 'hl' if b.ticker in highlight_tickers else '' }}">
  <td>{{ tlink(b.ticker) }}{% if b.ticker in highlight_tickers %} <span class="pill hl">universe</span>{% endif %}</td>
  <td class="small">{{ (b.issuer or '')[:30] }}</td>
  <td class="num">{{ b.n_funds }}</td>
  <td class="num" title="Skill-weighted conviction, indexed to the strongest watchlist buy this wave (=100)."><span class="ctrack"><span class="cbar" style="width:{{ b.conviction }}%"></span></span><b>{{ b.conviction }}</b></td>
  <td class="small">{{ flink(b.top_cik, b.top_name) }} <span class="pos">+{{ '%.0f'|format(100*b.top_alpha) }}%</span> <span class="muted">· {{ '%.1f'|format(100*b.top_conv) }}% of book</span></td></tr>{% endfor %}
</tbody></table>
{% endif %}
{% if watchlist_overlap %}
<h3>Watchlist — shared holdings
  <span class="muted small">latest disclosed longs; showing {{ watchlist_overlap|length }} of {{ n_watchlist_overlap }}, 2+ of {{ n_watchlist }} funds</span></h3>
<table><thead><tr><th>Ticker</th><th>Issuer</th><th class="num">Funds</th><th>Held by</th>
  <th class="num">Combined value</th><th class="num">Avg wt</th></tr></thead><tbody>
{% for h in watchlist_overlap %}<tr class="{{ 'hl' if h.ticker in highlight_tickers else '' }}">
  <td>{{ tlink(h.ticker) }}{% if h.ticker in highlight_tickers %} <span class="pill hl">universe</span>{% endif %}</td>
  <td class="small">{{ (h.issuer or '')[:30] }}</td>
  <td class="num">{{ h.n_funds }}/{{ n_watchlist }}</td>
  <td class="small">{% for f in h.funds %}{{ flink(f.cik, f.label) }}{% if not loop.last %}, {% endif %}{% endfor %}</td>
  <td class="num">${{ '%.0f'|format(h.combined_value/1e6) }}M</td>
  <td class="num">{{ '%.1f'|format(100*h.avg_weight) }}%</td>
</tr>{% endfor %}
</tbody></table>
{% endif %}
{% endif %}

{% if specialists %}
<h2 id="specialists">📌 Pinned specialists <span class="muted small">curated funds remain visible even when performance data is not rankable</span></h2>
<table><thead><tr><th>Specialty</th><th>Fund</th><th class="num">Alpha*</th>
  <th class="num">Beat SPY</th><th class="num">Book</th><th class="num">Cov</th><th>Status</th></tr></thead><tbody>
{% for s in specialists %}<tr><td>{{ s.category }}</td>
  <td><a href="funds/{{ s.cik }}.html">{{ s.label }}</a>
    {% if s.label != s.name %}<span class="small muted">· {{ s.name }}</span>{% endif %}</td>
  <td class="num {{ 'pos' if s.alpha>=0 else 'neg' }}">{{ '%+.1f'|format(100*s.alpha) }}%</td>
  <td class="num">{{ ('%.0f%%'|format(100*s.hit_rate)) if s.hit_rate is not none else '—' }}</td>
  <td class="num">${{ '%.1f'|format(s.latest_book/1e9) }}B</td>
  <td class="num">{{ '%.0f'|format(100*s.coverage) }}%</td>
  <td>{% if s.status == 'ranked' %}<span class="pill">ranked</span>
    {% else %}<span class="pill warn">unranked</span> <span class="small muted">{{ s.reason }}</span>{% endif %}</td>
</tr>{% endfor %}
</tbody></table>
<p class="small muted">*Modeled results for unranked specialists are provisional and do not affect the leaderboard.</p>
{% endif %}

{% if specialist_overlap %}
<h2>🧬 Shared holdings among pinned specialists
  <span class="muted small">latest disclosed long positions; showing {{ specialist_overlap|length }} of {{ n_specialist_overlap }}, ordered by fund count then combined conviction</span></h2>
<table><thead><tr><th>Ticker</th><th>Issuer</th><th class="num">Funds</th><th>Held by</th>
  <th class="num">Combined value</th><th class="num">Avg wt</th><th class="num">Alpha contrib.</th></tr></thead><tbody>
{% for h in specialist_overlap %}<tr>
  <td>{{ tlink(h.ticker) }}</td><td class="small">{{ h.issuer[:30] }}</td>
  <td class="num">{{ h.n_funds }}/{{ n_specialists }}</td>
  <td class="small">{% for f in h.funds %}{{ flink(f.cik, f.label) }}{% if not loop.last %}, {% endif %}{% endfor %}</td>
  <td class="num">${{ '%.0f'|format(h.combined_value/1e6) }}M</td>
  <td class="num" title="Average position weight among pinned funds that hold it">{{ '%.1f'|format(100*h.avg_weight) }}%</td>
  <td class="num {{ 'pos' if h.alpha_contribution is not none and h.alpha_contribution >= 0 else 'neg' }}"
      title="Sum of this stock's modeled contribution to alpha across pinned specialists">{{ '%+.1fpp'|format(100*h.alpha_contribution) if h.alpha_contribution is not none else '—' }}</td>
</tr>{% endfor %}
</tbody></table>
<p class="small muted">Alpha contribution uses the same modeled weight &times; excess-return methodology as the fund pages, summed across the pinned specialists&rsquo; full backtest history. It describes historical contribution, while the overlap columns describe current disclosed holdings. Combined value sums reported 13F values; unresolved CUSIPs are omitted.</p>
{% endif %}

{% if gated %}<details><summary><b>Gated out ({{ n_gated }})</b>
  <span class="muted small">&mdash; beat-rate &le; 50%, too few filings, or coverage &lt; {{ min_cov }}%; kept in the data. Showing first {{ gated|length }}.</span></summary>
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


def _gate_reason(record: dict, min_filings: int, min_cov: float, min_hit: float) -> str:
    reasons = []
    if record["n_periods"] < min_filings - 1:   # N filings -> N-1 completed periods
        reasons.append(f"only {record['n_periods']} periods")
    if record["coverage"] < min_cov:
        reasons.append(f"coverage {100*record['coverage']:.0f}%")
    hit_rate = record.get("hit_rate")
    if hit_rate is None or hit_rate <= min_hit:
        reasons.append(f"beat-rate {100*(hit_rate or 0):.0f}%")
    return "; ".join(reasons)


def _absent_reason(cik: int, cfg: dict) -> str:
    """Why a pinned CIK has no performance row — usually the concentration band."""
    filers_path = HEDGE_DIR / "filers.json"
    filers = load_json(filers_path) if filers_path.exists() else {}
    filer = filers.get(str(cik)) or {}
    accession = filer.get("latest_accession")
    aum_index_path = EDGAR_CACHE / "aum_index.json.gz"
    index = load_json_gz(aum_index_path) if aum_index_path.exists() else {}
    aum, n_holdings = index.get(accession, [None, 0]) if accession else (None, 0)
    max_holdings = cfg.get("max_holdings", 150)
    min_holdings = cfg.get("min_holdings", 5)
    min_aum = cfg.get("min_aum", 100_000_000)
    if not filer:
        return "not in the EDGAR 13F universe"
    if n_holdings and n_holdings > max_holdings:
        return f"{n_holdings:,} holdings (pool max {max_holdings})"
    if n_holdings and n_holdings < min_holdings:
        return f"{n_holdings} holdings (pool min {min_holdings})"
    if aum is not None and aum < min_aum:
        return f"AUM ${aum/1e6:.0f}M below ${min_aum/1e6:.0f}M floor"
    return "no performance data yet"


def _pin_row(spec: dict, record: dict | None, rank_by_cik: dict, cfg: dict) -> dict:
    if not record:
        filers_path = HEDGE_DIR / "filers.json"
        filers = load_json(filers_path) if filers_path.exists() else {}
        name = (filers.get(str(spec["cik"])) or {}).get("name") or spec["label"]
        return {
            **spec, "name": name, "alpha": None, "hit_rate": None,
            "latest_book": None, "coverage": None, "n_periods": None,
            "rank": None, "status": "absent",
            "reason": _absent_reason(spec["cik"], cfg),
        }
    return {
        **spec,
        "name": record["name"],
        "alpha": record["alpha"],
        "cumulative_return": record["cumulative_return"],
        "spy_return": record["spy_return"],
        "coverage": record["coverage"],
        "latest_book": record["latest_book"],
        "n_periods": record["n_periods"],
        "hit_rate": record.get("hit_rate"),
        "rank": rank_by_cik.get(record["cik"]),
        "status": "unranked" if record["reason"] else "ranked",
        "reason": record["reason"],
    }


def _skill_weighted_buys(fund_records: list, changes: dict, limit: int = 15) -> list:
    """New 13F buys scored by max(alpha,0)×hit-rate × position/book, indexed 0–100."""
    alpha_by = {str(r["cik"]): r["alpha"] for r in fund_records}
    skill_by = {str(r["cik"]): max(r.get("alpha") or 0, 0) * (r.get("hit_rate") or 0)
                for r in fund_records}
    book_by = {str(r["cik"]): (r.get("latest_book") or 0) for r in fund_records}
    buys: dict = {}
    for record in fund_records:
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
            entry = buys.setdefault(ticker, {"ticker": ticker, "issuer": row.get("issuer", ""),
                                             "score": 0.0, "n_funds": 0, "top": None, "top_vote": -1.0})
            entry["score"] += vote
            entry["n_funds"] += 1
            if vote > entry["top_vote"]:
                entry["top_vote"] = vote
                entry["top"] = (delta.get("manager"), record["cik"], alpha_by[cik], conv)
    ranked_buys = sorted(buys.values(), key=lambda x: x["score"], reverse=True)[:limit]
    top_score = ranked_buys[0]["score"] if ranked_buys else 0.0
    for entry in ranked_buys:
        entry["top_name"], entry["top_cik"], entry["top_alpha"], entry["top_conv"] = entry["top"]
        entry["conviction"] = round(100 * entry["score"] / top_score) if top_score > 0 else 0
    return ranked_buys


def _drivers_from_saved(ciks, perf: dict, limit: int = 15) -> list:
    """Roll up each fund's saved top drivers/detractors into a watchlist alpha list."""
    total_alpha = 0.0
    agg: dict = {}
    for cik in ciks:
        rec = perf.get(str(cik))
        if not rec:
            continue
        total_alpha += rec.get("alpha") or 0
        seen = set()
        for row in list(rec.get("drivers") or []) + list(rec.get("detractors") or []):
            ticker = row.get("ticker")
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            contrib = row.get("contribution") or 0
            stock = agg.setdefault(ticker, {"issuer": row.get("issuer"), "contribution": 0.0,
                                            "n_funds": 0, "top": None})
            stock["contribution"] += contrib
            stock["n_funds"] += 1
            stock["issuer"] = row.get("issuer") or stock["issuer"]
            if stock["top"] is None or contrib > stock["top"][2]:
                stock["top"] = (rec["cik"], rec["name"], contrib)
    stocks = []
    for ticker, stock in agg.items():
        stocks.append({
            "ticker": ticker, "issuer": stock["issuer"] or "",
            "contribution": round(stock["contribution"], 4),
            "share": round(stock["contribution"] / total_alpha, 4) if total_alpha else 0,
            "n_funds": stock["n_funds"],
            "top_fund_cik": stock["top"][0] if stock["top"] else None,
            "top_fund_name": stock["top"][1] if stock["top"] else None,
        })
    stocks.sort(key=lambda row: row["contribution"], reverse=True)
    return stocks[:limit]


def _highlight_tickers(drivers: list, buying_now: list, attr_stocks: list,
                       congress_rank: dict) -> set:
    """Tickers that also appear in the broader universe alpha/buying-now/convergence lists."""
    universe = {row["ticker"] for row in drivers if row.get("ticker")}
    universe |= {row["ticker"] for row in buying_now if row.get("ticker")}
    hedge_alpha = {row["ticker"] for row in attr_stocks
                   if row.get("ticker") and (row.get("share") or 0) > 0}
    congress_alpha = {row["ticker"] for row in congress_rank.get("drivers", [])
                      if row.get("ticker") and (row.get("score") or 0) > 0}
    congress_buys = {row["ticker"] for row in congress_rank.get("recent_buys", [])
                     if row.get("ticker")}
    universe_buys = {row["ticker"] for row in buying_now if row.get("ticker")}
    return universe | (hedge_alpha & congress_alpha) | (universe_buys & congress_buys)


def run() -> None:
    from jinja2 import Template
    cfg = load_config().get("hedge", {})
    min_filings = cfg.get("min_filings", 4)
    min_cov = cfg.get("min_coverage", 0.90)
    min_hit = cfg.get("min_hit_rate", 0.50)
    watchlist_size = cfg.get("watchlist_size", 40)
    leaderboard_size = cfg.get("leaderboard_size", 500)
    pins = set(str(c) for c in cfg.get("watchlist_pins", []))
    specialist_specs = configured_specialists(cfg)
    watchlist_specs = configured_watchlist(cfg)

    if not PERFORMANCE_PATH.exists():
        log.error("No fund_performance.json; run backtest_13f.py first")
        return
    perf = load_json_gz(PERFORMANCE_PATH)

    ranked, gated, records_by_cik = [], [], {}
    for cik, r in perf.items():
        reason = _gate_reason(r, min_filings, min_cov, min_hit)
        record = {**r, "reason": reason}
        records_by_cik[int(cik)] = record
        (gated if reason else ranked).append(record)

    ranked.sort(key=lambda r: r["alpha"], reverse=True)
    gated.sort(key=lambda r: r["alpha"], reverse=True)
    watchlist = ({r["cik"] for r in ranked[:watchlist_size]}
                 | {int(c) for c in pins if c.isdigit()}
                 | {s["cik"] for s in watchlist_specs})
    board = ranked[:leaderboard_size]        # only funds with pages are displayed/linked
    rank_by_cik = {r["cik"]: i + 1 for i, r in enumerate(ranked)}
    specialists = []
    for spec in specialist_specs:
        row = _pin_row(spec, records_by_cik.get(spec["cik"]), rank_by_cik, cfg)
        if row["status"] == "absent":
            log.warning("Pinned specialist CIK %s is absent from performance data: %s",
                        spec["cik"], row["reason"])
        specialists.append(row)
    watchlist_pinned = []
    for spec in watchlist_specs:
        row = _pin_row(spec, records_by_cik.get(spec["cik"]), rank_by_cik, cfg)
        if row["status"] == "absent":
            log.warning("Watchlist CIK %s is absent from performance data: %s",
                        spec["cik"], row["reason"])
        watchlist_pinned.append(row)
    specialist_data = load_json(SPECIALIST_HOLDINGS_PATH) \
        if SPECIALIST_HOLDINGS_PATH.exists() else {"overlap": []}
    all_specialist_overlap = specialist_data.get("overlap", [])
    specialist_overlap = all_specialist_overlap[:cfg.get("specialist_overlap_size", 30)]
    all_watchlist_overlap = specialist_data.get("watchlist_overlap", [])
    watchlist_overlap = all_watchlist_overlap[:cfg.get("specialist_overlap_size", 30)]

    # leaderboard DATA keeps the full ranked set; the page only shows `board` (clickable).
    rankings = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "n_displayed": len(board),
        "leaderboard": [{"cik": r["cik"], "name": r["name"], "alpha": r["alpha"],
                         "cumulative_return": r["cumulative_return"], "spy_return": r["spy_return"],
                         "coverage": r["coverage"], "latest_book": r["latest_book"],
                         "n_periods": r["n_periods"], "hit_rate": r.get("hit_rate")} for r in ranked],
        "watchlist_ciks": sorted(watchlist),
        "watchlist_pinned": watchlist_pinned,
        "specialists": specialists,
        "specialist_overlap": specialist_overlap,
        "n_specialist_overlap": len(all_specialist_overlap),
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
    changes_funds = load_json_gz(CHANGES_PATH).get("funds", {}) if CHANGES_PATH.exists() else {}
    buying_now = _skill_weighted_buys(ranked, changes_funds, limit=15)

    watchlist_records = [records_by_cik[s["cik"]] for s in watchlist_specs
                         if s["cik"] in records_by_cik]
    watchlist_attr = (attr.get("watchlist") or {}).get("stocks") or []
    watchlist_drivers = watchlist_attr[:15] if watchlist_attr else _drivers_from_saved(
        [s["cik"] for s in watchlist_specs], perf, limit=15)
    watchlist_buys = _skill_weighted_buys(watchlist_records, changes_funds, limit=15)

    congress_rank = load_json(DATA_DIR / "rankings.json") if (DATA_DIR / "rankings.json").exists() else {}
    highlight_tickers = _highlight_tickers(
        drivers, buying_now, attr.get("stocks") or [], congress_rank)

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
            watchlist=watchlist, watchlist_pinned=watchlist_pinned, specialists=specialists,
            specialist_overlap=specialist_overlap, n_specialists=len(specialists),
            n_specialist_overlap=len(all_specialist_overlap),
            watchlist_overlap=watchlist_overlap, n_watchlist=len(watchlist_specs),
            n_watchlist_overlap=len(all_watchlist_overlap),
            watchlist_drivers=watchlist_drivers, watchlist_buys=watchlist_buys,
            highlight_tickers=highlight_tickers,
            n_ranked=len(ranked), n_total=len(perf), n_displayed=len(board),
            drivers=drivers, buying_now=buying_now, min_cov=int(100 * min_cov),
            leaderboard_preview=LEADERBOARD_PREVIEW,
            leaderboard_top=board[:LEADERBOARD_PREVIEW],
            leaderboard_rest=board[LEADERBOARD_PREVIEW:],
            snapshot=snapshot, as_of=as_of, archive=archive)

    (DOCS_DIR / "index.html").write_text(render(False, idx))          # live: shows archive
    (DOCS_DIR / "reports" / f"{as_of}.html").write_text(render(True, None))   # dated snapshot
    log.info("Ranked %d funds (%d gated) -> %s, index.html, reports/%s.html",
             len(ranked), len(gated), RANKINGS_PATH.name, as_of)


if __name__ == "__main__":
    run()
