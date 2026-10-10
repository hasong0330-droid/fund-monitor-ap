from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from source_router import (  # noqa: E402
    is_live_source,
    is_research_reference_source,
    is_trading_eligible_source,
    live_source_names,
    ordered_source_handlers,
    source_provenance,
    sources_are_independent,
)


class SourceRouterTests(unittest.TestCase):
    def test_live_registry_is_shared_by_category(self) -> None:
        self.assertTrue(is_live_source("ifind-edb", "macro"))
        self.assertIn("ifind-edb", live_source_names("macro"))
        self.assertTrue(is_live_source("ifind-financials", "fundamentals"))
        self.assertFalse(is_live_source("cache", "macro"))

    def test_index_handlers_follow_configured_public_fallback_order(self) -> None:
        handlers = {
            "yahoo": lambda: "yahoo",
            "eastmoney-index": lambda: "eastmoney",
            "tencent-index": lambda: "tencent",
        }

        ordered = [source for source, _ in ordered_source_handlers("indexes", handlers)]

        self.assertEqual(ordered, ["tencent-index", "eastmoney-index", "yahoo"])

    def test_same_vendor_endpoints_are_not_independent_confirmation(self) -> None:
        self.assertFalse(
            sources_are_independent(["eastmoney-fund-quote", "eastmoney-pingzhongdata"])
        )
        self.assertTrue(sources_are_independent(["eastmoney-holdings", "sina-holdings"]))

    def test_cache_is_never_a_trading_eligible_source(self) -> None:
        cached = source_provenance("fund_nav", "cache")
        live = source_provenance("fund_nav", "eastmoney-fund-quote")

        self.assertFalse(cached["trading_eligible"])
        self.assertTrue(live["trading_eligible"])
        self.assertEqual(live["provider_group"], "eastmoney")

    def test_ifind_is_a_separate_vendor_and_follows_macro_policy(self) -> None:
        handlers = {
            "ifind-edb": lambda: "ifind",
            "tradingeconomics": lambda: "tradingeconomics",
            "fred": lambda: "fred",
        }

        ordered = [source for source, _ in ordered_source_handlers("macro", handlers)]

        self.assertEqual(ordered, ["fred", "ifind-edb", "tradingeconomics"])
        self.assertTrue(sources_are_independent(["ifind-edb", "fred"]))

    def test_ifind_fund_nav_is_independent_from_eastmoney(self) -> None:
        self.assertTrue(is_live_source("ifind-fund-nav", "fund_nav"))
        self.assertTrue(
            sources_are_independent(["eastmoney-fund-quote", "ifind-fund-nav"])
        )

    def test_official_fund_manager_source_is_live_and_prioritized(self) -> None:
        self.assertTrue(is_live_source("fund-manager", "fund_nav"))
        self.assertTrue(is_live_source("fund-manager-history", "fund_history"))
        self.assertEqual(source_provenance("fund_nav", "fund-manager")["priority"], 1)

    def test_browser_fund_source_is_live_but_separate_from_public_vendor(self) -> None:
        self.assertTrue(is_live_source("browser-fund-page", "fund_nav"))
        self.assertTrue(sources_are_independent(["browser-fund-page", "eastmoney-fund-quote"]))

    def test_yahoo_is_research_only_and_not_trading_eligible(self) -> None:
        for source, category in (
            ("yahoo", "indexes"),
            ("yahoo-stock", "stock_trends"),
            ("yahoo-fundamentals", "fundamentals"),
        ):
            self.assertTrue(is_research_reference_source(source))
            self.assertFalse(is_trading_eligible_source(source))
            self.assertFalse(is_live_source(source, category))
        self.assertFalse(sources_are_independent(["yahoo", "eastmoney-index"]))


if __name__ == "__main__":
    unittest.main()
