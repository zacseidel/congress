from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import generate_decisions


class DecisionPricingTests(unittest.TestCase):
    def test_price_on_uses_first_trading_day_on_or_after_date(self):
        bars = {
            date(2026, 7, 24): 101.0,
            date(2026, 7, 27): 103.0,
        }

        self.assertEqual(generate_decisions._price_on(bars, date(2026, 7, 24)), 101.0)
        self.assertEqual(generate_decisions._price_on(bars, date(2026, 7, 25)), 103.0)
        self.assertIsNone(generate_decisions._price_on(bars, date(2026, 7, 28)))
        self.assertIsNone(generate_decisions._price_on({}, date(2026, 7, 24)))


if __name__ == "__main__":
    unittest.main()
