from datetime import date, timedelta
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant_model import build_portfolio_risk_budget  # noqa: E402


def _history(code: str, count: int = 220) -> dict:
    start = date(2024, 1, 1)
    values = []
    for index in range(count):
        if code == "C":
            nav = 1.0 + index * 0.0008 + (0.004 if index % 2 else 0)
        elif code == "B":
            nav = 1.0 + index * 0.0005
        else:
            nav = 1.0 + index * 0.001
        values.append(
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "nav": nav,
            }
        )
    return {"records": values}


def _holding(stock_code: str) -> dict:
    return {
        "holdings": [
            {"code": f"{stock_code}1", "name": "Company 1", "weight": 0.20},
            {"code": f"{stock_code}2", "name": "Company 2", "weight": 0.15},
        ]
    }


class PortfolioRiskBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.histories = {
            "018229": _history("A"),
            "118001": _history("B"),
            "005534": _history("C"),
        }
        self.holdings = {
            "018229": _holding("A"),
            "118001": _holding("B"),
            "005534": _holding("C"),
        }
        self.positions = {
            "018229": {"invested_amount": 600.0},
            "118001": {"invested_amount": 400.0},
        }

    def test_budget_reports_correlation_block(self) -> None:
        result = build_portfolio_risk_budget(
            "018229",
            self.positions,
            self.histories,
            self.holdings,
            candidate_holding=self.holdings["018229"],
        )

        self.assertTrue(result["available"])
        self.assertFalse(result["allowed"])
        self.assertGreaterEqual(result["candidate_correlation"], 0.70)
        self.assertTrue(any("高相关" in item for item in result["blocking_reasons"]))

    def test_budget_returns_capacity_for_low_correlation_candidate(self) -> None:
        result = build_portfolio_risk_budget(
            "005534",
            self.positions,
            self.histories,
            self.holdings,
            candidate_holding=self.holdings["005534"],
        )

        self.assertTrue(result["available"])
        self.assertGreater(result["max_additional_weight"], 0)
        self.assertGreater(result["recommended_target_weight"], 0)
        self.assertIn("current_volatility", result)
        self.assertIn("weighted_overlap", result)

    def test_budget_blocks_when_history_is_not_sufficient(self) -> None:
        short_histories = {**self.histories, "005534": _history("C", 20)}
        result = build_portfolio_risk_budget(
            "005534",
            self.positions,
            short_histories,
            self.holdings,
            candidate_holding=self.holdings["005534"],
        )

        self.assertFalse(result["available"])
        self.assertFalse(result["allowed"])
        self.assertTrue(result["blocking_reasons"])


if __name__ == "__main__":
    unittest.main()
