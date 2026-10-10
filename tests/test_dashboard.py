from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import (  # noqa: E402
    _holding_alert_html,
    _merge_portfolio_fund_quality,
    _nav_return_metrics,
    render_dashboard,
)
from config import PORTFOLIO_HOLDINGS, TARGET_FUNDS, TARGET_FUND_NAMES  # noqa: E402


class DashboardRenderTests(unittest.TestCase):
    def test_nav_return_metrics_use_previous_and_21st_published_nav(self) -> None:
        records = [
            {"date": f"2026-07-{index + 1:02d}", "nav": 1.0}
            for index in range(22)
        ]
        records[-2]["nav"] = 1.25
        records[-1]["nav"] = 1.375

        result = _nav_return_metrics({"records": records})

        self.assertEqual(result["daily_return_pct"], 10.0)
        self.assertEqual(result["monthly_return_pct"], 37.5)

    def test_return_strip_uses_red_for_positive_and_green_for_negative(self) -> None:
        report = {
            "generated_at": "2026-08-26 10:00:00",
            "market": {},
            "data_quality": {"records": []},
            "targets": [{
                "code": "018229", "name": "测试基金", "nav": 1.0,
                "est_nav": None, "nav_date": "2026-08-26", "target_range": "--",
                "buy_levels": [], "trigger": "--", "dont_buy": "--", "method": "--",
                "summary": "--", "valuation_hint": "--", "rule_date": "2026-08-26",
                "rule_basis": "--", "status": "观察", "status_level": 1,
                "daily_return_pct": 2.5, "monthly_return_pct": -1.25,
            }],
            "recommendations": [], "recommendation_qualifications": {},
            "strong_buy": {}, "holding_alerts": {}, "news": [], "headlines": [],
            "data_acquisition": {"network_preflight": {}},
            "fetch_telemetry": {"modules": {}},
        }

        page = render_dashboard(report, "数据可信度 79.9/100")

        self.assertIn("return-positive", page)
        self.assertIn("return-negative", page)
        self.assertIn("↑ +2.50%", page)
        self.assertIn("↓ -1.25%", page)
        self.assertNotIn("规则更新：", page)
        self.assertIn('<details class="rule-box">', page)
        self.assertNotIn('<details class="rule-box" open>', page)
        self.assertIn("<summary>买入节点</summary>", page)
        self.assertIn("<summary>不买条件</summary>", page)

    def test_target_fund_pool_is_empty(self) -> None:
        self.assertEqual(TARGET_FUNDS, [])
        self.assertEqual(TARGET_FUND_NAMES, {})
        self.assertIsInstance(PORTFOLIO_HOLDINGS, dict)
    def test_portfolio_quality_merge_preserves_target_score_and_records(self) -> None:
        target_quality = {
            "overall_score": 79.9,
            "records": [{"key": "fund_nav"}],
            "funds": {"018229": {"gate": "READY"}},
        }
        portfolio_quality = {
            "overall_score": 12.3,
            "records": [{"key": "portfolio-only"}],
            "funds": {"002891": {"gate": "LIMITED"}},
        }

        _merge_portfolio_fund_quality(target_quality, portfolio_quality)

        self.assertEqual(target_quality["overall_score"], 79.9)
        self.assertEqual(target_quality["records"], [{"key": "fund_nav"}])
        self.assertEqual(
            set(target_quality["funds"]), {"018229", "002891"}
        )

    def test_portfolio_card_uses_alert_name_and_ledger_nav_cache_fallback(self) -> None:
        report = {
            "generated_at": "2026-08-26 10:00:00",
            "market": {},
            "data_quality": {"records": []},
            "targets": [],
            "recommendations": [],
            "recommendation_qualifications": {},
            "strong_buy": {},
            "holding_alerts": {
                "002891": {
                    "name": "华夏移动互联混合(QDII)",
                    "action": "仅观察",
                }
            },
            "portfolio_ledger": {
                "positions": {
                    "002891": {
                        "shares": 1174.92045,
                        "cost_basis": 1200.00,
                        "cost_nav": 2.5897,
                        "current_nav": 2.612,
                        "nav_date": "2026-08-25",
                        "nav_source": "cache",
                        "market_value": 3068.89,
                    }
                }
            },
            "news": [],
            "headlines": [],
            "data_acquisition": {"network_preflight": {}},
            "fetch_telemetry": {"modules": {}},
        }

        page = render_dashboard(report, "数据可信度 79.9/100")

        self.assertIn("华夏移动互联混合(QDII)", page)
        self.assertIn("官方单位净值 2.6120", page)
        self.assertIn("净值日 2026-08-25", page)
        self.assertIn("来源 cache · 缓存估值，仅供展示", page)
        self.assertNotIn("来源 cache · 官方净值已更新至本次抓取日", page)

    def test_critical_macro_notice_and_decimal_quality_score_render(self) -> None:
        report = {
            "generated_at": "2026-08-26 10:00:00",
            "market": {},
            "data_quality": {
                "overall_score": 79.9,
                "records": [],
                "macro": {"critical_missing": True},
            },
            "targets": [],
            "recommendations": [],
            "strong_buy": {},
            "holding_alerts": {},
            "news": [],
            "headlines": [],
            "data_acquisition": {"network_preflight": {}},
            "fetch_telemetry": {"modules": {}},
        }

        page = render_dashboard(report, "数据可信度 79.9/100")

        self.assertIn("79.9<small>/100", page)
        self.assertIn("数据可信度：79.9/100", page)
        self.assertNotIn("数据可信度：80/100", page)
        self.assertIn(
            "当前未配置指定基金；季度 GDP 或真实 OAS 未完整获取，当前仅允许保守分析。",
            page,
        )

    def test_dashboard_renders_tabs_and_current_value_portfolio_form(self) -> None:
        report = {
            "generated_at": "2026-09-22 10:00:00",
            "market": {},
            "data_quality": {"records": []},
            "targets": [],
            "recommendations": [],
            "recommendation_qualifications": {},
            "strong_buy": {},
            "holding_alerts": {},
            "news": [],
            "headlines": [],
            "data_acquisition": {"network_preflight": {}},
            "fetch_telemetry": {"modules": {}},
        }

        page = render_dashboard(report, "数据可信度 79.9/100")

        self.assertIn('class="dashboard-tabs"', page)
        self.assertIn('data-tab-target="tab-holdings"', page)
        self.assertIn('data-tab-target="tab-ai"', page)
        self.assertIn('data-tab-target="tab-recommend"', page)
        self.assertIn('data-tab-target="tab-target"', page)
        self.assertIn('data-tab-target="tab-extra"', page)
        self.assertIn('data-tab-target="tab-watch"', page)
        self.assertIn('id="portfolio-config-form"', page)
        self.assertIn("当前市值（元）", page)
        self.assertIn("当前盈亏（元）", page)
        self.assertIn("买入日期", page)
        self.assertIn("当前市值必须大于盈亏金额", page)
        self.assertIn('fetch("/api/portfolio"', page)
        self.assertIn('fetch(`/api/portfolio/${encodeURIComponent(code)}`', page)
    def test_reduction_evidence_panel_renders_auditable_fields(self) -> None:
        page = _holding_alert_html(
            {
                "level": "RED",
                "level_label": "红色",
                "risk_score": 80,
                "reduction_strategy": {
                    "eligible": True,
                    "percentage_label": "建议减持 25%",
                    "basis": "跌破120日均线的技术硬触发标准档位",
                    "evidence": [{
                        "category": "技术确认",
                        "fact": "基金净值跌破120日均线",
                        "value": "当前净值 1.7000，MA120 1.9000，偏离 -10.53%",
                        "threshold": "当前净值 < MA120（偏离 < 0）",
                        "as_of": "2026-08-06",
                        "source": "eastmoney-history",
                    }],
                    "review_condition": "下一有效净值日复核",
                },
            }
        )
        self.assertIn("建议减持 25%", page)
        self.assertIn("MA120 1.9000", page)
        self.assertIn("2026-08-06", page)
        self.assertIn("eastmoney-history", page)

    def test_data_acquisition_audit_is_compact_and_at_bottom(self) -> None:
        report = {
            "generated_at": "2026-08-04 10:00:00",
            "market": {},
            "data_quality": {"records": []},
            "targets": [
                {
                    "code": "018229",
                    "name": "测试基金",
                    "nav": 1.0,
                    "est_nav": None,
                    "nav_date": "2026-08-04",
                    "data_source": "东方财富",
                    "status": "等待实时数据",
                    "status_level": 1,
                    "target_range": "--",
                    "buy_levels": [],
                    "trigger": "关键数据未通过校验。",
                    "dont_buy": "不执行新增买入。",
                    "method": "等待数据",
                    "summary": "测试摘要",
                    "valuation_hint": "仅供展示",
                    "rule_date": "2026-08-04",
                    "rule_basis": "数据闸门",
                    "factor_model": None,
                }
            ],
            "recommendations": [],
            "recommendation_qualifications": {},
            "strong_buy": {},
            "holding_alerts": {},
            "news": [],
            "headlines": [],
            "data_acquisition": {
                "policy": "严格数据闸门",
                "source_priority": {"fund_nav": ["eastmoney", "eastmoney-js"]},
                "network_preflight": {
                    "available": False,
                    "checked_at": "2026-08-04 10:00:00",
                    "probes": [],
                },
                "attempts": [
                    {
                        "at": "2026-08-04 10:00:00",
                        "source": "eastmoney",
                        "status": "SKIPPED_NETWORK",
                        "error": "网络预检未通过",
                    }
                ],
            },
            "fetch_telemetry": {"modules": {}},
        }

        page = render_dashboard(report, "数据可信度 0/100；禁止判断")

        self.assertIn("数据获取审计", page)
        self.assertIn("网络不可用", page)
        self.assertIn('class="bottom-data-results"', page)
        self.assertNotIn("SKIPPED_NETWORK", page)
        self.assertNotIn("实时来源优先顺序", page)
        self.assertNotIn("本次实际来源调用", page)
        self.assertNotIn("严格数据闸门", page)
        self.assertNotIn("预检时间", page)
        self.assertNotIn('class="audit-grid"', page)
        self.assertNotIn('class="quality-table"', page)
