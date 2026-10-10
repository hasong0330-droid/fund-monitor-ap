from datetime import date, timedelta
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backtest_model as model  # noqa: E402


def _nav_history(count: int = 520) -> list[dict]:
    start = date(2023, 1, 1)
    return [
        {
            "date": (start + timedelta(days=index)).isoformat(),
            "nav": 1.0 + index * 0.001,
        }
        for index in range(count)
    ]


def _index_history(count: int = 520) -> list[dict]:
    start = date(2023, 1, 1)
    return [
        {
            "date": (start + timedelta(days=index)).isoformat(),
            "close": 100.0 + index * 0.1,
        }
        for index in range(count)
    ]


def _holding() -> dict:
    return {
        "disclosure_date": "2023-01-01",
        "effective_date": "2023-01-31",
        "source": "eastmoney-holdings",
        "holdings": [
            {"code": "STK1", "name": "Test stock", "weight": 0.30},
            {"code": "STK2", "name": "Test stock 2", "weight": 0.20},
        ],
    }


def _point_in_time_bundle() -> dict:
    return {
        "fundamentals": {
            "TEST": [
                {
                    "available_date": "2023-01-01",
                    "as_of_date": "2022-12-31",
                    "score": 72,
                    "coverage": 0.80,
                    "source": "sec-companyfacts",
                },
                {
                    "available_date": "2025-01-01",
                    "as_of_date": "2024-09-30",
                    "score": 99,
                    "coverage": 0.90,
                    "source": "sec-companyfacts",
                },
            ]
        },
        "news": {
            "TEST": [
                {
                    "available_date": "2023-09-01",
                    "score": 65,
                    "source": "ifind-news",
                },
                {
                    "available_date": "2023-12-15",
                    "score": 60,
                    "source": "ifind-news",
                },
                {
                    "available_date": "2025-01-01",
                    "score": 10,
                    "source": "ifind-news",
                },
            ]
        },
    }


