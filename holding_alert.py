from __future__ import annotations

import json
import math
import statistics
from datetime import date
from pathlib import Path
from typing import Iterable

from config import (
    CACHE_DIR,
    FUND_BENCHMARKS,
    HOLDING_ALERT_CALIBRATION_FILE,
    PORTFOLIO_HOLDINGS,
    HOLDING_ALERT_STATE_FILE,
    TARGET_FUND_NAMES,
)
from data_quality import CONSERVATIVE_FALLBACK, READY
from decision_gate import build_decision_gate
from factor_model import FACTOR_WEIGHTS, evaluate_fund_factors
from source_router import is_trading_eligible_source, live_source_names
from storage import atomic_write_json


MODEL_VERSION = "Fund Guard v2.0 Fundamental-Led Five-Factor"
STATE_VERSION = 3
LEVEL_SEVERITY = {"GREEN": 0, "YELLOW": 1, "ORANGE": 2, "RED": 3}
ALERT_LEVELS = {
    "GREEN": "绿色",
    "YELLOW": "黄色",
    "ORANGE": "橙色",
    "RED": "红色",
    "GRAY": "灰色",
}
LIVE_QUOTE_SOURCES = live_source_names("fund_nav")
LIVE_HISTORY_SOURCES = live_source_names("fund_history")
LIVE_HOLDING_SOURCES = live_source_names("holdings")
LIVE_MACRO_SOURCES = live_source_names("macro")
LIVE_STOCK_TREND_SOURCES = live_source_names("stock_trends")
INDEX_MAP = {
    "018229": "IXIC",
    "005534": "000300.SS",
    "161815": "DJI",
    "016701": "IXIC",
    "162415": "DJI",
    "270023": "IXIC",
}

# Low-frequency macro series remain valid between scheduled releases.  A
# cached value in this window should be treated as a normal disclosure delay,
# not as missing evidence.  The alert is still downgraded to conservative
# delayed mode (no buy or exact-reduction instruction).
LOW_FREQUENCY_MACRO_MAX_AGE_DAYS = {
    "monthly": 62,
    "quarterly": 190,
    "annual": 400,
}

# Small moving-average breaches are common noise for delayed fund NAVs.  Use a
# buffer before escalating, and reserve immediate confirmation for a deep
# 120-day break.
MA60_ALERT_BUFFER = 0.02
MA120_ALERT_BUFFER = 0.03
MA120_IMMEDIATE_BUFFER = 0.08
PORTFOLIO_RISK_WEIGHT = 0.10
HOLDING_CORE_HISTORY_MIN_RECORDS = 126
HOLDING_LIMITED_MODE = "HOLDING_LIMITED"


def _is_valid_delayed_macro_item(item: dict | None, reference_date: date | None = None) -> bool:
    if not isinstance(item, dict) or item.get("source") != "cache":
        return False
    frequency = str(item.get("frequency") or "").lower()
    max_age = LOW_FREQUENCY_MACRO_MAX_AGE_DAYS.get(frequency)
    if max_age is None:
        return False
    try:
        observed = date.fromisoformat(str(item.get("date")))
    except (TypeError, ValueError):
        return False
    age_days = ((reference_date or date.today()) - observed).days
    if age_days < 0 or age_days > max_age:
        return False
    required_fields = item.get("required_fields") or (
        ["value", "change_12_pct"] if item.get("key") == "US_M2" else ["value"]
    )
    return all(isinstance(item.get(field), (int, float)) for field in required_fields)


def _macro_reference_date(market_snapshot: dict | None) -> date:
    dates = []
    for item in (market_snapshot or {}).get("macro_factors", []):
        try:
            dates.append(date.fromisoformat(str(item.get("date"))))
        except (TypeError, ValueError):
            continue
    return max(dates) if dates else date.today()


def _delayed_macro_keys(market_snapshot: dict | None) -> list[str]:
    macro = {
        item.get("key"): item
        for item in (market_snapshot or {}).get("macro_factors", [])
    }
    required = (
        "VIX", "US2Y", "US10Y", "US10Y_REAL", "DXY", "USDCNY",
        "US_GDP", "US_CPI", "US_UNEMPLOYMENT", "US_M2", "US_FED_FUNDS",
        "US_HY_SPREAD",
    )
    reference_date = _macro_reference_date(market_snapshot)
    return [
        key
        for key in required
        if _is_valid_delayed_macro_item(macro.get(key), reference_date)
    ]


def _benchmark_snapshots(code: str, market_snapshot: dict) -> list[dict]:
    specs = FUND_BENCHMARKS.get(code, ((INDEX_MAP.get(code, "IXIC"), 1.0),))
    indexes = {
        item.get("symbol"): item for item in market_snapshot.get("indexes", [])
    }
    return [
        {**indexes[symbol], "weight": weight}
        for symbol, weight in specs
        if symbol in indexes
    ]


def _weighted_benchmark_gap(code: str, market_snapshot: dict, field: str) -> float:
    snapshots = _benchmark_snapshots(code, market_snapshot)
    if not snapshots:
        return 0.0
    total_weight = sum(float(item.get("weight", 1.0)) for item in snapshots) or 1.0
    return sum(
        float(item.get(field, 0)) * float(item.get("weight", 1.0))
        for item in snapshots
    ) / total_weight


