from __future__ import annotations

"""
Render the personal "Decisions" page from a hand-maintained markdown file.

Reads `positions.md` (repo root) — my own Open / Closed stock positions with reasoning —
prices each against the benchmark (SPY), and writes `docs/decisions.html`, grouped into
sections by the quarter each position was OPENED.

Input format (forgiving; one pipe-delimited bullet per position):

    ## Open
    - NVDA | 2025-03-02 | AI capex cycle still early
    - AAPL | 2025-01-15 | Bought the post-earnings dip

    ## Closed
    - TSLA | 2024-06-10 | 2025-02-20 | Took profits, rich valuation

For each Open position we show return-since-open, SPY over the same window, and alpha,
plus the current price. For each Closed position we show performance DURING holding
(open->close) and SINCE closure (close->now), each vs SPY, plus the current price.

Prices reuse the repo's per-ticker aggregate cache; tickers not already cached (personal
names congress hasn't traded) are fetched from Polygon on demand. Returns are per-share
price performance only (no position sizing).

Usage:
  python src/generate_decisions.py [--date YYYY-MM-DD]
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, str(Path(__file__).parent))
from utils import (AGGS_CACHE, ROOT, PolygonClient, load_config, load_json_gz,
                   most_recent_trading_day, parse_date, setup_logging)
from generate_report import money, usd, pct, cls          # shared display helpers
from fetch_charts import _price_on                          # close on/after a date
from compute_performance import _safe_pct                   # window return %

log = setup_logging("generate_decisions")

POSITIONS_PATH = ROOT / "positions.md"
DOCS = ROOT / "docs"
TEMPLATES_DIR = ROOT / "templates"


# --------------------------------------------------------------------------- #
# Parse the markdown positions file
# --------------------------------------------------------------------------- #
_SECTION_RE = re.compile(r"^#+\s*(open|closed)\b", re.I)


def parse_positions(text: str) -> tuple[list, list]:
    """Return (open_positions, closed_positions). Section-driven arity: an Open bullet is
    `TICKER | OPEN_DATE | notes?`; a Closed bullet is `TICKER | OPEN_DATE | CLOSE_DATE | notes?`.
    Notes may contain `|`. Malformed / undated rows are skipped with a warning."""
    section = None
    opens, closed = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1).lower()
            continue
        if section is None or not line.startswith("-"):
            continue
        parts = [p.strip() for p in line[1:].split("|")]
        ticker = parts[0].upper()
        if not ticker:
            continue
        if section == "open":
            open_d = parse_date(parts[1]) if len(parts) > 1 else None
            if not open_d:
                log.warning("Skipping open position (bad/missing open date): %s", line)
                continue
            notes = "|".join(parts[2:]).strip() if len(parts) > 2 else ""
            opens.append({"ticker": ticker, "open_date": open_d, "notes": notes})
        else:
            open_d = parse_date(parts[1]) if len(parts) > 1 else None
            close_d = parse_date(parts[2]) if len(parts) > 2 else None
            if not (open_d and close_d):
                log.warning("Skipping closed position (need open + close dates): %s", line)
                continue
            notes = "|".join(parts[3:]).strip() if len(parts) > 3 else ""
            closed.append({"ticker": ticker, "open_date": open_d,
                           "close_date": close_d, "notes": notes})
    return opens, closed


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #
def _bar_date(b: dict) -> date:
    return datetime.utcfromtimestamp(b["t"] / 1000).date()


def _load_bars(poly, ticker: str, start_iso: str, today_iso: str) -> dict:
    """{date: close} for `ticker` over [start, today]. Uses the shared aggregate cache;
    fetches (or extends, if the cached series doesn't reach `start`) via Polygon when a key
    is available, else falls back to whatever is cached (offline / no-key)."""
    bars: list = []
    if poly is not None:
        bars = poly.aggregates(ticker, start_iso, today_iso, use_cache=True) or []
        # Cached series may only cover the 2-yr pipeline window; force a full fetch if it
        # doesn't reach back to an older open date.
        if bars and _bar_date(bars[0]) > date.fromisoformat(start_iso):
            bars = poly.aggregates(ticker, start_iso, today_iso, use_cache=False) or []
    if not bars:
        path = AGGS_CACHE / f"{ticker}.json.gz"
        if path.exists():
            bars = load_json_gz(path) or []
    return {_bar_date(b): b["c"] for b in bars if b.get("c") is not None}


def _latest(bars_by_date: dict):
    return bars_by_date[max(bars_by_date)] if bars_by_date else None


def _delta(a, b):
    return (a - b) if (a is not None and b is not None) else None


def _held(a: date, b: date) -> str:
    """Compact holding duration, e.g. '18d', '7mo', '2y 3mo'."""
    days = (b - a).days
    if days < 0:
        return ""
    if days < 60:
        return f"{days}d"
    if days < 730:
        return f"{days // 30}mo"
    return f"{days // 365}y {(days % 365) // 30}mo"


# --------------------------------------------------------------------------- #
# Grouping + rendering
# --------------------------------------------------------------------------- #
def _quarter(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def _by_quarter(rows: list) -> list:
    """Group rows by open-date quarter, newest quarter first, newest position first."""
    g: dict = defaultdict(list)
    for r in rows:
        g[_quarter(r["open_date"])].append(r)
    return [{"quarter": q, "rows": sorted(g[q], key=lambda r: r["open_date"], reverse=True)}
            for q in sorted(g, reverse=True)]


def _avg(vals: list):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
    env.globals.update(money=money, usd=usd, pct=pct, cls=cls)
    return env


def _latest_report_id():
    """Newest congress report stem, so base.html's Leaderboard nav link resolves."""
    rdir = DOCS / "reports"
    stems = sorted(p.stem for p in rdir.glob("*.html")) if rdir.exists() else []
    return stems[-1] if stems else None


def run(today: date | None = None) -> None:
    cfg = load_config()
    benchmark = cfg["pipeline"]["benchmark_ticker"]
    today = most_recent_trading_day(today)
    today_iso = today.isoformat()

    if not POSITIONS_PATH.exists():
        log.warning("No positions file at %s — skipping decisions page.", POSITIONS_PATH)
        return
    opens, closed = parse_positions(POSITIONS_PATH.read_text(encoding="utf-8"))
    if not opens and not closed:
        log.warning("No positions parsed from %s — skipping decisions page.", POSITIONS_PATH)
        return

    all_pos = opens + closed
    start_iso = min(p["open_date"] for p in all_pos).isoformat()

    key = os.environ.get("POLYGON_API_KEY", "")
    poly = PolygonClient(key, cfg["polygon"]) if key else None
    if poly is None:
        log.warning("No POLYGON_API_KEY — pricing from cached bars only; uncached tickers blank.")

    tickers = {p["ticker"] for p in all_pos} | {benchmark}
    bars = {t: _load_bars(poly, t, start_iso, today_iso) for t in tickers}
    spy = bars.get(benchmark, {})
    spy_now = _latest(spy)

    def enrich_open(p):
        b = bars.get(p["ticker"], {})
        entry, current = _price_on(b, p["open_date"]), _latest(b)
        ret = _safe_pct(current, entry)
        spy_ret = _safe_pct(spy_now, _price_on(spy, p["open_date"]))
        return {**p, "entry": entry, "current": current, "ret": ret, "spy_ret": spy_ret,
                "alpha": _delta(ret, spy_ret), "held": _held(p["open_date"], today)}

    def enrich_closed(p):
        b = bars.get(p["ticker"], {})
        entry = _price_on(b, p["open_date"])
        close_px = _price_on(b, p["close_date"])
        current = _latest(b)
        ret_h = _safe_pct(close_px, entry)
        spy_h = _safe_pct(_price_on(spy, p["close_date"]), _price_on(spy, p["open_date"]))
        ret_s = _safe_pct(current, close_px)
        spy_s = _safe_pct(spy_now, _price_on(spy, p["close_date"]))
        return {**p, "entry": entry, "close_px": close_px, "current": current,
                "ret_held": ret_h, "spy_held": spy_h, "alpha_held": _delta(ret_h, spy_h),
                "ret_since": ret_s, "spy_since": spy_s, "alpha_since": _delta(ret_s, spy_s),
                "held": _held(p["open_date"], p["close_date"])}

    open_rows = [enrich_open(p) for p in opens]
    closed_rows = [enrich_closed(p) for p in closed]
    stats = {
        "avg_alpha_open": _avg([r["alpha"] for r in open_rows]),
        "avg_alpha_closed": _avg([r["alpha_held"] for r in closed_rows]),
    }

    DOCS.mkdir(parents=True, exist_ok=True)
    html = _env().get_template("decisions.html").render(
        root="", latest_report=_latest_report_id(), benchmark=benchmark,
        generated=today_iso, n_open=len(open_rows), n_closed=len(closed_rows),
        open_quarters=_by_quarter(open_rows), closed_quarters=_by_quarter(closed_rows),
        stats=stats,
    )
    (DOCS / "decisions.html").write_text(html, encoding="utf-8")
    log.info("Decisions: %d open, %d closed positions across %d tickers -> %s",
             len(open_rows), len(closed_rows), len(tickers), DOCS / "decisions.html")


def main() -> None:
    ap = argparse.ArgumentParser(description="Render the personal Decisions page.")
    ap.add_argument("--date", type=parse_date, default=None, help="as-of date (default: today)")
    args = ap.parse_args()
    run(args.date)


if __name__ == "__main__":
    main()
