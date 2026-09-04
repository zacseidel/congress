from __future__ import annotations

"""
Render the personal "Decisions" page from a hand-maintained markdown file.

Reads `positions.md` (repo root) — buckets of Open / Closed stock positions with
reasoning — prices each against the benchmark (SPY), and writes `docs/decisions.html`.

Each `## Bucket` is a subportfolio. Under it, `### Open` / `### Closed` list names.
`## Archive` is a sentinel at the same heading level: cut a bullet or a whole
`## Bucket` block and paste it below Archive to file it away. The page collapses
Archive by default.

Category cards use the same analytics as the eval strategy cards (equal-weight
NAV rebalanced on open/close dates; 12M/3M price return and Sharpe vs SPY;
half-Kelly from closed trades). Post-close performance of sold names is a
separate, collapsible card per category.

Input format:

    ## Watchlist
    ### Open
    - NVDA | 2025-03-02 | AI capex cycle still early
    ### Closed
    - TSLA | 2024-06-10 | 2025-02-20 | Took profits

    ## Archive
    - MSFT | 2024-03-05 | 2024-12-18 | Rotated to cash

Legacy `## Open` / `## Closed` files still parse into a single Positions bucket.

Prices reuse the repo's per-ticker aggregate cache; tickers not already cached
are fetched from Polygon on demand. Returns are per-share price performance
(no share counts); category cards are equal-weight paper portfolios.

Usage:
  python src/generate_decisions.py [--date YYYY-MM-DD]
"""

import argparse
import math
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, str(Path(__file__).parent))
from utils import (AGGS_CACHE, ROOT, PolygonClient, load_config, load_json_gz,
                   most_recent_trading_day, parse_date, setup_logging)
from generate_report import _copy_static_assets, cls, money, pct, usd
from compute_performance import _safe_pct

log = setup_logging("generate_decisions")

POSITIONS_PATH = ROOT / "positions.md"
DOCS = ROOT / "docs"
TEMPLATES_DIR = ROOT / "templates"

KELLY_MIN_CLOSED_TRADES = 20
RISK_FREE_RATE_ANNUAL = 0.05
SHARPE_MIN_12M = 200
SHARPE_MIN_3M = 40

_H2_RE = re.compile(r"^##\s+(.+?)\s*$")
_H3_RE = re.compile(r"^###\s+(.+?)\s*$")


# --------------------------------------------------------------------------- #
# Parse the markdown positions file
# --------------------------------------------------------------------------- #
def _is_archive(name: str) -> bool:
    return name.strip().lower() == "archive"


def _open_closed(name: str) -> str | None:
    key = name.strip().lower()
    if key.startswith("open"):
        return "open"
    if key.startswith("closed"):
        return "closed"
    return None


def _new_category(name: str, archived: bool) -> dict:
    return {"name": name, "archived": archived, "opens": [], "closed": []}


def _parse_bullet(line: str, section: str | None) -> dict | None:
    """Parse a pipe-delimited position bullet. `section` forces arity; None auto-detects
    closed vs open from whether the third field is a date (so Archive pastes just work)."""
    parts = [p.strip() for p in line[1:].split("|")]
    ticker = parts[0].upper()
    if not re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", ticker or ""):
        return None
    open_d = parse_date(parts[1]) if len(parts) > 1 else None
    close_d = parse_date(parts[2]) if len(parts) > 2 else None

    if section == "open":
        if not open_d:
            log.warning("Skipping open position (bad/missing open date): %s", line)
            return None
        notes = "|".join(parts[2:]).strip() if len(parts) > 2 else ""
        return {"ticker": ticker, "open_date": open_d, "close_date": None,
                "notes": notes, "status": "open"}
    if section == "closed":
        if not (open_d and close_d):
            log.warning("Skipping closed position (need open + close dates): %s", line)
            return None
        notes = "|".join(parts[3:]).strip() if len(parts) > 3 else ""
        return {"ticker": ticker, "open_date": open_d, "close_date": close_d,
                "notes": notes, "status": "closed"}

    if open_d and close_d:
        notes = "|".join(parts[3:]).strip() if len(parts) > 3 else ""
        return {"ticker": ticker, "open_date": open_d, "close_date": close_d,
                "notes": notes, "status": "closed"}
    if open_d:
        notes = "|".join(parts[2:]).strip() if len(parts) > 2 else ""
        return {"ticker": ticker, "open_date": open_d, "close_date": None,
                "notes": notes, "status": "open"}
    log.warning("Skipping position (bad/missing dates): %s", line)
    return None