def evaluate_portfolio_alerts(
    portfolio: dict[str, dict],
    quotes: dict[str, object],
    histories: dict[str, dict],
    holdings: dict[str, dict],
    market_snapshot: dict,
    news: Iterable[object],
    data_quality: dict | None = None,
    stock_trends: dict[str, dict] | None = None,
    state_path: Path | None = None,
    target_signals: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """Evaluate holding risk separately from the buy-signal model.

    The probability overlay is deliberately labelled an estimate until a
    point-in-time walk-forward calibration has been completed.
    """
    data_quality = data_quality or {}
    news_items = list(news)
    stock_trends = stock_trends or {}
    target_signals = target_signals or {}
    state_path = state_path or HOLDING_ALERT_STATE_FILE
    previous = _read_state(state_path)
    calibration = _load_calibration()
    alerts = {}
    for code, config in portfolio.items():
        alert = _evaluate_one(
            code,
            config,
            quotes.get(code),
            histories.get(code),
            holdings.get(code),
            market_snapshot,
            news_items,
            data_quality.get("funds", {}).get(code, {}),
            data_quality,
            portfolio,
            histories,
            holdings,
            stock_trends,
            calibration,
        )
        _apply_confirmation(alert, previous.get(code, {}))
        _apply_operation_decision(alert, target_signals.get(code, {}), config)
        alerts[code] = alert
    _write_state(state_path, alerts, previous)
    return alerts


def _evaluate_one(
    code: str,
    config: dict,
    quote: object | None,
    history: dict | None,
    holding: dict | None,
    market_snapshot: dict,
    news: list[object],
    quality_item: dict,
    overall_quality: dict,
    portfolio: dict[str, dict],
    histories: dict[str, dict],
    holdings_by_fund: dict[str, dict],
    stock_trends: dict[str, dict],
    calibration: dict,
) -> dict:
    holding = holding or {}
    name = getattr(quote, "name", None) or TARGET_FUND_NAMES.get(code, code)
    as_of = (history or {}).get("latest_date") or getattr(quote, "date", "--")
    delayed_macro_keys = _delayed_macro_keys(market_snapshot)
    decision_gate = build_decision_gate(quality_item, overall_quality)
    if delayed_macro_keys and decision_gate["mode"] == "STANDARD":
        decision_gate = {
            **decision_gate,
            "mode": "CONSERVATIVE_DELAYED",
            "can_buy": False,
            "can_strong_buy": False,
            "can_exact_reduce": False,
            "reasons": [
                *decision_gate.get("reasons", []),
                "低频宏观数据处于正常披露间隔（"
                + "、".join(delayed_macro_keys)
                + "使用带日期缓存），仅允许保守分析。",
            ],
        }
    conservative_macro = decision_gate["mode"] == CONSERVATIVE_FALLBACK
    conservative_delayed = decision_gate["mode"] == "CONSERVATIVE_DELAYED"
    base = {
        "model_version": MODEL_VERSION,
        "code": code,
        "name": name,
        "as_of": as_of,
        "data_gate": quality_item.get("gate", "--"),
        "decision_gate": decision_gate,
        "conservative_macro": conservative_macro,
        "calibration_status": calibration.get("status", "尚未生成滚动样本外校准结果"),
        "production_approved": bool(calibration.get("production_approved")),
        "confirmation_days": 0,
        "confirmed": False,
    }
    readiness = _holding_data_readiness(
        code, quote, history, holding, quality_item, market_snapshot
    )
    if not readiness["core_ready"]:
        if quality_item.get("analysis_mode") == CONSERVATIVE_FALLBACK:
            return {
                **base,
                "level": "GRAY",
                "level_label": "保守回退",
                "risk_score": None,
                "confidence": float(quality_item.get("confidence", 0) or 0),
                "action": "数据回退观察，暂停新增仓位；不输出精确减持比例。",
                "summary": "实时源已按优先顺序回退至缓存或静态参考，保留持仓风险观察但不输出确定性交易指令。",
                "reasons": list(quality_item.get("reasons", [])) or [
                    "当前使用缓存或静态参考数据，结论仅供保守观察。"
                ],
                "metrics": {},
                "components": {},
                "hard_trigger": False,
                "critical_trigger": False,
                "trigger_type": "FALLBACK",
                "conservative_fallback": True,
                "data_readiness": readiness,
            }
        return {
            **base,
            "level": "GRAY",
            "level_label": ALERT_LEVELS["GRAY"],
            "risk_score": None,
            "confidence": 0.0,
            "action": "数据不足，暂停判断",
            "summary": "净值、历史净值、持仓披露或数据可信度不足，模型不输出继续持有或减持结论。",
            "reasons": ["关键数据未同时通过来源、时效和完整性校验。"],
            "metrics": {},
            "components": {},
            "hard_trigger": False,
            "critical_trigger": False,
            "trigger_type": "DATA",
            "data_readiness": readiness,
        }

    holding_limited = bool(readiness["limited_reasons"])
    if holding_limited and decision_gate["mode"] not in {
        CONSERVATIVE_FALLBACK,
        "CONSERVATIVE_DELAYED",
    }:
        decision_gate = {
            **decision_gate,
            "gate": "LIMITED",
            "gate_label": "受限数据",
            "mode": HOLDING_LIMITED_MODE,
            "can_analyze": True,
            "can_buy": False,
            "can_strong_buy": False,
            "can_exact_reduce": False,
            "reasons": [*decision_gate.get("reasons", []), *readiness["limited_reasons"]],
        }
        conservative_macro = False
        conservative_delayed = False
    base["decision_gate"] = decision_gate
    base["data_readiness"] = readiness

    metrics = _history_metrics(history["records"])
    event_evidence = _news_event_evidence(code, name, holding, news)
    macro_evidence = _macro_evidence(market_snapshot)
    holding_risk, holding_evidence = _holding_risk(
        code, holding, news, event_evidence, stock_trends
    )
    portfolio_risk, portfolio_evidence = _portfolio_risk(
        code, holding, portfolio, holdings_by_fund, histories
    )
    factor_evaluation = evaluate_fund_factors(
        code,
        history,
        holding,
        market_snapshot,
        news,
        stock_trends,
    )
    factor_scores = factor_evaluation["scores"]
    components = {
        "fundamental_risk": 100 - factor_scores["fundamental"],
        "technical_risk": 100 - factor_scores["technical"],
        "industry_risk": 100 - factor_scores["industry"],
        "other_risk": 100 - factor_scores["other"],
        "tail_risk": _tail_risk(metrics),
        "trend_risk": _trend_risk(metrics),
        "holding_risk": holding_risk,
        "macro_risk": 100 - factor_scores["macro"],
        "portfolio_risk": portfolio_risk,
        "operation_risk": _operation_risk(holding),
    }
    factor_base_score = (
        components["fundamental_risk"] * FACTOR_WEIGHTS["fundamental"]
        + components["macro_risk"] * FACTOR_WEIGHTS["macro"]
        + components["technical_risk"] * FACTOR_WEIGHTS["technical"]
        + components["industry_risk"] * FACTOR_WEIGHTS["industry"]
        + components["other_risk"] * FACTOR_WEIGHTS["other"]
    )
    # Portfolio concentration/correlation is a distinct overlay.  Blend it
    # explicitly instead of only using it as an occasional veto, so a fund
    # with high standalone quality still reflects concentration risk.
    # Use the same one-decimal inputs that are exposed in the breakdown when
    # calculating the displayed score, so the total is reproducible from the
    # page values without a rounding mismatch.
    factor_base_score_display = round(factor_base_score, 1)
    portfolio_risk_display = round(components["portfolio_risk"], 1)
    base_score = (
        factor_base_score_display * (1.0 - PORTFOLIO_RISK_WEIGHT)
        + portfolio_risk_display * PORTFOLIO_RISK_WEIGHT
    )
    p20 = _probability_estimate(metrics, calibration, horizon="20")
    p63 = _probability_estimate(metrics, calibration, horizon="63")
    production_approved = bool(calibration.get("production_approved"))
    risk_score = (
        _clamp(0.65 * base_score + 0.20 * p20 * 100 + 0.15 * p63 * 100, 0, 100)
        if production_approved
        else _clamp(base_score, 0, 100)
    )
    trigger = _trigger_assessment(
        code,
        metrics,
        components,
        event_evidence,
        holding_evidence,
        market_snapshot,
    )
    hard_trigger = bool(trigger["immediate"])
    if trigger["force_red"]:
        level = "RED"
    else:
        level = _level_for_score(risk_score)
    reasons = _reasons_for_components(
        code, metrics, components, p20, p63, production_approved
    )
    if trigger["reason"]:
        reasons.insert(0, trigger["reason"])
    confidence, factor_status = _factor_confidence(
        code,
        quote,
        history,
        holding,
        market_snapshot,
        news,
        portfolio,
        histories,
        stock_trends,
    )
    summary = _summary(level, hard_trigger)
    if conservative_delayed:
        confidence = min(confidence, 75.0)
        factor_evaluation["confidence"] = min(
            float(factor_evaluation.get("confidence", 0) or 0), 75.0
        )
        reasons.insert(
            0,
            "低频宏观数据处于正常披露间隔（"
            + "、".join(delayed_macro_keys)
            + "使用带日期缓存），仅用于保守预警；暂停新增仓位和精确减持。",
        )
        summary = f"保守延迟：{summary}"
    elif conservative_macro:
        confidence = min(confidence, 65.0)
        factor_evaluation["confidence"] = min(
            float(factor_evaluation.get("confidence", 0) or 0), 65.0
        )
        factor_status["cached_macro"] = {
            "weight": 0,
            "available": True,
            "mode": "CONSERVATIVE_CACHE",
        }
        reasons.insert(
            0,
            "宏观风险指标含缓存或低频数据，仅用于保守预警；暂停新增仓位和精确减持。",
        )
        summary = f"保守预警：{summary}"
    elif holding_limited:
        confidence = min(confidence, 74.9)
        factor_evaluation["confidence"] = min(
            float(factor_evaluation.get("confidence", 0) or 0), 74.9
        )
        reasons.insert(
            0,
            "持仓核心数据可用，但增强数据不完整；仅输出风险预警，暂停新增仓位和精确减持。",
        )
        summary = f"有限判断：{summary}"
    action = _action(level, False, hard_trigger, production_approved)
    return {
        **base,
        "level": level,
        "level_label": ALERT_LEVELS[level],
        "risk_score": round(risk_score, 1),
        "confidence": round(confidence, 1),
        "action": action,
        "summary": summary,
        "reasons": reasons[:6],
        "metrics": {
            "current_nav": round(metrics["current_nav"], 6),
            "ma20": round(metrics["ma20"], 6),
            "ma60": round(metrics["ma60"], 6),
            "ma120": round(metrics["ma120"], 6),
            "as_of": metrics["as_of"],
            "source": history.get("source", "missing"),
            "max_drawdown_20": round(metrics["max_drawdown_20"], 4),
            "max_drawdown_63": round(metrics["max_drawdown_63"], 4),
            "max_drawdown_126": round(metrics["max_drawdown_126"], 4),
            "downside_volatility_63": round(metrics["downside_volatility_63"], 4),
            "cvar_95_63": round(metrics["cvar_95_63"], 4),
            "ma60_gap": round(metrics["ma60_gap"], 4),
            "ma120_gap": round(metrics["ma120_gap"], 4),
            "recovery_days_63": metrics["recovery_days_63"],
            "p20_drawdown_over_8": round(p20, 4),
            "p63_drawdown_over_12": round(p63, 4),
        },
        "components": {key: round(value, 1) for key, value in components.items()},
        "risk_score_breakdown": {
            "factor_base_score": factor_base_score_display,
            "portfolio_risk": portfolio_risk_display,
            "portfolio_weight": PORTFOLIO_RISK_WEIGHT,
            "portfolio_contribution": round(
                components["portfolio_risk"] * PORTFOLIO_RISK_WEIGHT, 1
            ),
        },
        "factor_status": factor_status,
        "factor_model": factor_evaluation,
        "event_evidence": event_evidence,
        "holding_evidence": holding_evidence,
        "portfolio_evidence": portfolio_evidence,
        "macro_evidence": macro_evidence,
        "hard_trigger": hard_trigger,
        "critical_trigger": bool(trigger["force_red"]),
        "trigger_type": trigger["type"],
        "confirmation_rule": "跌破60日均线需连续两个有效净值日确认；跌破120日均线或双来源重大事件立即确认红色。",
    }


def _holding_data_readiness(
    code: str,
    quote: object | None,
    history: dict | None,
    holding: dict | None,
    quality: dict,
    market_snapshot: dict | None,
) -> dict:
    """Separate minimum trend evidence from optional risk enrichment.

    A holding alert can safely describe NAV drawdown and trend risk with a
    live quote, a usable history, and a live benchmark.  Holdings disclosure
    and macro fields improve attribution and confidence, but their normal
    publication gaps must not erase the core alert.
    """
    holding = holding or {}
    market_snapshot = market_snapshot or {}
    reasons: list[str] = []
    limited_reasons: list[str] = []

    quote_ready = bool(
        quote
        and getattr(quote, "source", "") in LIVE_QUOTE_SOURCES
        and isinstance(getattr(quote, "nav", None), (int, float))
        and float(getattr(quote, "nav", 0) or 0) > 0
        and str(getattr(quote, "date", "")).strip()
    )
    if not quote_ready:
        reasons.append("最新基金净值没有通过实时来源和有效值校验。")

    records = (history or {}).get("records", []) if isinstance(history, dict) else []
    valid_records = []
    for item in records:
        try:
            if str(item.get("date", "")).strip() and float(item.get("nav")) > 0:
                valid_records.append(item)
        except (AttributeError, TypeError, ValueError):
            continue
    history_ready = bool(
        history
        and history.get("source") in LIVE_HISTORY_SOURCES
        and len(valid_records) >= HOLDING_CORE_HISTORY_MIN_RECORDS
    )
    if not history_ready:
        reasons.append(
            f"有效历史净值不足{HOLDING_CORE_HISTORY_MIN_RECORDS}条或来源不可用于分析。"
        )

    benchmark_specs = FUND_BENCHMARKS.get(code, ((INDEX_MAP.get(code, "IXIC"), 1.0),))
    indexes = {
        item.get("symbol"): item
        for item in market_snapshot.get("indexes", [])
        if isinstance(item, dict)
    }
    benchmark_ready = any(
        symbol in indexes and indexes[symbol].get("source") in live_source_names("indexes")
        for symbol, _weight in benchmark_specs
    )
    if not benchmark_ready:
        reasons.append("基金对应基准指数没有通过实时来源校验。")

    if holding.get("source") not in LIVE_HOLDING_SOURCES or (
        not holding.get("allocation_only") and len(holding.get("holdings", [])) < 5
    ):
        limited_reasons.append("基金前十大持仓披露不可用，重仓结构风险未纳入本次评分。")
    elif holding.get("allocation_only"):
        limited_reasons.append("基金只有资产配置披露，个股重仓风险不适用。")

    macro_items = {
        item.get("key"): item
        for item in market_snapshot.get("macro_factors", [])
        if isinstance(item, dict)
    }
    macro_core_keys = ("VIX", "US10Y", "DXY", "USDCNY")
    macro_available = any(
        isinstance(macro_items.get(key, {}).get("value"), (int, float))
        and (
            macro_items[key].get("source") in LIVE_MACRO_SOURCES
            or _is_valid_delayed_macro_item(macro_items[key], _macro_reference_date(market_snapshot))
        )
        for key in macro_core_keys
    )
    if not macro_available:
        limited_reasons.append("宏观市场风险指标不完整，仅使用基金和基准走势进行预警。")

    if quality.get("gate") == "BLOCKED":
        limited_reasons.append("数据质量闸门未完全通过，仅允许核心风险预警。")
    elif quality.get("gate") == "LIMITED" and quality.get("analysis_mode") not in {
        CONSERVATIVE_FALLBACK,
        "CONSERVATIVE_DELAYED",
    }:
        limited_reasons.append("数据质量处于受限状态，仅允许核心风险预警。")

    unique_limited = list(dict.fromkeys(str(item) for item in limited_reasons))
    return {
        "core_ready": bool(quote_ready and history_ready and benchmark_ready),
        "quote_ready": quote_ready,
        "history_ready": history_ready,
        "history_records": len(valid_records),
        "benchmark_ready": benchmark_ready,
        "holding_disclosure_ready": bool(
            holding.get("source") in LIVE_HOLDING_SOURCES
            and not holding.get("allocation_only")
            and len(holding.get("holdings", [])) >= 5
        ),
        "holding_disclosure_applicable": not bool(holding.get("allocation_only")),
        "macro_core_ready": macro_available,
        "limited_reasons": unique_limited,
        "blocking_reasons": reasons,
    }


def _data_ready(
    quote: object | None,
    history: dict | None,
    holding: dict | None,
    quality: dict,
    market_snapshot: dict | None = None,
) -> bool:
    return _data_ready_with_market(quote, history, holding, quality, market_snapshot or {})


def _data_ready_with_market(
    quote: object | None,
    history: dict | None,
    holding: dict | None,
    quality: dict,
    market_snapshot: dict,
) -> bool:
    shared_gate = build_decision_gate(quality)
    if not shared_gate["can_analyze"]:
        return False
    conservative_fallback = shared_gate["mode"] == CONSERVATIVE_FALLBACK
    conservative_delayed = shared_gate["mode"] == "CONSERVATIVE_DELAYED"
    if shared_gate["mode"] in {"CONSERVATIVE_DELAYED", CONSERVATIVE_FALLBACK}:
        quality = {"gate": READY, "reasons": []}
    if quality and quality.get("gate") not in {None, READY}:
        reasons = " ".join(str(item) for item in quality.get("reasons", []))
        allowed_limited = quality.get("gate") == "LIMITED" and reasons and all(
            term not in reasons
            for term in ("官方净值缺失", "历史净值缺失", "主基准指数缺失", "已过期")
        )
        if not allowed_limited:
            return False
    macro = {item.get("key"): item for item in market_snapshot.get("macro_factors", [])}
    required_macro = (
        "VIX", "US2Y", "US10Y", "US10Y_REAL", "DXY", "USDCNY",
        "US_GDP", "US_CPI", "US_UNEMPLOYMENT", "US_M2", "US_FED_FUNDS",
    )
    if conservative_fallback:
        # GDP and M2 are valuable for regime analysis but may be legitimately
        # delayed. A conservative risk alert can proceed without them.
        required_macro = tuple(
            key for key in required_macro if key not in {"US_GDP", "US_M2"}
        )
    hy_spread = macro.get("US_HY_SPREAD", {})
    credit_proxy = macro.get("US_CREDIT_STRESS_PROXY", {})
    credit_key = (
        "US_HY_SPREAD"
        if isinstance(hy_spread.get("value"), (int, float))
        and hy_spread.get("source") in LIVE_MACRO_SOURCES
        and not hy_spread.get("proxy")
        else "US_CREDIT_STRESS_PROXY"
        if isinstance(credit_proxy.get("value"), (int, float))
        else "US_CREDIT_STRESS_PROXY"
    )
    required_macro = (*required_macro, credit_key)

    def macro_item_ready(key: str) -> bool:
        item = macro.get(key, {})
        required_fields = item.get("required_fields") or (
            ["value", "change_12_pct"] if key == "US_M2" else ["value"]
        )
        allowed_sources = LIVE_MACRO_SOURCES | ({"cache", "fallback", "baseline"} if conservative_fallback else set())
        semantic_status = item.get("semantic_status", "COMPLETE")
        # GDP is an annual/quarterly background series. It can support a
        # delayed QDII risk score, but cannot by itself authorize a trade.
        allowed_background = conservative_delayed and key == "US_GDP" and semantic_status == "BACKGROUND_ONLY"
        delayed_source_allowed = _is_valid_delayed_macro_item(
            item, _macro_reference_date(market_snapshot)
        )
        return bool(
            all(isinstance(item.get(field), (int, float)) for field in required_fields)
            and (item.get("source") in allowed_sources or delayed_source_allowed)
            and (semantic_status not in {"INCOMPLETE", "BACKGROUND_ONLY"} or allowed_background)
        )

    macro_ready = bool(market_snapshot) and all(
        macro_item_ready(key) for key in required_macro
    )
    return bool(
        quote
        and getattr(quote, "source", "") in LIVE_QUOTE_SOURCES
        and history
        and history.get("source") in LIVE_HISTORY_SOURCES
        and len(history.get("records", [])) >= 250
        and holding
        and holding.get("source") in LIVE_HOLDING_SOURCES
        and len(holding.get("holdings", [])) >= 5
        and macro_ready
    )


def _history_metrics(records: list[dict]) -> dict:
    ordered = sorted(records, key=lambda item: item["date"])
    navs = [float(item["nav"]) for item in ordered if float(item["nav"]) > 0]
    returns = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))]
    result = {}
    for horizon in (20, 63, 126):
        window = navs[-(horizon + 1):]
        window_returns = [window[i] / window[i - 1] - 1 for i in range(1, len(window))]
        result[f"max_drawdown_{horizon}"] = _max_drawdown(window)
        downside = [value for value in window_returns if value < 0]
        result[f"downside_volatility_{horizon}"] = (
            statistics.pstdev(downside) * math.sqrt(252) if len(downside) > 1 else 0.0
        )
        count = max(1, math.ceil(len(window_returns) * 0.05))
        result[f"cvar_95_{horizon}"] = statistics.fmean(sorted(window_returns)[:count]) if window_returns else 0.0
        result[f"recovery_days_{horizon}"] = _recovery_days(window)
    current = navs[-1]
    ma20 = statistics.fmean(navs[-20:])
    ma60 = statistics.fmean(navs[-60:])
    ma120 = statistics.fmean(navs[-120:])
    result["current_nav"] = current
    result["ma20"] = ma20
    result["ma60"] = ma60
    result["ma120"] = ma120
    result["as_of"] = str(ordered[-1].get("date", "--"))
    result["ma20_gap"] = current / ma20 - 1
    result["ma60_gap"] = current / ma60 - 1
    result["ma120_gap"] = current / ma120 - 1
    result["momentum_63"] = current / navs[-64] - 1
    result["returns"] = returns
    return result


