from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from fetcher import (
    FundQuote,
    NewsItem,
    _build_macro_factor,
    _holding_eastmoney_secid,
    _holding_yahoo_symbol,
    _parse_sina_forex_history,
    _parse_sina_us_stock_history,
    _parse_treasury_series,
)
from holding_alert import (
    STATE_VERSION,
    _build_reduction_strategy,
    _apply_confirmation,
    _apply_operation_decision,
    _macro_risk,
    _portfolio_risk,
    _reduction_fundamental_evidence,
    _trigger_assessment,
    evaluate_portfolio_alerts,
)


TODAY = date(2026, 7, 29)


def live_history(values: list[float]) -> dict:
    start = TODAY - timedelta(days=len(values) - 1)
    return {
        "records": [
            {"date": (start + timedelta(days=index)).isoformat(), "nav": value}
            for index, value in enumerate(values)
        ],
        "source": "eastmoney-history",
        "latest_date": TODAY.isoformat(),
    }


def live_holding() -> dict:
    return {
        "source": "eastmoney-holdings",
        "disclosure_date": (TODAY - timedelta(days=20)).isoformat(),
        "holdings": [
            {"code": f"STK{index}", "name": f"股票{index}", "weight": 0.08}
            for index in range(1, 6)
        ],
    }


def market() -> dict:
    return {
        "indexes": [
            {
                "symbol": "IXIC",
                "name": "纳斯达克",
                "ma20_gap_pct": 0.5,
                "ma60_gap_pct": 0.5,
                "source": "tencent-index",
            },
            {
                "symbol": "DJI",
                "name": "道琼斯",
                "ma20_gap_pct": 0.5,
                "ma60_gap_pct": 0.5,
                "source": "tencent-index",
            },
        ],
        "macro_factors": [
            {"key": "VIX", "value": 18.0, "date": TODAY.isoformat(), "source": "fred"},
            {"key": "US2Y", "value": 4.0, "date": TODAY.isoformat(), "source": "fred"},
            {"key": "US10Y", "value": 4.2, "change_20": 0.0, "date": TODAY.isoformat(), "source": "fred"},
            {"key": "US10Y_REAL", "value": 1.8, "date": TODAY.isoformat(), "source": "fred"},
            {"key": "DXY", "value": 100.0, "change_20_pct": 0.0, "date": TODAY.isoformat(), "source": "fred"},
            {"key": "USDCNY", "value": 7.1, "change_20_pct": 0.0, "date": TODAY.isoformat(), "source": "fred"},
            {"key": "US_GDP", "value": 2.0, "date": TODAY.isoformat(), "source": "fred"},
            {"key": "US_CPI", "value": 300.0, "date": TODAY.isoformat(), "source": "fred"},
            {"key": "US_UNEMPLOYMENT", "value": 4.2, "date": TODAY.isoformat(), "source": "fred"},
            {"key": "US_M2", "value": 21000.0, "change_12_pct": 3.0, "date": TODAY.isoformat(), "source": "fred"},
            {"key": "US_FED_FUNDS", "value": 4.0, "date": TODAY.isoformat(), "source": "fred"},
            {"key": "US_HY_SPREAD", "value": 3.5, "date": TODAY.isoformat(), "source": "fred"},
        ]
    }


def weak_stock_trends() -> dict[str, dict]:
    return {
        f"STK{index}": {
            "ma60_gap": -0.20,
            "ma120_gap": -0.12,
            "max_drawdown_63": -0.30,
            "source": "sina-stock",
        }
        for index in range(1, 6)
    }


def healthy_stock_fundamentals() -> dict[str, dict]:
    return {
        f"STK{index}": {
            "source": "sina-stock",
            "ma60_gap": 0.05,
            "ma120_gap": 0.08,
            "max_drawdown_63": -0.05,
            "fundamentals": {
                "source": "sec-companyfacts",
                "filed_at": "2026-02-01",
                "fiscal_end": "2025-12-31",
                "metrics": {
                    "revenue_yoy": 0.18,
                    "net_income_yoy": 0.22,
                    "net_margin": 0.18,
                    "operating_margin": 0.21,
                    "free_cash_flow_margin": 0.16,
                    "roe": 0.24,
                },
            },
        }
        for index in range(1, 6)
    }