def parse_positions(text: str) -> list[dict]:
    """Return categories in markdown order.

    `## Name` starts a bucket. `### Open` / `### Closed` (or legacy `## Open` /
    `## Closed`) select which list a bullet joins. `## Archive` is a sentinel:
    every bucket after it is archived. Bullets pasted directly under Archive
    auto-detect open vs closed from the date fields.
    """
    categories: list[dict] = []
    current: dict | None = None
    section: str | None = None
    in_archive = False

    def ensure(name: str) -> dict:
        nonlocal current
        current = _new_category(name, in_archive)
        categories.append(current)
        return current

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        h2 = _H2_RE.match(line)
        if h2:
            title = h2.group(1).strip()
            oc = _open_closed(title)
            if _is_archive(title):
                in_archive = True
                current = ensure("Archive")
                section = None
                continue
            if oc:
                if current is None:
                    ensure("Positions")
                section = oc
                continue
            current = ensure(title)
            section = None
            continue
        h3 = _H3_RE.match(line)
        if h3:
            oc = _open_closed(h3.group(1))
            if oc and current is not None:
                section = oc
            continue
        if not line.startswith("-"):
            continue
        if current is None:
            if not in_archive:
                continue
            ensure("Archive")
        row = _parse_bullet(line, section)
        if row is None:
            continue
        row["category"] = current["name"]
        row["archived"] = current["archived"]
        if row["status"] == "closed":
            current["closed"].append(row)
        else:
            current["opens"].append(row)

    return categories


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
        if bars and _bar_date(bars[0]) > date.fromisoformat(start_iso):
            bars = poly.aggregates(ticker, start_iso, today_iso, use_cache=False) or []
    if not bars:
        path = AGGS_CACHE / f"{ticker}.json.gz"
        if path.exists():
            bars = load_json_gz(path) or []
    return {_bar_date(b): b["c"] for b in bars if b.get("c") is not None}


def _latest(bars_by_date: dict):
    return bars_by_date[max(bars_by_date)] if bars_by_date else None


def _price_on(bars_by_date: dict, day: date):
    """Return the first available close on or after ``day``."""
    trading_day = next((d for d in sorted(bars_by_date) if d >= day), None)
    return bars_by_date[trading_day] if trading_day is not None else None


def _first_bar_on_or_after(bars_by_date: dict, day: date):
    return next((d for d in sorted(bars_by_date) if d >= day), None)


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


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "bucket"


# --------------------------------------------------------------------------- #
# Category analytics (eval card math)
# --------------------------------------------------------------------------- #
def compute_trade_stats(returns: list) -> dict:
    """Summarize closed outcomes and estimate a long-only half-Kelly risk fraction.

    Same rules as eval: winners > 0, losers < 0, break-evens excluded from the
    odds; half-Kelly is `0.5 × max(0, p − q/b)` once there are 20 closed trades
    with both a win and a loss.
    """
    closed = [r for r in returns if r is not None]
    winners = [r for r in closed if r > 0]
    losers = [r for r in closed if r < 0]
    breakeven_count = len(closed) - len(winners) - len(losers)
    decided_count = len(winners) + len(losers)
    average_win = sum(winners) / len(winners) if winners else None
    average_loss = sum(losers) / len(losers) if losers else None
    payoff_ratio = (
        average_win / abs(average_loss)
        if average_win is not None and average_loss is not None and average_loss != 0
        else None
    )

    full_kelly = None
    half_kelly = None
    if (
        len(closed) >= KELLY_MIN_CLOSED_TRADES
        and decided_count > 0
        and payoff_ratio is not None
        and payoff_ratio > 0
    ):
        win_probability = len(winners) / decided_count
        loss_probability = len(losers) / decided_count
        full_kelly = win_probability - loss_probability / payoff_ratio
        half_kelly = max(0.0, full_kelly / 2)

    if len(closed) < KELLY_MIN_CLOSED_TRADES:
        kelly_status = "insufficient_sample"
    elif payoff_ratio is None or decided_count == 0:
        kelly_status = "needs_winners_and_losers"
    else:
        kelly_status = "calculated"

    return {
        "closed_count": len(closed),
        "winner_count": len(winners),
        "loser_count": len(losers),
        "breakeven_count": breakeven_count,
        "winner_pct": round(len(winners) / len(closed) * 100, 2) if closed else None,
        "loser_pct": round(len(losers) / len(closed) * 100, 2) if closed else None,
        "average_win_pct": round(average_win, 2) if average_win is not None else None,
        "average_loss_pct": round(average_loss, 2) if average_loss is not None else None,
        "payoff_ratio": round(payoff_ratio, 4) if payoff_ratio is not None else None,
        "full_kelly_pct": round(full_kelly * 100, 2) if full_kelly is not None else None,
        "half_kelly_pct": round(half_kelly * 100, 2) if half_kelly is not None else None,
        "minimum_closed_trades": KELLY_MIN_CLOSED_TRADES,
        "minimum_sample_met": len(closed) >= KELLY_MIN_CLOSED_TRADES,
        "kelly_status": kelly_status,
    }


