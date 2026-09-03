from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import generate_dashboard as dash


class SelectTopFundsTests(unittest.TestCase):
    def test_drops_pinned_specialists_then_takes_top_n(self):
        board = [
            {"cik": 1, "alpha": 2.0},
            {"cik": 99, "alpha": 1.8},   # pinned
            {"cik": 2, "alpha": 1.5},
            {"cik": 3, "alpha": 0.4},
            {"cik": 4, "alpha": -0.1},
        ]
        selected = dash.select_top_funds(board, top_n=2, min_alpha=0.0, exclude_ciks={99})
        self.assertEqual([r["cik"] for r in selected], [1, 2])

    def test_min_alpha_filters_before_cut(self):
        board = [
            {"cik": 1, "alpha": 1.0},
            {"cik": 2, "alpha": 0.0},
            {"cik": 3, "alpha": -0.2},
        ]
        selected = dash.select_top_funds(board, top_n=10, min_alpha=0.1, exclude_ciks=set())
        self.assertEqual([r["cik"] for r in selected], [1])


class HedgeBuyingTests(unittest.TestCase):
    def test_skill_weighted_conviction_and_top_buyer(self):
        funds = [
            {"cik": 10, "alpha": 1.0, "hit_rate": 1.0, "latest_book": 100},
            {"cik": 20, "alpha": 0.5, "hit_rate": 1.0, "latest_book": 100},
        ]
        changes = {
            "10": {"manager": "Fund A", "new": [
                {"ticker": "AAA", "value": 50},
                {"ticker": "BBB", "value": 10},
            ]},
            "20": {"manager": "Fund B", "new": [
                {"ticker": "AAA", "value": 20},
            ]},
        }
        buys = dash.hedge_buying_from_changes(funds, changes)
        self.assertEqual(set(buys), {"AAA", "BBB"})
        self.assertGreater(buys["AAA"]["score"], buys["BBB"]["score"])
        self.assertEqual(buys["AAA"]["n_funds"], 2)
        self.assertEqual(buys["AAA"]["top_cik"], 10)
        self.assertEqual(buys["AAA"]["top_name"], "Fund A")

    def test_skips_unresolved_tickers_and_zero_skill(self):
        funds = [
            {"cik": 10, "alpha": 0.0, "hit_rate": 1.0, "latest_book": 100},
            {"cik": 20, "alpha": 1.0, "hit_rate": 1.0, "latest_book": 100},
        ]
        changes = {
            "10": {"manager": "Zero", "new": [{"ticker": "ZZZ", "value": 90}]},
            "20": {"manager": "Skill", "new": [{"ticker": None, "value": 90},
                                               {"ticker": "OK", "value": 10}]},
        }
        buys = dash.hedge_buying_from_changes(funds, changes)
        self.assertEqual(set(buys), {"OK"})
        self.assertGreater(buys["OK"]["score"], 0)


class CongressBuyingTests(unittest.TestCase):
    def test_scores_outperformer_purchases_inside_window(self):
        positions = [
            {"member_id": "op", "member": "Op Member", "ticker": "AAA",
             "weight": 50, "entry_date": "2026-08-01"},
            {"member_id": "op", "member": "Op Member", "ticker": "BBB",
             "weight": 50, "entry_date": "2026-01-01"},
            {"member_id": "other", "member": "Other", "ticker": "AAA",
             "weight": 999, "entry_date": "2026-08-01"},
        ]
        members = {"op": {"total_dollars": 100}, "other": {"total_dollars": 999}}
        buys = dash.congress_buying_from_positions(
            positions, members, {"op"}, cutoff=date(2026, 5, 1))
        self.assertEqual(set(buys), {"AAA"})
        self.assertAlmostEqual(buys["AAA"]["score"], 0.5)
        self.assertEqual(buys["AAA"]["n_outperformers"], 1)
        self.assertEqual(buys["AAA"]["top_member"], "Op Member")
        self.assertEqual(buys["AAA"]["top_member_id"], "op")


class BuyingNowConvergenceTests(unittest.TestCase):
    def test_intersection_only_and_sorts_by_sum_of_shares(self):
        hedge = {
            "AAA": {"score": 80, "n_funds": 3, "top_cik": 1, "top_name": "Fund A"},
            "BBB": {"score": 20, "n_funds": 1, "top_cik": 2, "top_name": "Fund B"},
            "CCC": {"score": 100, "n_funds": 2, "top_cik": 3, "top_name": "Fund C"},
        }
        congress = {
            "AAA": {"score": 10.0, "n_outperformers": 2,
                    "top_member": "Ann", "top_member_id": "ann"},
            "BBB": {"score": 30.0, "n_outperformers": 1,
                    "top_member": "Bob", "top_member_id": "bob"},
            "DDD": {"score": 60.0, "n_outperformers": 1,
                    "top_member": "Dee", "top_member_id": "dee"},
        }
        rows = dash.buying_now_convergence(hedge, congress)
        # Totals: hedge 200, congress 100. AAA 80/200+10/100=0.50; BBB 20/200+30/100=0.40.
        # CCC and DDD are one-sided and dropped.
        self.assertEqual([r["ticker"] for r in rows], ["AAA", "BBB"])
        self.assertAlmostEqual(rows[0]["hedge_share"], 0.4)
        self.assertAlmostEqual(rows[0]["congress_share"], 0.1)
        self.assertAlmostEqual(rows[0]["combined"], 0.5)
        self.assertEqual(rows[0]["top_member_id"], "ann")

    def test_sum_of_shares_matches_alpha_combination(self):
        hedge = {
            "NIBBLE": {"score": 1, "n_funds": 1, "top_cik": 1, "top_name": "A"},
            "BOTH": {"score": 50, "n_funds": 4, "top_cik": 2, "top_name": "B"},
        }
        congress = {
            "NIBBLE": {"score": 40.0, "n_outperformers": 1,
                       "top_member": "Ann", "top_member_id": "ann"},
            "BOTH": {"score": 20.0, "n_outperformers": 2,
                     "top_member": "Bob", "top_member_id": "bob"},
        }
        rows = dash.buying_now_convergence(hedge, congress)
        # Hedge total 51, congress total 60.
        # NIBBLE: 1/51 + 40/60 ≈ 0.686; BOTH: 50/51 + 20/60 ≈ 1.313.
        self.assertEqual([r["ticker"] for r in rows], ["BOTH", "NIBBLE"])

    def test_empty_sides_yield_no_rows(self):
        self.assertEqual(dash.buying_now_convergence({}, {"AAA": {}}), [])
        self.assertEqual(dash.buying_now_convergence({"AAA": {}}, {}), [])


if __name__ == "__main__":
    unittest.main()
