from __future__ import annotations

import gzip
import json
import re
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import fetch_charts
import fetch_prices
import stock_universe


class StockUniverseTests(unittest.TestCase):
    def test_page_universe_is_disclosures_plus_bounded_hedge_names(self):
        ledger = {
            "1": {"ticker": "COHR", "tx_type": "P"},
            "2": {"ticker": "SELL", "tx_type": "S"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pages = base / "stock_pages.json"
            holders = base / "stock_holders.json.gz"
            pages.write_text(json.dumps({"tickers": ["DMRA", "NO_DATA"]}))
            with gzip.open(holders, "wt", encoding="utf-8") as stream:
                json.dump({"DMRA": {"holders": []}}, stream)
            with patch.object(stock_universe, "HEDGE_PAGES_PATH", pages), \
                    patch.object(stock_universe, "HEDGE_HOLDERS_PATH", holders):
                self.assertEqual(
                    stock_universe.stock_page_tickers(ledger),
                    {"COHR", "SELL", "DMRA"},
                )
                self.assertEqual(
                    stock_universe.price_tickers(ledger, "SPY"),
                    {"COHR", "SELL", "DMRA", "SPY"},
                )
            with patch.object(stock_universe, "HEDGE_PAGES_PATH", pages), \
                    patch.object(stock_universe, "HEDGE_HOLDERS_PATH", base / "missing.gz"):
                self.assertEqual(
                    stock_universe.stock_page_tickers(ledger),
                    {"COHR", "SELL"},
                )


class PriceHistoryTests(unittest.TestCase):
    def test_merge_bars_is_sorted_and_later_input_wins(self):
        merged = fetch_prices._merge_bars(
            [{"t": 2, "c": 20, "v": 99}, {"t": 1, "c": 10}],
            [{"t": 2, "c": 21}, {"t": 3, "c": 30}],
        )
        self.assertEqual([bar["t"] for bar in merged], [1, 2, 3])
        self.assertEqual(merged[1], {"t": 2, "c": 21, "v": 99})

    def test_trim_bars_prevents_rolling_cache_growth(self):
        start, end = date(2025, 1, 2), date(2025, 1, 3)
        stamp = lambda day: int(datetime(
            2025, 1, day, 16, tzinfo=timezone.utc).timestamp() * 1000)
        bars = [{"t": stamp(day), "c": day} for day in (1, 2, 3, 4)]
        self.assertEqual(
            [bar["c"] for bar in fetch_prices._trim_bars(bars, start, end)],
            [2, 3],
        )

    def test_late_or_empty_grouped_history_needs_backfill(self):
        start = date(2025, 1, 1)
        late = datetime(2025, 4, 1, tzinfo=timezone.utc)
        early = datetime(2025, 1, 15, tzinfo=timezone.utc)
        self.assertTrue(fetch_prices._needs_history_backfill([], start))
        self.assertTrue(fetch_prices._needs_history_backfill(
            [{"t": int(late.timestamp() * 1000), "c": 1}], start))
        self.assertFalse(fetch_prices._needs_history_backfill(
            [{"t": int(early.timestamp() * 1000), "c": 1}], start))


class CompactChartTests(unittest.TestCase):
    def test_exchange_events_are_not_mislabeled_as_sales(self):
        rows = [
            {"tx_type": "P", "disclosure_date": "2026-01-01"},
            {"tx_type": "S", "disclosure_date": "2026-01-02"},
            {"tx_type": "E", "disclosure_date": "2026-01-03"},
        ]
        buys, sells = fetch_charts._markers_for(rows)
        self.assertEqual(buys, [date(2026, 1, 1)])
        self.assertEqual(sells, [date(2026, 1, 2)])

    def test_svg_keeps_every_daily_point_without_matplotlib_overhead(self):
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        bars = [
            {"t": int((start + timedelta(days=i)).timestamp() * 1000),
             "c": 100 + (i % 17)}
            for i in range(500)
        ]
        svg = fetch_charts.render_svg("TEST", bars, [], [], "Test Corporation")
        self.assertIsNotNone(svg)
        self.assertIn("Test Corporation", svg)
        polyline = next(line for line in svg.splitlines() if "<polyline" in line)
        points = re.search(r'points="([^"]+)"', polyline).group(1).split()
        self.assertEqual(len(points), 500)
        self.assertLess(len(svg.encode("utf-8")), 12_000)


if __name__ == "__main__":
    unittest.main()