def _kelly_note(stats: dict) -> str:
    if stats["kelly_status"] == "needs_winners_and_losers":
        return f"{stats['closed_count']} closed trades · needs both wins and losses"
    if stats["half_kelly_pct"] is None:
        return (f"Needs at least {stats['minimum_closed_trades']} closed trades "
                f"({stats['closed_count']} available)")
    be = f" · {stats['breakeven_count']} break-even" if stats["breakeven_count"] else ""
    if stats["half_kelly_pct"] == 0 and (stats["full_kelly_pct"] or 0) <= 0:
        return f"{stats['closed_count']} closed trades{be} · no positive historical Kelly edge"
    return f"{stats['closed_count']} closed trades{be} · historical estimate"


def _add_return_windows(series: list[dict]) -> None:
    for index, point in enumerate(series):
        current_date = date.fromisoformat(point["date"])
        for days, field, benchmark_field in (
            (91, "rolling_3m", "spy_rolling_3m"),
            (365, "return_12m", "spy_12m"),
        ):
            cutoff = (current_date - timedelta(days=days)).isoformat()
            past = next(
                (series[j] for j in range(index - 1, -1, -1) if series[j]["date"] <= cutoff),
                None,
            )
            if past is None:
                point[field] = None
                point[benchmark_field] = None
                continue
            point[field] = round((point["value"] / past["value"] - 1) * 100, 2)
            point[benchmark_field] = round(
                (point["spy_value"] / past["spy_value"] - 1) * 100, 2
            )


def _return_summary(series: list[dict], field: str = "value"):
    """Match eval: 12M when a full year exists, otherwise since the first point."""
    if not series or len(series) < 2:
        return None, "Price Return"
    first, last = series[0], series[-1]
    if last.get(field) is None:
        return None, "Price Return"
    if field == "value" and last.get("return_12m") is not None:
        return last["return_12m"], "12M Price"
    if field == "spy_value" and last.get("spy_12m") is not None:
        return last["spy_12m"], "12M Price"
    return (last[field] / first[field] - 1) * 100, f"Since {first['date']}"


def _sharpe_from_series(series: list[dict], cutoff: str, min_returns: int):
    values = [point["value"] for point in series if point["date"] >= cutoff]
    returns = [
        math.log(values[i] / values[i - 1])
        for i in range(1, len(values))
        if values[i] > 0 and values[i - 1] > 0
    ]
    if len(returns) < min_returns:
        return None
    daily_rfr = math.log1p(RISK_FREE_RATE_ANNUAL) / 252
    mean_return = sum(returns) / len(returns)
    variance = sum((value - mean_return) ** 2 for value in returns) / (len(returns) - 1)
    if variance <= 0:
        return None
    return round((mean_return - daily_rfr) / math.sqrt(variance) * math.sqrt(252), 2)


