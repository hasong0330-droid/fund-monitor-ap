from datetime import date
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_cycle_alert import apply_ai_cycle_gates, build_ai_cycle_alert  # noqa: E402
from app import _ai_cycle_alert_html, _ai_cycle_fund_badge  # noqa: E402


class AICycleAlertTests(unittest.TestCase):
    def news(
        self,
        title: str,
        source: str,
        published_at: str,
        link: str = "https://example.test/evidence",
    ) -> dict:
        return {
            "title": title,
            "summary": title,
            "source": source,
            "published_at": published_at,
            "link": link,
        }

    def test_missing_evidence_is_gray_not_green(self) -> None:
        alert = build_ai_cycle_alert([], {}, today=date(2026, 9, 4))
        self.assertEqual(alert["level"], "GRAY")
        self.assertIsNone(alert["risk_score"])
        self.assertEqual(alert["coverage"], 0)

    def test_shorter_gpu_lead_time_alone_cannot_trigger_cycle_warning(self) -> None:
        alert = build_ai_cycle_alert(
            [self.news("英伟达GPU交期缩短", "产业媒体A", "2026-09-03")],
            {},
            today=date(2026, 9, 4),
        )
        self.assertEqual(alert["level"], "GRAY")
        self.assertEqual(alert["coverage"], 0.15)
        self.assertTrue(alert["dimensions"]["delivery_lead_time"]["active"])

    def test_three_dimensions_with_official_and_independent_sources_can_turn_red(self) -> None:
        rows = [
            self.news("微软官方下调资本开支", "公司公告 · Microsoft", "2026-09-01"),
            self.news("亚马逊削减资本开支", "财经媒体A", "2026-09-02"),
            self.news("美光DRAM价格下跌", "产业媒体B", "2026-09-01"),
            self.news("SK海力士HBM价格下跌", "产业媒体C", "2026-09-03"),
            self.news("英伟达财报下调指引", "公司公告 · NVIDIA", "2026-09-02"),
            self.news("AMD盈利指引下调", "财经媒体D", "2026-09-03"),
        ]
        alert = build_ai_cycle_alert(rows, {}, today=date(2026, 9, 4))
        self.assertEqual(alert["level"], "RED")
        self.assertGreaterEqual(alert["coverage"], 0.60)
        self.assertGreaterEqual(len(alert["active_dimensions"]), 3)
        self.assertTrue(alert["official_confirmed"])

    def test_news_body_saying_financial_report_is_not_official_evidence(self) -> None:
        alert = build_ai_cycle_alert(
            [self.news("媒体解读英伟达财报下调指引", "财经媒体A", "2026-09-03")],
            {},
            today=date(2026, 9, 4),
        )
        self.assertFalse(alert["dimensions"]["earnings_guidance"]["official_confirmed"])
        self.assertFalse(alert["official_confirmed"])

    def test_ai_context_matches_word_or_chinese_term_not_plain_word_fragment(self) -> None:
        rows = [
            self.news("Retail融资成本上升", "财经媒体A", "2026-09-03"),
            self.news("AI收入增速低于折旧成本", "财经媒体B", "2026-09-03"),
        ]
        alert = build_ai_cycle_alert(rows, {}, today=date(2026, 9, 4))
        evidence = alert["dimensions"]["ai_economics"]["evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["source"], "财经媒体B")

    def test_fund_exposure_and_fof_are_kept_separate(self) -> None:
        holdings = {
            "016702": {
                "holdings": [
                    {"code": "NVDA", "name": "英伟达", "weight": 0.25},
                    {"code": "MSFT", "name": "微软", "weight": 0.20},
                    {"code": "XOM", "name": "埃克森美孚", "weight": 0.05},
                ]
            },
            "161815": {"allocation_only": True, "holdings": []},
        }
        alert = build_ai_cycle_alert([], holdings, today=date(2026, 9, 4))
        self.assertEqual(alert["fund_impacts"]["016702"]["observed_exposure"], 0.45)
        self.assertEqual(alert["fund_impacts"]["016702"]["level"], "很高")
        self.assertFalse(alert["fund_impacts"]["161815"]["applicable"])
        self.assertEqual(alert["fund_impacts"]["161815"]["gate"], "NOT_APPLICABLE")

    def test_gate_blocks_add_only_and_does_not_create_sell_percentage(self) -> None:
        alert = {
            "level": "ORANGE",
            "level_label": "橙色",
            "fund_impacts": {
                "002891": {"gate": "BLOCKED", "observed_exposure": 0.42}
            },
        }
        holding_alerts = {
            "002891": {
                "level": "GREEN",
                "action": "继续持有",
                "reasons": [],
                "reduction_strategy": {"eligible": False, "percentage": None},
            }
        }
        strong_buy = {"code": "002891", "is_strong_buy": True}
        apply_ai_cycle_gates(alert, holding_alerts, strong_buy)
        self.assertEqual(holding_alerts["002891"]["buy_gate"], "BLOCKED")
        self.assertIsNone(holding_alerts["002891"]["reduction_strategy"]["percentage"])
        self.assertFalse(strong_buy["is_strong_buy"])

    def test_dashboard_panel_and_fund_badge_render(self) -> None:
        alert = {
            "level": "YELLOW",
            "level_label": "黄色",
            "risk_score": 46.2,
            "confidence": 65,
            "as_of": "2026-09-03",
            "summary": "资本开支出现转弱证据。",
            "dimensions": {},
            "fund_impacts": {
                "016702": {
                    "applicable": True,
                    "observed_exposure": 0.41,
                    "level": "很高",
                    "gate": "CAUTION",
                }
            },
            "evidence": [],
            "rules": "单一信号不触发红色。",
        }
        panel = _ai_cycle_alert_html(alert)
        badge = _ai_cycle_fund_badge(alert, "016702")
        self.assertIn("AI资本开支周期预警", panel)
        self.assertIn("46.2/100", panel)
        self.assertIn("查看证据与判定口径", panel)
        self.assertIn("AI周期暴露 41.0%", badge)
        self.assertIn("谨慎观察", badge)


if __name__ == "__main__":
    unittest.main()