def _tail_risk(metrics: dict) -> float:
    drawdown = max(
        _ratio_risk(-metrics["max_drawdown_20"], 0.08, 0.18),
        _ratio_risk(-metrics["max_drawdown_63"], 0.12, 0.25),
        _ratio_risk(-metrics["max_drawdown_126"], 0.20, 0.35),
    )
    downside = _ratio_risk(metrics["downside_volatility_63"], 0.18, 0.38)
    cvar = _ratio_risk(-metrics["cvar_95_63"], 0.015, 0.05)
    recovery = _ratio_risk(metrics["recovery_days_63"], 20, 126)
    return _clamp(drawdown * 0.45 + downside * 0.25 + cvar * 0.20 + recovery * 0.10, 0, 100)


def _trend_risk(metrics: dict) -> float:
    gaps = (
        _ratio_risk(-metrics["ma20_gap"], 0.02, 0.08) * 0.20
        + _ratio_risk(-metrics["ma60_gap"], 0.02, 0.08) * 0.40
        + _ratio_risk(-metrics["ma120_gap"], 0.02, 0.10) * 0.25
        + _ratio_risk(-metrics["momentum_63"], 0.02, 0.20) * 0.15
    )
    return _clamp(gaps, 0, 100)


def _holding_risk(
    code: str,
    holding: dict,
    news: list[object],
    evidence: dict,
    stock_trends: dict[str, dict],
) -> tuple[float, dict]:
    items = holding.get("holdings", [])
    concentration = sum(float(item.get("weight", 0)) for item in items[:10])
    score = _ratio_risk(concentration, 0.25, 0.65)
    negative_weight = 0.0
    for item in items[:10]:
        item_text = f"{item.get('name', '')} {item.get('code', '')}".lower()
        for news_item in news:
            text = f"{getattr(news_item, 'title', '')} {getattr(news_item, 'summary', '')}".lower()
            if item_text and (str(item.get("name", "")).lower() in text or str(item.get("code", "")).lower() in text):
                if getattr(news_item, "direction", "") == "偏利空":
                    negative_weight += float(item.get("weight", 0))
    broad_negative = sum(
        1
        for item in news
        if getattr(item, "direction", "") == "偏利空"
        and code in str(getattr(item, "affected_funds", ""))
    )
    covered_weight = 0.0
    below_ma60 = 0.0
    below_ma120 = 0.0
    weighted_drawdown = 0.0
    for item in items[:10]:
        weight = float(item.get("weight", 0))
        trend = stock_trends.get(str(item.get("code", "")))
        if not trend:
            continue
        covered_weight += weight
        below_ma60 += weight if float(trend.get("ma60_gap", 0)) < 0 else 0
        below_ma120 += weight if float(trend.get("ma120_gap", 0)) < 0 else 0
        weighted_drawdown += weight * abs(min(float(trend.get("max_drawdown_63", 0)), 0))
    total_weight = max(concentration, 1e-9)
    breadth60 = below_ma60 / covered_weight if covered_weight else 0.5
    breadth120 = below_ma120 / covered_weight if covered_weight else 0.5
    average_drawdown = weighted_drawdown / covered_weight if covered_weight else 0.15
    stock_trend_risk = _clamp(
        _ratio_risk(breadth60, 0.30, 0.75) * 0.45
        + _ratio_risk(breadth120, 0.25, 0.65) * 0.30
        + _ratio_risk(average_drawdown, 0.10, 0.35) * 0.25,
        0,
        100,
    )
    event_risk = _clamp(
        _ratio_risk(negative_weight, 0.05, 0.25) * 0.60
        + min(broad_negative, 2) * 10
        + (30 if evidence.get("confirmed") else 0),
        0,
        100,
    )
    final = _clamp(score * 0.35 + stock_trend_risk * 0.45 + event_risk * 0.20, 0, 100)
    return final, {
        "top10_weight": round(concentration, 4),
        "stock_trend_covered_weight": round(covered_weight, 4),
        "stock_trend_coverage": round(covered_weight / total_weight, 4),
        "below_ma60_weight": round(below_ma60, 4),
        "below_ma120_weight": round(below_ma120, 4),
        "covered_below_ma60_ratio": round(breadth60, 4),
        "covered_below_ma120_ratio": round(breadth120, 4),
        "covered_average_drawdown_63": round(average_drawdown, 4),
    }


