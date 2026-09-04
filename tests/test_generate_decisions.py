from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import generate_decisions as gd


SAMPLE = """
# Investment Decisions

## Watchlist
### Open
- AAPL | 2025-01-15 | services thesis
- COST | 2024-11-20 | membership moat
### Closed

## Healthcare
### Open
### Closed

## New Tech
### Open
- NVDA | 2025-03-02 | AI capex
### Closed
- TSLA | 2024-06-10 | 2025-02-20 | took profits

## Archive
- MSFT | 2024-03-05 | 2024-12-18 | rotated to cash

## Old Theme
### Closed
- IBM | 2023-01-03 | 2023-06-01 | done
"""

LEGACY = """
## Open
- NVDA | 2025-03-02 | still early
## Closed
- TSLA | 2024-06-10 | 2025-02-20 | profits
"""


class ParsePositionsTests(unittest.TestCase):
    def test_buckets_open_closed_and_archive_sentinel(self):
        cats = {c["name"]: c for c in gd.parse_positions(SAMPLE)}
        self.assertIn("Watchlist", cats)
        self.assertIn("Healthcare", cats)
        self.assertIn("New Tech", cats)
        self.assertIn("Archive", cats)
        self.assertIn("Old Theme", cats)

        self.assertFalse(cats["Watchlist"]["archived"])
        self.assertFalse(cats["Healthcare"]["archived"])
        self.assertFalse(cats["New Tech"]["archived"])
        self.assertTrue(cats["Archive"]["archived"])
        self.assertTrue(cats["Old Theme"]["archived"])

        self.assertEqual([p["ticker"] for p in cats["Watchlist"]["opens"]], ["AAPL", "COST"])
        self.assertEqual(cats["Healthcare"]["opens"], [])
        self.assertEqual(cats["New Tech"]["opens"][0]["ticker"], "NVDA")
        self.assertEqual(cats["New Tech"]["closed"][0]["ticker"], "TSLA")
        self.assertEqual(cats["New Tech"]["closed"][0]["close_date"], date(2025, 2, 20))

        # Bullet pasted directly under Archive auto-detects two dates as closed.
        self.assertEqual(cats["Archive"]["closed"][0]["ticker"], "MSFT")
        self.assertEqual(cats["Archive"]["opens"], [])
        self.assertEqual(cats["Old Theme"]["closed"][0]["ticker"], "IBM")

    def test_legacy_open_closed_headings_become_positions_bucket(self):
        cats = gd.parse_positions(LEGACY)
        self.assertEqual(len(cats), 1)
        self.assertEqual(cats[0]["name"], "Positions")
        self.assertFalse(cats[0]["archived"])
        self.assertEqual(cats[0]["opens"][0]["ticker"], "NVDA")
        self.assertEqual(cats[0]["closed"][0]["ticker"], "TSLA")

    def test_intro_prose_bullets_are_not_positions(self):
        text = Path(ROOT / "positions.md").read_text(encoding="utf-8")
        names = [c["name"] for c in gd.parse_positions(text)]
        self.assertNotIn("Positions", names)
        self.assertEqual(names[0], "Watchlist")
        self.assertEqual(
            [c["name"] for c in gd.parse_positions(text) if not c["archived"]],
            ["Watchlist", "Healthcare", "New Tech"],
        )

    def test_rejects_non_ticker_bullets(self):
        text = """
## Watchlist
### Open
- not a ticker | 2025-01-15 | nope
- AAPL | 2025-01-15 | ok
"""
        cats = gd.parse_positions(text)
        self.assertEqual([p["ticker"] for p in cats[0]["opens"]], ["AAPL"])

    def test_copy_paste_item_under_archive_without_subhead(self):
        text = """
## Watchlist
### Open
- AAPL | 2025-01-15 | keep
## Archive
- AAPL | 2025-01-15 | filed away
"""
        cats = gd.parse_positions(text)
        live = [c for c in cats if not c["archived"]]
        archived = [c for c in cats if c["archived"]]
        self.assertEqual(live[0]["opens"][0]["ticker"], "AAPL")
        self.assertEqual(archived[0]["opens"][0]["notes"], "filed away")


class DecisionPricingTests(unittest.TestCase):
    def test_price_on_uses_first_trading_day_on_or_after_date(self):
        bars = {
            date(2026, 7, 24): 101.0,
            date(2026, 7, 27): 103.0,
        }
        self.assertEqual(gd._price_on(bars, date(2026, 7, 24)), 101.0)
        self.assertEqual(gd._price_on(bars, date(2026, 7, 25)), 103.0)
        self.assertIsNone(gd._price_on(bars, date(2026, 7, 28)))
        self.assertIsNone(gd._price_on({}, date(2026, 7, 24)))


