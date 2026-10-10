from datetime import date, timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fetcher import FundQuote, NewsItem  # noqa: E402
from quant_model import (  # noqa: E402
    _evaluate_candidate,
    _holding_score,
    build_scoring_candidates,
    evaluate_strong_buy,
)


TODAY = date(2026, 7, 29)


def history() -> dict:
    start = TODAY - timedelta(days=299)
    return {
        "records": [
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "nav": 1.0 + index * 0.001,
            }
            for index in range(300)
        ],
        "latest_date": TODAY.isoformat(),
        "source": "eastmoney-history",
    }


def holding() -> dict:
    return {
        "source": "eastmoney-holdings",
        "disclosure_date": (TODAY - timedelta(days=30)).isoformat(),
        "holdings": [
            {"code": f"STK{index}", "name": f"股票{index}", "weight": 0.10}
            for index in range(1, 6)
        ],
    }


def trends(count: int = 5) -> dict[str, dict]:
    return {
        f"STK{index}": {
            "source": "sina-stock",
            "ma60_gap": 0.03,
            "ma120_gap": 0.05,
            "max_drawdown_63": -0.08,
        }
        for index in range(1, count + 1)
    }


class QuantHoldingModelTests(unittest.TestCase):
    def test_scoring_universe_includes_global_targets_and_qualified_external_funds(self) -> None:
        candidates = build_scoring_candidates(
            [
                {"code": "016702", "name": "全球候选"},
                {"code": "118001", "name": "亚洲候选"},
            ],
            [{"code": "017653", "name": "外部合格基金"}],
        )

        self.assertEqual([item["code"] for item in candidates], ["017653", "016702"])

    def test_user_direct_candidate_enters_scoring_universe(self) -> None:
        candidates = build_scoring_candidates(
            [],
            [{"code": "161815", "name": "用户指定基金"}],
        )

        self.assertEqual([item["code"] for item in candidates], ["161815"])

    @patch("quant_model._update_signal_streak", return_value=0)
    @patch("quant_model._evaluate_candidate")
    def test_highest_score_is_recommended_even_when_strong_buy_is_blocked(
        self, evaluate_candidate, _update_streak
    ) -> None:
        def result(item, *_args, **_kwargs):
            score = 76.0 if item["code"] == "HIGH" else 65.0
            return {
                "code": item["code"],
                "name": item["name"],
                "total_score": score,
                "confidence": 80.0,
                "failed_conditions": ["测试交易闸门未通过"],
            }

        evaluate_candidate.side_effect = result
        output = evaluate_strong_buy(
            [
                {"code": "LOW", "name": "低分基金"},
                {"code": "HIGH", "name": "高分基金"},
            ],
            {"LOW": object(), "HIGH": object()},
            {"LOW": {"records": [1]}, "HIGH": {"records": [1]}},
            {},
            {},
            [],
        )

        self.assertEqual(output["code"], "HIGH")
        self.assertTrue(output["is_recommended"])
        self.assertFalse(output["is_strong_buy"])
        self.assertEqual(output["verdict"], "综合评分首选（暂缓执行）")
        self.assertEqual(output["all_candidates"][0]["rank"], 1)

    @patch("quant_model._update_signal_streak", return_value=0)
    @patch("quant_model._evaluate_candidate")
    def test_paused_purchase_candidate_is_removed_before_scoring(
        self, evaluate_candidate, _update_streak
    ) -> None:
        def result(item, *_args, **_kwargs):
            return {
                "code": item["code"],
                "name": item["name"],
                "total_score": 60.0,
                "confidence": 80.0,
                "failed_conditions": ["测试交易闸门未通过"],
            }

        evaluate_candidate.side_effect = result
        output = evaluate_strong_buy(
            [
                {"code": "PAUSED", "name": "暂停基金"},
                {"code": "OPEN", "name": "开放基金"},
            ],
            {"PAUSED": object(), "OPEN": object()},
            {
                "PAUSED": {"records": [1], "purchase_status": "暂停申购"},
                "OPEN": {"records": [1], "purchase_status": "限制大额申购"},
            },
            {},
            {},
            [],
        )

        self.assertEqual(output["code"], "OPEN")
        self.assertTrue(any("硬性条件剔除" in item for item in output["data_errors"]))
        scored_codes = [item["code"] for item in output["all_candidates"]]
        self.assertNotIn("PAUSED", scored_codes)

    def test_holding_score_uses_stock_trend_coverage(self) -> None:
        full = _holding_score(holding(), [], trends(), TODAY.isoformat())
        partial = _holding_score(holding(), [], trends(2), TODAY.isoformat())

        self.assertEqual(full[4], 1.0)
        self.assertAlmostEqual(partial[4], 0.4)
        self.assertGreater(full[0], partial[0])

    def test_negative_holding_news_reduces_final_score(self) -> None:
        negative_news = [
            NewsItem(
                title="股票1与股票2下调业绩指引",
                source="东方财富 · 测试",
                summary="股票1和股票2盈利预期下修",
                direction="偏利空",
            )
        ]

        neutral = _holding_score(holding(), [], trends(), TODAY.isoformat())
        negative = _holding_score(
            holding(), negative_news, trends(), TODAY.isoformat()
        )

        self.assertLess(negative[0], neutral[0])
        self.assertLess(negative[5], 40)
        self.assertGreaterEqual(negative[6], 0.20)

    def test_candidate_is_blocked_when_stock_coverage_is_below_sixty_percent(self) -> None:
        code = "017653"
        result = _evaluate_candidate(
            {"code": code, "name": "测试候选基金"},
            FundQuote(code, "测试候选基金", 1.3, TODAY.isoformat(), source="eastmoney"),
            history(),
            holding(),
            None,
            {
                "indexes": [
                    {"symbol": "IXIC", "ma20_gap_pct": 0, "ma60_gap_pct": 1},
                    {"symbol": "DJI", "ma20_gap_pct": 0, "ma60_gap_pct": 1},
                ]
            },
            [],
            {"gate": "READY", "confidence": 95},
            trends(2),
        )

        self.assertAlmostEqual(result["holding_stock_coverage"], 0.4)
        self.assertTrue(
            any("低于强买硬门槛 60%" in item for item in result["failed_conditions"])
        )


if __name__ == "__main__":
    unittest.main()
