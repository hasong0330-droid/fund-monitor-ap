from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio_config import (  # noqa: E402
    apply_quote_based_shares,
    delete_portfolio_position,
    load_portfolio_config,
    upsert_portfolio_position,
)


class PortfolioConfigTests(unittest.TestCase):
    def test_upsert_persists_market_value_pnl_and_purchase_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio_positions.json"
            result = upsert_portfolio_position("900001", 1200, -50, "2026-09-01", path)
            payload = load_portfolio_config(path)

        self.assertEqual(result["cost_basis"], 1250.0)
        self.assertEqual(payload["positions"]["900001"]["market_value"], 1200.0)
        self.assertEqual(payload["positions"]["900001"]["pnl"], -50.0)
        self.assertEqual(payload["positions"]["900001"]["purchase_date"], "2026-09-01")

    def test_invalid_cost_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio_positions.json"
            with self.assertRaisesRegex(ValueError, "正的累计成本"):
                upsert_portfolio_position("900001", 100, 100, "2026-09-01", path)

    def test_delete_purges_active_config_ledger_and_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "portfolio_positions.json"
            ledger_path = root / "portfolio_ledger.json"
            snapshots_path = root / "portfolio_snapshots.json"
            upsert_portfolio_position("900001", 1200, -50, "2026-09-01", config_path)
            ledger_path.write_text(
                '{"transactions":[{"code":"900001"},{"code":"378546"}]}',
                encoding="utf-8",
            )
            snapshots_path.write_text(
                '{"snapshots":[{"positions":{"900001":{"market_value":1200},"378546":{}}}]}',
                encoding="utf-8",
            )
            with patch("portfolio_config.PORTFOLIO_LEDGER_FILE", ledger_path), patch(
                "portfolio_config.PORTFOLIO_SNAPSHOTS_FILE", snapshots_path
            ):
                delete_portfolio_position("900001", config_path)
            payload = load_portfolio_config(config_path)
            self.assertNotIn("900001", payload["positions"])
            self.assertIn("900001", payload["disabled_codes"])
            self.assertNotIn("900001", ledger_path.read_text(encoding="utf-8"))
            self.assertNotIn("900001", snapshots_path.read_text(encoding="utf-8"))

    def test_quote_nav_fills_shares_for_model_valuation(self) -> None:
        portfolio = {
            "900001": {
                "market_value_input": 1200.0,
                "shares": 0.0,
            }
        }
        apply_quote_based_shares(portfolio, {"900001": SimpleNamespace(nav=2.5)})
        self.assertEqual(portfolio["900001"]["shares"], 480.0)


if __name__ == "__main__":
    unittest.main()
