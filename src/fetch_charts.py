from __future__ import annotations

"""
Generate a 2-year price chart per enriched ticker, annotated with green
purchase markers and red sale markers on each member's disclosure date.

Reads cached daily bars (data/cache/aggs/{ticker}.json) written by enrich.py —
makes no Polygon calls itself. Charts for tickers without cached bars are skipped.

Output: docs/assets/charts/{ticker}.svg

SVG, not PNG, on purpose: these are regenerated every run and committed. The bars are
a rolling 2-year window, so nearly every chart changes daily. PNG is already
DEFLATE-compressed, so git can neither delta nor pack it — 1,400 raster charts added
~44MB of permanently incompressible objects to history per refresh. SVG is text: it
compresses in the packfile, cutting the published push roughly 3x.

Full daily resolution is kept deliberately. Thinning the series to shrink the SVG
further was measured and rejected: even extrema-preserving decimation moved the drawn
line up to ~49px on a 345px-tall chart, because daily gaps exceed the per-point pixel
spacing — and the worst distortion lands on the biggest movers, which are the
out-performers these charts exist to show.

Usage:
  python src/fetch_charts.py
"""

import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

# Byte-for-byte reproducible SVG, so a chart whose data didn't move produces no git
# diff at all. `svg.hashsalt` pins the clip-path element ids (otherwise they're salted
# per process); `svg.fonttype="none"` emits <text> referencing the font by name instead
# of embedding glyph outlines, which is both smaller and stable across machines.
# The remaining source of nondeterminism, the <dc:date> stamp, is dropped at savefig.
matplotlib.rcParams["svg.hashsalt"] = "congress-charts"
matplotlib.rcParams["svg.fonttype"] = "none"

sys.path.insert(0, str(Path(__file__).parent))
from utils import AGGS_CACHE, DATA_DIR, ROOT, load_json, load_json_gz, parse_date, setup_logging

log = setup_logging("fetch_charts")

LEDGER_PATH = DATA_DIR / "transactions.json"
COMPANY_INFO_PATH = DATA_DIR / "company_info.json"
CHARTS_DIR = ROOT / "docs" / "assets" / "charts"


def _markers_for(ticker: str, ledger_rows: list[dict]) -> tuple[list, list]:
    buys, sells = [], []
    for r in ledger_rows:
        d = parse_date(r["disclosure_date"])
        if not d:
            continue
        (buys if r["tx_type"] == "P" else sells).append(d)
    return sorted(set(buys)), sorted(set(sells))


def _price_on(bars_by_date: dict, d) -> float | None:
    """Closest close on/after date d within the chart window."""
    keys = sorted(bars_by_date)
    for k in keys:
        if k >= d:
            return bars_by_date[k]
    return None


def make_chart(ticker: str, bars: list[dict], buys: list, sells: list,
               company_name: str | None) -> bool:
    if not bars:
        return False
    dates = [datetime.utcfromtimestamp(b["t"] / 1000).date() for b in bars]
    closes = [b["c"] for b in bars]
    bars_by_date = dict(zip(dates, closes))

    fig, ax = plt.subplots(figsize=(9, 3.6), dpi=130)
    ax.plot(dates, closes, color="#1f6feb", linewidth=1.3, zorder=2)
    ax.fill_between(dates, closes, min(closes), color="#1f6feb", alpha=0.06, zorder=1)

    win = [d for d in dates]
    lo, hi = min(win), max(win)
    for d in buys:
        if lo <= d <= hi and (p := _price_on(bars_by_date, d)):
            ax.scatter([d], [p], marker="^", s=55, color="#1a7f37", zorder=3,
                       edgecolors="white", linewidths=0.6)
    for d in sells:
        if lo <= d <= hi and (p := _price_on(bars_by_date, d)):
            ax.scatter([d], [p], marker="v", s=55, color="#cf222e", zorder=3,
                       edgecolors="white", linewidths=0.6)

    title = ticker if not company_name else f"{ticker} — {company_name}"
    ax.set_title(title[:60], fontsize=11, loc="left", color="#24292f")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax.grid(True, axis="y", color="#eaeef2", linewidth=0.8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(labelsize=8, colors="#57606a")
    fig.tight_layout()

    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    # metadata Date=None strips the generation timestamp; without it every chart would
    # differ every run even when the underlying bars are unchanged.
    fig.savefig(CHARTS_DIR / f"{ticker}.svg", facecolor="white",
                metadata={"Date": None})
    plt.close(fig)
    return True


def run() -> None:
    if not LEDGER_PATH.exists():
        log.error("No ledger; run fetchers first")
        return
    ledger = load_json(LEDGER_PATH)
    info = load_json(COMPANY_INFO_PATH) if COMPANY_INFO_PATH.exists() else {}

    by_ticker = defaultdict(list)
    for r in ledger.values():
        by_ticker[r["ticker"]].append(r)

    made = skipped = 0
    for ticker, rows in by_ticker.items():
        aggs_path = AGGS_CACHE / f"{ticker}.json.gz"
        if not aggs_path.exists():
            skipped += 1
            continue
        bars = load_json_gz(aggs_path)
        buys, sells = _markers_for(ticker, rows)
        name = (info.get(ticker) or {}).get("name")
        if make_chart(ticker, bars, buys, sells, name):
            made += 1
        else:
            skipped += 1
    log.info("Charts: %d generated, %d skipped (no cached bars)", made, skipped)


if __name__ == "__main__":
    run()