def _macro_risk(code: str, market_snapshot: dict) -> float:
    snapshots = _benchmark_snapshots(code, market_snapshot)
    primary_symbol = FUND_BENCHMARKS.get(
        code, ((INDEX_MAP.get(code, "IXIC"), 1.0),)
    )[0][0]
    if not any(item.get("symbol") == primary_symbol for item in snapshots):
        return 80.0
    ma60 = _weighted_benchmark_gap(code, market_snapshot, "ma60_gap_pct") / 100
    ma20 = _weighted_benchmark_gap(code, market_snapshot, "ma20_gap_pct") / 100
    equity_risk = _clamp(
        _ratio_risk(-ma60, 0.02, 0.08) * 0.70
        + _ratio_risk(-ma20, 0.02, 0.08) * 0.30,
        0,
        100,
    )
    factors = {item.get("key"): item for item in market_snapshot.get("macro_factors", [])}
    vix = factors.get("VIX", {})
    us2y = factors.get("US2Y", {})
    us10y = factors.get("US10Y", {})
    real10y = factors.get("US10Y_REAL", {})
    dxy = factors.get("DXY", {})
    usdcny = factors.get("USDCNY", {})
    required_values = (vix, us2y, us10y, real10y, dxy, usdcny)
    if not all(isinstance(item.get("value"), (int, float)) for item in required_values) or not all(
        isinstance(value, (int, float))
        for value in (us10y.get("change_20"), dxy.get("change_20_pct"), usdcny.get("change_20_pct"))
    ):
        return 100.0
    vix_risk = _ratio_risk(float(vix["value"]), 18, 35)
    rates_risk = _clamp(
        _ratio_risk(float(us2y["value"]), 3.0, 5.0) * 0.30
        + _ratio_risk(float(us10y["value"]), 3.5, 5.0) * 0.25
        + _ratio_risk(float(real10y["value"]), 1.0, 2.5) * 0.25
        + _ratio_risk(float(us10y["change_20"]), 0.10, 0.45) * 0.20,
        0,
        100,
    )
    dollar_risk = _clamp(
        _ratio_risk(float(dxy["change_20_pct"]), 0.5, 3.0) * 0.65
        + _ratio_risk(-float(usdcny["change_20_pct"]), 0.5, 2.5) * 0.35,
        0,
        100,
    )
    return _clamp(
        equity_risk * 0.45 + vix_risk * 0.20 + rates_risk * 0.25 + dollar_risk * 0.10,
        0,
        100,
    )


def _macro_evidence(market_snapshot: dict) -> dict:
    factors = {item.get("key"): item for item in market_snapshot.get("macro_factors", [])}
    return {
        key: {
            "value": factors.get(key, {}).get("value"),
            "change_20": factors.get(key, {}).get("change_20"),
            "change_20_pct": factors.get(key, {}).get("change_20_pct"),
            "date": factors.get(key, {}).get("date", "--"),
            "source": factors.get(key, {}).get("source", "missing"),
        }
        for key in (
            "VIX", "US2Y", "US10Y", "US10Y_REAL", "DXY", "USDCNY",
            "US_GDP", "US_CPI", "US_UNEMPLOYMENT", "US_M2", "US_FED_FUNDS",
            "US_HY_SPREAD", "US_CREDIT_STRESS_PROXY",
        )
    }


def _portfolio_risk(
    code: str,
    holding: dict,
    portfolio: dict,
    holdings_map: dict[str, dict],
    histories: dict[str, dict],
) -> tuple[float, dict]:
    correlations = []
    for other_code in portfolio:
        if other_code == code:
            continue
        correlation = _history_correlation(histories.get(code, {}), histories.get(other_code, {}))
        if correlation is not None:
            correlations.append(correlation)
    mean_correlation = statistics.fmean(correlations) if correlations else 0.5
    correlation_risk = _ratio_risk(mean_correlation, 0.50, 0.90)
    current_weights = {
        str(item.get("code") or item.get("name")): float(item.get("weight", 0))
        for item in (holding or {}).get("holdings", [])[:10]
    }
    overlap = 0.0
    for other_code, other in holdings_map.items():
        if other_code == code:
            continue
        other_weights = {
            str(item.get("code") or item.get("name")): float(item.get("weight", 0))
            for item in other.get("holdings", [])[:10]
        }
        overlap = max(
            overlap,
            sum(min(weight, other_weights.get(stock, 0)) for stock, weight in current_weights.items()),
        )
    exposure_values = {
        code: float(item.get("market_value", 0) or item.get("invested_amount", 0) or 0)
        for code, item in portfolio.items()
    }
    total_invested = sum(exposure_values.values()) or 1.0
    semiconductor_exposure = 0.0
    for fund_code, config in portfolio.items():
        fund_weight = exposure_values.get(fund_code, 0.0) / total_invested
        semiconductor_exposure += fund_weight * sum(
            float(item.get("weight", 0))
            for item in holdings_map.get(fund_code, {}).get("holdings", [])[:10]
            if _is_semiconductor_holding(item)
        )
    final = _clamp(
        correlation_risk * 0.40
        + _ratio_risk(overlap, 0.05, 0.25) * 0.25
        + _ratio_risk(semiconductor_exposure, 0.20, 0.45) * 0.35,
        0,
        100,
    )
    return final, {
        "mean_nav_correlation": round(mean_correlation, 4),
        "max_top10_overlap": round(overlap, 4),
        "portfolio_semiconductor_exposure": round(semiconductor_exposure, 4),
        "fund_semiconductor_exposure": round(
            sum(
                float(item.get("weight", 0))
                for item in (holding or {}).get("holdings", [])[:10]
                if _is_semiconductor_holding(item)
            ),
            4,
        ),
    }


def _operation_risk(holding: dict) -> float:
    try:
        age = (date.today() - date.fromisoformat(holding.get("disclosure_date", ""))).days
    except (TypeError, ValueError):
        return 70.0
    return _ratio_risk(age, 90, 150)


def _is_semiconductor_holding(item: dict) -> bool:
    text = f"{item.get('code', '')} {item.get('name', '')}".lower()
    terms = (
        "半导体",
        "芯片",
        "存储",
        "美光",
        "超威",
        "英特尔",
        "台积电",
        "三星电子",
        "sk海力士",
        "应用材料",
        "拉姆研究",
        "阿斯麦",
        "kioxia",
        "闪迪",
        "micron",
        "amd",
        "intel",
        "tsm",
        "asml",
        "amat",
        "lrcx",
        "mu ",
        "sndk",
        "onto",
        "stm",
        "alab",
    )
    return any(term in text for term in terms)


def _probability_estimate(metrics: dict, calibration: dict, horizon: str) -> float:
    model = calibration.get("horizons", {}).get(horizon, {})
    coefficients = model.get("coefficients", {})
    if coefficients:
        features = _calibration_features(metrics)
        logit = float(coefficients.get("intercept", 0)) + sum(
            float(coefficients.get(name, 0)) * value
            for name, value in features.items()
        )
        return _sigmoid(logit)
    base = (_tail_risk(metrics) * 0.60 + _trend_risk(metrics) * 0.40 - 50) / 18
    return _sigmoid(base)


