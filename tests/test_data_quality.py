from datetime import date, timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_quality import (  # noqa: E402
    BLOCKED,
    CACHED,
    LIMITED,
    LIVE,
    STATIC,
    STALE,
    _confidence_score,
    _fundamental_quality_record,
    _is_trading_day,
    _trading_day_age,
    _macro_quality_summary,
    _quality_record,
    build_data_quality_report,
    resolve_fund_navs,
)
from analyzer import (  # noqa: E402
    _index_rule,
    analyze_market,
    analyze_recommendations,
    analyze_target_funds,
)
import analyzer  # noqa: E402
from fetcher import FundQuote, NewsItem  # noqa: E402
from fetcher import IndexSnapshot  # noqa: E402
import fetcher  # noqa: E402
from browser_source import parse_fund_detail_page  # noqa: E402
from holding_alert import evaluate_portfolio_alerts  # noqa: E402


TODAY = date(2026, 7, 29)


def iso(days_ago: int = 0) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat()


def history(source: str = "eastmoney-history", days_ago: int = 0) -> dict:
    end = TODAY - timedelta(days=days_ago)
    records = [
        {"date": (end - timedelta(days=offset)).isoformat(), "nav": 1.0 + offset / 1000}
        for offset in reversed(range(80))
    ]
    return {
        "records": records,
        "source": source,
        "latest_date": end.isoformat(),
    }


def market(index_days_ago: int = 0, source: str = "eastmoney-index") -> dict:
    return {
        "indexes": [
            {
                "symbol": "IXIC",
                "name": "纳斯达克综合指数",
                "date": iso(index_days_ago),
                "source": source,
            },
            {
                "symbol": "DJI",
                "name": "道琼斯工业指数",
                "date": iso(index_days_ago),
                "source": source,
            },
        ],
        "macro_factors": [
            {"key": "VIX", "value": 18.0, "date": iso(), "source": "fred", "frequency": "daily"},
            {"key": "US2Y", "value": 4.0, "date": iso(), "source": "fred", "frequency": "daily"},
            {"key": "US10Y", "value": 4.2, "date": iso(), "source": "fred", "frequency": "daily"},
            {"key": "US10Y_REAL", "value": 1.8, "date": iso(), "source": "fred", "frequency": "daily"},
            {"key": "DXY", "value": 100.0, "change_20_pct": 0.0, "date": iso(), "source": "fred", "frequency": "daily"},
            {"key": "USDCNY", "value": 7.1, "change_20_pct": 0.0, "date": iso(), "source": "fred", "frequency": "daily"},
            {"key": "US_GDP", "value": 2.0, "date": iso(), "source": "fred", "frequency": "quarterly"},
            {"key": "US_CPI", "value": 300.0, "change_12_pct": 2.5, "date": iso(), "source": "fred", "frequency": "monthly"},
            {"key": "US_UNEMPLOYMENT", "value": 4.2, "date": iso(), "source": "fred", "frequency": "monthly"},
            {"key": "US_M2", "value": 21000.0, "change_12_pct": 4.0, "date": iso(), "source": "fred", "frequency": "monthly"},
            {"key": "US_FED_FUNDS", "value": 4.0, "date": iso(), "source": "fred", "frequency": "monthly"},
            {"key": "US_HY_SPREAD", "value": 3.5, "date": iso(), "source": "fred", "frequency": "daily"},
        ],
    }


def live_news() -> list[NewsItem]:
    return [
        NewsItem(
            title="全球科技行业保持观察",
            source="东方财富 · 测试媒体",
            summary="测试用实时来源新闻。",
            published_at=iso(),
            affected_funds="018229",
            direction="中性观察",
        )
    ]


def holding(source: str = "eastmoney-holdings") -> dict:
    return {
        "disclosure_date": iso(30),
        "source": source,
        "holdings": [{"code": "NVDA", "name": "英伟达", "weight": 0.1}],
    }


def quality_for(
    quote_source: str = "eastmoney",
    nav_days_ago: int = 0,
    history_source: str = "eastmoney-history",
    history_days_ago: int = 0,
    index_days_ago: int = 0,
) -> dict:
    code = "018229"
    quotes = {
        code: FundQuote(code, "测试基金", 1.2, iso(nav_days_ago), source=quote_source)
    }
    histories = {code: history(history_source, history_days_ago)}
    return build_data_quality_report(
        [code],
        [],
        quotes,
        histories,
        {code: holding()},
        market(index_days_ago),
        live_news(),
        today=TODAY,
    )