def compute_sharpe(series: list[dict], as_of: date) -> dict:
    cutoff_12m = (as_of - timedelta(days=365)).isoformat()
    cutoff_3m = (as_of - timedelta(days=91)).isoformat()
    return {
        "12m": _sharpe_from_series(series, cutoff_12m, SHARPE_MIN_12M),
        "3m": _sharpe_from_series(series, cutoff_3m, SHARPE_MIN_3M),
    }


def build_nav_series(book: list[dict], bars_by_ticker: dict, spy: dict,
                     today: date) -> list[dict]:
    """Equal-weight paper portfolio, rebalanced only on entry/exit dates.

    Same policy as eval: on an event day, sell exits at that close, then split
    remaining NAV equally across names that are still active after the event.
    Daily marks use the last available close. Missing a ticker on an event day
    drops it from that rebalance rather than failing the page.
    """
    book = [p for p in book if p.get("entry_date") is not None]
    if not book or not spy:
        return []
    start = min(p["entry_date"] for p in book)
    days = [d for d in sorted(spy) if start <= d <= today]
    if not days:
        return []
    spy_base = spy.get(days[0])
    if not spy_base:
        return []

    id_to_ticker = {p["id"]: p["ticker"] for p in book}
    entries_by_date: dict = defaultdict(list)
    exits_by_date: dict = defaultdict(list)
    for p in book:
        entries_by_date[p["entry_date"]].append(p)
        if p.get("exit_date"):
            exits_by_date[p["exit_date"]].append(p)

    cash = 100.0
    holdings: dict[str, float] = {}
    last_closes: dict[str, float] = {}
    series = []

    def mark_holdings():
        return cash + sum(
            shares * last_closes[id_to_ticker[pid]]
            for pid, shares in holdings.items()
            if id_to_ticker[pid] in last_closes
        )

    for day in days:
        for ticker, tbar in bars_by_ticker.items():
            px = tbar.get(day)
            if px is not None:
                last_closes[ticker] = px

        day_entries = entries_by_date.get(day, [])
        day_exits = exits_by_date.get(day, [])
        if day_entries or day_exits:
            for p in day_exits:
                shares = holdings.pop(p["id"], 0.0)
                px = last_closes.get(p["ticker"])
                if px is not None:
                    cash += shares * px

            active = [
                p for p in book
                if p["entry_date"] <= day
                and (p.get("exit_date") is None or p["exit_date"] > day)
                and last_closes.get(p["ticker"]) is not None
            ]
            nav_at_exec = mark_holdings()
            if active:
                target = nav_at_exec / len(active)
                holdings = {
                    p["id"]: target / last_closes[p["ticker"]]
                    for p in active
                }
                cash = 0.0
            else:
                holdings = {}
                cash = nav_at_exec

        spy_px = spy.get(day)
        if spy_px is None:
            continue
        series.append({
            "date": day.isoformat(),
            "value": round(mark_holdings(), 4),
            "spy_value": round(spy_px / spy_base * 100, 4),
            "rolling_3m": None,
            "spy_rolling_3m": None,
            "return_12m": None,
            "spy_12m": None,
        })

    _add_return_windows(series)
    return series


def _spy_nav_series(spy: dict, today: date) -> list[dict]:
    days = [d for d in sorted(spy) if d <= today]
    if not days:
        return []
    base = spy[days[0]]
    series = [{
        "date": d.isoformat(),
        "value": round(spy[d] / base * 100, 4),
        "spy_value": round(spy[d] / base * 100, 4),
        "rolling_3m": None,
        "spy_rolling_3m": None,
        "return_12m": None,
        "spy_12m": None,
    } for d in days]
    _add_return_windows(series)
    return series


def build_analytics_card(series: list[dict], spy_sharpe: dict, open_rows: list,
                         closed_rows: list, closed_returns: list, as_of: date,
                         mode: str = "live") -> dict:
    ret, ret_label = _return_summary(series, "value")
    spy_ret, _ = _return_summary(series, "spy_value")
    last = series[-1] if series else None
    sharpe = compute_sharpe(series, as_of) if series else {"12m": None, "3m": None}
    stats = compute_trade_stats(closed_returns)
    tags = (open_rows if mode == "live" else closed_rows)
    return {
        "mode": mode,
        "return_label": ret_label,
        "ret12m": ret,
        "spy12m": spy_ret,
        "ret3m": last["rolling_3m"] if last else None,
        "spy3m": last["spy_rolling_3m"] if last else None,
        "sharpe12m": sharpe["12m"],
        "sharpe3m": sharpe["3m"],
        "spy_sharpe12m": spy_sharpe.get("12m"),
        "spy_sharpe3m": spy_sharpe.get("3m"),
        "n_open": len(open_rows),
        "n_closed": len(closed_rows),
        "open_tickers": [r["ticker"] for r in tags],
        "trade_stats": stats,
        "kelly_note": _kelly_note(stats),
        "has_series": len(series) >= 2,
    }