def _trigger_assessment(
    code: str,
    metrics: dict,
    components: dict,
    event_evidence: dict,
    holding_evidence: dict,
    market_snapshot: dict,
) -> dict:
    if event_evidence.get("confirmed"):
        return {
            "force_red": True,
            "immediate": True,
            "type": "CONFIRMED_EVENT",
            "reason": (
                "重大事件立即触发：严重事件已由至少两个独立媒体确认，且明确影响基金或"
                f"前十大持仓合计权重 {event_evidence.get('matched_weight', 0):.1%}。"
            ),
        }

    if metrics["ma120_gap"] <= -MA120_IMMEDIATE_BUFFER:
        return {
            "force_red": True,
            "immediate": True,
            "type": "MA120_BREAK",
            "reason": (
                "红色技术硬触发：基金官方净值跌破120日均线，"
                f"当前偏离 {metrics['ma120_gap']:.1%}。"
            ),
        }
    if metrics["ma120_gap"] <= -MA120_ALERT_BUFFER:
        return {
            "force_red": True,
            "immediate": False,
            "type": "MA120_BREAK",
            "reason": (
                "红色技术条件待连续确认：基金官方净值跌破120日均线缓冲线，"
                f"当前偏离 {metrics['ma120_gap']:.1%}（触发阈值 -{MA120_ALERT_BUFFER:.0%}）。"
            ),
        }
    if metrics["ma60_gap"] <= -MA60_ALERT_BUFFER:
        return {
            "force_red": True,
            "immediate": False,
            "type": "MA60_BREAK",
            "reason": (
                "红色技术条件待连续确认：基金官方净值跌破60日均线，"
                f"当前偏离 {metrics['ma60_gap']:.1%}（触发阈值 -{MA60_ALERT_BUFFER:.0%}）。"
            ),
        }

    benchmark_snapshots = _benchmark_snapshots(code, market_snapshot)
    if not benchmark_snapshots:
        return {"force_red": False, "immediate": False, "type": "NONE", "reason": ""}
    benchmark_ma60 = (
        _weighted_benchmark_gap(code, market_snapshot, "ma60_gap_pct") / 100
    )
    relative_ma60 = metrics["ma60_gap"] - benchmark_ma60
    stock_coverage = float(holding_evidence.get("stock_trend_coverage", 0))
    weak_stock_ratio = float(holding_evidence.get("covered_below_ma60_ratio", 0))
    technical_confirmed = (
        metrics["max_drawdown_63"] <= -0.18
        and metrics["ma60_gap"] <= -0.05
        and metrics["ma120_gap"] <= -MA120_ALERT_BUFFER
        and relative_ma60 <= -0.06
        and stock_coverage >= 0.60
        and weak_stock_ratio >= 0.60
    )
    if technical_confirmed:
        return {
            "force_red": True,
            "immediate": False,
            "type": "TECHNICAL_CONFIRMATION",
            "reason": (
                "红色技术条件待连续确认：63日回撤"
                f" {metrics['max_drawdown_63']:.1%}，相对基准60日趋势落后"
                f" {abs(relative_ma60):.1%}，重仓股弱势比例 {weak_stock_ratio:.0%}。"
            ),
        }
    if (
        components["holding_risk"] >= 90
        and components["trend_risk"] >= 75
        and stock_coverage >= 0.60
        and weak_stock_ratio >= 0.60
    ):
        return {
            "force_red": True,
            "immediate": False,
            "type": "HOLDING_TREND_CONFIRMATION",
            "reason": "红色技术条件待连续确认：持仓风险与基金趋势同步进入高风险区。",
        }
    return {"force_red": False, "immediate": False, "type": "NONE", "reason": ""}


def _reasons_for_components(
    code: str,
    metrics: dict,
    components: dict,
    p20: float,
    p63: float,
    production_approved: bool,
) -> list[str]:
    reasons = []
    if components["fundamental_risk"] >= 45:
        reasons.append(f"企业基本面风险评分 {components['fundamental_risk']:.1f}，需复核重仓企业财报与盈利质量。")
    if components["macro_risk"] >= 55:
        reasons.append(f"宏观经济风险评分 {components['macro_risk']:.1f}，利率、流动性或汇率环境偏不利。")
    if components["technical_risk"] >= 45:
        reasons.append(f"技术与资金面风险评分 {components['technical_risk']:.1f}，相对60日/120日均线 {metrics['ma60_gap']:+.1%}/{metrics['ma120_gap']:+.1%}。")
    if components["industry_risk"] >= 55:
        reasons.append(f"行业景气风险评分 {components['industry_risk']:.1f}，订单、资本开支或盈利预期偏弱。")
    if components["other_risk"] >= 35:
        reasons.append(f"负面新闻与突发事件风险评分 {components['other_risk']:.1f}。")
    if components["portfolio_risk"] >= 55:
        reasons.append(f"组合集中风险评分 {components['portfolio_risk']:.1f}，与其他持仓的联动风险偏高。")
    if production_approved:
        reasons.append(f"未来20/63日较大回撤校准概率 {p20:.1%}/{p63:.1%}。")
    else:
        reasons.append(
            f"20/63日模型原始概率 {p20:.1%}/{p63:.1%}，但样本外验收未通过，"
            "不参与风险分和减持比例。"
        )
    return reasons or [f"{code} 各项风险指标暂未出现明显异常。"]


def _news_event_evidence(code: str, fund_name: str, holding: dict, news: list[object]) -> dict:
    severe_terms = (
        "下调指引",
        "业绩预警",
        "财务造假",
        "暂停赎回",
        "基金经理离任",
        "制裁",
        "芯片禁令",
        "出口管制",
        "断供",
    )
    items = holding.get("holdings", [])[:10]
    source_terms: dict[str, set[str]] = {}
    matched_codes: set[str] = set()
    exact_fund_sources: set[str] = set()
    evidence_titles = []
    evidence_items = []
    for news_item in news:
        if getattr(news_item, "direction", "") != "偏利空":
            continue
        text = f"{getattr(news_item, 'title', '')} {getattr(news_item, 'summary', '')}".lower()
        terms = {term for term in severe_terms if term.lower() in text}
        if not terms:
            continue
        source = _independent_source(getattr(news_item, "source", ""))
        if not source:
            continue
        if code in text or fund_name.lower() in text:
            exact_fund_sources.add(source)
            source_terms.setdefault(source, set()).update(terms)
            evidence_titles.append(str(getattr(news_item, "title", "")))
            evidence_items.append(
                {
                    "title": str(getattr(news_item, "title", "")),
                    "source": source,
                    "date": str(getattr(news_item, "published_at", "") or "--"),
                    "terms": sorted(terms),
                    "matched": "fund",
                }
            )
        for item in items:
            item_code = str(item.get("code", "")).lower()
            item_name = str(item.get("name", "")).lower()
            if (item_code and item_code in text) or (item_name and item_name in text):
                matched_codes.add(str(item.get("code") or item.get("name")))
                source_terms.setdefault(source, set()).update(terms)
                evidence_titles.append(str(getattr(news_item, "title", "")))
                evidence_items.append(
                    {
                        "title": str(getattr(news_item, "title", "")),
                        "source": source,
                        "date": str(getattr(news_item, "published_at", "") or "--"),
                        "terms": sorted(terms),
                        "matched": str(item.get("code") or item.get("name") or "holding"),
                    }
                )

    matched_weight = sum(
        float(item.get("weight", 0))
        for item in items
        if str(item.get("code") or item.get("name")) in matched_codes
    )
    independent_sources = sorted(source_terms)
    common_terms = set.intersection(*source_terms.values()) if len(source_terms) >= 2 else set()
    exact_fund_confirmed = len(exact_fund_sources) >= 2 and bool(common_terms)
    holding_confirmed = (
        len(independent_sources) >= 2
        and matched_weight >= 0.20
        and bool(common_terms)
    )
    return {
        "confirmed": exact_fund_confirmed or holding_confirmed,
        "independent_sources": independent_sources,
        "source_count": len(independent_sources),
        "matched_weight": round(matched_weight, 4),
        "common_severe_terms": sorted(common_terms),
        "evidence_titles": list(dict.fromkeys(evidence_titles))[:4],
        "evidence_items": list(
            {
                (item["title"], item["source"], item["date"], item["matched"]): item
                for item in evidence_items
            }.values()
        )[:6],
        "rule": "双源确认+同一严重事件+基金直指或前十大持仓影响权重>=20%",
    }


def _independent_source(source: str) -> str:
    text = str(source).strip()
    if not text or text in {"离线观察项", "缓存"}:
        return ""
    return text.split("·", 1)[-1].strip()


