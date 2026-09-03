from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "hedge"))
sys.path.insert(0, str(ROOT / "src"))

import generate_hedge_report
import generate_hedge_stocks
import backtest_13f
import price_hedge
import rank_funds
import resolve_cusip
import specialist_funds
from utils import save_json_gz


class SpecialistConfigTests(unittest.TestCase):
    def test_specialist_alpha_attribution_uses_existing_contribution_metric(self):
        attribution = backtest_13f._aggregate_alpha_attribution(
            [1, 2],
            {
                "1": {"cik": 1, "name": "Fund One", "alpha": 0.2},
                "2": {"cik": 2, "name": "Fund Two", "alpha": 0.1},
            },
            {
                1: {"BIO": (0.1, "Shared Bio")},
                2: {"BIO": (-0.05, "Shared Bio"), "OTHER": (0.02, "Other")},
            },
        )
        bio = next(row for row in attribution["stocks"] if row["ticker"] == "BIO")
        self.assertEqual(attribution["total_alpha"], 0.3)
        self.assertEqual(attribution["n_funds"], 2)
        self.assertEqual(bio["contribution"], 0.05)
        self.assertEqual(bio["n_funds"], 2)

    def test_watchlist_is_separate_from_specialists(self):
        cfg = {
            "specialist_funds": [
                {"cik": 1224962, "label": "Perceptive Advisors", "category": "Healthcare"},
            ],
            "watchlist_funds": [
                {"cik": 1562087, "label": "Thiel Macro"},
                {"cik": "1562087", "label": "Duplicate"},
            ],
            "pool_pins": [1423053],
        }
        watch = specialist_funds.configured_watchlist(cfg)
        self.assertEqual(watch, [{
            "cik": 1562087, "label": "Thiel Macro", "category": "Watchlist",
        }])
        self.assertEqual(specialist_funds.configured_pin_ciks(cfg),
                         {1224962, 1562087, 1423053})

    def test_watchlist_drivers_roll_up_saved_contributions(self):
        perf = {
            "1": {"cik": 1, "name": "Fund A", "alpha": 0.20,
                  "drivers": [{"ticker": "AAA", "issuer": "Aaa", "contribution": 0.15}],
                  "detractors": [{"ticker": "BBB", "issuer": "Bbb", "contribution": -0.05}]},
            "2": {"cik": 2, "name": "Fund B", "alpha": 0.10,
                  "drivers": [{"ticker": "AAA", "issuer": "Aaa", "contribution": 0.08}]},
        }
        rows = rank_funds._drivers_from_saved([1, 2], perf, limit=10)
        aaa = next(r for r in rows if r["ticker"] == "AAA")
        self.assertAlmostEqual(aaa["contribution"], 0.23)
        self.assertEqual(aaa["n_funds"], 2)
        self.assertEqual(aaa["top_fund_cik"], 1)
        self.assertAlmostEqual(aaa["share"], 0.23 / 0.30, places=4)

    def test_highlight_includes_universe_and_convergence_names(self):
        drivers = [{"ticker": "MU", "share": 0.1}]
        buying_now = [{"ticker": "INTC"}]
        attr = [{"ticker": "NVDA", "share": 0.05}, {"ticker": "ZZZ", "share": 0.0}]
        congress = {"drivers": [{"ticker": "NVDA", "score": 12.0}],
                    "recent_buys": [{"ticker": "INTC"}]}
        marks = rank_funds._highlight_tickers(drivers, buying_now, attr, congress)
        self.assertEqual(marks, {"MU", "INTC", "NVDA"})

    def test_skill_weighted_buys_indexes_to_strongest(self):
        records = [
            {"cik": 10, "alpha": 1.0, "hit_rate": 1.0, "latest_book": 100, "name": "A"},
            {"cik": 20, "alpha": 0.5, "hit_rate": 1.0, "latest_book": 100, "name": "B"},
        ]
        changes = {
            "10": {"manager": "A", "new": [{"ticker": "AAA", "value": 50, "issuer": "Aaa"}]},
            "20": {"manager": "B", "new": [{"ticker": "BBB", "value": 10, "issuer": "Bbb"}]},
        }
        rows = rank_funds._skill_weighted_buys(records, changes, limit=10)
        self.assertEqual(rows[0]["ticker"], "AAA")
        self.assertEqual(rows[0]["conviction"], 100)
        self.assertGreater(rows[0]["conviction"], rows[1]["conviction"])

    def test_pin_row_marks_absent_funds(self):
        row = rank_funds._pin_row(
            {"cik": 1, "label": "Missing", "category": "Watchlist"},
            None, {}, {"max_holdings": 150, "min_holdings": 5, "min_aum": 1e8})
        self.assertEqual(row["status"], "absent")
        self.assertEqual(row["label"], "Missing")
        self.assertIsNone(row["alpha"])

    def test_only_valid_unique_specialists_are_configured(self):
        records = specialist_funds.configured_specialists({
            "specialist_funds": [
                {"cik": "1224962", "label": "Perceptive Advisors", "category": "Healthcare"},
                {"cik": 1224962, "label": "Duplicate"},
                {"label": "Missing CIK"},
            ],
        })
        self.assertEqual(records, [{
            "cik": 1224962,
            "label": "Perceptive Advisors",
            "category": "Healthcare",
        }])

    def test_gated_specialist_does_not_pass_ranking_rules(self):
        record = {"n_periods": 12, "coverage": 0.641, "hit_rate": 0.833}
        self.assertEqual(
            rank_funds._gate_reason(record, min_filings=4, min_cov=0.90, min_hit=0.50),
            "coverage 64%",
        )

    def test_specialists_and_watchlist_pins_get_pages_without_joining_leaderboard(self):
        rankings = {
            "leaderboard": [{"cik": 1}, {"cik": 2}],
            "watchlist_ciks": [3],
            "specialists": [{"cik": 1224962, "status": "unranked"}],
        }
        perf = {str(cik): {} for cik in (1, 2, 3, 1224962)}
        self.assertEqual(
            generate_hedge_report._page_targets(rankings, perf, board_size=1),
            ["1", "3", "1224962"],
        )

    def test_overlap_uses_latest_long_holdings_from_two_or_more_specialists(self):
        specialists = [
            {"cik": 1, "label": "Fund One", "category": "Healthcare"},
            {"cik": 2, "label": "Fund Two", "category": "Healthcare"},
        ]
        perf = {
            "1": {"name": "Fund One LLC", "latest_book": 1_000},
            "2": {"name": "Fund Two LLC", "latest_book": 2_000},
        }
        holdings = {
            "old": {"cik": 1, "filing_date": "2026-01-01", "cusip": "OLD",
                    "issuer": "Old Inc.", "value": 900, "put_call": None},
            "one": {"cik": 1, "filing_date": "2026-04-01", "cusip": "SHARED",
                    "issuer": "Shared Bio", "value": 100, "put_call": None},
            "two": {"cik": 2, "filing_date": "2026-04-01", "cusip": "SHARED",
                    "issuer": "Shared Bio", "value": 400, "put_call": None},
            "put": {"cik": 2, "filing_date": "2026-04-01", "cusip": "SHARED",
                    "issuer": "Shared Bio", "value": 700, "put_call": "PUT"},
        }
        with patch.object(generate_hedge_stocks, "_ticker",
                          side_effect=lambda cusip: {"SHARED": "BIO"}.get(cusip)):
            overlap = generate_hedge_stocks._build_specialist_overlap(
                holdings, specialists, perf, {
                    "stocks": [{"ticker": "BIO", "contribution": 0.1234}],
                })

        self.assertEqual(len(overlap), 1)
        self.assertEqual(overlap[0]["ticker"], "BIO")
        self.assertEqual(overlap[0]["n_funds"], 2)
        self.assertEqual(overlap[0]["combined_value"], 500)
        self.assertEqual(overlap[0]["avg_weight"], 0.15)
        self.assertEqual(overlap[0]["alpha_contribution"], 0.1234)