# --------------------------------------------------------------------------- #
# Enrichment + rendering
# --------------------------------------------------------------------------- #
def sharpe_fmt(v):
    return "—" if v is None else f"{v:.2f}"


def sharpe_cls(v):
    if v is None:
        return "neutral"
    if v >= 1:
        return "pos"
    if v < 0:
        return "neg"
    return "neutral"


def stat_pct(v, signed=False):
    if v is None:
        return "—"
    sign = "+" if signed and v > 0 else ""
    return f"{sign}{v:.1f}%"


def kelly_fmt(v):
    return "—" if v is None else f"{v:.1f}%"


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True,
                      trim_blocks=True, lstrip_blocks=True)
    env.globals.update(money=money, usd=usd, pct=pct, cls=cls,
                       sharpe_fmt=sharpe_fmt, sharpe_cls=sharpe_cls,
                       stat_pct=stat_pct, kelly_fmt=kelly_fmt)
    return env


def _latest_report_id():
    """Newest congress report stem, so base.html's Leaderboard nav link resolves."""
    rdir = DOCS / "reports"
    stems = sorted(p.stem for p in rdir.glob("*.html")) if rdir.exists() else []
    return stems[-1] if stems else None


def _enrich_open(p, bars, spy, spy_now, today, pid):
    b = bars.get(p["ticker"], {})
    entry_date = _first_bar_on_or_after(b, p["open_date"])
    entry, current = _price_on(b, p["open_date"]), _latest(b)
    ret = _safe_pct(current, entry)
    spy_ret = _safe_pct(spy_now, _price_on(spy, p["open_date"]))
    return {**p, "id": pid, "entry": entry, "current": current, "ret": ret,
            "spy_ret": spy_ret, "alpha": _delta(ret, spy_ret),
            "held": _held(p["open_date"], today),
            "entry_date": entry_date, "exit_date": None}


def _enrich_closed(p, bars, spy, spy_now, today, pid):
    b = bars.get(p["ticker"], {})
    entry_date = _first_bar_on_or_after(b, p["open_date"])
    exit_date = _first_bar_on_or_after(b, p["close_date"]) if p.get("close_date") else None
    entry = _price_on(b, p["open_date"])
    close_px = _price_on(b, p["close_date"])
    current = _latest(b)
    ret_h = _safe_pct(close_px, entry)
    spy_h = _safe_pct(_price_on(spy, p["close_date"]), _price_on(spy, p["open_date"]))
    ret_s = _safe_pct(current, close_px)
    spy_s = _safe_pct(spy_now, _price_on(spy, p["close_date"]))
    return {**p, "id": pid, "entry": entry, "close_px": close_px, "current": current,
            "ret_held": ret_h, "spy_held": spy_h, "alpha_held": _delta(ret_h, spy_h),
            "ret_since": ret_s, "spy_since": spy_s, "alpha_since": _delta(ret_s, spy_s),
            "held": _held(p["open_date"], p["close_date"]),
            "since_held": _held(p["close_date"], today),
            "entry_date": entry_date, "exit_date": exit_date}


