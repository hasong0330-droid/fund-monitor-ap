from datetime import date
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ifind_client import (  # noqa: E402
    IFindClient,
    IFindError,
    parse_global_fundamentals,
    parse_fund_nav_quotes,
    parse_macro_observations,
    parse_news_items,
)


class IFindParsingTests(unittest.TestCase):
    def test_gdp_and_oas_queries_use_required_semantics(self) -> None:
        client = IFindClient("test-token")
        captured = []

        def fake_call(service: str, tool_name: str, arguments: dict) -> dict:
            captured.append((service, tool_name, arguments["query"]))
            return {
                "datas": [{
                    "data": {
                        "columns": ["日期", "值"],
                        "data": [["2025-01-01", 1.0], ["2025-01-02", 1.1]],
                    }
                }]
            }

        client.call = fake_call
        client.macro_observations("US_GDP")
        client.macro_observations("US_HY_SPREAD")

        self.assertEqual(
            captured[0][2],
            "美国实际GDP季度环比折年率，2018Q1至最新，不含预测值",
        )
        self.assertEqual(
            captured[1][2],
            "美国高收益债期权调整利差OAS日度值，2025-01-01至最新，不含预测值",
        )
    def test_fund_nav_parser_joins_code_date_and_published_unit_nav(self) -> None:
        markdown = "\n".join(
            [
                "|证券代码|证券简称|净值日期|",
                "|---|---|---|",
                "|002891.OF|华夏移动互联混合(QDII)|20260811|",
                "|378546.OF|摩根全球天然资源混合(QDII)A|20260811|",
                "",
                "|证券代码|证券简称|单位净值（单位：元）|累计单位净值（单位：元）|",
                "|---|---|---|---|",
                "|002891.OF|华夏移动互联混合(QDII)|2.612|2.612|",
                "|378546.OF|摩根全球天然资源混合(QDII)A|1.5447|1.5447|",
            ]
        )
        result = parse_fund_nav_quotes(
            {"answer": markdown}, ["002891", "378546"]
        )
        self.assertEqual(result["002891"]["date"], "2026-08-11")
        self.assertEqual(result["002891"]["nav"], 2.612)
        self.assertEqual(result["378546"]["nav"], 1.5447)

    def test_fund_nav_parser_rejects_estimate_fields(self) -> None:
        with self.assertRaises(IFindError):
            parse_fund_nav_quotes(
                {
                    "answer": "|证券代码|净值日期|IOPV净值估值|\n"
                    "|---|---|---|\n|002891.OF|20260811|2.7|"
                },
                ["002891"],
            )

    def test_m2_requires_and_preserves_thirteen_published_months(self) -> None:
        rows = [[f"2025-{month:02d}-28", 20_000 + month] for month in range(1, 13)]
        rows.append(["2026-01-31", 20_013])
        observations = parse_macro_observations(
            {"datas": [{"data": {"columns": ["日期", "美国:M2:当月值"], "data": rows}}]},
            "US_M2",
        )
        self.assertEqual(len(observations), 13)
        self.assertEqual(observations[-1], ("2026-01-31", 20013.0))

    def test_forecast_macro_header_is_rejected(self) -> None:
        with self.assertRaises(IFindError):
            parse_macro_observations(
                {
                    "datas": [
                        {
                            "data": {
                                "columns": ["日期", "美国:实际GDP(含预测):当季值"],
                                "data": [["2026-03-31", 1.0], ["2026-06-30", 1.1]],
                            }
                        }
                    ]
                },
                "US_GDP",
            )

    def test_financials_require_report_period_and_keep_only_return_metrics(self) -> None:
        report_date = min(date.today().replace(month=1, day=31).isoformat(), "2026-01-31")
        markdown = "\n".join(
            [
                "|证券代码|报告截止日期|",
                "|---|---|",
                f"|NVDA.O|{report_date}|",
                "",
                "|证券代码|营业收入(同比增长率)（单位：%）|净利润(同比增长率)（单位：%）|净资产收益率ROE|",
                "|---|---|---|---|",
                "|NVDA.O|20|30|25|",
            ]
        )
        result = parse_global_fundamentals({"answer": markdown}, "NVDA")
        self.assertEqual(result["fiscal_end"], report_date)
        self.assertEqual(result["source"], "ifind-financials")
        self.assertEqual(result["metrics"]["revenue_yoy"], 0.2)
        self.assertEqual(result["metrics"]["net_income_yoy"], 0.3)
        self.assertEqual(result["metrics"]["roe"], 0.25)
        self.assertIsNone(result["metrics"]["revenue"])

    def test_financials_accept_exact_metric_date_when_report_field_is_blank(self) -> None:
        markdown = "\n".join(
            [
                "|证券代码|报告截止日期|最近定期报告披露日期|",
                "|---|---|---|",
                "|TTE.N||20260724|",
                "",
                "|证券代码|日期|营业收入(同比增长率)|净利润(同比增长率)|净资产收益率ROE|",
                "|---|---|---|---|---|",
                "|TTE.N|20260630|15.1|71.0|9.2|",
            ]
        )

        result = parse_global_fundamentals({"answer": markdown}, "TTE")

        self.assertEqual(result["fiscal_end"], "2026-06-30")
        self.assertAlmostEqual(result["metrics"]["revenue_yoy"], 0.151)

    def test_news_requires_auditable_title_date_and_content(self) -> None:
        result = parse_news_items(
            {
                "data": '[{"资讯标题":"美债收益率上行","资讯内容":"科技股估值承压",'
                '"日期":"2026-08-05","URL":"https://example.test/news"}]'
            }
        )
        self.assertEqual(result[0]["published_at"], "2026-08-05")
        self.assertEqual(result[0]["title"], "美债收益率上行")


if __name__ == "__main__":
    unittest.main()
