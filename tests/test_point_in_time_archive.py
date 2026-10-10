import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fetcher import NewsItem
import point_in_time_archive as archive


def _fundamentals() -> dict:
    return {
        "ticker": "TEST",
        "company": "Test issuer",
        "fiscal_end": "2023-12-31",
        "filed_at": "2024-02-01",
        "metrics": {
            "revenue_yoy": 0.15,
            "net_income_yoy": 0.20,
            "net_margin": 0.18,
            "operating_margin": 0.22,
            "free_cash_flow_margin": 0.12,
            "roe": 0.20,
        },
        "source": "sec-companyfacts",
    }


class PointInTimeArchiveTests(unittest.TestCase):
    def _paths(self, directory: str):
        base = Path(directory)
        return (
            base / "archive.json",
            base / "audit.jsonl",
            base / "panel.json",
        )

    def test_duplicate_events_are_audited_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_file, audit_file, panel_file = self._paths(directory)
            news = [
                NewsItem(
                    title="测试新闻", source="东方财富", summary="测试摘要",
                    affected_funds="018229", direction="中性观察", published_at="2024-02-02 08:30:00",
                )
            ]
            with patch.object(archive, "POINT_IN_TIME_ARCHIVE_FILE", archive_file), patch.object(
                archive, "POINT_IN_TIME_AUDIT_FILE", audit_file
            ), patch.object(archive, "POINT_IN_TIME_PANEL_FILE", panel_file):
                first = archive.archive_observations({"stocks": {"TEST": {"fundamentals": _fundamentals()}}}, news)
                second = archive.archive_observations({"stocks": {"TEST": {"fundamentals": _fundamentals()}}}, news)

            self.assertEqual(first["new_events"], 2)
            self.assertEqual(second["new_events"], 0)
            audit_rows = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(audit_rows), 2)
            self.assertEqual(len({item["event_id"] for item in audit_rows}), 2)
            news_record = next(item["record"] for item in audit_rows if item["record"]["source"] == "eastmoney-news")
            self.assertEqual(news_record["published_at"], "2024-02-02 08:30:00")

    def test_panel_never_uses_filing_before_its_available_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_file, audit_file, panel_file = self._paths(directory)
            with patch.object(archive, "POINT_IN_TIME_ARCHIVE_FILE", archive_file), patch.object(
                archive, "POINT_IN_TIME_AUDIT_FILE", audit_file
            ), patch.object(archive, "POINT_IN_TIME_PANEL_FILE", panel_file):
                archive.archive_observations({"stocks": {"TEST": {"fundamentals": _fundamentals()}}}, [])
                panel = archive.build_backtest_panel({
                    "018229": [{
                        "effective_date": "2024-01-15",
                        "holdings": [{"code": "TEST", "weight": 0.20}],
                    }]
                })

            snapshots = panel["fundamentals"]["018229"]
            self.assertEqual([item["available_date"] for item in snapshots], ["2024-02-01"])
            self.assertGreaterEqual(snapshots[0]["coverage"], 0.60)

    def test_panel_uses_next_day_for_dated_news(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_file, audit_file, panel_file = self._paths(directory)
            news = [
                NewsItem(
                    title="测试新闻", source="东方财富", summary="测试摘要",
                    affected_funds="018229", direction="中性观察",
                    published_at="2024-02-02 19:30:00",
                )
            ]
            with patch.object(archive, "POINT_IN_TIME_ARCHIVE_FILE", archive_file), patch.object(
                archive, "POINT_IN_TIME_AUDIT_FILE", audit_file
            ), patch.object(archive, "POINT_IN_TIME_PANEL_FILE", panel_file):
                archive.archive_observations({}, news)
                panel = archive.build_backtest_panel({"018229": []})

            self.assertEqual(panel["news"]["018229"][0]["published_at"], "2024-02-02 19:30:00")
            self.assertEqual(panel["news"]["018229"][0]["available_date"], "2024-02-03")

    def test_topic_mapping_expands_news_to_research_funds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_file, audit_file, panel_file = self._paths(directory)
            news = [
                NewsItem(
                    title="美联储测试新闻", source="东方财富", summary="测试摘要",
                    affected_funds="018229", direction="中性观察", tag="美元利率",
                    published_at="2024-02-02 08:30:00",
                )
            ]
            with patch.object(archive, "POINT_IN_TIME_ARCHIVE_FILE", archive_file), patch.object(
                archive, "POINT_IN_TIME_AUDIT_FILE", audit_file
            ), patch.object(archive, "POINT_IN_TIME_PANEL_FILE", panel_file):
                archive.archive_observations({}, news)
                panel = archive.build_backtest_panel({"000041": []})

            self.assertIn("000041", panel["news"])
            self.assertEqual(panel["news"]["000041"][0]["available_date"], "2024-02-03")

    def test_fund_level_coverage_gate_blocks_incomplete_research_universe(self) -> None:
        coverage = {
            "018229": {"snapshots_at_or_above_60pct": 4},
            "005698": {"snapshots_at_or_above_60pct": 5},
            "017437": {"snapshots_at_or_above_60pct": 3},
            "000041": {"snapshots_at_or_above_60pct": 2},
        }

        status = archive._fundamental_coverage_acceptance(coverage)

        self.assertFalse(status["fundamental_coverage_ready"])
        self.assertEqual(status["funds_with_60pct_fundamental_coverage"], 4)
        self.assertTrue(status["coverage_blocking_reasons"])

    def test_public_news_backfill_records_actual_coverage_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_file, audit_file, panel_file = self._paths(directory)
            news = [
                NewsItem(
                    title="测试新闻", source="东方财富", summary="测试摘要",
                    affected_funds="018229", direction="中性观察",
                    published_at="2024-02-02 08:30:00",
                )
            ]
            with patch.object(archive, "POINT_IN_TIME_ARCHIVE_FILE", archive_file), patch.object(
                archive, "POINT_IN_TIME_AUDIT_FILE", audit_file
            ), patch.object(archive, "POINT_IN_TIME_PANEL_FILE", panel_file), patch.object(
                archive, "EASTMONEY_FUND_NEWS_QUERIES", ("测试",)
            ), patch("fetcher._fetch_eastmoney_search_news", side_effect=[news, []]):
                result = archive.archive_historical_public_news(
                    "2024-01-01", "2024-03-01", max_pages_per_query=2
                )
                saved = json.loads(archive_file.read_text(encoding="utf-8"))

            query_audit = result["query_audit"]["测试"]
            self.assertEqual(query_audit["earliest_archived_published_at"], "2024-02-02")
            self.assertEqual(query_audit["latest_archived_published_at"], "2024-02-02")
            self.assertEqual(query_audit["stop_reason"], "empty_page")
            self.assertEqual(saved["backfill_runs"][-1]["queries"]["测试"]["pages_attempted"], 2)

    def test_gdelt_backfill_preserves_windowed_publication_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_file, audit_file, panel_file = self._paths(directory)
            news = [
                NewsItem(
                    title="美联储测试新闻", source="GDELT · example.com", summary="测试摘要",
                    affected_funds="018229", direction="中性观察",
                    published_at="2024-01-12 08:30:00",
                )
            ]
            with patch.object(archive, "POINT_IN_TIME_ARCHIVE_FILE", archive_file), patch.object(
                archive, "POINT_IN_TIME_AUDIT_FILE", audit_file
            ), patch.object(archive, "POINT_IN_TIME_PANEL_FILE", panel_file), patch.object(
                archive, "EASTMONEY_FUND_NEWS_QUERIES", ("美联储",)
            ), patch(
                "fetcher._fetch_gdelt_historical_news", side_effect=[news, []]
            ):
                result = archive.archive_historical_gdelt_news(
                    "2024-01-01", "2024-02-01", chunk_days=31
                )
                saved = json.loads(archive_file.read_text(encoding="utf-8"))

            windows = result["query_audit"]["美联储"]
            self.assertEqual(windows[0]["earliest_published_at"], "2024-01-12")
            self.assertEqual(windows[0]["latest_published_at"], "2024-01-12")
            self.assertEqual(saved["news"][0]["source"], "gdelt-news")
            self.assertEqual(saved["backfill_runs"][-1]["type"], "gdelt-news")


if __name__ == "__main__":
    unittest.main()