class PointInTimeBacktestTests(unittest.TestCase):
    def test_research_pool_contains_resource_and_fof_peer_samples(self) -> None:
        self.assertEqual(model.RESEARCH_CANDIDATES["160216"], "国泰大宗商品(QDII)A")
        self.assertEqual(model.RESEARCH_CANDIDATES["005976"], "长信稳进资产配置(FOF)")
        self.assertNotIn("160216", model.RECOMMENDATION_POOL)
        self.assertNotIn("005976", model.RECOMMENDATION_POOL)

    def test_research_loader_consumes_audited_peer_history_cache(self) -> None:
        histories = model.load_research_histories(use_cache=True)

        self.assertGreaterEqual(len(histories["160216"]["records"]), 600)
        self.assertGreaterEqual(len(histories["005976"]["records"]), 600)
        self.assertEqual(histories["160216"]["source"], "eastmoney-pingzhongdata")

    def test_nasdaq_history_parser_reads_public_rows(self) -> None:
        payload = json.dumps({
            "data": {
                "tradesTable": {
                    "rows": [
                        {"date": "01/03/2020", "close": "9,020.77"},
                        {"date": "01/02/2020", "close": "9,020.00"},
                    ]
                }
            }
        })
        with patch.object(model, "_http_get", return_value=payload):
            records = model._fetch_nasdaq_index_history("IXIC", "20190101")

        self.assertEqual(records[0], {"date": "2020-01-02", "close": 9020.0})
        self.assertEqual(records[-1]["date"], "2020-01-03")

    def test_latest_available_records_are_used_without_lookahead(self) -> None:
        snapshot = model._point_in_time_snapshot(
            "TEST", "2024-01-01", _point_in_time_bundle(), True
        )

        self.assertTrue(snapshot["eligible"])
        self.assertEqual(snapshot["fundamental_as_of"], "2022-12-31")
        self.assertEqual(snapshot["fundamental_score"], 72)
        self.assertTrue(snapshot["lookahead_check"]["passed"])
        self.assertGreater(snapshot["lookahead_check"]["future_fundamental_records"], 0)
        self.assertTrue(snapshot["fundamental_coverage_ok"])
        self.assertTrue(snapshot["news_recency_ok"])

    def test_stale_news_cannot_satisfy_a_point_in_time_signal(self) -> None:
        bundle = _point_in_time_bundle()
        bundle["news"]["TEST"] = [
            {"available_date": "2023-01-02", "score": 65, "source": "ifind-news"}
        ]

        snapshot = model._point_in_time_snapshot("TEST", "2024-01-01", bundle, True)

        self.assertFalse(snapshot["eligible"])
        self.assertIsNone(snapshot["news"])
        self.assertFalse(snapshot["news_recency_ok"])

    def test_low_fundamental_coverage_cannot_satisfy_a_point_in_time_signal(self) -> None:
        bundle = _point_in_time_bundle()
        bundle["fundamentals"]["TEST"][0]["coverage"] = 0.59

        snapshot = model._point_in_time_snapshot("TEST", "2024-01-01", bundle, True)

        self.assertFalse(snapshot["eligible"])
        self.assertFalse(snapshot["fundamental_coverage_ok"])

    def test_formal_acceptance_requires_raw_archive_coverage_proof(self) -> None:
        status = model.point_in_time_acceptance_status(_point_in_time_bundle())

        self.assertFalse(status["ready"])
        self.assertTrue(status["blocking_reasons"])

    def test_formal_acceptance_blocks_incomplete_fund_level_coverage(self) -> None:
        bundle = {
            "acceptance": {
                "acceptance_ready": False,
                "fundamental_events": 200,
                "mapped_news_events": 200,
                "fundamental_span_days": 400,
                "news_span_days": 400,
                "fundamental_coverage_ready": False,
                "funds_with_60pct_fundamental_coverage": 15,
                "required_funds_with_60pct_fundamental_coverage": 18,
                "usable_fundamental_snapshots": 300,
                "required_usable_fundamental_snapshots": 180,
                "coverage_blocking_reasons": ["基金覆盖不足"],
            }
        }

        status = model.point_in_time_acceptance_status(bundle)

        self.assertFalse(status["ready"])
        self.assertIn("基金覆盖不足", status["blocking_reasons"])

    def test_missing_point_in_time_bundle_is_not_treated_as_neutral(self) -> None:
        snapshot = model._point_in_time_snapshot("TEST", "2024-01-01", {}, True)

        self.assertFalse(snapshot["eligible"])
        self.assertEqual(snapshot["status"], "missing")
        self.assertFalse(snapshot["lookahead_check"]["passed"])

    def test_backtest_records_execution_audit_and_net_return(self) -> None:
        histories = {"TEST": {"records": _nav_history(), "source": "eastmoney-history"}}
        indexes = {"IXIC": _index_history(), "DJI": _index_history()}
        holdings = {"TEST": [_holding()]}

        with patch_model_universe():
            result = model.run_backtest(
                histories,
                indexes,
                holdings,
                point_in_time=_point_in_time_bundle(),
                transaction_cost_bps=50,
                execution_lag=1,
                require_point_in_time=True,
            )

        row = result["rows"][0]
        self.assertEqual(row["signal_date"], row["information_cutoff_date"])
        self.assertEqual(row["holding_effective_date"], row["entry_date"])
        self.assertEqual(row["execution_lag_observations"], 1)
        self.assertTrue(row["point_in_time_eligible"])
        self.assertTrue(row["lookahead_check"]["passed"])
        self.assertLess(row["return_126"], row["gross_return_126"])

    def test_strict_run_without_pit_data_has_no_hard_pass(self) -> None:
        histories = {"TEST": {"records": _nav_history(), "source": "eastmoney-history"}}
        indexes = {"IXIC": _index_history(), "DJI": _index_history()}
        holdings = {"TEST": [_holding()]}

        with patch_model_universe():
            result = model.run_backtest(
                histories,
                indexes,
                holdings,
                point_in_time=None,
                require_point_in_time=True,
            )

        self.assertEqual(result["summary"]["point_in_time_coverage"], 0.0)
        self.assertFalse(any(row["hard_pass"] for row in result["rows"]))
        self.assertEqual(result["architecture_comparison"]["evaluation_rows"], 0)
        self.assertFalse(result["summary"]["statistics_available"])
        self.assertTrue(result["descriptive_summary"]["statistics_available"])
        self.assertFalse(result["formal_acceptance"]["passed"])


class patch_model_universe:
    def __enter__(self):
        self.candidates = model.CANDIDATES
        self.current = model.CURRENT_HOLDING_CODE
        model.CANDIDATES = {"TEST": "Test fund"}
        model.CURRENT_HOLDING_CODE = "TEST"
        return self

    def __exit__(self, exc_type, exc, traceback):
        model.CANDIDATES = self.candidates
        model.CURRENT_HOLDING_CODE = self.current
        return False


if __name__ == "__main__":
    unittest.main()