def run_alert(
    values: list[float],
    quality_gate: str = "READY",
    stock_trends: dict[str, dict] | None = None,
    target_signal: dict | None = None,
    analysis_mode: str | None = None,
    market_snapshot: dict | None = None,
    holding_data: dict | None = None,
) -> dict:
    code = "018229"
    quality_item = {"gate": quality_gate, "confidence": 95}
    if analysis_mode:
        quality_item["analysis_mode"] = analysis_mode
    with tempfile.TemporaryDirectory() as directory:
        result = evaluate_portfolio_alerts(
            {code: {"invested_amount": 2998.83, "shares": 1414.9}},
            {code: FundQuote(code, "测试基金", values[-1], TODAY.isoformat(), source="eastmoney")},
            {code: live_history(values)},
            {code: live_holding() if holding_data is None else holding_data},
            market_snapshot or market(),
            [],
            data_quality={"funds": {code: quality_item}},
            stock_trends=stock_trends or healthy_stock_fundamentals(),
            state_path=Path(directory) / "state.json",
            target_signals={code: target_signal} if target_signal else None,
        )
        return result[code]


class HoldingAlertTests(unittest.TestCase):
    def test_yahoo_fundamentals_cannot_create_reduction_evidence(self) -> None:
        alert = {
            "factor_model": {
                "details": {
                    "fundamental": {
                        "companies": [
                            {
                                "name": "测试公司",
                                "weight": 0.2,
                                "fiscal_end": "2025-12-31",
                                "source": "yahoo-fundamentals",
                                "metrics": {"revenue_yoy": -0.2},
                            }
                        ]
                    }
                }
            }
        }
        self.assertEqual(_reduction_fundamental_evidence(alert), [])

    def test_reduction_strategy_is_blocked_before_production_acceptance(self) -> None:
        alert = run_alert([1.0] * 230 + [1.0 - index * 0.004 for index in range(70)])
        strategy = alert["reduction_strategy"]
        self.assertFalse(strategy["eligible"])
        self.assertIsNone(strategy["percentage"])
        self.assertIn("生产验收", strategy["basis"])

    def test_ma60_reduction_requires_confirmation_and_complete_values(self) -> None:
        base = {
            "level": "RED",
            "trigger_type": "MA60_BREAK",
            "production_approved": True,
            "confirmed": False,
            "decision_gate": {
                "gate": "READY",
                "mode": "STANDARD",
                "can_exact_reduce": True,
            },
            "metrics": {
                "current_nav": 1.84,
                "ma60": 1.90,
                "ma60_gap": -0.0315789,
                "as_of": TODAY.isoformat(),
                "source": "eastmoney-history",
            },
        }
        pending = _build_reduction_strategy(base)
        self.assertFalse(pending["eligible"])
        self.assertIsNone(pending["percentage"])
        self.assertIn("连续两个", pending["basis"])

        confirmed = _build_reduction_strategy({**base, "confirmed": True})
        self.assertTrue(confirmed["eligible"])
        self.assertEqual(confirmed["percentage"], 0.15)
        evidence = confirmed["evidence"][0]
        for field in ("fact", "value", "threshold", "as_of", "source"):
            self.assertTrue(evidence[field])

    def test_ma120_reduction_is_rendered_with_percentage_basis(self) -> None:
        alert = {
            "level": "RED",
            "trigger_type": "MA120_BREAK",
            "production_approved": True,
            "confirmed": True,
            "hard_trigger": True,
            "as_of": TODAY.isoformat(),
            "portfolio_evidence": {},
            "metrics": {
                "current_nav": 1.70,
                "ma120": 1.90,
                "ma120_gap": -0.105263,
                "as_of": TODAY.isoformat(),
                "source": "eastmoney-history",
            },
            "decision_gate": {
                "gate": "READY",
                "mode": "STANDARD",
                "can_exact_reduce": True,
            },
        }
        _apply_operation_decision(
            alert,
            {"status": "wait", "status_level": 0},
            {"holding_months_min": 3, "holding_months_max": 9},
        )
        self.assertTrue(alert["reduction_strategy"]["eligible"])
        self.assertIn("25%", alert["action"])
        self.assertIn("120日均线", alert["decision_reason"])

    def test_confirmed_event_requires_two_dated_independent_sources(self) -> None:
        incomplete = {
            "trigger_type": "CONFIRMED_EVENT",
            "production_approved": True,
            "decision_gate": {"gate": "READY", "mode": "STANDARD", "can_exact_reduce": True},
            "event_evidence": {
                "confirmed": True,
                "matched_weight": 0.25,
                "common_severe_terms": ["出口管制"],
                "evidence_items": [
                    {"source": "source-a", "date": "--", "title": "event"},
                    {"source": "source-b", "date": TODAY.isoformat(), "title": "event"},
                ],
            },
        }
        blocked = _build_reduction_strategy(incomplete)
        self.assertFalse(blocked["eligible"])
        self.assertIsNone(blocked["percentage"])

        complete = {**incomplete, "event_evidence": {
            **incomplete["event_evidence"],
            "evidence_items": [
                {"source": "source-a", "date": TODAY.isoformat(), "title": "event a"},
                {"source": "source-b", "date": TODAY.isoformat(), "title": "event b"},
            ],
        }}
        strategy = _build_reduction_strategy(complete)
        self.assertTrue(strategy["eligible"])
        self.assertEqual(strategy["percentage"], 0.40)
        self.assertIn("双独立来源", strategy["basis"])

    def test_macro_risk_uses_weighted_benchmark_basket(self) -> None:
        snapshot = market()
        snapshot["indexes"][0]["ma20_gap_pct"] = -8.0
        snapshot["indexes"][0]["ma60_gap_pct"] = -8.0
        snapshot["indexes"][1]["ma20_gap_pct"] = 8.0
        snapshot["indexes"][1]["ma60_gap_pct"] = 8.0

        mixed_risk = _macro_risk("018229", snapshot)
        all_weak = market()
        for item in all_weak["indexes"]:
            item["ma20_gap_pct"] = -8.0
            item["ma60_gap_pct"] = -8.0

        self.assertLess(mixed_risk, _macro_risk("018229", all_weak))

    def test_stable_series_is_green(self) -> None:
        alert = run_alert([1 + index * 0.001 for index in range(300)])
        self.assertEqual(alert["level"], "GREEN")
        self.assertEqual(alert["operation_code"], "HOLD")
        self.assertEqual(alert["buy_gate"], "WAIT")

    def test_limited_quality_gate_keeps_core_alert_but_blocks_trade(self) -> None:
        alert = run_alert([1 + index * 0.001 for index in range(300)], "LIMITED")
        self.assertNotEqual(alert["level"], "GRAY")
        self.assertEqual(alert["decision_gate"]["mode"], "HOLDING_LIMITED")
        self.assertEqual(alert["buy_gate"], "LIMITED")
        self.assertIn("核心趋势预警", alert["action"])

    def test_missing_holding_disclosure_keeps_core_alert(self) -> None:
        alert = run_alert(
            [1 + index * 0.001 for index in range(300)],
            holding_data={},
        )
        self.assertNotEqual(alert["level"], "GRAY")
        self.assertFalse(alert["data_readiness"]["holding_disclosure_ready"])
        self.assertEqual(alert["buy_gate"], "LIMITED")
        self.assertIn("增强数据不完整", alert["action"])

    def test_missing_macro_fields_keeps_core_alert_but_limits_action(self) -> None:
        snapshot = market()
        snapshot["macro_factors"] = []
        alert = run_alert(
            [1 + index * 0.001 for index in range(300)],
            market_snapshot=snapshot,
        )
        self.assertNotEqual(alert["level"], "GRAY")
        self.assertFalse(alert["data_readiness"]["macro_core_ready"])
        self.assertEqual(alert["buy_gate"], "LIMITED")

    def test_fallback_data_uses_observe_only_action(self) -> None:
        alert = {
            "level": "GRAY",
            "as_of": TODAY.isoformat(),
            "portfolio_evidence": {},
            "conservative_fallback": True,
        }

        _apply_operation_decision(
            alert,
            {"status": "可分批", "status_level": 3},
            {"holding_months_min": 3, "holding_months_max": 9},
        )

        self.assertEqual(alert["operation_code"], "FALLBACK_OBSERVE")
        self.assertEqual(alert["buy_gate"], "FALLBACK")
        self.assertIn("不输出精确减持比例", alert["action"])

    def test_severe_drawdown_is_red_with_hard_trigger(self) -> None:
        values = [1.0] * 230 + [1.0 - index * 0.004 for index in range(70)]
        alert = run_alert(values, stock_trends=weak_stock_trends())
        self.assertEqual(alert["level"], "RED")
        self.assertTrue(alert["critical_trigger"])
        self.assertTrue(alert["hard_trigger"])
        self.assertIn("120日均线", alert["reasons"][0])
        self.assertTrue(alert["confirmed"])
        self.assertEqual(alert["buy_gate"], "BLOCKED")

    def test_green_risk_allows_small_add_only_when_buy_node_is_active(self) -> None:
        alert = run_alert(
            [1 + index * 0.001 for index in range(300)],
            target_signal={"status": "可分批", "status_level": 3},
        )
        self.assertEqual(alert["operation_code"], "ADD_ALLOWED")
        self.assertEqual(alert["buy_gate"], "ALLOW")
        self.assertIn("小额分批", alert["action"])

    def test_qdii_delay_never_converts_an_active_buy_node_into_add_order(self) -> None:
        alert = run_alert(
            [1 + index * 0.001 for index in range(300)],
            "LIMITED",
            target_signal={"status": "active", "status_level": 3},
            analysis_mode="CONSERVATIVE_DELAYED",
        )
        self.assertEqual(alert["operation_code"], "DATA_DELAYED")
        self.assertEqual(alert["buy_gate"], "LIMITED")
        self.assertFalse(alert["decision_gate"]["can_buy"])

    def test_qdii_delay_allows_score_when_gdp_is_background_only(self) -> None:
        snapshot = market()
        for item in snapshot["macro_factors"]:
            if item["key"] == "US_GDP":
                item["semantic_status"] = "BACKGROUND_ONLY"
                item["source"] = "world-bank"
        alert = run_alert(
            [1 + index * 0.001 for index in range(300)],
            "LIMITED",
            analysis_mode="CONSERVATIVE_DELAYED",
            market_snapshot=snapshot,
        )

        self.assertIsNotNone(alert["risk_score"])
        self.assertIn("factor_model", alert)
        self.assertEqual(alert["decision_gate"]["mode"], "CONSERVATIVE_DELAYED")
        self.assertFalse(alert["decision_gate"]["can_buy"])
        self.assertFalse(alert["decision_gate"]["can_exact_reduce"])

    def test_cached_macro_supports_conservative_alert_without_trade_instruction(self) -> None:
        snapshot = market()
        for item in snapshot["macro_factors"]:
            if item["key"] in {"US_CPI", "US_UNEMPLOYMENT"}:
                item["source"] = "cache"
        alert = run_alert(
            [1 + index * 0.001 for index in range(300)],
            "LIMITED",
            target_signal={"status": "active", "status_level": 3},
            analysis_mode="CONSERVATIVE_FALLBACK",
            market_snapshot=snapshot,
        )
        self.assertNotEqual(alert["level"], "GRAY")
        self.assertIn("metrics", alert)
        self.assertIn("factor_model", alert)
        self.assertLessEqual(alert["confidence"], 65)
        self.assertEqual(alert["operation_code"], "FALLBACK_HOLD")
        self.assertEqual(alert["buy_gate"], "FALLBACK")
        self.assertFalse(alert["decision_gate"]["can_exact_reduce"])

    def test_monthly_cached_macro_is_normal_delay_not_missing_data(self) -> None:
        snapshot = market()
        for item in snapshot["macro_factors"]:
            if item["key"] == "US_UNEMPLOYMENT":
                item["source"] = "cache"
                item["date"] = (TODAY - timedelta(days=20)).isoformat()
                item["frequency"] = "monthly"
        alert = run_alert(
            [1 + index * 0.001 for index in range(300)],
            market_snapshot=snapshot,
        )

        self.assertNotEqual(alert["level"], "GRAY")
        self.assertEqual(alert["decision_gate"]["mode"], "CONSERVATIVE_DELAYED")
        self.assertFalse(alert["decision_gate"]["can_buy"])
        self.assertFalse(alert["decision_gate"]["can_exact_reduce"])
        self.assertIn("保守延迟", alert["summary"])
        self.assertEqual(alert["operation_code"], "DATA_DELAYED")

    def test_yellow_risk_vetoes_active_buy_node(self) -> None:
        alert = {
            "level": "YELLOW",
            "as_of": TODAY.isoformat(),
            "portfolio_evidence": {},
            "production_approved": False,
        }
        _apply_operation_decision(
            alert,
            {"status": "可分批", "status_level": 3},
            {"holding_months_min": 3, "holding_months_max": 9},
        )
        self.assertEqual(alert["buy_gate"], "BLOCKED")
        self.assertEqual(alert["operation_code"], "PAUSE_ADD")
        self.assertIn("暂停加仓", alert["action"])

    def test_fund_ma_break_triggers_red_independent_of_benchmark(self) -> None:
        trigger = _trigger_assessment(
            "018229",
            {
                "max_drawdown_63": -0.24,
                "ma60_gap": -0.12,
                "ma120_gap": -0.10,
            },
            {"holding_risk": 60, "trend_risk": 80},
            {"confirmed": False},
            {"stock_trend_coverage": 0.8, "covered_below_ma60_ratio": 0.8},
            {"indexes": [{"symbol": "IXIC", "ma60_gap_pct": -20}]},
        )
        self.assertTrue(trigger["force_red"])
        self.assertTrue(trigger["immediate"])
        self.assertEqual(trigger["type"], "MA120_BREAK")

    def test_ma60_break_triggers_red_pending_confirmation(self) -> None:
        trigger = _trigger_assessment(
            "018229",
            {
                "max_drawdown_63": -0.08,
                "ma60_gap": -0.03,
                "ma120_gap": 0.02,
            },
            {"holding_risk": 20, "trend_risk": 20},
            {"confirmed": False},
            {"stock_trend_coverage": 0.8, "covered_below_ma60_ratio": 0.2},
            market(),
        )
        self.assertTrue(trigger["force_red"])
        self.assertFalse(trigger["immediate"])
        self.assertEqual(trigger["type"], "MA60_BREAK")

    def test_small_moving_average_breach_is_not_a_hard_trigger(self) -> None:
        trigger = _trigger_assessment(
            "018229",
            {"max_drawdown_63": -0.04, "ma60_gap": -0.005, "ma120_gap": -0.01},
            {"holding_risk": 20, "trend_risk": 20},
            {"confirmed": False},
            {"stock_trend_coverage": 0.8, "covered_below_ma60_ratio": 0.2},
            market(),
        )
        self.assertFalse(trigger["force_red"])
        self.assertEqual(trigger["type"], "NONE")

    def test_alert_model_exposes_probability_estimates(self) -> None:
        alert = run_alert([1 + index * 0.001 for index in range(300)])
        self.assertIn("p20_drawdown_over_8", alert["metrics"])
        self.assertIn("p63_drawdown_over_12", alert["metrics"])
        self.assertFalse(alert["production_approved"])
        self.assertIn("未通过", alert["calibration_status"])

    def test_single_broad_negative_news_cannot_force_red(self) -> None:
        code = "018229"
        values = [1 + index * 0.001 for index in range(300)]
        with tempfile.TemporaryDirectory() as directory:
            alert = evaluate_portfolio_alerts(
                {code: {"invested_amount": 2998.83, "shares": 1414.9}},
                {code: FundQuote(code, "测试基金", values[-1], TODAY.isoformat(), source="eastmoney")},
                {code: live_history(values)},
                {code: live_holding()},
                market(),
                [
                    NewsItem(
                        title="全球贸易限制风险上升",
                        source="东方财富 · 测试媒体",
                        summary="出口管制风险值得关注",
                        affected_funds=code,
                        direction="偏利空",
                    )
                ],
                data_quality={"funds": {code: {"gate": "READY"}}},
                state_path=Path(directory) / "state.json",
            )[code]
        self.assertFalse(alert["hard_trigger"])
        self.assertNotEqual(alert["level"], "RED")

    def test_confidence_reflects_missing_earnings_forecast(self) -> None:
        alert = run_alert([1 + index * 0.001 for index in range(300)])
        self.assertLess(alert["confidence"], 100)
        self.assertTrue(alert["factor_status"]["extended_macro"]["available"])
        self.assertFalse(alert["factor_status"]["earnings_forecast"]["available"])

    def test_two_sources_and_twenty_percent_holding_weight_can_confirm_event(self) -> None:
        code = "018229"
        values = [1 + index * 0.001 for index in range(300)]
        news = [
            NewsItem(
                title="股票1与股票2面临出口管制",
                source="东方财富 · 媒体甲",
                summary="出口管制已经正式发布",
                affected_funds=code,
                direction="偏利空",
            ),
            NewsItem(
                title="股票1、股票2和股票3出口管制获确认",
                source="新浪财经 · 媒体乙",
                summary="出口管制影响相关公司",
                affected_funds=code,
                direction="偏利空",
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            alert = evaluate_portfolio_alerts(
                {code: {"invested_amount": 2998.83, "shares": 1414.9}},
                {code: FundQuote(code, "测试基金", values[-1], TODAY.isoformat(), source="eastmoney")},
                {code: live_history(values)},
                {code: live_holding()},
                market(),
                news,
                data_quality={"funds": {code: {"gate": "READY"}}},
                state_path=Path(directory) / "state.json",
            )[code]
        self.assertTrue(alert["event_evidence"]["confirmed"])
        self.assertTrue(alert["hard_trigger"])
        self.assertEqual(alert["level"], "RED")
        self.assertNotIn("30%", alert["action"])

    def test_ordinary_escalation_requires_two_distinct_dates(self) -> None:
        first = {
            "level": "ORANGE",
            "as_of": "2026-07-29",
            "hard_trigger": False,
            "production_approved": False,
        }
        _apply_confirmation(first, {})
        self.assertFalse(first["confirmed"])
        self.assertEqual(first["confirmation_days"], 1)

        previous = {
            "state_version": STATE_VERSION,
            "stable_level": first["stable_level"],
            "pending_level": first["pending_level"],
            "pending_days": first["confirmation_days"],
            "release_level": None,
            "release_days": 0,
            "last_as_of": "2026-07-29",
        }
        repeated = {**first, "level": "ORANGE", "as_of": "2026-07-29"}
        _apply_confirmation(repeated, previous)
        self.assertEqual(repeated["confirmation_days"], 1)

        second = {**first, "level": "ORANGE", "as_of": "2026-07-30"}
        _apply_confirmation(second, previous)
        self.assertTrue(second["confirmed"])
        self.assertEqual(second["stable_level"], "ORANGE")

    def test_risk_release_requires_three_distinct_dates(self) -> None:
        previous = {
            "state_version": STATE_VERSION,
            "stable_level": "RED",
            "pending_level": None,
            "pending_days": 0,
            "release_level": None,
            "release_days": 0,
            "last_as_of": "2026-07-27",
        }
        for index, day in enumerate(("2026-07-28", "2026-07-29", "2026-07-30"), start=1):
            alert = {
                "level": "YELLOW",
                "as_of": day,
                "hard_trigger": False,
                "production_approved": False,
            }
            _apply_confirmation(alert, previous)
            if index < 3:
                self.assertEqual(alert["level"], "RED")
            previous = {
                "state_version": STATE_VERSION,
                "stable_level": alert["stable_level"],
                "pending_level": alert.get("pending_level"),
                "pending_days": alert.get("confirmation_days", 0),
                "release_level": alert.get("release_level"),
                "release_days": alert.get("release_days", 0),
                "last_as_of": day,
            }
        self.assertEqual(alert["level"], "YELLOW")

    def test_extended_macro_stress_increases_risk(self) -> None:
        base_index = {
            "indexes": [{"symbol": "IXIC", "ma20_gap_pct": 0, "ma60_gap_pct": 0}],
        }
        calm = {
            **base_index,
            "macro_factors": [
                {"key": "VIX", "value": 16, "change_20": -1, "change_20_pct": -5},
                {"key": "US2Y", "value": 3.0, "change_20": 0, "change_20_pct": 0},
                {"key": "US10Y", "value": 3.5, "change_20": 0, "change_20_pct": 0},
                {"key": "US10Y_REAL", "value": 1.0, "change_20": 0, "change_20_pct": 0},
                {"key": "DXY", "value": 100, "change_20": 0, "change_20_pct": 0},
                {"key": "USDCNY", "value": 7.0, "change_20": 0, "change_20_pct": 0},
            ],
        }
        stress = {
            **base_index,
            "macro_factors": [
                {"key": "VIX", "value": 36, "change_20": 12, "change_20_pct": 45},
                {"key": "US2Y", "value": 5.1, "change_20": 0.4, "change_20_pct": 9},
                {"key": "US10Y", "value": 5.0, "change_20": 0.5, "change_20_pct": 11},
                {"key": "US10Y_REAL", "value": 2.6, "change_20": 0.4, "change_20_pct": 18},
                {"key": "DXY", "value": 115, "change_20": 4, "change_20_pct": 3.5},
                {"key": "USDCNY", "value": 6.8, "change_20": -0.2, "change_20_pct": -2.8},
            ],
        }
        self.assertGreater(_macro_risk("018229", stress), _macro_risk("018229", calm))

    def test_portfolio_risk_reports_overlap_and_semiconductor_exposure(self) -> None:
        portfolio = {
            "018229": {"invested_amount": 3000},
            "002891": {"invested_amount": 2000},
        }
        holdings = {
            "018229": {
                "holdings": [
                    {"code": "AMD", "name": "超威半导体", "weight": 0.20},
                    {"code": "MSFT", "name": "微软", "weight": 0.10},
                ]
            },
            "002891": {
                "holdings": [
                    {"code": "AMD", "name": "超威半导体", "weight": 0.15},
                    {"code": "TSM", "name": "台积电", "weight": 0.20},
                ]
            },
        }
        histories = {
            code: live_history([1 + index * 0.001 * multiplier for index in range(300)])
            for code, multiplier in (("018229", 1.0), ("002891", 1.1))
        }
        _, evidence = _portfolio_risk(
            "018229", holdings["018229"], portfolio, holdings, histories
        )
        self.assertAlmostEqual(evidence["max_top10_overlap"], 0.15)
        self.assertGreater(evidence["portfolio_semiconductor_exposure"], 0.25)

    def test_portfolio_risk_is_included_in_score_breakdown(self) -> None:
        alert = run_alert([1 + index * 0.001 for index in range(300)])
        breakdown = alert["risk_score_breakdown"]
        self.assertIn("factor_base_score", breakdown)
        self.assertEqual(breakdown["portfolio_weight"], 0.10)
        self.assertAlmostEqual(
            alert["risk_score"],
            round(
                breakdown["factor_base_score"] * 0.90
                + breakdown["portfolio_risk"] * 0.10,
                1,
            ),
        )

    def test_macro_and_stock_parsers_preserve_market_symbols(self) -> None:
        observations = [
            ((TODAY - timedelta(days=70 - index)).isoformat(), 4.0 + index * 0.01)
            for index in range(70)
        ]
        factor = _build_macro_factor("US10Y", "美国10年期国债收益率", observations, "fred")
        self.assertEqual(factor["source"], "fred")
        self.assertEqual(_holding_yahoo_symbol("688361"), "688361.SS")
        self.assertEqual(_holding_yahoo_symbol("000660"), "000660.KS")
        self.assertEqual(_holding_yahoo_symbol("AMD"), "AMD")
        self.assertEqual(_holding_eastmoney_secid("005930"), "177.005930")
        self.assertEqual(_holding_eastmoney_secid("285A"), "176.285A")

    def test_sina_us_stock_parser_requires_and_returns_daily_closes(self) -> None:
        rows = [
            {"d": (TODAY - timedelta(days=150 - index)).isoformat(), "c": str(100 + index)}
            for index in range(130)
        ]
        payload = "/* redirect guard */ var AMD=(" + json.dumps(rows) + ");"

        observations = _parse_sina_us_stock_history(payload)

        self.assertEqual(len(observations), 130)
        self.assertEqual(observations[-1][1], 229.0)

    def test_sina_forex_parser_uses_close_column(self) -> None:
        rows = "|".join(
            f"2026-06-{index:02d},100.0,99.0,102.0,{100 + index / 10:.1f},"
            for index in range(1, 25)
        )

        observations = _parse_sina_forex_history(f'var _DINIW=("{rows}|");')

        self.assertEqual(len(observations), 24)
        self.assertAlmostEqual(observations[-1][1], 102.4)

    def test_treasury_parser_reads_namespaced_curve_fields(self) -> None:
        entries = "".join(
            f"<m:properties><d:NEW_DATE>2026-06-{index:02d}T00:00:00</d:NEW_DATE>"
            f"<d:BC_10YEAR>{4 + index / 100:.2f}</d:BC_10YEAR></m:properties>"
            for index in range(1, 25)
        )
        payload = (
            '<feed xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" '
            'xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices">'
            + entries
            + "</feed>"
        )

        observations = _parse_treasury_series(payload, "BC_10YEAR")

        self.assertEqual(len(observations), 24)
        self.assertEqual(observations[-1], ("2026-06-24", 4.24))


if __name__ == "__main__":
    unittest.main()