class CusipOverrideTests(unittest.TestCase):
    def tearDown(self):
        resolve_cusip._OVERRIDES = None

    def test_manual_override_wins_over_cached_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            overrides = Path(tmp) / "overrides.json"
            overrides.write_text(json.dumps({
                "03940C100": {"ticker": "ACLX", "name": "Arcellx, Inc."},
            }))
            with patch.object(resolve_cusip, "OVERRIDES_PATH", overrides), \
                    patch.object(resolve_cusip, "_MAP", {
                        "03940C100": {"cusip": "03940C100", "ticker": None},
                    }):
                resolve_cusip._OVERRIDES = None
                record = resolve_cusip.cached("03940c100")
        self.assertEqual(record["ticker"], "ACLX")
        self.assertEqual(record["source"], "manual")


class GroupedPriceCoverageTests(unittest.TestCase):
    class FakePolygon:
        BASE = "https://example.test"

        def __init__(self):
            self.grouped_calls = 0
            self.aggregate_calls = 0

        def _get(self, url, params):
            self.grouped_calls += 1
            return {"results": []}

        def aggregates(self, ticker, from_date, to_date, use_cache=True):
            self.aggregate_calls += 1
            stamp = int(datetime(
                2026, 7, 29, 16, tzinfo=timezone.utc).timestamp() * 1000)
            return [{"t": stamp, "c": 20}]

        @staticmethod
        def _safe_err(error):
            return str(error)

    def test_newly_resolved_ticker_uses_one_aggregate_history_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            grouped = Path(tmp) / "grouped"
            aggs = Path(tmp) / "aggs"
            grouped.mkdir()
            aggs.mkdir()
            day = date(2026, 7, 29)
            save_json_gz(grouped / "2026-07-29.json.gz", {"OLD": 10})
            polygon = self.FakePolygon()

            with patch.object(price_hedge, "HEDGE_GROUPED", grouped), \
                    patch.object(price_hedge, "AGGS_CACHE", aggs), \
                    patch.object(price_hedge, "_pending_price_tickers", return_value={"NEW"}):
                prices = price_hedge.build_price_map(
                    {"2026-07-29"}, poly=polygon,
                    keep={"OLD", "NEW"},
                    keep_by_date={"2026-07-29": {"OLD", "NEW"}},
                )

            self.assertEqual(prices["2026-07-29"], {"OLD": 10, "NEW": 20})
            self.assertEqual(polygon.grouped_calls, 0)
            self.assertEqual(polygon.aggregate_calls, 1)


if __name__ == "__main__":
    unittest.main()
