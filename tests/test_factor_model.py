from datetime import date, timedelta
import unittest

from fetcher import NewsItem
from factor_model import FACTOR_WEIGHTS, _company_fundamental_score, _macro_factor, evaluate_fund_factors


TODAY = date(2026, 7, 29)


def history(values: list[float]) -> dict:
    start = TODAY - timedelta(days=len(values) - 1)
    return {
        "records": [
            {"date": (start + timedelta(days=index)).isoformat(), "nav": value}
            for index, value in enumerate(values)
        ]
    }


def holding() -> dict:
    return {
        "holdings": [
            {"code": "AAA", "name": "企业甲", "weight": 0.30},
            {"code": "BBB", "name": "企业乙", "weight": 0.20},
        ]
    }


def fundamentals() -> dict:
    metrics = {
        "revenue_yoy": 0.20,
        "net_income_yoy": 0.25,
        "net_margin": 0.18,
        "operating_margin": 0.22,
        "free_cash_flow_margin": 0.15,
        "roe": 0.24,
    }
    return {
        code: {
            "fundamentals": {
                "source": "sec-companyfacts",
                "filed_at": "2026-02-01",
                "fiscal_end": "2025-12-31",
                "metrics": metrics,
            }
        }
        for code in ("AAA", "BBB")
    }


class FiveFactorModelTests(unittest.TestCase):
    def test_allocation_only_fund_marks_enterprise_fundamentals_not_applicable(self) -> None:
        result = evaluate_fund_factors(
            "161815",
            history([1.0 + index / 1000 for index in range(130)]),
            {"holdings": [], "allocation_only": True},
            {},
            [],
            {},
        )

        detail = result["details"]["fundamental"]
        self.assertFalse(detail["applicable"])
        self.assertIn("企业财报覆盖不适用", detail["reason"])

    def test_weights_match_investment_policy(self) -> None:
        self.assertEqual(
            FACTOR_WEIGHTS,
            {
                "fundamental": 0.40,
                "macro": 0.20,
                "technical": 0.20,
                "industry": 0.10,
                "other": 0.10,
            },
        )

    def test_fundamental_profile_is_exposed_for_current_fund_pool(self) -> None:
        result = evaluate_fund_factors(
            "378546",
            history([1 + index * 0.001 for index in range(150)]),
            holding(),
            {"macro_factors": []},
            [],
            fundamentals(),
        )
        self.assertEqual(result["details"]["fundamental"]["profile"], "resource")

    def test_resource_profile_allows_wider_cyclical_earnings_range(self) -> None:
        metrics = {"revenue_yoy": -0.20, "net_income_yoy": -0.30}
        balanced_score, _ = _company_fundamental_score(metrics, "balanced")
        resource_score, _ = _company_fundamental_score(metrics, "resource")
        self.assertGreater(resource_score, balanced_score)

    def test_fundamental_score_is_weighted_by_disclosed_positions(self) -> None:
        result = evaluate_fund_factors(
            "017653",
            history([1 + index * 0.001 for index in range(150)]),
            holding(),
            {"macro_factors": []},
            [],
            fundamentals(),
        )
        self.assertEqual(result["coverage"]["fundamental"], 1.0)
        self.assertGreater(result["scores"]["fundamental"], 60)
        self.assertAlmostEqual(
            result["contributions"]["fundamental"],
            round(result["scores"]["fundamental"] * 0.40, 1),
        )

    def test_yahoo_fundamentals_do_not_enter_production_score(self) -> None:
        yahoo_data = {
            code: {
                "fundamentals": {
                    "source": "yahoo-fundamentals",
                    "filed_at": "2026-02-01",
                    "fiscal_end": "2025-12-31",
                    "metrics": {
                        "revenue_yoy": 0.20,
                        "net_income_yoy": 0.25,
                        "net_margin": 0.18,
                        "operating_margin": 0.22,
                        "free_cash_flow_margin": 0.15,
                        "roe": 0.24,
                    },
                }
            }
            for code in ("AAA", "BBB")
        }
        result = evaluate_fund_factors(
            "017653",
            history([1 + index * 0.001 for index in range(150)]),
            holding(),
            {"macro_factors": []},
            [],
            yahoo_data,
        )
        self.assertEqual(result["coverage"]["fundamental"], 0.0)
        self.assertEqual(result["details"]["fundamental"]["companies"], [])

    def test_below_ma60_is_exposed_as_hard_alert_input(self) -> None:
        values = [1.0] * 130 + [1.0 - index * 0.005 for index in range(20)]
        result = evaluate_fund_factors(
            "017653", history(values), holding(), {}, [], fundamentals()
        )
        self.assertTrue(result["technical_flags"]["below_ma60"])

    def test_two_source_severe_event_creates_veto(self) -> None:
        news = [
            NewsItem(
                title="企业甲下调指引",
                source="东方财富 · 媒体甲",
                summary="企业甲下调年度盈利指引",
                direction="偏利空",
                affected_funds="017653",
            ),
            NewsItem(
                title="企业甲业绩预警获确认",
                source="新浪财经 · 媒体乙",
                summary="企业甲发布业绩预警",
                direction="偏利空",
                affected_funds="017653",
            ),
        ]
        result = evaluate_fund_factors(
            "017653",
            history([1 + index * 0.001 for index in range(150)]),
            holding(),
            {},
            news,
            fundamentals(),
        )
        self.assertTrue(result["hard_veto"])
        self.assertLess(result["scores"]["other"], 60)

    def test_real_oas_takes_priority_over_credit_proxy(self) -> None:
        macro = {
            "macro_factors": [
                {"key": "US_GDP", "value": 2.0, "frequency": "quarterly"},
                {"key": "US_CPI", "value": 2.5, "change_12_pct": 2.5},
                {"key": "US_UNEMPLOYMENT", "value": 4.2},
                {"key": "US_M2", "value": 21000, "change_12_pct": 3.0},
                {"key": "US_FED_FUNDS", "value": 4.0},
                {"key": "US_HY_SPREAD", "value": 3.0, "proxy": False},
                {"key": "US_CREDIT_STRESS_PROXY", "value": 7.0, "proxy": True, "semantic_status": "PROXY"},
            ]
        }

        result = _macro_factor(macro, [])

        self.assertIn("US_HY_SPREAD", result["available_keys"])
        self.assertNotIn("US_CREDIT_STRESS_PROXY", result["available_keys"])

    def test_higher_credit_proxy_stress_lowers_macro_score(self) -> None:
        common = [
            {"key": "US_GDP", "value": 2.0, "frequency": "quarterly"},
            {"key": "US_CPI", "value": 2.5, "change_12_pct": 2.5},
            {"key": "US_UNEMPLOYMENT", "value": 4.2},
            {"key": "US_M2", "value": 21000, "change_12_pct": 3.0},
            {"key": "US_FED_FUNDS", "value": 4.0},
        ]
        calm = _macro_factor(
            {"macro_factors": [*common, {"key": "US_CREDIT_STRESS_PROXY", "value": 0.5, "proxy": True, "semantic_status": "PROXY"}]},
            [],
        )
        stress = _macro_factor(
            {"macro_factors": [*common, {"key": "US_CREDIT_STRESS_PROXY", "value": 7.0, "proxy": True, "semantic_status": "PROXY"}]},
            [],
        )

        self.assertLess(stress["score"], calm["score"])


if __name__ == "__main__":
    unittest.main()
