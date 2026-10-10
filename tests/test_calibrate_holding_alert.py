from datetime import date
import json
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import calibrate_holding_alert as calibration  # noqa: E402


class HoldingAlertCalibrationTests(unittest.TestCase):
    def test_profile_assignment_uses_explicit_and_name_based_rules(self) -> None:
        self.assertEqual(calibration._profile_for_fund("378546", ""), "resource")
        self.assertEqual(calibration._profile_for_fund("160216", ""), "resource")
        self.assertEqual(calibration._profile_for_fund("161815", ""), "fof")
        self.assertEqual(calibration._profile_for_fund("005976", ""), "fof")
        self.assertEqual(
            calibration._profile_for_fund("TEST", "全球互联网科技基金"),
            "technology",
        )
        self.assertEqual(
            calibration._profile_for_fund("TEST", "全球价值精选基金"),
            "balanced",
        )

    def test_moving_average_buffers_reduce_small_breach_false_positives(self) -> None:
        rows = [
            {"label": 1, "ma60_gap": -0.09, "ma120_gap": -0.09},
            {"label": 0, "ma60_gap": -0.005, "ma120_gap": 0.01},
            {"label": 0, "ma60_gap": 0.01, "ma120_gap": -0.01},
            {"label": 0, "ma60_gap": 0.02, "ma120_gap": 0.03},
        ]

        audit = calibration._rule_buffer_audit(rows)

        self.assertAlmostEqual(
            audit["legacy_zero_buffer"]["false_positive_rate"], 2 / 3, places=6
        )
        self.assertEqual(audit["buffered_rule"]["false_positive_rate"], 0)
        self.assertEqual(audit["buffered_rule"]["recall"], 1.0)
        self.assertGreater(audit["false_positive_rate_reduction"], 0)

    def test_history_merge_adds_current_funds_and_deduplicates_dates(self) -> None:
        frozen = {
            "018229": [
                {"date": "2026-01-01", "nav": 1.0, "name": "旧名称"},
                {"date": "2026-01-02", "nav": 1.1, "name": "旧名称"},
            ]
        }
        current = {
            "018229": [
                {"date": "2026-01-02", "nav": 1.2, "name": "新名称"},
            ],
            "378546": [
                {"date": "2026-01-02", "nav": 1.5, "name": "资源基金"},
            ],
        }

        merged = calibration._merge_histories(frozen, current)

        self.assertEqual(set(merged), {"018229", "378546"})
        self.assertEqual(len(merged["018229"]), 2)
        self.assertEqual(merged["018229"][-1]["nav"], 1.2)
        self.assertEqual(merged["018229"][-1]["name"], "新名称")

    def test_scope_coverage_requires_a_full_year_for_every_fund(self) -> None:
        histories = {
            "A": [
                {"date": f"2026-{index // 28 + 1:02d}-{index % 28 + 1:02d}", "nav": 1.0}
                for index in range(252)
            ],
            "B": [{"date": "2026-01-01", "nav": 1.0}],
        }

        result = calibration._history_scope_coverage(histories, ["A", "B"])

        self.assertEqual(result["ready_count"], 1)
        self.assertFalse(result["ready"])
        self.assertTrue(result["funds"]["A"]["ready"])
        self.assertFalse(result["funds"]["B"]["ready"])

    def test_profile_validation_blocks_missing_resource_and_fof_samples(self) -> None:
        passing = {
            "fund_count": 2,
            "statistics_ready": True,
            "passed": True,
            "samples": 40,
            "events": 8,
        }
        missing = {
            "fund_count": 0,
            "statistics_ready": False,
            "passed": False,
            "samples": 0,
            "events": 0,
        }
        horizons = {
            key: {
                "profile_metrics": {
                    "technology": passing,
                    "balanced": passing,
                    "resource": missing,
                    "fof": missing,
                }
            }
            for key in ("20", "63")
        }

        result = calibration._profile_validation(horizons)

        self.assertFalse(result["passed"])
        self.assertTrue(result["profiles"]["technology"]["passed"])
        self.assertFalse(result["profiles"]["resource"]["coverage_ready"])
        self.assertTrue(any("资源周期" in reason for reason in result["blocking_reasons"]))
        self.assertTrue(any("FOF资产配置" in reason for reason in result["blocking_reasons"]))

    def test_production_acceptance_requires_portfolio_overlay_history(self) -> None:
        horizons = {
            "20": {"production_approved": True},
            "63": {"production_approved": True},
        }
        profiles = {"passed": True, "blocking_reasons": []}
        triggers = {"passed": True, "blocking_reasons": []}
        portfolio = {"ready": False, "reason": "缺少组合风险点时样本"}

        result = calibration._production_acceptance(
            horizons, profiles, triggers, portfolio
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["portfolio_overlay_validation_passed"])
        self.assertIn("缺少组合风险点时样本", result["blocking_reasons"])

    def test_point_in_time_loader_deduplicates_and_excludes_future_records(self) -> None:
        def period(disclosure: str, available: str, stock: str) -> dict:
            return {
                "disclosure_date": disclosure,
                "available_date": available,
                "holdings": [{"code": stock, "name": stock, "weight": 0.2}],
            }

        payload = {
            "funds": {
                "002891": [
                    period("2024-03-31", "2024-05-15", "A"),
                    period("2024-03-31", "2024-05-15", "A"),
                    period("2024-06-30", "2024-08-14", "B"),
                    period("2025-12-31", "2026-02-14", "C"),
                ],
                "378546": [
                    period("2024-03-31", "2024-05-15", "D"),
                    period("2024-06-30", "2024-08-14", "E"),
                    period("2025-12-31", "2026-02-14", "F"),
                ],
            }
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False
        ) as handle:
            json.dump(payload, handle)
            path = Path(handle.name)
        try:
            result = calibration._load_portfolio_point_in_time(
                path, signal_date=date(2025, 1, 1)
            )
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(result["periods_by_fund"], {"002891": 2, "378546": 2})
        self.assertEqual(result["valid_period_count"], 4)
        self.assertEqual(result["duplicate_periods"]["002891"], 1)
        self.assertEqual(len(result["lookahead_violations"]), 2)
        self.assertFalse(result["ready"])
        self.assertTrue(result["blocking_reasons"])

    def test_current_point_in_time_cache_is_ready_with_both_holdings(self) -> None:
        result = calibration._load_portfolio_point_in_time()

        self.assertTrue(result["ready"])
        self.assertEqual(result["fund_count"], 2)
        self.assertEqual(result["valid_period_count"], 28)
        self.assertEqual(result["top10_overlap_sample_count"], 14)
        self.assertTrue(result["correlation_coverage"]["ready"])

    def test_threshold_selector_rejects_always_on_alert(self) -> None:
        rows = [
            {
                "label": 1 if index in (0, 1) else 0,
                "x": [0.1] * 7,
                "forward_drawdown": -0.1 if index in (0, 1) else -0.01,
            }
            for index in range(10)
        ]
        details = calibration._select_alert_threshold_details(rows, [0.0] * 8)

        self.assertFalse(details["usable"])
        self.assertEqual(details["candidate_count"], 0)
        self.assertEqual(details["threshold"], calibration.NO_USABLE_THRESHOLD)

    def test_threshold_selector_requires_precision_and_limits_false_positives(self) -> None:
        rows = [
            {
                "label": int(index < 3),
                "x": [1.0 if index < 4 else 0.0] * 7,
                "forward_drawdown": -0.1 if index < 3 else -0.01,
            }
            for index in range(10)
        ]
        details = calibration._select_alert_threshold_details(
            rows, [-3.0, 3.0, 0, 0, 0, 0, 0, 0]
        )

        self.assertTrue(details["usable"])
        self.assertGreaterEqual(
            details["selected_metrics"]["precision"], calibration.MIN_ALERT_PRECISION
        )
        self.assertLessEqual(
            details["selected_metrics"]["false_positive_rate"],
            calibration.MAX_ALERT_FALSE_POSITIVE_RATE,
        )


if __name__ == "__main__":
    unittest.main()