def _factor_confidence(
    code: str,
    quote: object,
    history: dict,
    holding: dict,
    market_snapshot: dict,
    news: list[object],
    portfolio: dict,
    histories: dict[str, dict],
    stock_trends: dict[str, dict],
) -> tuple[float, dict]:
    benchmark_specs = FUND_BENCHMARKS.get(
        code, ((INDEX_MAP.get(code, "IXIC"), 1.0),)
    )
    indexes = {
        item.get("symbol"): item for item in market_snapshot.get("indexes", [])
    }
    live_index_sources = {"eastmoney-index", "tencent-index"}
    primary_symbol = benchmark_specs[0][0]
    primary_index = indexes.get(primary_symbol, {})
    total_benchmark_weight = sum(float(weight) for _, weight in benchmark_specs) or 1.0
    live_benchmark_weight = sum(
        float(weight)
        for symbol, weight in benchmark_specs
        if indexes.get(symbol, {}).get("source") in live_index_sources
    )
    benchmark_coverage = live_benchmark_weight / total_benchmark_weight
    benchmark_ready = (
        primary_index.get("source") in live_index_sources
        and benchmark_coverage >= 0.75
    )
    news_sources = {
        _independent_source(getattr(item, "source", ""))
        for item in news
        if _independent_source(getattr(item, "source", ""))
    }
    correlation_ready = any(
        other != code and _history_correlation(history, histories.get(other, {})) is not None
        for other in portfolio
    )
    macro = {
        item.get("key"): item
        for item in market_snapshot.get("macro_factors", [])
        if item.get("source") in LIVE_MACRO_SOURCES
    }
    credit_key = (
        "US_HY_SPREAD"
        if isinstance(macro.get("US_HY_SPREAD", {}).get("value"), (int, float))
        and not macro.get("US_HY_SPREAD", {}).get("proxy")
        else "US_CREDIT_STRESS_PROXY"
    )

    def macro_semantically_ready(key: str) -> bool:
        item = macro.get(key, {})
        required_fields = item.get("required_fields") or (
            ["value", "change_12_pct"] if key == "US_M2" else ["value"]
        )
        return bool(
            all(isinstance(item.get(field), (int, float)) for field in required_fields)
            and item.get("semantic_status", "COMPLETE")
            not in {"INCOMPLETE", "BACKGROUND_ONLY"}
        )

    market_macro_ready = all(
        macro_semantically_ready(key)
        for key in ("VIX", "US2Y", "US10Y", "US10Y_REAL", "DXY")
    )
    economic_keys = (
        "US_GDP", "US_CPI", "US_UNEMPLOYMENT", "US_M2", "US_FED_FUNDS",
    )
    economic_macro_ready = sum(
        macro_semantically_ready(key)
        and macro.get(key, {}).get("semantic_status", "COMPLETE") == "COMPLETE"
        for key in economic_keys
    )
    credit_macro_ready = (
        credit_key in macro
        and macro.get(credit_key, {}).get("semantic_status", "COMPLETE")
        in {"COMPLETE", "PROXY"}
    )
    # OAS and HYG/LQD are alternatives, not two independent confirmations.
    economic_macro_ready = economic_macro_ready + int(credit_macro_ready) >= 4
    extended_macro = market_macro_ready and economic_macro_ready
    earnings = bool(holding.get("earnings_forecast_source"))
    top_items = holding.get("holdings", [])[:10]
    top_weight = sum(float(item.get("weight", 0)) for item in top_items) or 1.0
    stock_covered = sum(
        float(item.get("weight", 0))
        for item in top_items
        if stock_trends.get(str(item.get("code", "")), {}).get("source")
        in LIVE_STOCK_TREND_SOURCES
    )
    stock_trend_ready = stock_covered / top_weight >= 0.60
    factors = {
        "official_nav": {
            "weight": 10,
            "available": getattr(quote, "source", "") in LIVE_QUOTE_SOURCES,
        },
        "history_nav": {
            "weight": 15,
            "available": history.get("source") in LIVE_HISTORY_SOURCES and len(history.get("records", [])) >= 250,
        },
        "holding_disclosure": {
            "weight": 15,
            "available": holding.get("source") in LIVE_HOLDING_SOURCES and len(holding.get("holdings", [])) >= 5,
        },
        "benchmark_basket": {
            "weight": 10,
            "available": benchmark_ready,
            "coverage": round(benchmark_coverage, 4),
            "primary_symbol": primary_symbol,
        },
        "cross_source_news": {"weight": 10, "available": len(news_sources) >= 2},
        "portfolio_correlation": {"weight": 10, "available": correlation_ready},
        "extended_macro": {"weight": 15, "available": extended_macro},
        "holding_stock_trends": {"weight": 10, "available": stock_trend_ready},
        "earnings_forecast": {"weight": 5, "available": earnings},
    }
    score = sum(item["weight"] for item in factors.values() if item["available"])
    return float(score), factors


def _level_for_score(score: float) -> str:
    if score >= 70:
        return "RED"
    if score >= 55:
        return "ORANGE"
    if score >= 35:
        return "YELLOW"
    return "GREEN"


def _action(
    level: str,
    confirmed: bool,
    hard_trigger: bool,
    production_approved: bool = False,
) -> str:
    if level == "GRAY":
        return "数据不足，暂停判断"
    if level == "GREEN":
        return "继续持有，正常复核"
    if level == "YELLOW":
        return "继续持有，但暂停加仓"
    if not production_approved:
        return "风险观察，暂停加仓；模型未通过生产验收，不给出减持比例"
    if level == "ORANGE":
        return "风险预警待完整证据链确认，暂停加仓"
    return "高风险预警已触发，暂停加仓并人工复核；仅完整证据链可给出减持比例"


def _is_reduction_source_live(source: object, allowed_sources: set[str]) -> bool:
    normalized = str(source or "").strip()
    return normalized in allowed_sources and is_trading_eligible_source(normalized)


def _reduction_technical_evidence(alert: dict) -> list[dict]:
    metrics = alert.get("metrics") or {}
    trigger_type = str(alert.get("trigger_type") or "")
    metric_key = {
        "MA60_BREAK": ("ma60", "ma60_gap", "60日均线", "MA60"),
        "MA120_BREAK": ("ma120", "ma120_gap", "120日均线", "MA120"),
        "TECHNICAL_CONFIRMATION": ("ma60", "ma60_gap", "60日均线", "MA60"),
    }.get(trigger_type)
    if not metric_key:
        return []
    average_key, gap_key, label, threshold_label = metric_key
    current = metrics.get("current_nav")
    average = metrics.get(average_key)
    gap = metrics.get(gap_key)
    as_of = str(metrics.get("as_of") or alert.get("as_of") or "--")
    source = metrics.get("source")
    if not (
        all(isinstance(value, (int, float)) and math.isfinite(float(value))
            for value in (current, average, gap))
        and as_of != "--"
        and _is_reduction_source_live(source, LIVE_HISTORY_SOURCES)
    ):
        return []
    return [{
        "category": "技术确认",
        "fact": f"基金净值跌破{label}",
        "value": f"当前净值 {float(current):.4f}，{threshold_label} {float(average):.4f}，偏离 {float(gap):+.2%}",
        "threshold": f"当前净值 < {threshold_label}（偏离 < 0）",
        "as_of": as_of,
        "source": str(source),
    }]


def _reduction_fundamental_evidence(alert: dict) -> list[dict]:
    companies = (
        (alert.get("factor_model") or {})
        .get("details", {})
        .get("fundamental", {})
        .get("companies", [])
    )
    evidence = []
    for company in companies:
        source = company.get("source")
        period = str(company.get("fiscal_end") or "--")
        metrics = company.get("metrics") or {}
        if period == "--" or not _is_reduction_source_live(
            source,
            {
                "sec-companyfacts", "eastmoney-financials", "naver-financials",
                "ifind-financials",
            },
        ):
            continue
        company_name = str(company.get("name") or company.get("code") or "重仓企业")
        weight = float(company.get("weight", 0) or 0)
        checks = (
            ("revenue_yoy", "营业收入同比", 0.0, "< 0"),
            ("net_income_yoy", "净利润同比", 0.0, "< 0"),
            ("roe", "ROE", 0.05, "< 5%"),
        )
        for metric, label, threshold, threshold_text in checks:
            value = metrics.get(metric)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                continue
            if float(value) >= threshold:
                continue
            evidence.append({
                "category": "企业基本面",
                "fact": f"{company_name} {label}低于风险阈值（基金披露权重 {weight:.1%}）",
                "value": f"{label} {float(value):.2%}",
                "threshold": f"{label} {threshold_text}",
                "as_of": period,
                "source": str(source),
            })
            break
        if len(evidence) >= 3:
            break
    return evidence


def _reduction_macro_evidence(alert: dict) -> list[dict]:
    macro = alert.get("macro_evidence") or {}
    checks = (
        ("VIX", "VIX", "value", 30.0, ">= 30"),
        ("US10Y_REAL", "美国10年期实际利率", "value", 2.5, ">= 2.50%"),
        ("US_HY_SPREAD", "美国高收益信用利差", "value", 5.0, ">= 5.00%"),
        ("DXY", "美元指数20日变动", "change_20_pct", 3.0, ">= 3.00%"),
    )
    evidence = []
    for key, label, field, threshold, threshold_text in checks:
        item = macro.get(key) or {}
        value = item.get(field)
        source = item.get("source")
        as_of = str(item.get("date") or "--")
        if not (
            isinstance(value, (int, float))
            and math.isfinite(float(value))
            and float(value) >= threshold
            and as_of != "--"
            and _is_reduction_source_live(source, LIVE_MACRO_SOURCES)
        ):
            continue
        suffix = "%" if key != "VIX" else ""
        evidence.append({
            "category": "宏观环境",
            "fact": f"{label}达到风险阈值",
            "value": f"{label} {float(value):.2f}{suffix}",
            "threshold": f"{label} {threshold_text}",
            "as_of": as_of,
            "source": str(source),
        })
    return evidence


def _reduction_event_evidence(alert: dict) -> list[dict]:
    event = alert.get("event_evidence") or {}
    if not event.get("confirmed"):
        return []
    items = [
        item for item in event.get("evidence_items", [])
        if str(item.get("source") or "") not in {"cache", "baseline", "fallback"}
        and str(item.get("date") or "--") != "--"
    ]
    sources = sorted({str(item.get("source")) for item in items if item.get("source")})
    terms = event.get("common_severe_terms") or []
    if len(sources) < 2 or not terms:
        return []
    dated_titles = [
        f"{item.get('source')}（{item.get('date')}）：{item.get('title')}"
        for item in items[:4]
    ]
    return [{
        "category": "重大事件",
        "fact": f"严重事件“{'、'.join(str(term) for term in terms)}”已获 {len(sources)} 个独立来源确认",
        "value": f"影响前十大持仓权重 {float(event.get('matched_weight', 0) or 0):.1%}；" + "；".join(dated_titles),
        "threshold": "双独立来源 + 同一严重事件 + 基金直指或前十大持仓影响权重 >= 20%",
        "as_of": ", ".join(sorted({str(item.get("date")) for item in items})),
        "source": "、".join(sources),
    }]


