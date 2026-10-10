import tempfile
import unittest
from datetime import date
from pathlib import Path

import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio_ledger import (  # noqa: E402
    PortfolioLedgerError,
    calculate_positions,
    load_or_initialize_ledger,
    refresh_portfolio_ledger,
)


class Quote:
    def __init__(self, nav: float, when: str = "2026-08-06") -> None:
        self.nav = nav
        self.date = when
        self.source = "eastmoney"


class PortfolioLedgerTests(unittest.TestCase):
    def test_opening_balance_preserves_existing_cost_and_shares(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger, status = load_or_initialize_ledger(
                {"018229": {"invested_amount": 2998.83, "shares": 1414.9}},
                Path(directory) / "ledger.json",
            )
        self.assertEqual(status, "INITIALIZED")
        position = calculate_positions(ledger, {"018229": Quote(2.1)})["018229"]
        self.assertEqual(position["shares"], 1414.9)
        self.assertEqual(position["cost_basis"], 2998.83)
        self.assertAlmostEqual(position["cost_nav"], 2998.83 / 1414.9, places=6)

    def test_moving_cost_and_realized_profit_are_calculated(self) -> None:
        ledger = {
            "version": 1,
            "transactions": [
                {"type": "BUY", "code": "A", "shares": 100, "amount": 100},
                {"type": "BUY", "code": "A", "shares": 50, "amount": 60},
                {"type": "SELL", "code": "A", "shares": 30, "amount": 42},
            ],
        }
        position = calculate_positions(ledger, {"A": Quote(1.2)})["A"]
        self.assertEqual(position["shares"], 120.0)
        self.assertAlmostEqual(position["cost_basis"], 128.0)
        self.assertAlmostEqual(position["realized_pnl"], 10.0)
        self.assertAlmostEqual(position["market_value"], 144.0)
        self.assertAlmostEqual(position["unrealized_pnl"], 16.0)
        self.assertAlmostEqual(position["total_pnl"], 26.0)

    def test_daily_snapshot_calculates_change_only_for_unchanged_shares(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            portfolio = {"A": {"invested_amount": 100.0, "shares": 100.0}}
            refresh_portfolio_ledger(
                {"A": Quote(1.0, "2026-08-05")},
                portfolio,
                root / "ledger.json",
                root / "snapshots.json",
                date(2026, 8, 5),
            )
            result = refresh_portfolio_ledger(
                {"A": Quote(1.1, "2026-08-06")},
                portfolio,
                root / "ledger.json",
                root / "snapshots.json",
                date(2026, 8, 6),
            )
        self.assertEqual(result["positions"]["A"]["daily_pnl"], 10.0)
        self.assertEqual(result["snapshot_count"], 2)

    def test_oversell_is_rejected(self) -> None:
        ledger = {
            "version": 1,
            "transactions": [
                {"type": "BUY", "code": "A", "shares": 1, "amount": 1},
                {"type": "SELL", "code": "A", "shares": 2, "amount": 2},
            ],
        }
        with self.assertRaises(PortfolioLedgerError):
            calculate_positions(ledger, {"A": Quote(1.0)})


if __name__ == "__main__":
    unittest.main()