class ResolveFundNavTests(unittest.TestCase):
    def setUp(self) -> None:
        # Unit tests that exercise network fallback order must not depend on a
        # browser cache left by a real dashboard run. Individual browser-cache
        # tests override this patch with their explicit fixture.
        self.browser_cache_patcher = patch.object(
            fetcher, "load_browser_quote_cache", return_value={}
        )
        self.browser_cache_patcher.start()
        self.addCleanup(self.browser_cache_patcher.stop)

    def test_browser_quote_cache_is_used_before_blocked_network_sources(self) -> None:
        browser_item = {
            "016702": {
                "code": "016702",
                "name": "浏览器基金",
                "nav": 2.0571,
                "date": "2026-08-28",
            }
        }
        with patch.object(fetcher, "TARGET_FUNDS", ["016702"]), patch.object(
            fetcher, "RECOMMENDATION_POOL", {}
        ), patch.object(fetcher, "PORTFOLIO_HOLDINGS", {}), patch.object(
            fetcher, "load_browser_quote_cache", return_value=browser_item
        ), patch.object(fetcher, "_fetch_fund_quote_batch", side_effect=RuntimeError("blocked")), patch.object(
            fetcher, "_fetch_fund_quote_js", side_effect=RuntimeError("blocked")
        ), patch.object(fetcher, "ifind_is_configured", return_value=False), patch.object(
            fetcher, "_merge_estimate_quotes"
        ):
            result = fetcher.fetch_fund_quotes()

        self.assertEqual(result["016702"].source, "browser-fund-page")
        self.assertEqual(result["016702"].nav, 2.0571)

    def test_fund_quote_fetch_accepts_dynamic_portfolio_codes(self) -> None:
        quote = fetcher.FundQuote(
            code="900001", name="动态持仓测试基金", nav=1.25, date="2026-09-22"
        )
        with patch.object(fetcher, "TARGET_FUNDS", []), patch.object(
            fetcher, "RECOMMENDATION_POOL", {}
        ), patch.object(fetcher, "PORTFOLIO_HOLDINGS", {}), patch.object(
            fetcher, "load_browser_quote_cache", return_value={}
        ), patch.object(fetcher, "_fetch_fund_quote_batch", return_value={"900001": quote}), patch.object(
            fetcher, "_merge_estimate_quotes"
        ):
            result = fetcher.fetch_fund_quotes(["900001"])

        self.assertIn("900001", result)
        self.assertEqual(result["900001"].nav, 1.25)

    def test_browser_fund_detail_parser_extracts_published_nav_and_returns(self) -> None:
        html = """
        <html><head><title>银华海外数字经济量化选股混合发起式(QDII)C(016702)</title></head>
        <body>
          单位净值 (2026-08-28)
          2.0571-0.95%
          近1月：6.76%
        </body></html>
        """
        result = parse_fund_detail_page("016702", html, captured_at="2026-09-01T15:00:00")
        self.assertEqual(result["source"], "browser-fund-page")
        self.assertEqual(result["name"], "银华海外数字经济量化选股混合发起式(QDII)C")
        self.assertEqual(result["nav"], 2.0571)
        self.assertEqual(result["date"], "2026-08-28")
        self.assertEqual(result["daily_return_pct"], -0.95)
        self.assertEqual(result["monthly_return_pct"], 6.76)

    def test_browser_fund_detail_parser_rejects_missing_published_date(self) -> None:
        with self.assertRaises(ValueError):
            parse_fund_detail_page("016702", "<body>单位净值 2.0571 估算净值 2.0600</body>")

    def test_network_preflight_reports_optional_source_configuration(self) -> None:
        connection = type("Connection", (), {"close": lambda self: None})()
        with patch.object(fetcher.socket, "getaddrinfo", return_value=[(None, None, None, None, None)]), patch.object(
            fetcher.socket, "create_connection", return_value=connection
        ), patch.object(fetcher, "ifind_is_configured", return_value=True), patch.object(
            fetcher, "FUND_MANAGER_NAV_URL_TEMPLATE", "https://official.test/{code}"
        ), patch.object(fetcher, "FUND_MANAGER_HISTORY_URL_TEMPLATE", "https://official.test/{code}/history"):
            result = fetcher.run_network_preflight()

        self.assertEqual(result["status"], "AVAILABLE")
        self.assertTrue(result["ifind_configured"])
        self.assertTrue(result["official_fund_nav_configured"])
        self.assertTrue(result["official_fund_history_configured"])
        self.assertGreaterEqual(len(result["probes"]), 5)

    def test_official_fund_manager_quote_parser_accepts_json_envelope(self) -> None:
        payload = {
            "data": {
                "records": [
                    {
                        "code": "016702",
                        "name": "官方测试基金",
                        "unit_nav": "1.2345",
                        "nav_date": "2026-07-29",
                    }
                ]
            }
        }

        with patch.object(fetcher, "_fetch_official_payload", return_value=payload), patch.object(
            fetcher, "FUND_MANAGER_NAV_URL_TEMPLATE", "https://official.test/{code}"
        ):
            quote = fetcher._fetch_fund_manager_quote("016702")

        self.assertEqual(quote.source, "fund-manager")
        self.assertEqual(quote.name, "官方测试基金")
        self.assertEqual(quote.nav, 1.2345)
        self.assertEqual(quote.date, "2026-07-29")

    def test_official_fund_manager_history_parser_rejects_short_history(self) -> None:
        payload = {"records": [{"date": "2026-07-29", "nav": 1.2}]}
        with patch.object(fetcher, "_fetch_official_payload", return_value=payload), patch.object(
            fetcher, "FUND_MANAGER_HISTORY_URL_TEMPLATE", "https://official.test/{code}"
        ):
            with self.assertRaises(ValueError):
                fetcher._fetch_fund_manager_history("016702")

    def test_fund_holdings_fetch_prioritizes_actual_portfolio(self) -> None:
        requested = ["018229", "017653", "378546", "002891"]
        started = []

        def fetch_one(code: str) -> dict:
            started.append(code)
            return holding()

        with tempfile.TemporaryDirectory() as directory, patch.object(
            fetcher, "FUND_HOLDINGS_CACHE_FILE", Path(directory) / "holdings.json"
        ), patch.object(fetcher, "_fetch_fund_holdings", side_effect=fetch_one):
            result = fetcher.fetch_fund_holdings(requested)

        self.assertEqual(set(started[:2]), {"002891", "378546"})
        self.assertEqual(set(result), set(requested))

    def test_fund_holdings_fetch_accepts_dynamic_portfolio_priority(self) -> None:
        requested = ["018229", "900001"]
        started = []

        def fetch_one(code: str) -> dict:
            started.append(code)
            return holding()

        with tempfile.TemporaryDirectory() as directory, patch.object(
            fetcher, "FUND_HOLDINGS_CACHE_FILE", Path(directory) / "holdings.json"
        ), patch.object(fetcher, "_fetch_fund_holdings", side_effect=fetch_one):
            result = fetcher.fetch_fund_holdings(requested, portfolio_codes=["900001"])

        self.assertEqual(started[0], "900001")
        self.assertEqual(set(result), set(requested))

    def test_holdings_fallback_queries_calendar_quarter_end(self) -> None:
        requested_dates = []

        def fetch_sina(code: str, query_date: str) -> dict:
            requested_dates.append(query_date)
            return {
                "disclosure_date": query_date,
                "holdings": [
                    {"code": f"TEST{index}", "name": f"测试持仓{index}", "weight": 0.05}
                    for index in range(5)
                ],
            }

        with patch.object(
            fetcher, "_fetch_eastmoney_holdings", side_effect=ValueError("busy")
        ), patch.object(
            fetcher, "_fetch_sina_holdings", side_effect=fetch_sina
        ), patch.object(fetcher, "_source_is_open", return_value=False):
            fetcher._fetch_fund_holdings("018229")

        self.assertTrue(requested_dates)
        self.assertIn(requested_dates[0][5:], {"03-31", "06-30", "09-30", "12-31"})

    def test_non_equity_fund_asset_allocation_is_accepted(self) -> None:
        script = (
            'var Data_assetAllocation = {"series":['
            '{"name":"现金占比","data":[19.55]},'
            '{"name":"其他资产","data":[7.63]},'
            '{"name":"净资产","type":"line","data":[7.6279]}],'
            '"categories":["2026-06-30"]};'
        )
        with patch.object(fetcher, "_http_get", return_value=script):
            record = fetcher._fetch_fund_holdings("161815")

        self.assertEqual(record["source"], "eastmoney-asset-allocation")
        self.assertTrue(record["allocation_only"])
        self.assertEqual(record["disclosure_date"], "2026-06-30")
        self.assertEqual(len(record["asset_allocation"]), 2)
        self.assertNotIn(
            "净资产", {item["name"] for item in record["asset_allocation"]}
        )

    def test_regular_fund_does_not_fall_back_to_asset_allocation(self) -> None:
        with (
            patch.object(fetcher, "_fetch_eastmoney_holdings", side_effect=ValueError("no holdings")),
            patch.object(fetcher, "_fetch_sina_holdings", side_effect=ValueError("no holdings")),
            patch.object(fetcher, "_fetch_ths_holdings", side_effect=ValueError("no holdings")),
            patch.object(fetcher, "_fetch_fund_asset_allocation") as allocation,
            patch.object(fetcher, "_source_is_open", return_value=False),
            self.assertRaises(ValueError),
        ):
            fetcher._fetch_fund_holdings("018229")

        allocation.assert_not_called()

    def test_fred_ranges_limit_large_series_downloads(self) -> None:
        gdp = fetcher._fred_csv_params("US_GDP", "GDP")
        spread = fetcher._fred_csv_params("US_HY_SPREAD", "OAS")

        self.assertEqual(gdp["id"], "GDP")
        self.assertIn("cosd", gdp)
        self.assertIn("coed", gdp)
        self.assertLess(gdp["cosd"], spread["cosd"])

    def test_fred_direct_fallback_bypasses_open_primary_circuit(self) -> None:
        direct = {
            "key": "US_GDP",
            "name": "GDP",
            "value": 2.1,
            "date": "2026-04-01",
            "source": "fred",
            "frequency": "quarterly",
            "semantic_status": "COMPLETE",
        }
        with (
            patch.object(fetcher, "_http_get", side_effect=TimeoutError("timeout")),
            patch.object(fetcher, "_source_is_open", return_value=True),
            patch.object(fetcher, "_fetch_fred_csv_direct", return_value=direct) as fallback,
        ):
            result = fetcher._fetch_macro_factor("US_GDP", "GDP", "GDP")

        self.assertEqual(result, direct)
        fallback.assert_called_once()

    def test_gdp_and_oas_use_single_short_fred_then_ifind(self) -> None:
        for key in ("US_GDP", "US_HY_SPREAD"):
            ifind_result = {
                "key": key,
                "value": 2.1,
                "date": "2026-08-25",
                "source": "ifind-edb",
            }
            with self.subTest(key=key), patch.object(
                fetcher,
                "_fetch_fred_csv_direct",
                side_effect=TimeoutError("timeout"),
            ) as fred, patch.object(
                fetcher, "ifind_is_configured", return_value=True
            ), patch.object(
                fetcher,
                "_fetch_ifind_macro_factor",
                return_value=ifind_result,
            ) as ifind, patch.object(
                fetcher, "_http_get"
            ) as generic_fred, patch.object(
                fetcher, "_fetch_bea_gdp_factor"
            ) as bea:
                result = fetcher._fetch_macro_factor(key, "SERIES", key)

            self.assertEqual(result, ifind_result)
            fred.assert_called_once()
            self.assertEqual(fred.call_args.kwargs, {"timeout": 8, "attempts": 1})
            ifind.assert_called_once_with(key, key)
            generic_fred.assert_not_called()
            bea.assert_not_called()

    def test_other_fred_series_use_two_twelve_second_attempts(self) -> None:
        csv_text = "observation_date,SERIES\n" + "\n".join(
            f"2026-07-{day:02d},{1 + day / 100:.2f}" for day in range(1, 26)
        )
        with patch.object(fetcher, "_http_get", return_value=csv_text) as request:
            result = fetcher._fetch_macro_factor("TEST_SERIES", "SERIES", "测试")

        self.assertEqual(result["source"], "fred")
        self.assertEqual(request.call_args.kwargs["timeout"], 12)
        self.assertEqual(request.call_args.kwargs["attempts"], 2)

    def test_yahoo_fundamentals_parser_supports_non_us_listing(self) -> None:
        def rows(current: float, previous: float) -> list[dict]:
            return [
                {"asOfDate": "2024-12-31", "reportedValue": {"raw": previous}},
                {"asOfDate": "2025-12-31", "reportedValue": {"raw": current}},
            ]

        result = {
            "timeseries": {
                "result": [
                    {"annualTotalRevenue": rows(120, 100)},
                    {"annualNetIncome": rows(15, 10)},
                    {"annualOperatingIncome": rows(24, 16)},
                    {"annualOperatingCashFlow": rows(26, 18)},
                    {"annualCapitalExpenditure": rows(6, 5)},
                    {"annualStockholdersEquity": rows(60, 50)},
                ]
            }
        }
        with patch.object(fetcher, "_http_get", return_value=__import__("json").dumps(result)):
            parsed = fetcher._fetch_yahoo_company_fundamentals("000660")

        self.assertEqual(parsed["source"], "yahoo-fundamentals")
        self.assertEqual(parsed["symbol"], "000660.KS")
        self.assertAlmostEqual(parsed["metrics"]["revenue_yoy"], 0.20)
        self.assertAlmostEqual(parsed["metrics"]["roe"], 0.25)

    def test_stale_fundamentals_cache_is_refetched_even_when_saved_today(self) -> None:
        stale = {
            "source": "sec-companyfacts",
            "fetched_at": TODAY.isoformat(),
            "fiscal_end": "2024-01-01",
        }
        with patch.object(fetcher, "date") as mocked_date:
            mocked_date.today.return_value = TODAY
            self.assertFalse(fetcher._fundamentals_cache_is_fresh(stale))

    def test_yahoo_current_dated_data_is_research_reference_only(self) -> None:
        record = _quality_record(
            "index:IXIC",
            "纳斯达克综合指数",
            TODAY.isoformat(),
            "yahoo",
            2,
            "2026-07-29 10:00:00",
            TODAY,
            required=True,
        )
        self.assertEqual(record.status, STATIC)
        self.assertIn("研究参考", record.message)

    def test_yahoo_fundamentals_do_not_enter_production_coverage(self) -> None:
        record = _fundamental_quality_record(
            "018229",
            {"holdings": [{"code": "AAA", "name": "企业甲", "weight": 1.0}]},
            {
                "AAA": {
                    "fundamentals": {
                        "source": "yahoo-fundamentals",
                        "fiscal_end": "2025-12-31",
                        "filed_at": "2026-02-01",
                        "metrics": {
                            "revenue_yoy": 0.1,
                            "net_income_yoy": 0.1,
                            "operating_margin": 0.2,
                        },
                    }
                }
            },
            "2026-07-29 10:00:00",
            TODAY,
        )
        self.assertEqual(record.details["fundamental_coverage"], 0.0)
        self.assertEqual(record.details["source_integrity"], 0.0)

    def test_allocation_only_fundamentals_are_not_applicable(self) -> None:
        record = _fundamental_quality_record(
            "161815",
            {
                "holdings": [],
                "allocation_only": True,
                "disclosure_date": "2026-06-30",
                "source": "eastmoney-asset-allocation",
            },
            {},
            "2026-07-29 10:00:00",
            TODAY,
        )

        self.assertEqual(record.status, "LIVE")
        self.assertFalse(record.required)
        self.assertFalse(record.details["applicable"])
        self.assertIn("企业财报覆盖不适用", record.message)

    def test_financials_are_kept_when_stock_trend_fetch_fails(self) -> None:
        code = "000660"
        with tempfile.TemporaryDirectory() as directory, patch.object(
            fetcher, "HOLDING_STOCK_TRENDS_CACHE_FILE", Path(directory) / "stocks.json"
        ), patch.object(
            fetcher, "_fetch_holding_stock_trend", side_effect=RuntimeError("行情不可用")
        ), patch.object(
            fetcher,
            "_resolve_stock_fundamentals",
            return_value={
                "source": "yahoo-fundamentals",
                "metrics": {"revenue_yoy": 0.1},
                "metric_coverage": 0.5,
            },
        ):
            result = fetcher.fetch_holding_stock_trends(
                {"018229": {"holdings": [{"code": code, "name": "测试公司", "weight": 0.1}]}}
            )

        self.assertEqual(result[code]["source"], "baseline")
        self.assertEqual(result[code]["fundamentals"]["source"], "yahoo-fundamentals")

    def test_sec_company_facts_builds_comparable_fundamental_metrics(self) -> None:
        def annual(concept: str, previous: float, current: float) -> tuple[str, dict]:
            return concept, {
                "units": {
                    "USD": [
                        {"end": "2024-12-31", "filed": "2025-02-01", "form": "10-K", "fp": "FY", "val": previous},
                        {"end": "2025-12-31", "filed": "2026-02-01", "form": "10-K", "fp": "FY", "val": current},
                    ]
                }
            }

        facts = dict(
            [
                annual("Revenues", 100.0, 120.0),
                annual("NetIncomeLoss", 10.0, 15.0),
                annual("OperatingIncomeLoss", 16.0, 24.0),
                annual("NetCashProvidedByUsedInOperatingActivities", 18.0, 26.0),
                annual("PaymentsToAcquirePropertyPlantAndEquipment", 5.0, 6.0),
                annual("StockholdersEquity", 50.0, 60.0),
            ]
        )
        result = fetcher._parse_sec_company_facts({"facts": {"us-gaap": facts}})

        self.assertAlmostEqual(result["metrics"]["revenue_yoy"], 0.20)
        self.assertAlmostEqual(result["metrics"]["net_income_yoy"], 0.50)
        self.assertAlmostEqual(result["metrics"]["operating_margin"], 0.20)
        self.assertAlmostEqual(result["metrics"]["free_cash_flow_margin"], 1 / 6)
        self.assertAlmostEqual(result["metrics"]["roe"], 0.25)
        self.assertEqual(result["metric_coverage"], 1.0)

    def test_sec_history_retains_original_and_amended_filing_dates(self) -> None:
        def annual(concept: str, prior: float, original: float, amended: float) -> tuple[str, dict]:
            return concept, {
                "units": {
                    "USD": [
                        {"end": "2023-12-31", "filed": "2024-02-01", "form": "10-K", "fp": "FY", "val": prior, "accn": "prior"},
                        {"end": "2024-12-31", "filed": "2025-02-01", "form": "10-K", "fp": "FY", "val": original, "accn": "original"},
                        {"end": "2024-12-31", "filed": "2025-04-01", "form": "10-K/A", "fp": "FY", "val": amended, "accn": "amended"},
                    ]
                }
            }

        facts = dict([
            annual("Revenues", 100.0, 120.0, 110.0),
            annual("NetIncomeLoss", 10.0, 15.0, 13.0),
            annual("OperatingIncomeLoss", 16.0, 24.0, 21.0),
            annual("NetCashProvidedByUsedInOperatingActivities", 18.0, 26.0, 24.0),
            annual("PaymentsToAcquirePropertyPlantAndEquipment", 5.0, 6.0, 6.0),
            annual("StockholdersEquity", 50.0, 60.0, 58.0),
        ])

        records = fetcher._parse_sec_company_facts_history({"facts": {"us-gaap": facts}})
        fiscal_2024 = [item for item in records if item["fiscal_end"] == "2024-12-31"]

        self.assertEqual([item["filed_at"] for item in fiscal_2024], ["2025-02-01", "2025-04-01"])
        self.assertEqual([item["metrics"]["revenue"] for item in fiscal_2024], [120.0, 110.0])

    def test_source_circuit_opens_after_repeated_systemic_failures(self) -> None:
        fetcher.reset_source_health()
        callback = unittest.mock.Mock(
            side_effect=RuntimeError("all network stacks failed: WinError 10013")
        )

        for _ in range(2):
            with self.assertRaises(RuntimeError):
                fetcher._call_source("测试源", callback)
        with self.assertRaisesRegex(RuntimeError, "本轮已熔断"):
            fetcher._call_source("测试源", callback)

        self.assertEqual(callback.call_count, 2)
        self.assertTrue(fetcher.source_health_snapshot()["测试源"]["open"])

    def test_failed_network_preflight_does_not_skip_source_callback(self) -> None:
        fetcher.reset_source_health()
        callback = unittest.mock.Mock(return_value={"source": "备用源", "value": 1})
        try:
            with fetcher._SOURCE_HEALTH_LOCK:
                fetcher._NETWORK_PREFLIGHT.update(
                    status="UNAVAILABLE", available=False, checked_at=iso()
                )
            result = fetcher._call_source("备用源", callback)

            self.assertEqual(result["value"], 1)
            self.assertTrue(callback.called)
            audit = fetcher.fetch_audit_snapshot()
            self.assertEqual(audit[-1]["source"], "备用源")
            self.assertEqual(audit[-1]["status"], "SUCCESS")
        finally:
            fetcher.reset_source_health()

    def test_preflight_failure_still_attempts_fund_quote_fallback(self) -> None:
        fetcher.reset_source_health()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            fetcher, "FUND_CACHE_FILE", Path(directory) / "quotes.json"
        ), patch.object(
            fetcher,
            "_fetch_fund_quote_batch",
            side_effect=RuntimeError("主源不可用"),
        ), patch.object(
            fetcher,
            "_fetch_fund_quote_js",
            side_effect=lambda code: FundQuote(code, code, 1.2, iso(), source="eastmoney-pingzhongdata"),
        ), patch.object(fetcher, "ifind_is_configured", return_value=False), patch.object(fetcher, "_merge_estimate_quotes"):
            with fetcher._SOURCE_HEALTH_LOCK:
                fetcher._NETWORK_PREFLIGHT.update(
                    status="UNAVAILABLE", available=False, checked_at=iso()
                )
            quotes = fetcher.fetch_fund_quotes()

        codes = list(dict.fromkeys([
            *fetcher.TARGET_FUNDS,
            *fetcher.RECOMMENDATION_POOL,
            *fetcher.PORTFOLIO_HOLDINGS,
        ]))
        self.assertTrue(
            all(quotes[code].source == "eastmoney-pingzhongdata" for code in codes),
            repr({code: (quotes.get(code).source if quotes.get(code) else None) for code in codes}),
        )
        audit_sources = {item["source"] for item in fetcher.fetch_audit_snapshot()}
        self.assertIn("eastmoney-fund-quote", audit_sources)
        self.assertIn("eastmoney-pingzhongdata", audit_sources)
        fetcher.reset_source_health()

    def test_history_primary_failure_returns_backup_without_abort(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            fetcher, "FUND_HISTORY_CACHE_FILE", Path(directory) / "history.json"
        ), patch.object(
            fetcher, "_fetch_fund_history", side_effect=RuntimeError("主源不可用")
        ), patch.object(
            fetcher,
            "_fetch_fund_history_backup",
            return_value={
                "records": [{"date": iso(), "nav": 1.2}],
                "source": "eastmoney-pingzhongdata",
                "latest_date": iso(),
            },
        ):
            histories = fetcher.fetch_fund_histories(["018229"])

        self.assertEqual(histories["018229"]["source"], "eastmoney-pingzhongdata")
        self.assertIn("已切换备用接口", histories["018229"]["error"])

    def test_history_dual_source_failure_returns_cache_without_abort(self) -> None:
        cached = {
            "funds": {
                "018229": {
                    "records": [{"date": iso(1), "nav": 1.1}],
                    "source": "eastmoney-history",
                    "latest_date": iso(1),
                }
            }
        }
        with patch.object(fetcher, "_load_json", return_value=cached), patch.object(
            fetcher, "_fetch_fund_history", side_effect=RuntimeError("主源不可用")
        ), patch.object(
            fetcher, "_fetch_fund_history_backup", side_effect=RuntimeError("备用源不可用")
        ):
            histories = fetcher.fetch_fund_histories(["018229"])

        self.assertEqual(histories["018229"]["source"], "cache")
        self.assertIn("备用接口失败", histories["018229"]["error"])

    def test_busy_batch_quote_api_retries_missing_codes_individually(self) -> None:
        codes = list(dict.fromkeys([
            *fetcher.TARGET_FUNDS,
            *fetcher.RECOMMENDATION_POOL,
            *fetcher.PORTFOLIO_HOLDINGS,
        ]))

        def fallback_quote(code: str) -> FundQuote:
            return FundQuote(
                code,
                f"测试基金{code}",
                1.2,
                iso(),
                source="eastmoney-js",
            )

        with tempfile.TemporaryDirectory() as directory, patch.object(
            fetcher, "FUND_CACHE_FILE", Path(directory) / "quotes.json"
        ), patch.object(
            fetcher,
            "_fetch_fund_quote_batch",
            side_effect=RuntimeError("Eastmoney fund quote: 网络繁忙，请稍后重试！"),
        ) as batch_fetch, patch.object(
            fetcher, "_fetch_fund_quote_js", side_effect=fallback_quote
        ) as fallback_fetch, patch.object(
            fetcher, "ifind_is_configured", return_value=False
        ), patch.object(fetcher, "_merge_estimate_quotes"), patch.object(
            fetcher.time, "sleep"
        ):
            fetcher.reset_source_health()
            result = fetcher.fetch_fund_quotes()

        expected_batch_calls = (len(codes) + 2) // 3 + 2
        self.assertEqual(batch_fetch.call_count, expected_batch_calls)
        self.assertEqual(fallback_fetch.call_count, len(codes))
        self.assertTrue(all(result[code].source == "eastmoney-js" for code in codes))
        self.assertTrue(
            fetcher.source_health_snapshot()["eastmoney-fund-quote"]["open"]
        )

    def test_batch_failure_does_not_skip_successful_single_quote_requests(self) -> None:
        codes = list(dict.fromkeys([
            *fetcher.TARGET_FUNDS,
            *fetcher.RECOMMENDATION_POOL,
            *fetcher.PORTFOLIO_HOLDINGS,
        ]))

        def batch_fails_single_succeeds(batch: list[str]) -> dict[str, FundQuote]:
            if len(batch) > 1:
                raise RuntimeError("Eastmoney fund quote: 网络繁忙，请稍后重试！")
            code = batch[0]
            return {code: FundQuote(code, code, 1.25, iso(), source="eastmoney")}

        with tempfile.TemporaryDirectory() as directory, patch.object(
            fetcher, "FUND_CACHE_FILE", Path(directory) / "quotes.json"
        ), patch.object(
            fetcher,
            "_fetch_fund_quote_batch",
            side_effect=batch_fails_single_succeeds,
        ) as quote_fetch, patch.object(
            fetcher, "ifind_is_configured", return_value=True
        ), patch.object(
            fetcher, "_fetch_ifind_fund_quotes"
        ) as ifind_fetch, patch.object(
            fetcher, "_fetch_fund_quote_js"
        ) as pingzhong_fetch, patch.object(fetcher, "_merge_estimate_quotes"):
            fetcher.reset_source_health()
            result = fetcher.fetch_fund_quotes()

        self.assertEqual(quote_fetch.call_count, 3 + len(codes))
        ifind_fetch.assert_not_called()
        pingzhong_fetch.assert_not_called()
        self.assertTrue(all(result[code].source == "eastmoney" for code in codes))
        self.assertFalse(
            fetcher.source_health_snapshot()["eastmoney-fund-quote"]["open"]
        )

    def test_ifind_is_used_before_pingzhongdata_for_missing_quotes(self) -> None:
        codes = list(dict.fromkeys([
            *fetcher.TARGET_FUNDS,
            *fetcher.RECOMMENDATION_POOL,
            *fetcher.PORTFOLIO_HOLDINGS,
        ]))

        def ifind_quotes(batch: list[str]) -> dict[str, FundQuote]:
            return {
                code: FundQuote(code, code, 1.3, iso(), source="ifind-fund-nav")
                for code in batch
            }

        with tempfile.TemporaryDirectory() as directory, patch.object(
            fetcher, "FUND_CACHE_FILE", Path(directory) / "quotes.json"
        ), patch.object(
            fetcher,
            "_fetch_fund_quote_batch",
            side_effect=RuntimeError("Eastmoney fund quote: 网络繁忙，请稍后重试！"),
        ), patch.object(
            fetcher, "ifind_is_configured", return_value=True
        ), patch.object(
            fetcher, "_fetch_ifind_fund_quotes", side_effect=ifind_quotes
        ) as ifind_fetch, patch.object(
            fetcher, "_fetch_fund_quote_js"
        ) as pingzhong_fetch, patch.object(fetcher, "_merge_estimate_quotes"):
            fetcher.reset_source_health()
            result = fetcher.fetch_fund_quotes()

        self.assertEqual(ifind_fetch.call_count, (len(codes) + 1) // 2)
        pingzhong_fetch.assert_not_called()
        self.assertTrue(all(result[code].source == "ifind-fund-nav" for code in codes))

    def test_ifind_fund_nav_retries_one_transient_failure(self) -> None:
        codes = list(dict.fromkeys([
            *fetcher.TARGET_FUNDS,
            *fetcher.RECOMMENDATION_POOL,
            *fetcher.PORTFOLIO_HOLDINGS,
        ]))
        calls = 0

        def transient_ifind(batch: list[str]) -> dict[str, FundQuote]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("iFinD 连接失败：timed out")
            return {
                code: FundQuote(code, code, 1.3, iso(), source="ifind-fund-nav")
                for code in batch
            }

        with tempfile.TemporaryDirectory() as directory, patch.object(
            fetcher, "FUND_CACHE_FILE", Path(directory) / "quotes.json"
        ), patch.object(
            fetcher,
            "_fetch_fund_quote_batch",
            side_effect=RuntimeError("Eastmoney fund quote: 网络繁忙，请稍后重试！"),
        ), patch.object(
            fetcher, "ifind_is_configured", return_value=True
        ), patch.object(
            fetcher, "_fetch_ifind_fund_quotes", side_effect=transient_ifind
        ), patch.object(
            fetcher, "_fetch_fund_quote_js"
        ) as pingzhong_fetch, patch.object(fetcher, "_merge_estimate_quotes"), patch.object(
            fetcher.time, "sleep"
        ):
            fetcher.reset_source_health()
            result = fetcher.fetch_fund_quotes()

        self.assertEqual(calls, (len(codes) + 1) // 2 + 1)
        pingzhong_fetch.assert_not_called()
        self.assertTrue(all(result[code].source == "ifind-fund-nav" for code in codes))

    def test_new_live_quote_is_not_overwritten_by_older_cache(self) -> None:
        quotes = {
            "018229": FundQuote(
                "018229", "测试基金", 1.25, iso(0), source="eastmoney"
            )
        }
        histories = {"018229": history("cache", 2)}

        resolve_fund_navs(quotes, histories)

        self.assertEqual(quotes["018229"].date, iso(0))
        self.assertEqual(quotes["018229"].nav, 1.25)
        self.assertEqual(quotes["018229"].source, "eastmoney")
        self.assertEqual(histories["018229"]["records"][-1]["date"], iso(0))

    def test_pingzhongdata_latest_nav_is_used_when_quote_api_falls_back(self) -> None:
        with patch.object(
            fetcher,
            "_fetch_fund_history_backup",
            return_value={
                "records": [{"date": iso(), "nav": 1.2345}],
                "source": "eastmoney-pingzhongdata",
            },
        ):
            result = fetcher._fetch_fund_quote_js("018229")

        self.assertEqual(result.nav, 1.2345)
        self.assertEqual(result.date, iso())
        self.assertEqual(result.source, "eastmoney-pingzhongdata")

    def test_tradingeconomics_macro_parser_uses_reported_cpi_rate(self) -> None:
        page = (
            '<script type="application/ld+json">'
            '{"@graph":[{"@type":"Dataset","description":"Inflation Rate in the United States decreased to 3.50 percent in June from 4.20 percent in May of 2026."}]}'
            '</script>'
        )
        with patch.object(fetcher, "_http_get", return_value=page):
            result = fetcher._fetch_tradingeconomics_macro_factor("US_CPI", "CPI")

        self.assertEqual(result["source"], "tradingeconomics")
        self.assertEqual(result["date"], "2026-06-30")
        self.assertAlmostEqual(result["value"], 3.5)
        self.assertAlmostEqual(result["change_12_pct"], 3.5)

    def test_fred_monthly_m2_builds_year_over_year_change(self) -> None:
        observations = [
            (f"2025-{month:02d}-01", 100.0 + month)
            for month in range(1, 13)
        ] + [("2026-01-01", 125.0)]

        result = fetcher._build_macro_factor(
            "US_M2", "M2", observations, "fred", meta=fetcher.FRED_MACRO_META["US_M2"]
        )

        self.assertAlmostEqual(result["change_12_pct"], 23.7624)
        self.assertEqual(result["semantic_status"], "COMPLETE")

    def test_annual_gdp_is_background_only(self) -> None:
        result = fetcher._build_macro_factor(
            "US_GDP",
            "GDP",
            [("2024-12-31", 2.8), ("2025-12-31", 2.1)],
            "bea",
            meta={"frequency": "annual", "value_type": "rate", "lookback": 1},
        )

        self.assertEqual(result["semantic_status"], "BACKGROUND_ONLY")

    def test_credit_stress_proxy_rises_when_hyg_lags_lqd(self) -> None:
        days = [f"2026-06-{day:02d}" for day in range(1, 31)] + [f"2026-07-{day:02d}" for day in range(1, 32)]
        hyg = [(day, 100.0 if index < 41 else 96.0) for index, day in enumerate(days)]
        lqd = [(day, 100.0) for day in days]
        with patch.object(fetcher, "_fetch_sina_us_stock_history", side_effect=[("HYG", hyg, "sina-stock"), ("LQD", lqd, "sina-stock")]):
            result = fetcher._fetch_sina_credit_stress_factor("US_CREDIT_STRESS_PROXY", "Credit")

        self.assertEqual(result["source"], "sina-credit-etf")
        self.assertEqual(result["key"], "US_CREDIT_STRESS_PROXY")
        self.assertTrue(result["proxy"])
        self.assertEqual(result["semantic_status"], "PROXY")
        self.assertGreater(result["value"], 4.0)

    def test_gdelt_historical_news_parser_preserves_source_and_publication_time(self) -> None:
        payload = {
            "articles": [{
                "title": "美联储会议释放降息信号",
                "snippet": "美国通胀回落，市场关注利率路径。",
                "seendate": "20250815T093000Z",
                "domain": "example.cn",
                "url": "https://example.cn/article/1",
            }]
        }
        with patch.object(fetcher, "_http_get", return_value=__import__("json").dumps(payload)):
            items = fetcher._fetch_gdelt_historical_news(
                "美联储", "2025-08-01", "2025-08-31"
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "GDELT · example.cn")
        self.assertEqual(items[0].published_at, "2025-08-15 09:30:00")
        self.assertIn("018229", items[0].affected_funds)

    def test_eastmoney_financials_parser_builds_fundamental_metrics(self) -> None:
        payload = {
            "result": {
                "data": [{
                    "REPORT_DATE": "2026-03-31 00:00:00",
                    "NOTICE_DATE": "2026-04-29 00:00:00",
                    "SECURITY_NAME_ABBR": "Test Co",
                    "TOTALOPERATEREVE": 120.0,
                    "TOTALOPERATEREVETZ": 20.0,
                    "PARENTNETPROFIT": 15.0,
                    "PARENTNETPROFITTZ": 50.0,
                    "OPERATE_PROFIT_PK": 24.0,
                    "FCFF_BACK": 20.0,
                    "ROEJQ": 25.0,
                }]
            }
        }
        with patch.object(fetcher, "_http_get", return_value=__import__("json").dumps(payload)):
            result = fetcher._fetch_eastmoney_company_fundamentals("688361")

        self.assertEqual(result["source"], "eastmoney-financials")
        self.assertAlmostEqual(result["metrics"]["revenue_yoy"], 0.20)
        self.assertAlmostEqual(result["metrics"]["net_income_yoy"], 0.50)
        self.assertAlmostEqual(result["metrics"]["free_cash_flow_margin"], 1 / 6)

    def test_naver_financials_parser_uses_latest_actual_annual_columns(self) -> None:
        def row(class_name: str, values: list[str]) -> str:
            return (
                f'<tr><th class="{class_name}">metric</th>'
                + "".join(f"<td>{value}</td>" for value in values)
                + "</tr>"
            )

        page = "".join(
            [
                "<table><thead><tr><th scope=\"col\">Label</th><th scope=\"col\">2024.12</th><th scope=\"col\">2025.12</th><th scope=\"col\">2026.12(E)</th></tr></thead><tbody>",
                row("th_cop_anal8", ["100", "120", "150"]),
                row("th_cop_anal9", ["20", "30", "40"]),
                row("th_cop_anal10", ["10", "18", "24"]),
                row("th_cop_anal13", ["5", "9", "12"]),
                "</tbody></table>",
            ]
        )
        with patch.object(fetcher, "_http_get", return_value=page):
            result = fetcher._fetch_naver_company_fundamentals("005930")

        self.assertEqual(result["source"], "naver-financials")
        self.assertEqual(result["fiscal_end"], "2025-12-31")
        self.assertAlmostEqual(result["metrics"]["revenue"], 120.0)
        self.assertAlmostEqual(result["metrics"]["revenue_yoy"], 0.20)
        self.assertAlmostEqual(result["metrics"]["net_income_yoy"], 0.80)

    def test_holdings_validation_rejects_missing_disclosure_date(self) -> None:
        with self.assertRaisesRegex(ValueError, "披露日期缺失"):
            fetcher._validate_holdings_record(
                {"disclosure_date": "--", "holdings": [{"name": "英伟达", "weight": 0.1}]},
                "018229",
            )

    def test_holdings_validation_requires_five_valid_positions(self) -> None:
        with self.assertRaisesRegex(ValueError, "不足5项"):
            fetcher._validate_holdings_record(
                {
                    "disclosure_date": iso(30),
                    "holdings": [{"name": "英伟达", "weight": 0.1}],
                },
                "018229",
            )

    def test_sina_style_holdings_rows_are_normalized(self) -> None:
        rows = [
            {"stock_code": "000001", "stock_name": "平安银行", "hold_ratio": "4.2%"},
            {"code": "000002", "name": "万科A", "weight": 0.031},
        ]

        normalized = fetcher._normalize_holdings_rows(rows)

        self.assertEqual(normalized[0]["name"], "平安银行")
        self.assertAlmostEqual(normalized[0]["weight"], 0.042)
        self.assertEqual(normalized[1]["code"], "000002")

    def test_index_failure_keeps_both_source_names(self) -> None:
        with patch.object(
            fetcher,
            "_fetch_eastmoney_index_snapshot",
            side_effect=RuntimeError("模拟东方财富失败"),
        ), patch.object(
            fetcher,
            "_fetch_tencent_index_snapshot",
            side_effect=RuntimeError("模拟腾讯失败"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "腾讯财经.*东方财富"
            ):
                fetcher._fetch_index_snapshot("IXIC", "纳斯达克综合指数")

    def test_index_uses_tencent_without_calling_yahoo(self) -> None:
        fallback_snapshot = IndexSnapshot(
            "IXIC",
            "纳斯达克综合指数",
            100.0,
            0.2,
            1.0,
            2.0,
            "tencent-index",
            iso(),
        )
        with patch.object(
            fetcher,
            "_fetch_eastmoney_index_snapshot",
            side_effect=RuntimeError("模拟东方财富失败"),
        ), patch.object(
            fetcher,
            "_fetch_tencent_index_snapshot",
            return_value=fallback_snapshot,
        ) as tencent_fetch:
            result = fetcher._fetch_index_snapshot("IXIC", "纳斯达克综合指数")

        self.assertEqual(result.source, "tencent-index")
        tencent_fetch.assert_called_once_with("IXIC", "纳斯达克综合指数")


class TradingCalendarTests(unittest.TestCase):
    def test_us_exchange_holidays_include_good_friday_and_juneteenth(self) -> None:
        self.assertFalse(_is_trading_day(date(2026, 4, 3), "us"))
        self.assertFalse(_is_trading_day(date(2026, 6, 19), "us"))

    def test_china_calendar_skips_spring_festival_and_weekend_makeup_days(self) -> None:
        self.assertFalse(_is_trading_day(date(2026, 2, 17), "cn"))
        self.assertFalse(_is_trading_day(date(2026, 2, 14), "cn"))
        self.assertEqual(
            _trading_day_age(date(2026, 2, 13), date(2026, 2, 24), "cn"),
            1,
        )

    def test_hong_kong_calendar_skips_easter_closure(self) -> None:
        self.assertFalse(_is_trading_day(date(2026, 4, 3), "hk"))


class DataQualityGateTests(unittest.TestCase):
    def test_macro_quality_separates_economic_coverage_from_market_data(self) -> None:
        snapshot = market()
        snapshot["macro_factors"] = [
            item
            for item in snapshot["macro_factors"]
            if item["key"] not in {"US_GDP", "US_CPI", "US_UNEMPLOYMENT"}
        ]
        report = build_data_quality_report(
            ["018229"], [],
            {"018229": FundQuote("018229", "测试基金", 1.2, iso(), source="eastmoney")},
            {"018229": history()}, {"018229": holding()}, snapshot, [], today=TODAY
        )

        macro_record = next(item for item in report["records"] if item["key"] == "macro_economy")
        self.assertEqual(macro_record["status"], "MISSING")
        self.assertEqual(report["macro"]["economic_coverage"], 0.5)
        self.assertEqual(report["funds"]["018229"]["gate"], BLOCKED)

    def test_macro_quality_accepts_low_frequency_live_data(self) -> None:
        records = [
            {"key": "US_GDP", "value": 2.0, "date": iso(), "source": "fred", "frequency": "quarterly"},
            {"key": "US_CPI", "value": 300.0, "date": iso(), "source": "fred", "frequency": "monthly"},
            {"key": "US_UNEMPLOYMENT", "value": 4.0, "date": iso(), "source": "fred", "frequency": "monthly"},
            {"key": "US_M2", "value": 20000.0, "change_12_pct": 3.0, "date": iso(), "source": "fred", "frequency": "monthly"},
            {"key": "US_FED_FUNDS", "value": 4.0, "date": iso(), "source": "fred", "frequency": "monthly"},
            {"key": "US_HY_SPREAD", "value": 3.0, "date": iso(), "source": "fred", "frequency": "daily"},
        ] + [
            {"key": key, "value": 18.0, "date": iso(), "source": "fred", "frequency": "daily"}
            for key in ("VIX", "US2Y", "US10Y", "US10Y_REAL", "DXY", "USDCNY")
        ]
        summary, detail = _macro_quality_summary(records, "2026-07-29 10:00:00", TODAY)

        self.assertEqual(summary.status, LIVE)
        self.assertEqual(detail["economic_coverage"], 1.0)

    def test_m2_without_year_over_year_change_is_incomplete(self) -> None:
        records = [
            {"key": "US_GDP", "value": 2.0, "date": iso(), "source": "fred", "frequency": "quarterly"},
            {"key": "US_CPI", "value": 300.0, "change_12_pct": 2.5, "date": iso(), "source": "fred", "frequency": "monthly"},
            {"key": "US_UNEMPLOYMENT", "value": 4.0, "date": iso(), "source": "fred", "frequency": "monthly"},
            {"key": "US_M2", "value": 20000.0, "date": iso(), "source": "tradingeconomics", "frequency": "monthly"},
            {"key": "US_FED_FUNDS", "value": 4.0, "date": iso(), "source": "fred", "frequency": "monthly"},
            {"key": "US_HY_SPREAD", "value": 3.0, "date": iso(), "source": "fred", "frequency": "daily"},
        ] + [
            {"key": key, "value": 18.0, "date": iso(), "source": "fred", "frequency": "daily"}
            for key in ("VIX", "US2Y", "US10Y", "US10Y_REAL", "DXY", "USDCNY")
        ]
        summary, detail = _macro_quality_summary(records, "2026-07-29 10:00:00", TODAY)

        m2 = next(item for item in detail["records"] if item["key"] == "macro:US_M2")
        self.assertEqual(m2["status"], "MISSING")
        self.assertEqual(summary.status, "DELAYED")
        self.assertTrue(detail["critical_missing"])

    def test_credit_proxy_is_delayed_and_never_full_macro_live(self) -> None:
        records = [
            {"key": "US_GDP", "value": 2.0, "date": iso(), "source": "fred", "frequency": "quarterly"},
            {"key": "US_CPI", "value": 300.0, "change_12_pct": 2.5, "date": iso(), "source": "fred", "frequency": "monthly"},
            {"key": "US_UNEMPLOYMENT", "value": 4.0, "date": iso(), "source": "fred", "frequency": "monthly"},
            {"key": "US_M2", "value": 20000.0, "change_12_pct": 3.0, "date": iso(), "source": "fred", "frequency": "monthly"},
            {"key": "US_FED_FUNDS", "value": 4.0, "date": iso(), "source": "fred", "frequency": "monthly"},
            {"key": "US_CREDIT_STRESS_PROXY", "value": 0.5, "proxy": True, "semantic_status": "PROXY", "date": iso(), "source": "sina-credit-etf", "frequency": "daily"},
        ] + [
            {"key": key, "value": 18.0, "date": iso(), "source": "fred", "frequency": "daily"}
            for key in ("VIX", "US2Y", "US10Y", "US10Y_REAL", "DXY", "USDCNY")
        ]
        summary, detail = _macro_quality_summary(records, "2026-07-29 10:00:00", TODAY)

        self.assertEqual(summary.status, "DELAYED")
        self.assertTrue(detail["proxy_in_use"])
        self.assertEqual(detail["credit_key"], "US_CREDIT_STRESS_PROXY")
        self.assertLess(detail["field_completeness"], 1.0)

    def test_confidence_caps_incomplete_macro_and_fundamentals(self) -> None:
        components = {"all": (LIVE, 90)}
        macro = {
            "field_completeness": 0.8,
            "source_integrity": 1.0,
            "critical_missing": True,
        }
        score, detail = _confidence_score(components, 10, macro_detail=macro)
        self.assertLessEqual(score, 79.9)
        self.assertTrue(detail["cap_reasons"])

        holding_data = {
            "holdings": [
                {"code": "AAA", "name": "企业甲", "weight": 0.60},
                {"code": "BBB", "name": "企业乙", "weight": 0.40},
            ]
        }
        stock_data = {
            "AAA": {
                "fundamentals": {
                    "source": "sec-companyfacts",
                    "filed_at": "2026-04-01",
                    "fiscal_end": "2025-12-31",
                    "metrics": {
                        "revenue_yoy": 0.2,
                        "net_income_yoy": 0.2,
                        "operating_margin": 0.2,
                    },
                }
            }
        }
        fundamental = _fundamental_quality_record(
            "018229", holding_data, stock_data, "2026-07-29 10:00:00", TODAY
        )
        score, detail = _confidence_score(
            components,
            10,
            macro_detail={"field_completeness": 1.0, "source_integrity": 1.0},
            fundamental_detail=fundamental.details,
        )
        self.assertEqual(fundamental.status, "DELAYED")
        self.assertAlmostEqual(fundamental.details["fundamental_coverage"], 0.6)
        self.assertLessEqual(score, 84.9)
        self.assertEqual(detail["score_cap"], 84.9)

    def test_high_holding_concentration_switches_buy_range_to_observation(self) -> None:
        code = "018229"
        quotes = {code: FundQuote(code, "测试基金", 1.2, iso(), source="eastmoney")}
        histories = {code: history()}
        concentrated = {
            code: {
                "source": "eastmoney-holdings",
                "disclosure_date": iso(20),
                "holdings": [
                    {"code": "NVDA", "name": "英伟达", "weight": 0.72}
                ],
            }
        }

        decisions = analyze_target_funds(
            quotes,
            histories,
            market(),
            {"funds": {code: {"gate": "READY"}}},
            concentrated,
        )
        decision = next(item for item in decisions if item.code == code)

        self.assertEqual(decision.status, "仅观察")
        self.assertEqual(decision.buy_levels, [])
        self.assertIn("10%-25%分位", decision.rule_basis)
        self.assertIn("不是企业基本面估值", decision.valuation_hint)

    def test_old_holding_disclosure_switches_buy_range_to_observation(self) -> None:
        code = "018229"
        quotes = {code: FundQuote(code, "测试基金", 1.2, iso(), source="eastmoney")}
        histories = {code: history()}
        old_holding = {
            code: {
                "source": "eastmoney-holdings",
                "disclosure_date": iso(130),
                "holdings": [
                    {"code": "NVDA", "name": "英伟达", "weight": 0.10},
                    {"code": "MSFT", "name": "微软", "weight": 0.10},
                    {"code": "GOOG", "name": "谷歌", "weight": 0.10},
                ],
            }
        }

        decisions = analyze_target_funds(
            quotes,
            histories,
            market(),
            {"funds": {code: {"gate": "READY"}}},
            old_holding,
        )
        decision = next(item for item in decisions if item.code == code)

        self.assertEqual(decision.status, "仅观察")
        self.assertIn("披露已过去 130 天", decision.trigger)

    def test_missing_secondary_benchmark_limits_otherwise_fresh_fund(self) -> None:
        snapshot = market()
        snapshot["indexes"] = [
            item for item in snapshot["indexes"] if item["symbol"] != "DJI"
        ]

        report = build_data_quality_report(
            ["018229"],
            [],
            {"018229": FundQuote("018229", "测试基金", 1.2, iso(), source="eastmoney")},
            {"018229": history()},
            {"018229": holding()},
            snapshot,
            [],
            today=TODAY,
        )

        self.assertEqual(report["funds"]["018229"]["gate"], BLOCKED)
        self.assertTrue(
            any("辅助基准指数缺失" in reason for reason in report["funds"]["018229"]["reasons"])
        )

    def test_weighted_benchmarks_drive_index_rule(self) -> None:
        trigger, risk = _index_rule(
            "组合基准",
            [
                {"ma20_gap_pct": 4.0, "ma60_gap_pct": 2.0, "weight": 0.75},
                {"ma20_gap_pct": 8.0, "ma60_gap_pct": 2.0, "weight": 0.25},
            ],
        )

        self.assertIn("5.0%", trigger)
        self.assertIn("趋势未处极端区", trigger)
        self.assertNotIn("数据不足", risk)

    def test_diverging_benchmarks_disable_chasing(self) -> None:
        trigger, risk = _index_rule(
            "组合基准",
            [
                {"ma20_gap_pct": 1.0, "ma60_gap_pct": 3.5, "weight": 0.55},
                {"ma20_gap_pct": -1.0, "ma60_gap_pct": -3.5, "weight": 0.45},
            ],
        )

        self.assertIn("明显分歧", trigger)
        self.assertEqual(risk, "多基准方向不一致时不追涨")

    def test_cached_nav_blocks_analysis_and_is_display_only(self) -> None:
        report = quality_for(
            quote_source="cache",
            nav_days_ago=2,
            history_source="cache",
            history_days_ago=2,
        )

        fund = report["funds"]["018229"]
        self.assertEqual(fund["nav"]["status"], CACHED)
        self.assertEqual(fund["gate"], LIMITED)
        self.assertEqual(fund["analysis_mode"], "CONSERVATIVE_FALLBACK")
        self.assertTrue(fund["analysis_ready"])

        code = "018229"
        decisions = analyze_target_funds(
            {code: FundQuote(code, "测试基金", 1.2, iso(2), source="cache")},
            {code: history("cache", 2)},
            market(),
            report,
            {code: holding()},
            live_news(),
            {},
        )
        decision = next(item for item in decisions if item.code == code)
        self.assertNotEqual(decision.target_range, "--")
        self.assertEqual(decision.analysis_mode, "CONSERVATIVE_FALLBACK")
        self.assertIsNotNone(decision.factor_model)

    def test_stale_nav_triggers_blocked_gate(self) -> None:
        report = quality_for(
            quote_source="cache",
            nav_days_ago=18,
            history_source="cache",
            history_days_ago=18,
        )

        fund = report["funds"]["018229"]
        self.assertEqual(fund["nav"]["status"], STALE)
        self.assertEqual(fund["gate"], BLOCKED)

    def test_stale_index_blocks_fund_even_with_fresh_nav(self) -> None:
        report = quality_for(index_days_ago=9)

        fund = report["funds"]["018229"]
        self.assertEqual(fund["gate"], BLOCKED)
        self.assertTrue(any("有效期" in reason for reason in fund["reasons"]))

    def test_trading_day_age_ignores_weekend_for_global_market(self) -> None:
        self.assertEqual(
            _trading_day_age(date(2026, 7, 31), date(2026, 8, 3), "global"),
            1,
        )

    def test_qdii_delay_generates_conservative_buy_range(self) -> None:
        code = "018229"
        decisions = analyze_target_funds(
            {code: FundQuote(code, "测试基金", 1.2, iso(), source="eastmoney")},
            {code: history()},
            market(),
            {
                "funds": {
                    code: {
                        "gate": LIMITED,
                        "gate_label": "仅供观察",
                        "analysis_mode": "CONSERVATIVE_DELAYED",
                    }
                }
            },
            {code: holding()},
        )
        decision = next(item for item in decisions if item.code == code)

        self.assertNotEqual(decision.target_range, "--")
        self.assertEqual(decision.analysis_mode, "CONSERVATIVE_DELAYED")
        self.assertIn("保守", decision.status)
        self.assertTrue(all("保守" in level for level in decision.buy_levels))

    def test_normal_qdii_delay_is_the_only_limited_analysis_mode(self) -> None:
        report = quality_for(nav_days_ago=6, history_days_ago=6)

        fund = report["funds"]["018229"]
        self.assertEqual(fund["gate"], LIMITED)
        self.assertEqual(fund["analysis_mode"], "CONSERVATIVE_DELAYED")
        self.assertTrue(fund["analysis_ready"])

    def test_missing_macro_data_does_not_generate_market_phase(self) -> None:
        snapshot = market()
        snapshot["macro_factors"] = snapshot["macro_factors"][:2]

        analysis = analyze_market(snapshot)

        self.assertEqual(analysis["phase"], "保守数据回退观察")
        self.assertIn("回退数据", analysis["summary"])

    def test_index_fetch_error_is_visible_in_quality_reason(self) -> None:
        report = quality_for()
        report = build_data_quality_report(
            ["018229"],
            [],
            {"018229": FundQuote("018229", "测试基金", 1.2, iso(), source="eastmoney")},
            {"018229": history()},
            {"018229": holding()},
            {
                **market(),
                "fetch_errors": {"IXIC": "东方财富：模拟失败；Yahoo Finance：模拟失败"},
            },
            [],
            today=TODAY,
        )

        index_record = next(item for item in report["records"] if item["key"] == "indexes")
        self.assertIn("模拟失败", index_record["message"])

    def test_quote_fetch_error_is_visible_in_quality_reason(self) -> None:
        code = "018229"
        quote = FundQuote(code, "测试基金", 1.2, iso(), source="cache", error="批量接口失败")
        report = build_data_quality_report(
            [code], [], {code: quote}, {code: history("cache")}, {code: holding()}, market(), [], today=TODAY
        )

        self.assertIn("批量接口失败", report["funds"][code]["nav"]["message"])

    def test_stale_index_removes_dynamic_buy_prices(self) -> None:
        code = "018229"
        quotes = {
            code: FundQuote(code, "测试基金", 1.2, iso(), source="eastmoney")
        }
        histories = {code: history()}
        snapshot = market(index_days_ago=9)
        report = build_data_quality_report(
            [],
            [code],
            quotes,
            histories,
            {code: holding()},
            snapshot,
            [],
            today=TODAY,
        )

        decisions = analyze_target_funds(quotes, histories, snapshot, report)
        decision = next(item for item in decisions if item.code == code)
        self.assertEqual(decision.target_range, "--")
        self.assertEqual(decision.buy_levels, [])
        self.assertIn("分析未执行，不生成买入节点", decision.trigger)

    def test_missing_holdings_limit_otherwise_fresh_fund(self) -> None:
        code = "018229"
        report = build_data_quality_report(
            [code],
            [],
            {code: FundQuote(code, "测试基金", 1.2, iso(), source="eastmoney")},
            {code: history()},
            {},
            market(),
            [],
            today=TODAY,
        )

        self.assertEqual(report["funds"][code]["gate"], BLOCKED)
        self.assertTrue(any("持仓披露缺失" in reason for reason in report["funds"][code]["reasons"]))

    def test_missing_recommendation_qualification_limits_candidate(self) -> None:
        code = "486002"
        report = build_data_quality_report(
            [],
            [code],
            {code: FundQuote(code, "推荐基金", 1.1, iso(), source="eastmoney")},
            {code: history()},
            {code: holding()},
            market(),
            [],
            today=TODAY,
        )

        self.assertEqual(report["funds"][code]["gate"], LIMITED)
        self.assertTrue(
            any(
                "推荐资格缺失" in reason
                for reason in report["funds"][code]["reasons"]
            )
        )

    def test_static_recommendation_note_does_not_pollute_target_fund(self) -> None:
        report = quality_for()

        reasons = report["funds"]["018229"]["reasons"]
        self.assertFalse(any("尚未联网核验" in reason for reason in reasons))

    def test_live_recommendation_qualification_releases_data_gate(self) -> None:
        code = "486002"
        qualifications = {
            code: {
                "source": "eastmoney-qualification",
                "as_of": iso(),
                "eligible": False,
                "reasons": ["当前单日申购限额100元，不高于1000元"],
            }
        }
        report = build_data_quality_report(
            [],
            [code],
            {code: FundQuote(code, "推荐基金", 1.1, iso(), source="eastmoney")},
            {code: history()},
            {code: holding()},
            market(),
            live_news(),
            qualifications,
            today=TODAY,
        )

        record = next(
            item
            for item in report["records"]
            if item["key"] == "recommendation_qualification"
        )
        self.assertEqual(record["status"], LIVE)
        self.assertEqual(report["funds"][code]["gate"], "READY")

    def test_recommendation_analyzer_uses_live_qualification(self) -> None:
        code = "017653"
        qualifications = {
            code: {
                "source": "eastmoney-qualification",
                "eligible": True,
                "name": "联网核验基金",
                "one_day_limit": 5000,
                "manager_type": "主动管理",
                "investment_scope": "全球",
                "rank_desc": "近36个月月均同类百分位 42.00%",
            }
        }
        recommendations = analyze_recommendations(
            {code: FundQuote(code, "推荐基金", 1.1, iso(), source="eastmoney")},
            qualifications,
        )

        self.assertEqual(len(recommendations), 1)
        self.assertEqual(recommendations[0].one_day_limit, 5000)
        self.assertIn("42.00%", recommendations[0].rank_desc)

    def test_recommendation_not_blocked_when_qualification_missing(self) -> None:
        code = "017653"
        pool = {code: {"name": "候选基金", "theme": "全球股票", "style": "主动管理", "reason": "测试"}}
        with patch.object(analyzer, "RECOMMENDATION_POOL", pool):
            recommendations = analyze_recommendations(
                {code: FundQuote(code, "候选基金", 1.1, iso(), source="eastmoney")},
                {},
            )

        self.assertEqual(len(recommendations), 1)
        self.assertEqual(recommendations[0].code, code)
    def test_new_target_is_not_duplicated_in_recommendation_pool(self) -> None:
        code = "161815"
        recommendations = analyze_recommendations(
            {code: FundQuote(code, "用户指定基金", 1.1, iso(), source="fallback")},
            {},
        )

        self.assertEqual(recommendations, [])

    def test_recommendation_analyzer_does_not_cap_eligible_count(self) -> None:
        pool = {
            f"00000{index}": {
                "name": f"候选基金{index}",
                "theme": "全球股票",
                "style": "主动管理",
                "reason": "测试",
            }
            for index in range(1, 5)
        }
        qualifications = {
            code: {
                "source": "multi-source-qualification",
                "eligible": True,
                "name": meta["name"],
                "one_day_limit": 5000,
                "manager_type": "主动管理",
                "investment_scope": "全球",
                "rank_desc": "近36个月月均同类百分位 40.00%",
            }
            for code, meta in pool.items()
        }

        quotes = {
            code: FundQuote(code, meta["name"], 1.1, iso(), source="eastmoney")
            for code, meta in pool.items()
        }
        with patch.object(analyzer, "RECOMMENDATION_POOL", pool):
            recommendations = analyze_recommendations(quotes, qualifications)

        self.assertEqual(len(recommendations), 4)

    def test_peer_rank_parser_keeps_last_month_observation(self) -> None:
        script = (
            'var Data_rateInSimilarType = ['
            '{"x":1767225600000,"y":30,"sc":100},'
            '{"x":1767312000000,"y":30,"sc":100},'
            '{"x":1769904000000,"y":10,"sc":100}'
            '];/* next */'
        )

        rows = fetcher._parse_peer_rank_months(script)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["rank"], 30)
        self.assertEqual(rows[1]["percentile"], 10)

    def test_recommendation_qualification_returns_complete_result(self) -> None:
        fee_page = (
            '<td class="th">申购状态</td><td>开放申购</td>'
            '<td class="th">日累计申购限额</td><td>500.00元</td>'
        )
        rank_rows = []
        for month in range(36):
            year = 2023 + month // 12
            month_number = month % 12 + 1
            row_date = date(year, month_number, 15)
            timestamp = int(
                __import__("datetime").datetime(
                    row_date.year, row_date.month, row_date.day
                ).timestamp()
                * 1000
            )
            rank_rows.append(
                f'{{"x":{timestamp},"y":40,"sc":100}}'
            )
        script = (
            "var Data_rateInSimilarType = ["
            + ",".join(rank_rows)
            + '];/* next */'
        )
        responses = [
            fee_page,
            script,
            __import__("json").dumps(
                {
                    "result": {
                        "status": {"code": 0},
                        "data": {
                            "NewType2Name": "股票型",
                            "NewType3Name": "普通股票型",
                            "bjjz": "MSCI全球指数收益率",
                            "tzfw": "投资全球证券市场",
                        },
                    }
                },
                ensure_ascii=False,
            ),
        ]
        with patch.object(fetcher, "_http_get", side_effect=responses):
            result = fetcher._fetch_recommendation_qualification(
                "486002",
                {"name": "测试基金", "is_global_or_us_focus": True, "investment_scope": "全球"},
            )

        self.assertIsInstance(result, dict)
        self.assertEqual(result["source"], "multi-source-qualification")
        self.assertEqual(result["one_day_limit"], 500)
        self.assertFalse(any("限额" in reason for reason in result["reasons"]))
        self.assertEqual(result["manager_type"], "主动管理")

    def test_global_focus_requires_majority_benchmark_weight(self) -> None:
        self.assertFalse(
            fetcher._is_global_or_us_focus(
                "全球消费基金",
                "40%×香港消费+40%×内地消费+15%×全球消费+5%×存款",
                "投资全球市场",
            )
        )
        self.assertTrue(
            fetcher._is_global_or_us_focus(
                "蓝筹精选基金",
                "75%×MSCI全球指数+15%×中国指数+10%×存款",
                "投资全球市场",
            )
        )
        self.assertFalse(
            fetcher._is_global_or_us_focus(
                "蓝筹精选基金",
                "90%×MSCI中国指数+10%×存款",
                "主要投资中国股票",
            )
        )

    def test_confidence_scores_are_always_bounded(self) -> None:
        reports = [
            quality_for(),
            quality_for("cache", 2, "cache", 2),
            quality_for("cache", 30, "cache", 30, 30),
        ]

        for report in reports:
            self.assertGreaterEqual(report["overall_score"], 0)
            self.assertLessEqual(report["overall_score"], 100)
            for fund in report["funds"].values():
                self.assertGreaterEqual(fund["confidence"], 0)
                self.assertLessEqual(fund["confidence"], 100)


if __name__ == "__main__":
    unittest.main()