class KellySizingTests(unittest.TestCase):
    def test_half_kelly_uses_win_rate_and_average_payoff(self):
        with patch.object(gd, "KELLY_MIN_CLOSED_TRADES", 1):
            stats = gd.compute_trade_stats([10.0, -5.0, 20.0, -10.0, 0.0])
        self.assertEqual(40.0, stats["winner_pct"])
        self.assertEqual(40.0, stats["loser_pct"])
        self.assertEqual(15.0, stats["average_win_pct"])
        self.assertEqual(-7.5, stats["average_loss_pct"])
        self.assertEqual(25.0, stats["full_kelly_pct"])
        self.assertEqual(12.5, stats["half_kelly_pct"])

    def test_negative_half_kelly_is_floored_at_zero(self):
        with patch.object(gd, "KELLY_MIN_CLOSED_TRADES", 1):
            stats = gd.compute_trade_stats([5.0, -10.0])
        self.assertEqual(-50.0, stats["full_kelly_pct"])
        self.assertEqual(0.0, stats["half_kelly_pct"])

    def test_small_sample_does_not_publish_kelly_size(self):
        stats = gd.compute_trade_stats([10.0, -5.0])
        self.assertEqual("insufficient_sample", stats["kelly_status"])
        self.assertIsNone(stats["half_kelly_pct"])


class NavSeriesTests(unittest.TestCase):
    def test_equal_weight_rebalance_on_exit(self):
        # AAA 100→110→120→130; BBB 50→55→40→45. Both enter 01-02; BBB exits 01-06.
        d0, d1, d2, d3 = date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
        bars = {
            "AAA": {d0: 100.0, d1: 110.0, d2: 120.0, d3: 130.0},
            "BBB": {d0: 50.0, d1: 55.0, d2: 40.0, d3: 45.0},
        }
        spy = {d0: 10.0, d1: 10.0, d2: 10.0, d3: 10.0}
        book = [
            {"id": "a", "ticker": "AAA", "entry_date": d0, "exit_date": None},
            {"id": "b", "ticker": "BBB", "entry_date": d0, "exit_date": d2},
        ]
        series = gd.build_nav_series(book, bars, spy, d3)
        by_day = {point["date"]: point["value"] for point in series}
        self.assertAlmostEqual(by_day["2026-01-02"], 100.0, places=3)
        self.assertAlmostEqual(by_day["2026-01-05"], 110.0, places=3)
        self.assertAlmostEqual(by_day["2026-01-06"], 100.0, places=3)
        self.assertAlmostEqual(by_day["2026-01-07"], 130.0 * (100.0 / 120.0), places=3)

    def test_ghost_book_starts_at_close(self):
        d0, d1, d2 = date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)
        bars = {"BBB": {d0: 50.0, d1: 40.0, d2: 45.0}}
        spy = {d0: 10.0, d1: 10.0, d2: 10.0}
        book = [{"id": "g", "ticker": "BBB", "entry_date": d1, "exit_date": None}]
        series = gd.build_nav_series(book, bars, spy, d2)
        self.assertEqual(series[0]["date"], "2026-01-05")
        self.assertAlmostEqual(series[0]["value"], 100.0, places=3)
        self.assertAlmostEqual(series[-1]["value"], 100.0 * 45.0 / 40.0, places=3)

    def test_return_summary_uses_12m_when_present(self):
        series = [
            {"date": "2025-01-02", "value": 100.0, "spy_value": 100.0,
             "return_12m": None, "spy_12m": None},
            {"date": "2026-01-02", "value": 120.0, "spy_value": 110.0,
             "return_12m": 15.5, "spy_12m": 8.0},
        ]
        value, label = gd._return_summary(series, "value")
        spy_value, spy_label = gd._return_summary(series, "spy_value")
        self.assertEqual(value, 15.5)
        self.assertEqual(label, "12M Price")
        self.assertEqual(spy_value, 8.0)
        self.assertEqual(spy_label, "12M Price")

    def test_sharpe_none_when_history_is_short(self):
        series = [
            {"date": "2026-01-02", "value": 100.0},
            {"date": "2026-01-05", "value": 101.0},
        ]
        result = gd.compute_sharpe(series, date(2026, 1, 5))
        self.assertIsNone(result["12m"])
        self.assertIsNone(result["3m"])


if __name__ == "__main__":
    unittest.main()
