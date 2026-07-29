from __future__ import annotations

"""Generate compact, deterministic, full-resolution SVG price charts.

Matplotlib's SVG output averaged about 50 KB per chart and changed thousands of
lines whenever the rolling price window moved. This renderer keeps every daily
point but emits only the SVG primitives the page needs, typically reducing each
chart to a few kilobytes and making Git deltas substantially smaller.

Congressional pages receive disclosure-date buy/sell markers. Hedge-only pages
receive the same price history without a misleading marker legend.
"""

import html
import sys
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from stock_universe import stock_page_tickers
from utils import AGGS_CACHE, DATA_DIR, ROOT, load_json, load_json_gz, parse_date, setup_logging

log = setup_logging("fetch_charts")

LEDGER_PATH = DATA_DIR / "transactions.json"
COMPANY_INFO_PATH = DATA_DIR / "company_info.json"
CHARTS_DIR = ROOT / "docs" / "assets" / "charts"

WIDTH, HEIGHT = 900, 360
LEFT, RIGHT, TOP, BOTTOM = 68, 20, 52, 42
PLOT_W, PLOT_H = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM


def _markers_for(ledger_rows: list[dict]) -> tuple[list, list]:
    buys, sells = [], []
    for row in ledger_rows:
        disclosed = parse_date(row.get("disclosure_date"))
        if not disclosed:
            continue
        if row.get("tx_type") == "P":
            buys.append(disclosed)
        elif row.get("tx_type") == "S":
            sells.append(disclosed)
    return sorted(set(buys)), sorted(set(sells))


def _price_label(value: float) -> str:
    if abs(value) >= 1000:
        return f"${value:,.0f}"
    if abs(value) >= 100:
        return f"${value:.0f}"
    return f"${value:.2f}"


def _point_on_or_after(dates: list, closes: list[float], day):
    index = bisect_left(dates, day)
    return (dates[index], closes[index]) if index < len(dates) else None