def _build_reduction_strategy(alert: dict) -> dict:
    """Return a trade-safe reduction plan with a complete auditable evidence chain."""
    gate = alert.get("decision_gate") or build_decision_gate(
        {"gate": alert.get("data_gate") or READY}
    )
    strategy = {
        "eligible": False,
        "percentage": None,
        "percentage_label": "暂不输出减持比例",
        "action": "继续观察/暂停加仓",
        "basis": "减持仅在模型验收、数据闸门和完整证据链同时满足时输出。",
        "reasons": [],
        "evidence": [],
        "blocked_reasons": [],
        "review_condition": "等待下一有效净值日及关键数据更新后复核。",
    }
    if not alert.get("production_approved"):
        strategy["blocked_reasons"].append("模型尚未通过样本外生产验收。")
    standard_gate = (
        gate.get("gate") == READY
        and gate.get("mode") in {"STANDARD", READY}
        and gate.get("can_exact_reduce")
    )
    if not standard_gate:
        strategy["blocked_reasons"].append(
            f"当前数据闸门为 {gate.get('mode', 'UNKNOWN')}，仅允许保守观察。"
        )
    if alert.get("conservative_fallback"):
        strategy["blocked_reasons"].append("当前分析含缓存或静态参考数据，不能据此执行减持。")

    trigger_type = str(alert.get("trigger_type") or "NONE")
    primary_evidence: list[dict] = []
    if trigger_type == "CONFIRMED_EVENT":
        primary_evidence = _reduction_event_evidence(alert)
        percentage = 0.40
        percentage_basis = "双独立来源确认的重大事件标准档位"
        requires_confirmation = False
    elif trigger_type == "MA120_BREAK":
        primary_evidence = _reduction_technical_evidence(alert)
        percentage = 0.25
        percentage_basis = "跌破120日均线的技术硬触发标准档位"
        requires_confirmation = False
    elif trigger_type in {"MA60_BREAK", "TECHNICAL_CONFIRMATION"}:
        primary_evidence = _reduction_technical_evidence(alert)
        percentage = 0.15
        percentage_basis = "连续两个有效净值日确认的技术触发标准档位"
        requires_confirmation = True
    else:
        percentage = None
        percentage_basis = "当前没有经模型定义的减持触发类型。"
        requires_confirmation = False

    if requires_confirmation and not alert.get("confirmed"):
        strategy["blocked_reasons"].append("技术触发尚未由连续两个有效净值日确认。")
    if not primary_evidence:
        strategy["blocked_reasons"].append("缺少包含数值、日期、阈值和来源的可核验减持证据。")
    if percentage is None:
        strategy["blocked_reasons"].append("当前风险仅构成观察或暂停加仓条件，未达到减持规则。")

    supporting_evidence = _reduction_fundamental_evidence(alert)
    supporting_evidence += _reduction_macro_evidence(alert)
    strategy["evidence"] = primary_evidence + supporting_evidence
    strategy["reasons"] = [item["fact"] for item in strategy["evidence"]]
    if strategy["blocked_reasons"]:
        strategy["basis"] = "；".join(strategy["blocked_reasons"])
        return strategy

    strategy.update(
        eligible=True,
        percentage=percentage,
        percentage_label=f"建议减持 {percentage:.0%}",
        action=f"建议减持 {percentage:.0%}，暂停加仓并人工复核",
        basis=f"减持比例依据：{percentage_basis}；已满足模型验收、标准数据闸门与证据链要求。",
        review_condition="减持后等待下一有效净值日复核；若触发解除规则，连续三个有效数据日后再评估。",
    )
    return strategy


def _apply_operation_decision(alert: dict, target_signal: dict, config: dict) -> None:
    """Create the single user-facing action used by every holding view."""
    level = str(alert.get("level", "GRAY"))
    target_status = str(target_signal.get("status", "数据待更新"))
    target_level = int(target_signal.get("status_level", 0) or 0)
    buy_candidate = target_level >= 3
    production_approved = bool(alert.get("production_approved"))
    portfolio_evidence = alert.get("portfolio_evidence", {})
    portfolio_semiconductor = float(
        portfolio_evidence.get("portfolio_semiconductor_exposure", 0) or 0
    )
    fund_semiconductor = float(
        portfolio_evidence.get("fund_semiconductor_exposure", 0) or 0
    )
    concentration_block = (
        portfolio_semiconductor >= 0.30 and fund_semiconductor >= 0.20
    )
    decision_gate = alert.get("decision_gate") or build_decision_gate(
        {"gate": alert.get("data_gate") or READY}
    )
    reduction_strategy = _build_reduction_strategy(alert)

    decision = {
        "buy_signal_status": target_status,
        "buy_signal_level": target_level,
        "manual_review_required": False,
        "holding_plan": _holding_plan(config, alert.get("as_of")),
        "decision_gate": decision_gate,
        "reduction_strategy": reduction_strategy,
    }
    if level == "GRAY":
        if alert.get("conservative_fallback"):
            decision.update(
                operation_code="FALLBACK_OBSERVE",
                action="数据回退观察，暂停新增仓位；不输出精确减持比例。",
                buy_gate="FALLBACK",
                buy_gate_label="数据回退：仅供保守观察",
                decision_reason="实时源已按优先顺序尝试，当前使用带日期的缓存或静态参考数据。",
                manual_review_required=True,
            )
            alert.update(decision)
            return
        decision.update(
            operation_code="NO_DECISION",
            action="数据不足，暂停判断",
            buy_gate="NO_DATA",
            buy_gate_label="数据闸门：禁止操作",
            decision_reason="关键数据未通过来源、时效或完整性校验。",
        )
    elif level == "RED":
        pending_technical = (
            alert.get("critical_trigger")
            and not alert.get("hard_trigger")
            and not alert.get("confirmed")
        )
        if reduction_strategy.get("eligible"):
            action = reduction_strategy["action"]
            reason = reduction_strategy["basis"]
        elif pending_technical:
            action = "红色技术风险待连续两个净值日确认，暂停加仓；暂不自动减持"
            reason = "技术条件已触发，但需第二个不同净值日确认，避免单日波动造成误判。"
        elif alert.get("hard_trigger"):
            if alert.get("trigger_type") == "MA120_BREAK":
                action = "跌破120日均线，红色预警已确认；暂停加仓并人工复核"
                reason = "120日均线硬触发无需等待第二个净值日。"
            else:
                action = "重大事件已确认，暂停加仓并人工复核"
                reason = "双来源重大事件满足立即升级条件。"
        elif not production_approved:
            action = "红色风险已确认，暂停加仓并人工复核；模型未验收，不给出减持比例"
            reason = "风险等级已确认，但样本外验收未通过，不能自动计算减持比例。"
        else:
            action = reduction_strategy["action"]
            reason = reduction_strategy["basis"]
        decision.update(
            operation_code="RISK_REVIEW",
            action=action,
            buy_gate="BLOCKED",
            buy_gate_label="风险闸门：禁止加仓",
            decision_reason=reason,
            manual_review_required=True,
        )
    elif level == "ORANGE":
        action = reduction_strategy["action"] if reduction_strategy.get("eligible") else (
            "橙色风险已确认，暂停加仓并人工复核"
            if alert.get("confirmed")
            else "橙色风险待连续两个净值日确认，暂停加仓"
        )
        decision.update(
            operation_code="REVIEW",
            action=action,
            buy_gate="BLOCKED",
            buy_gate_label="风险闸门：禁止加仓",
            decision_reason=(
                reduction_strategy["basis"]
                if reduction_strategy.get("eligible")
                else "风险指标明显上升，买入节点暂时失效；"
                + reduction_strategy["basis"]
            ),
            manual_review_required=bool(alert.get("confirmed")),
        )
    elif level == "YELLOW":
        decision.update(
            operation_code="PAUSE_ADD",
            action="继续持有，暂停加仓，等待趋势修复",
            buy_gate="BLOCKED",
            buy_gate_label="风险闸门：暂停加仓",
            decision_reason="风险开始上升，原始买入信号由持仓风险闸门否决。",
        )
    elif concentration_block:
        decision.update(
            operation_code="PAUSE_ADD",
            action="继续持有；组合行业暴露偏高，暂不加仓",
            buy_gate="BLOCKED",
            buy_gate_label="组合闸门：行业暴露偏高",
            decision_reason=(
                "监测组合半导体暴露"
                f" {portfolio_semiconductor:.1%}，本基金披露持仓暴露 {fund_semiconductor:.1%}。"
            ),
        )
    elif buy_candidate:
        decision.update(
            operation_code="ADD_ALLOWED",
            action="继续持有；买入节点满足时可小额分批加仓",
            buy_gate="ALLOW",
            buy_gate_label="风险闸门：允许按节点小额分批",
            decision_reason="持仓风险处于绿色，且原始买点进入可执行区间。",
        )
    else:
        decision.update(
            operation_code="HOLD",
            action="继续持有，等待下一买入节点",
            buy_gate="WAIT",
            buy_gate_label="风险闸门：等待买点",
            decision_reason="持仓风险可接受，但当前未进入原始买入区间。",
        )
    if decision_gate.get("mode") == CONSERVATIVE_FALLBACK:
        has_risk_alert = level in {"RED", "ORANGE", "YELLOW"}
        decision.update(
            operation_code=("FALLBACK_RISK_REVIEW" if has_risk_alert else "FALLBACK_HOLD"),
            action=(
                "保守预警显示风险上升，暂停新增仓位并人工复核；不输出精确减持比例。"
                if has_risk_alert
                else "保守预警：继续持有并观察，暂停新增仓位；不输出精确减持比例。"
            ),
            buy_gate="FALLBACK",
            buy_gate_label="数据闸门：缓存宏观，仅供保守预警",
            decision_reason="基金核心数据已校验；宏观数据含缓存或低频指标，仅可用于风险方向判断。",
            manual_review_required=True,
        )
    elif decision_gate.get("mode") == HOLDING_LIMITED_MODE:
        has_risk_alert = level in {"RED", "ORANGE", "YELLOW"}
        decision.update(
            operation_code=("LIMITED_RISK_REVIEW" if has_risk_alert else "LIMITED_HOLD"),
            action=(
                "有限数据预警显示风险上升，暂停新增仓位并人工复核。"
                if has_risk_alert
                else "核心趋势预警可用；增强数据不完整，继续持有并等待补充数据。"
            ),
            buy_gate="LIMITED",
            buy_gate_label="数据闸门：核心预警可用，暂停加仓",
            decision_reason="核心净值、历史和基准数据可用，但持仓披露或宏观增强数据不完整。",
            manual_review_required=True,
        )
    elif (
        decision_gate.get("mode") == "CONSERVATIVE_DELAYED"
        and level not in {"GRAY", "RED", "ORANGE", "YELLOW"}
    ):
        decision.update(
            operation_code="DATA_DELAYED",
            action="正常QDII披露延迟，仅保守分析，暂不新增仓位",
            buy_gate="LIMITED",
            buy_gate_label="数据闸门：QDII延迟，仅供观察",
            decision_reason="净值披露处于正常延迟窗口，允许生成收窄后的参考区间，但不执行新增仓位。",
            manual_review_required=True,
        )
    alert.update(decision)


