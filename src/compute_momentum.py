from __future__ import annotations

"""
Per-ticker momentum signals (4 dots), computed from the cached 2-year daily bars.
No API calls.

  1. price  > SMA10          short-term trend
  2. SMA10  > SMA50          medium-term trend
  3. SMA50  > SMA200         long-term trend (golden cross)
  4. up-volume > down-volume over the last 20 sessions   (directional volume)

Each dot is "bull" (green), "bear" (red), or "na" (grey, insufficient history).

Output: data/momentum.json  -> {ticker: {as_of, dots: [{key,label,state,detail}]}}

Usage:
  python src/compute_momentum.py
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import AGGS_CACHE, DATA_DIR, load_json_gz, save_json, setup_logging

log = setup_logging("compute_momentum")

MOMENTUM_PATH = DATA_DIR / "momentum.json"
VOL_WINDOW = 20


def _sma(values, n):
    return sum(values[-n:]) / n if len(values) >= n else None


def _state(cond):
    return "bull" if cond else "bear"


def momentum_for(bars: list) -> dict | None:
    # Keep close and volume paired so the two stay index-aligned even if a bar
    # is missing a close.
    cv = [(b["c"], b.get("v", 0) or 0) for b in bars if b.get("c") is not None]
    closes = [c for c, _ in cv]
    vols = [v for _, v in cv]
    if len(closes) < 2:
        return None
    price = closes[-1]
    sma10, sma50, sma200 = _sma(closes, 10), _sma(closes, 50), _sma(closes, 200)

    dots = []
    # 1. price > SMA10
    dots.append({"key": "p_sma10", "label": "Price vs SMA10",
                 "state": _state(price > sma10) if sma10 else "na",
                 "detail": f"price {price:.2f} vs SMA10 {sma10:.2f}" if sma10 else "need 10 sessions"})
    # 2. SMA10 > SMA50
    dots.append({"key": "sma10_50", "label": "SMA10 vs SMA50",
                 "state": _state(sma10 > sma50) if (sma10 and sma50) else "na",
                 "detail": f"SMA10 {sma10:.2f} vs SMA50 {sma50:.2f}" if (sma10 and sma50) else "need 50 sessions"})
    # 3. SMA50 > SMA200
    dots.append({"key": "sma50_200", "label": "SMA50 vs SMA200",
                 "state": _state(sma50 > sma200) if (sma50 and sma200) else "na",
                 "detail": f"SMA50 {sma50:.2f} vs SMA200 {sma200:.2f}" if (sma50 and sma200) else "need 200 sessions"})
    # 4. up-volume vs down-volume over last VOL_WINDOW sessions.
    #    Close-only bars (e.g. grouped-daily-derived) carry no volume — report "na"
    #    rather than a misleading tie/bearish reading.
    if not any(vols):
        dots.append({"key": "vol", "label": f"Up vs down volume ({VOL_WINDOW}d)",
                     "state": "na", "detail": "no volume data"})
    elif len(closes) >= VOL_WINDOW + 1:
        up = down = 0.0
        for i in range(len(closes) - VOL_WINDOW, len(closes)):
            if closes[i] > closes[i - 1]:
                up += vols[i]
            elif closes[i] < closes[i - 1]:
                down += vols[i]
        dots.append({"key": "vol", "label": f"Up vs down volume ({VOL_WINDOW}d)",
                     "state": _state(up > down),
                     "detail": f"up-vol {up:,.0f} vs down-vol {down:,.0f}"})
    else:
        dots.append({"key": "vol", "label": f"Up vs down volume ({VOL_WINDOW}d)",
                     "state": "na", "detail": f"need {VOL_WINDOW + 1} sessions"})

    as_of = datetime.utcfromtimestamp(bars[-1]["t"] / 1000).date().isoformat() if bars[-1].get("t") else None
    return {"as_of": as_of, "dots": dots}


def run() -> None:
    files = sorted(AGGS_CACHE.glob("*.json.gz"))
    out = {}
    for f in files:
        try:
            bars = load_json_gz(f)
        except Exception:
            continue
        if not bars:
            continue
        m = momentum_for(bars)
        if m:
            out[f.name[:-8].upper()] = m   # strip ".json.gz"
    save_json(MOMENTUM_PATH, out)
    log.info("Momentum computed for %d tickers", len(out))


if __name__ == "__main__":
    run()