def render_svg(ticker: str, bars: list[dict], buys: list, sells: list,
               company_name: str | None) -> str | None:
    """Return a standalone SVG string, retaining one point per valid daily bar."""
    valid = [bar for bar in bars if bar.get("t") is not None and bar.get("c") is not None]
    if not valid:
        return None
    valid.sort(key=lambda bar: bar["t"])
    dates = [datetime.utcfromtimestamp(bar["t"] / 1000).date() for bar in valid]
    closes = [float(bar["c"]) for bar in valid]
    ordinals = [day.toordinal() for day in dates]

    x0, x1 = ordinals[0], ordinals[-1]
    ymin, ymax = min(closes), max(closes)
    if ymin == ymax:
        pad = max(abs(ymin) * 0.05, 1.0)
    else:
        pad = (ymax - ymin) * 0.06
    ymin, ymax = ymin - pad, ymax + pad

    def x(day) -> float:
        span = x1 - x0
        return LEFT + (day.toordinal() - x0) * PLOT_W / span if span else LEFT + PLOT_W / 2

    def y(value: float) -> float:
        return TOP + (ymax - value) * PLOT_H / (ymax - ymin)

    points = " ".join(f"{x(day):.1f},{y(value):.1f}" for day, value in zip(dates, closes))
    title = ticker if not company_name else f"{ticker} — {company_name}"
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" '
        f'aria-labelledby="title-{html.escape(ticker)}">',
        f'<title id="title-{html.escape(ticker)}">{html.escape(title)} closing-price history</title>',
        '<rect width="100%" height="100%" fill="#fff"/>',
        f'<text x="{LEFT}" y="25" fill="#24292f" font-family="system-ui,sans-serif" '
        f'font-size="16" font-weight="600">{html.escape(title[:80])}</text>',
    ]

    # Five horizontal guides with compact price labels.
    for i in range(5):
        value = ymin + (ymax - ymin) * i / 4
        yy = y(value)
        lines.append(
            f'<line x1="{LEFT}" y1="{yy:.1f}" x2="{LEFT + PLOT_W}" y2="{yy:.1f}" '
            'stroke="#eaeef2" stroke-width="1"/>'
        )
        lines.append(
            f'<text x="{LEFT - 8}" y="{yy + 3:.1f}" text-anchor="end" fill="#57606a" '
            f'font-family="system-ui,sans-serif" font-size="11">{_price_label(value)}</text>'
        )

    # Five date labels, anchored at actual observations.
    indexes = sorted({round(i * (len(dates) - 1) / 4) for i in range(5)})
    for index in indexes:
        day = dates[index]
        lines.append(
            f'<text x="{x(day):.1f}" y="{TOP + PLOT_H + 24}" text-anchor="middle" '
            f'fill="#57606a" font-family="system-ui,sans-serif" font-size="11">'
            f'{day.strftime("%b %Y")}</text>'
        )

    lines.append(
        f'<polyline points="{points}" fill="none" stroke="#1f6feb" stroke-width="2" '
        'stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>'
    )

    lo, hi = dates[0], dates[-1]
    for kind, marker_days, color in (("buy", buys, "#1a7f37"), ("sell", sells, "#cf222e")):
        for marker_day in marker_days:
            if not lo <= marker_day <= hi:
                continue
            point = _point_on_or_after(dates, closes, marker_day)
            if not point:
                continue
            day, value = point
            xx, yy = x(day), y(value)
            if kind == "buy":
                coords = f"{xx:.1f},{yy - 7:.1f} {xx - 6:.1f},{yy + 5:.1f} {xx + 6:.1f},{yy + 5:.1f}"
            else:
                coords = f"{xx:.1f},{yy + 7:.1f} {xx - 6:.1f},{yy - 5:.1f} {xx + 6:.1f},{yy - 5:.1f}"
            lines.append(
                f'<polygon points="{coords}" fill="{color}" stroke="#fff" stroke-width="1"/>'
            )

    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def make_chart(ticker: str, bars: list[dict], buys: list, sells: list,
               company_name: str | None) -> bool:
    svg = render_svg(ticker, bars, buys, sells, company_name)
    if not svg:
        return False
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    path = CHARTS_DIR / f"{ticker}.svg"
    if not path.exists() or path.read_text() != svg:
        path.write_text(svg, encoding="utf-8")
    return True


def _prune_stale_charts(keep: set[str]) -> int:
    if not CHARTS_DIR.exists():
        return 0
    removed = 0
    for path in CHARTS_DIR.glob("*.svg"):
        if path.stem not in keep:
            path.unlink()
            removed += 1
    return removed


def run() -> None:
    if not LEDGER_PATH.exists():
        log.error("No ledger; run fetchers first")
        return
    ledger = load_json(LEDGER_PATH)
    info = load_json(COMPANY_INFO_PATH) if COMPANY_INFO_PATH.exists() else {}
    page_tickers = stock_page_tickers(ledger)

    rows_by_ticker = defaultdict(list)
    for row in ledger.values():
        rows_by_ticker[row["ticker"]].append(row)

    made, skipped = set(), 0
    for ticker in sorted(page_tickers):
        path = AGGS_CACHE / f"{ticker}.json.gz"
        if not path.exists():
            skipped += 1
            continue
        bars = load_json_gz(path)
        buys, sells = _markers_for(rows_by_ticker.get(ticker, []))
        name = (info.get(ticker) or {}).get("name")
        if make_chart(ticker, bars, buys, sells, name):
            made.add(ticker)
        else:
            skipped += 1

    # Prune only charts whose page is no longer eligible. A missing local aggregate
    # cache can be temporary (notably on a cold clone because that cache is ignored);
    # it should not delete an otherwise valid published chart.
    pruned = _prune_stale_charts(page_tickers)
    log.info("Charts: %d generated, %d skipped (no bars), %d stale charts pruned",
             len(made), skipped, pruned)


if __name__ == "__main__":
    run()