def _assemble_category(cat, bars, spy, spy_now, spy_sharpe, today) -> dict:
    open_rows = sorted(cat["open_rows"], key=lambda r: r["open_date"], reverse=True)
    closed_rows = sorted(cat["closed_rows"], key=lambda r: r["close_date"], reverse=True)
    live_book = [
        {"id": r["id"], "ticker": r["ticker"],
         "entry_date": r["entry_date"], "exit_date": r["exit_date"]}
        for r in open_rows + closed_rows
    ]
    live_series = build_nav_series(live_book, bars, spy, today)
    live_card = build_analytics_card(
        live_series, spy_sharpe, open_rows, closed_rows,
        [r["ret_held"] for r in closed_rows], today, mode="live",
    ) if open_rows or closed_rows else None

    ghost_book = [
        {"id": f"g{r['id']}", "ticker": r["ticker"],
         "entry_date": r["exit_date"], "exit_date": None}
        for r in closed_rows if r.get("exit_date")
    ]
    ghost_series = build_nav_series(ghost_book, bars, spy, today)
    after_card = build_analytics_card(
        ghost_series, spy_sharpe, [], closed_rows,
        [r["ret_since"] for r in closed_rows], today, mode="after_close",
    ) if closed_rows else None

    return {
        "name": cat["name"],
        "slug": _slug(cat["name"]),
        "archived": cat["archived"],
        "open_rows": open_rows,
        "closed_rows": closed_rows,
        "card": live_card,
        "after_card": after_card,
    }


def run(today: date | None = None) -> None:
    cfg = load_config()
    benchmark = cfg["pipeline"]["benchmark_ticker"]
    today = most_recent_trading_day(today)
    today_iso = today.isoformat()

    if not POSITIONS_PATH.exists():
        log.warning("No positions file at %s — skipping decisions page.", POSITIONS_PATH)
        return
    categories = parse_positions(POSITIONS_PATH.read_text(encoding="utf-8"))
    all_pos = [p for c in categories for p in c["opens"] + c["closed"]]
    if not categories:
        log.warning("No categories parsed from %s — skipping decisions page.", POSITIONS_PATH)
        return

    start_iso = (min(p["open_date"] for p in all_pos) if all_pos else today).isoformat()

    key = os.environ.get("POLYGON_API_KEY", "")
    poly = PolygonClient(key, cfg["polygon"]) if key else None
    if poly is None:
        log.warning("No POLYGON_API_KEY — pricing from cached bars only; uncached tickers blank.")

    tickers = {p["ticker"] for p in all_pos} | {benchmark}
    bars = {t: _load_bars(poly, t, start_iso, today_iso) for t in tickers}
    spy = bars.get(benchmark, {})
    spy_now = _latest(spy)
    spy_series = _spy_nav_series(spy, today)
    spy_sharpe = compute_sharpe(spy_series, today) if spy_series else {"12m": None, "3m": None}

    next_id = 0
    rendered = []
    for cat in categories:
        open_rows = []
        closed_rows = []
        for p in cat["opens"]:
            open_rows.append(_enrich_open(p, bars, spy, spy_now, today, f"o{next_id}"))
            next_id += 1
        for p in cat["closed"]:
            closed_rows.append(_enrich_closed(p, bars, spy, spy_now, today, f"c{next_id}"))
            next_id += 1
        rendered.append(_assemble_category(
            {**cat, "open_rows": open_rows, "closed_rows": closed_rows},
            bars, spy, spy_now, spy_sharpe, today,
        ))

    live = [c for c in rendered if not c["archived"]]
    archived = [c for c in rendered if c["archived"]]
    n_open = sum(len(c["open_rows"]) for c in live)
    n_closed = sum(len(c["closed_rows"]) for c in live)
    n_archived = sum(len(c["open_rows"]) + len(c["closed_rows"]) for c in archived)

    _copy_static_assets()
    DOCS.mkdir(parents=True, exist_ok=True)
    html = _env().get_template("decisions.html").render(
        root="", latest_report=_latest_report_id(), benchmark=benchmark,
        generated=today_iso, live=live, archived=archived,
        n_open=n_open, n_closed=n_closed, n_buckets=len(live),
        n_archived=n_archived,
    )
    (DOCS / "decisions.html").write_text(html, encoding="utf-8")
    log.info("Decisions: %d buckets, %d open, %d closed, %d archived -> %s",
             len(live), n_open, n_closed, n_archived, DOCS / "decisions.html")


def main() -> None:
    ap = argparse.ArgumentParser(description="Render the personal Decisions page.")
    ap.add_argument("--date", type=parse_date, default=None, help="as-of date (default: today)")
    args = ap.parse_args()
    run(args.date)


if __name__ == "__main__":
    main()