def _holding_plan(config: dict, as_of: object) -> str:
    minimum = int(config.get("holding_months_min", 3) or 3)
    maximum = int(config.get("holding_months_max", 9) or 9)
    purchase_date = config.get("purchase_date")
    if not purchase_date:
        return f"计划持有{minimum}至{maximum}个月；买入日期待补充，暂不能计算退出复核窗口"
    try:
        start = date.fromisoformat(str(purchase_date))
        end = date.fromisoformat(str(as_of))
    except (TypeError, ValueError):
        return f"计划持有{minimum}至{maximum}个月；买入日期格式无效"
    held_days = max((end - start).days, 0)
    held_months = held_days / 30.4375
    if held_months < minimum:
        state = "仍在最低持有期内"
    elif held_months <= maximum:
        state = "处于计划复核窗口"
    else:
        state = "已超过最长计划持有期，需重新评估"
    return f"已持有约{held_months:.1f}个月；{state}（计划{minimum}至{maximum}个月）"


def _summary(level: str, hard_trigger: bool) -> str:
    if level == "GREEN":
        return "风险指标处于可接受范围，继续持有并按计划复核。"
    if level == "YELLOW":
        return "风险开始上升，先停止新增仓位，观察趋势是否修复。"
    if level == "ORANGE":
        return "回撤、趋势或组合风险明显上升，等待确认后再决定是否减持。"
    if level == "RED" and hard_trigger:
        return "已触发重大风险条件，优先控制组合回撤。"
    if level == "RED":
        return "综合风险处于高位，普通信号需连续两日确认。"
    return "关键数据不足，模型不做方向性判断。"


def _apply_confirmation(alert: dict, previous: dict) -> None:
    candidate = alert.get("level")
    alert["raw_level"] = candidate
    if candidate == "GRAY":
        alert["state_status"] = (
            "实时源回退至缓存或静态参考，本次不更新有效预警状态"
            if alert.get("conservative_fallback")
            else "数据不足，本次不更新有效预警状态"
        )
        return
    if previous.get("state_version") != STATE_VERSION:
        previous = {}
    current_date = alert.get("as_of")
    previous_date = previous.get("last_as_of")
    is_new_date = bool(current_date and current_date != previous_date)
    stable = previous.get("stable_level") or (
        "YELLOW" if candidate in {"ORANGE", "RED"} and not alert.get("hard_trigger") else candidate
    )
    pending_level = previous.get("pending_level")
    pending_days = int(previous.get("pending_days", 0))
    release_level = previous.get("release_level")
    release_days = int(previous.get("release_days", 0))

    if alert.get("hard_trigger"):
        stable = candidate
        pending_level = None
        pending_days = 0
        release_level = None
        release_days = 0
        alert["confirmed"] = True
        alert["state_status"] = "重大条件立即确认"
    elif LEVEL_SEVERITY[candidate] > LEVEL_SEVERITY[stable]:
        if pending_level != candidate:
            pending_level = candidate
            pending_days = 1
        elif is_new_date:
            pending_days += 1
        if pending_days >= 2:
            stable = candidate
            pending_level = None
            pending_days = 0
            alert["confirmed"] = True
            alert["state_status"] = "升级已连续两个有效数据日确认"
        else:
            alert["confirmed"] = False
            alert["state_status"] = f"升级确认 {pending_days}/2"
    elif LEVEL_SEVERITY[candidate] < LEVEL_SEVERITY[stable]:
        if release_level != candidate:
            release_level = candidate
            release_days = 1
        elif is_new_date:
            release_days += 1
        if release_days >= 3:
            stable = candidate
            release_level = None
            release_days = 0
            alert["state_status"] = "风险解除已连续三个有效数据日确认"
        else:
            alert["state_status"] = f"风险解除观察 {release_days}/3"
        pending_level = None
        pending_days = 0
        alert["confirmed"] = False
    else:
        pending_level = None
        pending_days = 0
        release_level = None
        release_days = 0
        alert["confirmed"] = stable in {"ORANGE", "RED"}
        alert["state_status"] = "当前等级保持"

    displayed = stable
    if LEVEL_SEVERITY[candidate] > LEVEL_SEVERITY[stable]:
        displayed = candidate
    alert["level"] = displayed
    alert["level_label"] = ALERT_LEVELS[displayed]
    alert["confirmation_days"] = pending_days
    alert["release_days"] = release_days
    alert["stable_level"] = stable
    alert["pending_level"] = pending_level
    alert["release_level"] = release_level
    alert["action"] = _action(
        displayed,
        bool(alert["confirmed"]),
        bool(alert.get("hard_trigger")),
        bool(alert.get("production_approved")),
    )
    if pending_level:
        alert["level_label"] += f"（待确认 {pending_days}/2）"
    elif release_level:
        alert["level_label"] += f"（解除观察 {release_days}/3）"


def _history_correlation(left: dict, right: dict) -> float | None:
    left_map = {item["date"]: float(item["nav"]) for item in left.get("records", []) if float(item.get("nav", 0)) > 0}
    right_map = {item["date"]: float(item["nav"]) for item in right.get("records", []) if float(item.get("nav", 0)) > 0}
    dates = sorted(set(left_map) & set(right_map))[-190:]
    if len(dates) < 30:
        return None
    left_returns = [left_map[dates[i]] / left_map[dates[i - 1]] - 1 for i in range(1, len(dates))]
    right_returns = [right_map[dates[i]] / right_map[dates[i - 1]] - 1 for i in range(1, len(dates))]
    left_mean = statistics.fmean(left_returns)
    right_mean = statistics.fmean(right_returns)
    left_dev = math.sqrt(sum((value - left_mean) ** 2 for value in left_returns))
    right_dev = math.sqrt(sum((value - right_mean) ** 2 for value in right_returns))
    if not left_dev or not right_dev:
        return 0.0
    return sum((left_returns[i] - left_mean) * (right_returns[i] - right_mean) for i in range(len(left_returns))) / (left_dev * right_dev)


def _calibration_features(metrics: dict) -> dict[str, float]:
    return {
        "tail_risk": _tail_risk(metrics) / 100,
        "trend_risk": _trend_risk(metrics) / 100,
        "drawdown_63": min(max(-metrics["max_drawdown_63"], 0), 0.50) / 0.50,
        "downside_volatility_63": min(metrics["downside_volatility_63"], 0.60) / 0.60,
        "cvar_95_63": min(max(-metrics["cvar_95_63"], 0), 0.08) / 0.08,
        "ma60_below": min(max(-metrics["ma60_gap"], 0), 0.25) / 0.25,
        "ma120_below": min(max(-metrics["ma120_gap"], 0), 0.35) / 0.35,
    }


def _load_calibration() -> dict:
    try:
        return json.loads(HOLDING_ALERT_CALIBRATION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "production_approved": False,
            "status": "尚未生成滚动样本外校准结果",
            "horizons": {},
        }


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1 / (1 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1 + exponential)


def _read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(path: Path, alerts: dict[str, dict], previous: dict | None = None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            code: item
            for code, item in (previous or {}).items()
            if item.get("state_version") == STATE_VERSION
        }
        payload.update(
            {
                code: {
                    "state_version": STATE_VERSION,
                    "stable_level": alert.get("stable_level", alert.get("level")),
                    "pending_level": alert.get("pending_level"),
                    "pending_days": alert.get("confirmation_days", 0),
                    "release_level": alert.get("release_level"),
                    "release_days": alert.get("release_days", 0),
                    "last_as_of": alert.get("as_of"),
                }
                for code, alert in alerts.items()
                if alert.get("level") != "GRAY"
            }
        )
        atomic_write_json(path, payload)
    except OSError:
        pass


def _max_drawdown(navs: list[float]) -> float:
    peak = navs[0]
    drawdown = 0.0
    for nav in navs:
        peak = max(peak, nav)
        drawdown = min(drawdown, nav / peak - 1)
    return drawdown


def _recovery_days(navs: list[float]) -> int:
    peak = max(navs)
    if navs[-1] >= peak * 0.99:
        return 0
    peak_index = max(index for index, value in enumerate(navs) if value == peak)
    return len(navs) - 1 - peak_index


def _ratio_risk(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return _clamp((value - low) / (high - low) * 100, 0, 100)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
