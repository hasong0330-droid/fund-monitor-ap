from __future__ import annotations

import json
import math
import statistics
from datetime import date, datetime
from typing import Dict, Iterable, List

from config import (
    FUND_PROFILES,
    MIN_RECOMMENDATION_DAILY_LIMIT,
    QUANT_SIGNAL_FILE,
    RECOMMENDATION_POOL,
)
from data_quality import READY
from decision_gate import build_decision_gate
from factor_model import FACTOR_WEIGHTS, evaluate_fund_factors
from source_router import is_live_source, live_source_names
from storage import atomic_write_json


MODEL_VERSION = "GAF-QM v4.0 Fundamental-Led Five-Factor 3-9M"
MODEL_WEIGHTS = FACTOR_WEIGHTS
STRONG_BUY_THRESHOLD = 70.0
RISK_LIMITS = {
    "max_drawdown_126": -0.20,
    "downside_volatility_126": 0.28,
    "cvar_95_126": -0.04,
    "recovery_days_126": 126,
}
MIN_HOLDING_PERIODS = 63
TARGET_HOLDING_PERIODS = 126
MAX_HOLDING_PERIODS = 189
# v4.0 must pass a point-in-time, purged walk-forward review before production use.
MODEL_PRODUCTION_APPROVED = False

PORTFOLIO_RISK_LIMITS = {
    "max_single_weight": 0.35,
    "max_theme_weight": 0.65,
    "max_correlated_weight": 0.75,
    "max_portfolio_volatility": 0.30,
    "target_portfolio_volatility": 0.18,
    "correlation_threshold": 0.70,
    "min_return_observations": 60,
}

FUND_THEME_PROXY = {
    "018229": "global_growth",
    "005698": "global_technology",
    "118001": "asia_growth",
    "005534": "china_growth",
    "017653": "global_semiconductor",
    "161815": "global_inflation_hedge",
    "016701": "global_technology",
    "162415": "global_consumption",
}


def build_scoring_candidates(
    targets: Iterable[object], recommendations: Iterable[dict]
) -> List[dict]:
    """Combine qualified external funds with comparable specified funds."""
    candidates: List[dict] = []
    seen: set[str] = set()

    def value(item: object, key: str, default: object = None) -> object:
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

    for item in recommendations:
        code = str(value(item, "code", "") or "")
        if not code or code in seen:
            continue
        candidates.append(dict(item) if isinstance(item, dict) else {
            "code": code,
            "name": str(value(item, "name", code)),
        })
        seen.add(code)

    for item in targets:
        code = str(value(item, "code", "") or "")
        profile = FUND_PROFILES.get(code, {})
        if (
            not code
            or code in seen
            or not profile.get("is_global_or_us_focus")
            or float(profile.get("one_day_limit", 0) or 0)
            < MIN_RECOMMENDATION_DAILY_LIMIT
        ):
            continue
        candidates.append({
            "code": code,
            "name": str(value(item, "name", code)),
            "candidate_source": "specified_fund",
        })
        seen.add(code)
    return candidates


def _fund_nav_returns(history: dict) -> dict[str, float]:
    records = sorted(history.get("records", []), key=lambda item: item.get("date", ""))
    values = [
        (str(item.get("date", "")), float(item.get("nav", 0)))
        for item in records
        if float(item.get("nav", 0) or 0) > 0
    ]
    return {
        values[index][0]: values[index][1] / values[index - 1][1] - 1
        for index in range(1, len(values))
        if values[index - 1][0] and values[index][0]
    }


def _portfolio_position_weights(portfolio_positions: Dict[str, dict]) -> dict[str, float]:
    amounts = {}
    for code, item in (portfolio_positions or {}).items():
        if not isinstance(item, dict):
            continue
        value = item.get("market_value")
        if not isinstance(value, (int, float)) or float(value) <= 0:
            value = item.get("invested_amount")
        if isinstance(value, (int, float)) and float(value) > 0:
            amounts[str(code)] = float(value)
    total = sum(amounts.values())
    return {code: value / total for code, value in amounts.items()} if total else {}


def _portfolio_theme(code: str) -> str:
    if code in FUND_THEME_PROXY:
        return FUND_THEME_PROXY[code]
    profile = FUND_PROFILES.get(code) or RECOMMENDATION_POOL.get(code) or {}
    text = f"{profile.get('theme', '')} {profile.get('index_focus', '')}"
    if any(term in text.lower() for term in ("ai", "technology", "tech", "semiconductor")):
        return "global_technology"
    return "unclassified"


def _correlation(left: list[float], right: list[float]) -> float:
    if len(left) < 2 or len(left) != len(right):
        return 0.0
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right)
    )
    denominator = math.sqrt(
        sum((value - left_mean) ** 2 for value in left)
        * sum((value - right_mean) ** 2 for value in right)
    )
    return numerator / denominator if denominator else 0.0


def _annualized_volatility(values: list[float]) -> float:
    return statistics.pstdev(values) * math.sqrt(252) if len(values) > 1 else 0.0


def _weighted_portfolio_returns(
    weights: dict[str, float], returns: dict[str, dict[str, float]]
) -> list[float]:
    usable = [code for code in weights if code in returns]
    if not usable:
        return []
    dates = set(returns[usable[0]])
    for code in usable[1:]:
        dates &= set(returns[code])
    return [
        sum(weights[code] * returns[code][day] for code in usable)
        for day in sorted(dates)
    ]


def build_portfolio_risk_budget(
    candidate_code: str,
    portfolio_positions: Dict[str, dict] | None,
    histories: Dict[str, dict],
    holdings: Dict[str, dict],
    candidate_holding: dict | None = None,
    limits: dict | None = None,
) -> dict:
    """Calculate portfolio-level capacity before allowing a new fund position."""
    limits = {**PORTFOLIO_RISK_LIMITS, **(limits or {})}
    current_weights = _portfolio_position_weights(portfolio_positions or {})
    if not current_weights:
        return {
            "available": False,
            "allowed": False,
            "candidate_code": candidate_code,
            "blocking_reasons": ["组合账本没有可验证的持仓市值或投入金额"],
            "limits": limits,
        }

    return_series = {
        code: _fund_nav_returns(histories.get(code, {}))
        for code in set(current_weights) | {candidate_code}
    }
    current_codes = [
        code
        for code in current_weights
        if len(return_series.get(code, {})) >= limits["min_return_observations"]
    ]
    candidate_returns = return_series.get(candidate_code, {})
    common_current_dates = set()
    if current_codes:
        common_current_dates = set(return_series[current_codes[0]])
        for code in current_codes[1:]:
            common_current_dates &= set(return_series[code])
    if len(common_current_dates) < limits["min_return_observations"] or len(candidate_returns) < limits["min_return_observations"]:
        return {
            "available": False,
            "allowed": False,
            "candidate_code": candidate_code,
            "current_weights": current_weights,
            "blocking_reasons": [
                "组合或候选基金历史净值共同样本不足，无法计算相关性和边际风险"
            ],
            "limits": limits,
        }

    current_returns = {
        code: return_series[code] for code in current_codes
    }
    normalized_weights = {
        code: current_weights[code] / sum(current_weights[item] for item in current_codes)
        for code in current_codes
    }
    portfolio_returns = _weighted_portfolio_returns(normalized_weights, current_returns)
    candidate_dates = sorted(set(candidate_returns) & set(common_current_dates))
    candidate_aligned = [candidate_returns[day] for day in candidate_dates]
    portfolio_aligned = [
        sum(
            normalized_weights[code] * current_returns[code][day]
            for code in current_codes
        )
        for day in candidate_dates
    ]
    if len(candidate_aligned) < limits["min_return_observations"]:
        return {
            "available": False,
            "allowed": False,
            "candidate_code": candidate_code,
            "current_weights": current_weights,
            "blocking_reasons": ["候选基金与现有组合的共同交易日样本不足"],
            "limits": limits,
        }

    current_volatility = _annualized_volatility(portfolio_returns)
    candidate_volatility = _annualized_volatility(candidate_aligned)
    candidate_correlation = _correlation(candidate_aligned, portfolio_aligned)
    current_theme_exposure: dict[str, float] = {}
    for code, weight in normalized_weights.items():
        theme = _portfolio_theme(code)
        current_theme_exposure[theme] = current_theme_exposure.get(theme, 0.0) + weight
    candidate_theme = _portfolio_theme(candidate_code)
    candidate_theme_weight = current_theme_exposure.get(candidate_theme, 0.0)
    candidate_overlap = {}
    candidate_items = (candidate_holding or holdings.get(candidate_code) or {}).get("holdings", [])
    candidate_map = {str(item.get("code")): float(item.get("weight", 0)) for item in candidate_items}
    overlap_coverage = 0.0
    for code, weight in normalized_weights.items():
        items = (holdings.get(code) or {}).get("holdings", [])
        if items:
            overlap_coverage += weight
        existing_map = {str(item.get("code")): float(item.get("weight", 0)) for item in items}
        candidate_overlap[code] = sum(
            min(weight_value, existing_map.get(stock_code, 0.0))
            for stock_code, weight_value in candidate_map.items()
        )
    weighted_overlap = sum(
        normalized_weights[code] * candidate_overlap.get(code, 0.0)
        for code in normalized_weights
    )
    correlations = {
        code: _correlation(
            candidate_aligned,
            [current_returns[code][day] for day in candidate_dates],
        )
        for code in current_codes
    }
    correlated_exposure = sum(
        normalized_weights[code]
        for code, correlation in correlations.items()
        if correlation >= limits["correlation_threshold"]
    )

    max_candidate_weight = min(
        limits["max_single_weight"],
        max(0.0, limits["max_theme_weight"] - candidate_theme_weight),
        max(0.0, limits["max_correlated_weight"] - correlated_exposure)
        if candidate_correlation >= limits["correlation_threshold"]
        else limits["max_correlated_weight"],
    )
    volatility_capacity = 0.0
    if current_volatility <= limits["max_portfolio_volatility"]:
        for step in range(0, 101):
            weight = max_candidate_weight * step / 100
            post_returns = [
                (1 - weight) * portfolio_value + weight * candidate_value
                for portfolio_value, candidate_value in zip(
                    portfolio_aligned, candidate_aligned
                )
            ]
            if _annualized_volatility(post_returns) <= limits["max_portfolio_volatility"]:
                volatility_capacity = weight
    max_additional_weight = min(max_candidate_weight, volatility_capacity)
    risk_budget_weight = min(
        max_additional_weight,
        0.15,
        limits["target_portfolio_volatility"] / max(candidate_volatility, 0.01) * 0.05,
    )
    blocking_reasons = []
    if overlap_coverage < 0.80:
        blocking_reasons.append("现有组合持仓披露覆盖不足，重合风险只能部分验证")
    if current_volatility > limits["max_portfolio_volatility"]:
        blocking_reasons.append("现有组合年化波动率已超过组合硬上限")
    if candidate_correlation >= limits["correlation_threshold"] and correlated_exposure >= limits["max_correlated_weight"]:
        blocking_reasons.append("候选基金与现有组合高相关资产合计超过风险预算")
    if candidate_theme_weight >= limits["max_theme_weight"]:
        blocking_reasons.append(f"候选主题 {candidate_theme} 已达到主题集中度上限")
    if max_additional_weight <= 0:
        blocking_reasons.append("组合风险预算不允许新增该候选基金仓位")
    return {
        "available": True,
        "allowed": not blocking_reasons and max_additional_weight > 0,
        "candidate_code": candidate_code,
        "candidate_theme": candidate_theme,
        "current_weights": {code: round(value, 4) for code, value in normalized_weights.items()},
        "current_theme_exposure": {
            key: round(value, 4) for key, value in current_theme_exposure.items()
        },
        "candidate_theme_exposure_before": round(candidate_theme_weight, 4),
        "candidate_overlap": {code: round(value, 4) for code, value in candidate_overlap.items()},
        "weighted_overlap": round(weighted_overlap, 4),
        "holding_coverage": round(overlap_coverage, 4),
        "correlations": {code: round(value, 4) for code, value in correlations.items()},
        "candidate_correlation": round(candidate_correlation, 4),
        "correlated_exposure": round(correlated_exposure, 4),
        "current_volatility": round(current_volatility, 4),
        "candidate_volatility": round(candidate_volatility, 4),
        "max_additional_weight": round(max_additional_weight, 4),
        "recommended_target_weight": round(risk_budget_weight, 4),
        "blocking_reasons": blocking_reasons,
        "limits": limits,
        "sample_observations": len(candidate_aligned),
    }


def evaluate_strong_buy(
    recommendations: List[dict],
    quotes: Dict[str, object],
    histories: Dict[str, dict],
    holdings: Dict[str, dict],
    market_snapshot: dict,
    news: Iterable[object],
    current_holding_code: str = "018229",
    data_quality: dict | None = None,
    stock_trends: Dict[str, dict] | None = None,
    portfolio_positions: Dict[str, dict] | None = None,
) -> dict:
    news_items = list(news)
    data_quality = data_quality or {}
    stock_trends = stock_trends or {}
    overall_gate = build_decision_gate({}, data_quality)
    evaluations = []
    data_errors = []
    for recommendation in recommendations:
        code = recommendation["code"]
        history = histories.get(code)
        holding = holdings.get(code)
        quote = quotes.get(code)
        if not history or not quote:
            data_errors.append(f"{code} 缺少历史净值或当前净值。")
            continue
        purchase_status = str(history.get("purchase_status", "") or "").strip()
        if _purchase_is_closed(purchase_status):
            data_errors.append(
                f"{code} 当前申购状态为“{purchase_status}”，已按硬性条件剔除，不参与评分。"
            )
            continue
        try:
            evaluations.append(
                _evaluate_candidate(
                    recommendation,
                    quote,
                    history,
                    holding,
                    holdings.get(current_holding_code),
                    market_snapshot,
                    news_items,
                    data_quality.get("funds", {}).get(code),
                    stock_trends,
                    data_quality,
                    portfolio_positions,
                    histories,
                    holdings,
                )
            )
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
            data_errors.append(f"{code} 数据校验失败：{error}")

    if not evaluations:
        return _empty_result(
            ["候选基金数据不足，模型拒绝生成强烈买入信号。", *data_errors]
        )

    evaluations.sort(key=lambda item: (item["total_score"], item["confidence"]), reverse=True)
    best = evaluations[0]
    best["is_recommended"] = True
    best["recommendation_rank"] = 1
    best["scored_candidate_count"] = len(evaluations)
    eligible_now = not best["failed_conditions"]
    if not overall_gate["can_strong_buy"]:
        best["failed_conditions"].extend(
            [
                "当前为保守数据回退模式，强烈买入信号保持关闭。",
                str(
                    data_quality.get("summary")
                    or "等待实时数据恢复后重新评估强烈买入资格。"
                ),
            ]
        )
        eligible_now = False
    if not MODEL_PRODUCTION_APPROVED:
        best["failed_conditions"].append(
            "v4.0五因子模型尚未通过固定验证集与尾部风险验收，生产强买信号暂时关闭。"
        )
        eligible_now = False
    streak = _update_signal_streak(best, eligible_now, market_snapshot)
    best["signal_streak"] = streak

    if eligible_now and streak < 2:
        best["failed_conditions"].append(
            f"量化信号需连续两个有效数据周期确认，当前为 {streak}/2。"
        )

    best["is_strong_buy"] = eligible_now and streak >= 2
    best["verdict"] = (
        "强烈建议买入"
        if best["is_strong_buy"]
        else "综合评分首选（暂缓执行）"
    )
    best["all_candidates"] = [
        {
            "rank": index,
            "code": item["code"],
            "name": item["name"],
            "score": item["total_score"],
            "confidence": item["confidence"],
        }
        for index, item in enumerate(evaluations, start=1)
    ]
    best["data_errors"] = data_errors
    return best


def _evaluate_candidate(
    recommendation: dict,
    quote: object,
    history: dict,
    holding: dict | None,
    current_holding: dict | None,
    market_snapshot: dict,
    news: List[object],
    quality_item: dict | None = None,
    stock_trends: Dict[str, dict] | None = None,
    overall_quality: dict | None = None,
    portfolio_positions: Dict[str, dict] | None = None,
    portfolio_histories: Dict[str, dict] | None = None,
    portfolio_holdings: Dict[str, dict] | None = None,
) -> dict:
    quality_item = quality_item or {}
    decision_gate = build_decision_gate(quality_item, overall_quality)
    metrics = _history_metrics(history.get("records", []))
    quality_score = _quality_score(metrics, recommendation["code"])
    trend_score = _trend_score(metrics)
    pullback_score = _pullback_score(metrics)
    regime_score, regime_state, regime_probabilities, regime_reasons = (
        _market_regime_score(market_snapshot, news)
    )
    (
        holding_score,
        holding_reasons,
        concentration,
        holding_age,
        stock_trend_coverage,
        holding_sentiment,
        news_matched_weight,
    ) = _holding_score(
        holding, news, stock_trends or {}, history.get("latest_date")
    )
    fit_score, overlap, fit_reason = _portfolio_fit(holding, current_holding)
    confidence = _confidence_score(
        quote, history, holding, holding_age, market_snapshot, news, quality_item
    )
    risk_penalty, volatility_multiplier = _risk_penalty(
        metrics, concentration, confidence
    )

    defense_score = _defense_score(metrics)
    stability_score = _stability_score(metrics)
    risk_adjusted_score = _risk_adjusted_score(metrics)
    market_score = _market_defense_score(regime_probabilities)
    portfolio_score = holding_score * 0.5 + fit_score * 0.5
    market_multiplier = _market_position_multiplier(
        regime_state, regime_probabilities
    )
    factor_evaluation = evaluate_fund_factors(
        recommendation["code"],
        history,
        holding,
        market_snapshot,
        news,
        stock_trends or {},
    )
    factor_scores = factor_evaluation["scores"]
    factor_coverage = factor_evaluation["coverage"]

    total_score = float(factor_evaluation["total_score"])
    portfolio_risk_budget = {}
    if portfolio_positions is not None:
        portfolio_risk_budget = build_portfolio_risk_budget(
            recommendation["code"],
            portfolio_positions,
            portfolio_histories or {recommendation["code"]: history},
            portfolio_holdings or {},
            candidate_holding=holding,
        )

    failed = []
    if not decision_gate["can_strong_buy"] and quality_item:
        reasons = "；".join(dict.fromkeys(quality_item.get("reasons", [])))
        failed.append(
            f"数据可信度闸门为{quality_item.get('gate_label', '仅供观察')}"
            f"；{reasons or '关键数据未通过来源和时效校验。'}"
        )
    if total_score < STRONG_BUY_THRESHOLD:
        failed.append(
            f"综合评分 {total_score:.1f}，低于强烈建议阈值 {STRONG_BUY_THRESHOLD:.0f}。"
        )
    if confidence < 80:
        failed.append(f"数据置信度 {confidence:.1f}%，低于阈值 80%。")
    if factor_evaluation["confidence"] < 70:
        failed.append(
            f"五因子数据覆盖置信度 {factor_evaluation['confidence']:.1f}%，低于阈值 70%。"
        )
    if factor_coverage["fundamental"] < 0.60:
        failed.append(
            f"企业财报覆盖 {factor_coverage['fundamental']:.1%}，低于强买硬门槛 60%。"
        )
    if factor_scores["fundamental"] < 50:
        failed.append(f"企业基本面评分 {factor_scores['fundamental']:.1f}，低于阈值 50。")
    if factor_scores["macro"] < 40:
        failed.append(f"宏观经济评分 {factor_scores['macro']:.1f}，低于阈值 40。")
    if factor_scores["industry"] < 40:
        failed.append(f"行业景气评分 {factor_scores['industry']:.1f}，低于阈值 40。")
    if factor_scores["other"] < 60:
        failed.append(f"负面事件安全评分 {factor_scores['other']:.1f}，低于阈值 60。")
    if factor_evaluation["hard_veto"]:
        failed.append(f"重大事件一票否决：{factor_evaluation['hard_veto_reason']}")
    technical_flags = factor_evaluation["technical_flags"]
    if technical_flags.get("below_ma60") or technical_flags.get("below_ma120"):
        failed.append("基金净值跌破关键均线，红色技术预警期间禁止新增买入。")
    if defense_score < 55:
        failed.append(f"下行防御评分 {defense_score:.1f}，低于阈值 55。")
    if quality_score < 50:
        failed.append(f"基金质量评分 {quality_score:.1f}，低于阈值 50。")
    if _midterm_trend_score(metrics) < 50:
        failed.append(f"3至9个月趋势健康评分 {_midterm_trend_score(metrics):.1f}，低于阈值 50。")
    if stability_score < 50:
        failed.append(f"收益稳定性评分 {stability_score:.1f}，低于阈值 50。")
    if risk_adjusted_score < 50:
        failed.append(f"风险调整收益评分 {risk_adjusted_score:.1f}，低于阈值 50。")
    if pullback_score < 45:
        failed.append(f"回撤确认评分 {pullback_score:.1f}，低于阈值 45。")
    if regime_state == "压力/危机" and regime_probabilities["stress"] >= 0.55:
        failed.append(
            f"市场处于压力/危机状态，状态概率 {regime_probabilities['stress']:.1%}。"
        )
    if market_multiplier <= 0:
        failed.append("市场风险闸门关闭，当前不允许新增仓位。")
    if holding_score < 50:
        failed.append(f"持仓景气评分 {holding_score:.1f}，低于阈值 50。")
    if stock_trend_coverage < 0.60:
        failed.append(
            f"前十大持仓有效行情覆盖 {stock_trend_coverage:.1%}，低于强买硬门槛 60%。"
        )
    if holding_sentiment < 40 and news_matched_weight >= 0.10:
        failed.append(
            f"持仓相关新闻情绪评分 {holding_sentiment:.1f}，且直接影响权重约 "
            f"{news_matched_weight:.1%}，当前不允许强买。"
        )
    if fit_score < 35:
        failed.append(f"组合分散评分 {fit_score:.1f}，低于阈值 35。")
    if overlap > 0.65:
        failed.append(f"与现有持仓前十大股票重合度 {overlap:.1%}，超过 65%。")
    if portfolio_positions is not None:
        if not portfolio_risk_budget.get("available"):
            failed.extend(portfolio_risk_budget.get("blocking_reasons", []))
        elif not portfolio_risk_budget.get("allowed"):
            failed.extend(portfolio_risk_budget.get("blocking_reasons", []))
    if metrics["zscore_60"] > 1.8 or metrics["momentum_20"] > 0.12:
        failed.append("短期净值处于过热区，模型禁止追涨。")
    if metrics["annual_volatility"] > 0.45:
        failed.append(f"年化波动率 {metrics['annual_volatility']:.1%} 超过45%硬门槛。")
    if metrics["max_drawdown"] < -0.45:
        failed.append(f"历史最大回撤 {metrics['max_drawdown']:.1%} 超过45%硬门槛。")
    if metrics["cvar_95"] < -0.04:
        failed.append(f"日度95% CVaR {metrics['cvar_95']:.2%} 超过-4%硬门槛。")
    if metrics["max_drawdown_126"] < RISK_LIMITS["max_drawdown_126"]:
        failed.append(f"近126日最大回撤 {metrics['max_drawdown_126']:.1%} 超过-20%硬门槛。")
    if metrics["downside_volatility_126"] > RISK_LIMITS["downside_volatility_126"]:
        failed.append(f"近126日下行波动率 {metrics['downside_volatility_126']:.1%} 超过28%硬门槛。")
    if metrics["cvar_95_126"] < RISK_LIMITS["cvar_95_126"]:
        failed.append(f"近126日95% CVaR {metrics['cvar_95_126']:.2%} 超过-4%硬门槛。")
    if metrics["recovery_days_126"] > RISK_LIMITS["recovery_days_126"]:
        failed.append(f"当前回撤恢复已超过 {metrics['recovery_days_126']} 个净值周期。")
    if not is_live_source(getattr(quote, "source", "fallback"), "fund_nav"):
        failed.append("候选基金当前净值未通过实时来源校验。")
    if not is_live_source(history.get("source", "fallback"), "fund_history"):
        failed.append("三年历史净值未通过实时来源校验，不能触发强烈建议。")
    if not holding or not is_live_source(holding.get("source", "fallback"), "holdings"):
        failed.append("最新持仓披露未完成联网验证。")
    if _purchase_is_closed(str(history.get("purchase_status", "") or "")):
        failed.append("基金当前暂停申购。")

    top_names = [item["name"] for item in (holding or {}).get("holdings", [])[:3]]
    holdings_text = "、".join(top_names) if top_names else "暂无可靠持仓明细"
    return {
        "model_version": MODEL_VERSION,
        "code": recommendation["code"],
        "name": recommendation["name"],
        "total_score": round(total_score, 1),
        "factor_model": factor_evaluation,
        "factor_scores": factor_scores,
        "factor_coverage": factor_coverage,
        "confidence": round(confidence, 1),
        "quality_score": round(quality_score, 1),
        "trend_score": round(trend_score, 1),
        "defense_score": round(defense_score, 1),
        "stability_score": round(stability_score, 1),
        "risk_adjusted_score": round(risk_adjusted_score, 1),
        "market_score": round(factor_scores["macro"], 1),
        "market_multiplier": round(market_multiplier, 2),
        "portfolio_score": round(portfolio_score, 1),
        "pullback_score": round(pullback_score, 1),
        "regime_score": round(regime_score, 1),
        "regime_state": regime_state,
        "regime_probabilities": {
            key: round(value, 4) for key, value in regime_probabilities.items()
        },
        "holding_score": round(holding_score, 1),
        "holding_stock_coverage": round(stock_trend_coverage, 4),
        "holding_sentiment": round(holding_sentiment, 1),
        "news_matched_weight": round(news_matched_weight, 4),
        "fit_score": round(fit_score, 1),
        "risk_penalty": round(risk_penalty, 1),
        "overlap": round(overlap, 4),
        "portfolio_risk_budget": portfolio_risk_budget,
        "macro_reasons": regime_reasons,
        "holding_reasons": holding_reasons + [fit_reason],
        "risk_conditions": [
            "纳斯达克中期趋势转弱或美债利率重新快速上行时取消信号。",
            f"历史最大回撤 {metrics['max_drawdown']:.1%}，日度95% CVaR {metrics['cvar_95']:.2%}。",
            f"15%目标波动率对应风险仓位系数 {volatility_multiplier:.0%}；市场状态为{regime_state}。",
            "核心持仓盈利预期下修、地缘限制升级或基金暂停大额申购时重新评估。",
            "目标持有63至189个净值周期；达到189个周期后必须重新评估，不允许模型无限期持有。",
            "当前模型仍处于样本外观察期，未通过验收前不生成强买信号。",
            f"市场状态仓位系数 {market_multiplier:.0%}；压力状态时风险闸门关闭。",
        ],
        "failed_conditions": failed,
        "metrics": {
            "purchase_status": history.get("purchase_status", "--"),
            "annual_return": round(metrics["annual_return"], 4),
            "annual_volatility": round(metrics["annual_volatility"], 4),
            "volatility_multiplier": round(volatility_multiplier, 4),
            "sharpe": round(metrics["sharpe"], 2),
            "sortino": round(metrics["sortino"], 2),
            "calmar": round(metrics["calmar"], 2),
            "max_drawdown": round(metrics["max_drawdown"], 4),
            "cvar_95": round(metrics["cvar_95"], 4),
            "max_drawdown_63": round(metrics["max_drawdown_63"], 4),
            "max_drawdown_126": round(metrics["max_drawdown_126"], 4),
            "max_drawdown_189": round(metrics["max_drawdown_189"], 4),
            "downside_volatility_126": round(metrics["downside_volatility_126"], 4),
            "cvar_95_126": round(metrics["cvar_95_126"], 4),
            "recovery_days_126": metrics["recovery_days_126"],
            "positive_63_ratio": round(metrics["positive_63_ratio"], 4),
            "holding_window": f"{MIN_HOLDING_PERIODS}-{MAX_HOLDING_PERIODS}个净值周期",
            "momentum_120": round(metrics["momentum_120"], 4),
            "zscore_60": round(metrics["zscore_60"], 2),
            "top_holdings": holdings_text,
            "disclosure_date": (holding or {}).get("disclosure_date", "--"),
        },
        "data_signature": f"{MODEL_VERSION}:{recommendation['code']}:{history.get('latest_date')}:{getattr(quote, 'date', '--')}",
        "production_approved": MODEL_PRODUCTION_APPROVED,
        "decision_gate": decision_gate,
        "risk_limits": RISK_LIMITS,
    }


def _purchase_is_closed(status: str) -> bool:
    normalized = "".join(str(status or "").split())
    return any(term in normalized for term in ("暂停申购", "停止申购", "封闭"))


def _history_metrics(records: List[dict]) -> dict:
    ordered = sorted(records, key=lambda item: item["date"])
    navs = [float(item["nav"]) for item in ordered if float(item["nav"]) > 0]
    if len(navs) < 250:
        raise ValueError("history requires at least 250 observations")
    returns = [navs[index] / navs[index - 1] - 1 for index in range(1, len(navs))]
    years = max(len(returns) / 252, 1 / 252)
    annual_return = (navs[-1] / navs[0]) ** (1 / years) - 1
    annual_volatility = statistics.pstdev(returns) * math.sqrt(252)
    downside = [min(value, 0) for value in returns]
    downside_deviation = math.sqrt(sum(value * value for value in downside) / len(downside)) * math.sqrt(252)
    risk_free = 0.02
    sharpe = (annual_return - risk_free) / annual_volatility if annual_volatility else 0
    sortino = (annual_return - risk_free) / downside_deviation if downside_deviation else 0
    peak = navs[0]
    max_drawdown = 0.0
    for nav in navs:
        peak = max(peak, nav)
        max_drawdown = min(max_drawdown, nav / peak - 1)
    calmar = annual_return / abs(max_drawdown) if max_drawdown < 0 else 0
    tail_count = max(1, int(len(returns) * 0.05))
    cvar_95 = sum(sorted(returns)[:tail_count]) / tail_count
    window60 = navs[-60:]
    window252 = navs[-252:]
    std60 = statistics.pstdev(window60)
    mean60 = statistics.fmean(window60)
    percentile_252 = sum(1 for nav in window252 if nav <= navs[-1]) / len(window252)
    rolling = {}
    for horizon in (63, 126, 189):
        window = navs[-horizon:] if len(navs) >= horizon else navs
        rolling_returns = [
            window[index] / window[index - 1] - 1
            for index in range(1, len(window))
        ]
        rolling[f"max_drawdown_{horizon}"] = _max_drawdown(window)
        downside = [min(value, 0) for value in rolling_returns]
        rolling[f"downside_volatility_{horizon}"] = (
            statistics.pstdev(downside) * math.sqrt(252)
            if len(downside) > 1
            else 0.0
        )
        tail_count = max(1, math.ceil(len(rolling_returns) * 0.05))
        rolling[f"cvar_95_{horizon}"] = (
            statistics.fmean(sorted(rolling_returns)[:tail_count])
            if rolling_returns
            else 0.0
        )
        rolling[f"positive_{horizon}_ratio"] = (
            sum(value > 0 for value in rolling_returns) / len(rolling_returns)
            if rolling_returns
            else 0.0
        )
        rolling[f"recovery_days_{horizon}"] = _recovery_days(window)
    return {
        "count": len(navs),
        "annual_return": annual_return,
        "annual_volatility": annual_volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "calmar": calmar,
        "cvar_95": cvar_95,
        "momentum_20": navs[-1] / navs[-21] - 1,
        "momentum_60": navs[-1] / navs[-61] - 1,
        "momentum_120": navs[-1] / navs[-121] - 1,
        "drawdown_60": navs[-1] / max(window60) - 1,
        "zscore_60": (navs[-1] - mean60) / std60 if std60 else 0,
        "percentile_252": percentile_252,
        **rolling,
        "recovery_days_126": rolling["recovery_days_126"],
    }


def _max_drawdown(navs: List[float]) -> float:
    peak = navs[0]
    drawdown = 0.0
    for nav in navs:
        peak = max(peak, nav)
        drawdown = min(drawdown, nav / peak - 1)
    return drawdown


def _recovery_days(navs: List[float]) -> int:
    peak = max(navs)
    if navs[-1] >= peak * 0.99:
        return 0
    peak_index = max(index for index, value in enumerate(navs) if value == peak)
    return len(navs) - 1 - peak_index


def _quality_score(metrics: dict, code: str | None = None) -> float:
    # Current peer ranking is an eligibility filter, not a historical return factor.
    return (
        _scale(metrics["annual_return"], -0.10, 0.25) * 0.20
        + _scale(metrics["sharpe"], -0.3, 1.5) * 0.20
        + _scale(metrics["sortino"], -0.3, 2.0) * 0.15
        + _scale(metrics["calmar"], -0.2, 1.5) * 0.15
        + _scale(metrics["max_drawdown"], -0.65, -0.10) * 0.15
        + _scale(metrics["positive_63_ratio"], 0.40, 0.70) * 0.15
    )


def _trend_score(metrics: dict) -> float:
    long_trend = _scale(metrics["momentum_120"], -0.18, 0.25)
    medium_trend = _scale(metrics["momentum_60"], -0.12, 0.16)
    short_trend = _scale(metrics["momentum_20"], -0.08, 0.08)
    alignment_bonus = 8 if metrics["momentum_60"] > 0 and metrics["momentum_120"] > 0 else 0
    return _clamp(
        long_trend * 0.45 + medium_trend * 0.35 + short_trend * 0.20 + alignment_bonus,
        0,
        100,
    )


def _midterm_trend_score(metrics: dict) -> float:
    return _clamp(
        _scale(metrics["momentum_120"], -0.20, 0.25) * 0.55
        + _scale(metrics["momentum_60"], -0.15, 0.18) * 0.30
        + _scale(metrics["momentum_20"], -0.12, 0.10) * 0.15,
        0,
        100,
    )


def _defense_score(metrics: dict) -> float:
    drawdown = _scale(metrics["max_drawdown_126"], -0.45, -0.08)
    downside = _scale(-metrics["downside_volatility_126"], -0.38, -0.10)
    cvar = _scale(metrics["cvar_95_126"], -0.06, -0.015)
    recovery = _scale(-metrics["recovery_days_126"], -189, -30)
    return _clamp(
        drawdown * 0.35 + downside * 0.25 + cvar * 0.25 + recovery * 0.15,
        0,
        100,
    )


def _stability_score(metrics: dict) -> float:
    positive = _scale(metrics["positive_63_ratio"], 0.40, 0.70)
    sortino = _scale(metrics["sortino"], -0.5, 2.0)
    volatility = _scale(-metrics["annual_volatility"], -0.45, -0.15)
    return _clamp(positive * 0.45 + sortino * 0.35 + volatility * 0.20, 0, 100)


def _risk_adjusted_score(metrics: dict) -> float:
    """Reward return per unit of downside instead of rewarding raw beta."""
    sortino = _scale(metrics["sortino"], -0.5, 2.0)
    calmar = _scale(metrics["calmar"], -0.2, 1.5)
    downside = _scale(-metrics["downside_volatility_126"], -0.38, -0.10)
    cvar = _scale(metrics["cvar_95_126"], -0.06, -0.015)
    return _clamp(
        sortino * 0.35 + calmar * 0.25 + downside * 0.20 + cvar * 0.20,
        0,
        100,
    )


def _market_defense_score(probabilities: Dict[str, float]) -> float:
    return _clamp(
        probabilities.get("risk_on", 0) * 85
        + probabilities.get("neutral", 0) * 65
        + probabilities.get("stress", 0) * 25,
        0,
        100,
    )


def _market_position_multiplier(
    regime_state: str, probabilities: Dict[str, float]
) -> float:
    """Use the regime as a position gate instead of letting it distort ranking."""
    stress = probabilities.get("stress", 0.0)
    if regime_state == "压力/危机" and stress >= 0.55:
        return 0.0
    if regime_state == "震荡/中性":
        return 0.65
    return 1.0


def _pullback_score(metrics: dict) -> float:
    drawdown = _triangle(metrics["drawdown_60"], -0.25, -0.07, 0.01)
    zscore = _triangle(metrics["zscore_60"], -2.5, -0.7, 1.3)
    percentile = _triangle(metrics["percentile_252"], 0.03, 0.32, 0.82)
    return drawdown * 0.45 + zscore * 0.35 + percentile * 0.20


def _market_regime_score(
    market_snapshot: dict, news: List[object]
) -> tuple[float, str, Dict[str, float], List[str]]:
    indexes = {item.get("symbol"): item for item in market_snapshot.get("indexes", [])}
    ixic = indexes.get("IXIC", {})
    dji = indexes.get("DJI", {})
    ixic_20 = float(ixic.get("ma20_gap_pct", 0))
    ixic_60 = float(ixic.get("ma60_gap_pct", 0))
    dji_60 = float(dji.get("ma60_gap_pct", 0))
    relevant = [
        item for item in news
        if getattr(item, "tag", "") in {"美元利率", "政策与地缘风险", "AI与科技龙头", "汇率变化"}
    ]
    positive = sum(1 for item in relevant if getattr(item, "direction", "") == "偏利多")
    negative = sum(1 for item in relevant if getattr(item, "direction", "") == "偏利空")
    news_balance = positive - negative

    # News remains an explanatory overlay until point-in-time history is available.
    logits = {
        "risk_on": ixic_60 / 5 + dji_60 / 7 - max(ixic_20 - 6, 0) / 3,
        "neutral": 0.55 - abs(ixic_60) / 12 - abs(ixic_20) / 16,
        "stress": -ixic_60 / 4.5 - dji_60 / 7 + max(-ixic_20 - 3, 0) / 3,
    }
    probabilities = _softmax(logits)
    state_key = max(probabilities, key=probabilities.get)
    state_names = {
        "risk_on": "风险偏好上升",
        "neutral": "震荡/中性",
        "stress": "压力/危机",
    }
    score = (
        probabilities["risk_on"] * 86
        + probabilities["neutral"] * 62
        + probabilities["stress"] * 25
    )
    reasons = [
        f"三状态模型判定为{state_names[state_key]}：风险偏好 {probabilities['risk_on']:.1%}、中性 {probabilities['neutral']:.1%}、压力 {probabilities['stress']:.1%}。",
        f"纳斯达克相对60日均线 {ixic_60:+.1f}%，相对20日均线 {ixic_20:+.1f}%；道琼斯相对60日均线 {dji_60:+.1f}%。",
        f"相关宏观与科技新闻中，偏利多 {positive} 条、偏利空 {negative} 条。",
    ]
    return score, state_names[state_key], probabilities, reasons


def _holding_score(
    holding: dict | None,
    news: List[object],
    stock_trends: Dict[str, dict] | None = None,
    as_of: str | None = None,
) -> tuple[float, List[str], float, int, float, float, float]:
    if not holding or not holding.get("holdings"):
        return (
            20.0,
            ["缺少可验证的前十大持仓，持仓景气评分被显著下调。"],
            1.0,
            999,
            0.0,
            50.0,
            0.0,
        )
    stock_trends = stock_trends or {}
    live_stock_sources = live_source_names("stock_trends")
    items = holding["holdings"][:10]
    concentration = sum(float(item.get("weight", 0)) for item in items)
    holding_age = _days_since(holding.get("disclosure_date", ""), as_of)
    recency_score = math.exp(-holding_age / 120) * 100
    concentration_score = _triangle(concentration, 0.18, 0.38, 0.70)
    sentiment_value = 50.0
    matched_weight = 0.0
    for item in items:
        for news_item in news:
            text = f"{getattr(news_item, 'title', '')} {getattr(news_item, 'summary', '')}".lower()
            item_name = str(item.get("name", "")).lower()
            item_code = str(item.get("code", "")).lower()
            if (not item_name or item_name not in text) and (not item_code or item_code not in text):
                continue
            direction = getattr(news_item, "direction", "中性观察")
            weight = float(item.get("weight", 0))
            sentiment_value += weight * (80 if direction == "偏利多" else -80 if direction == "偏利空" else 0)
            matched_weight += weight
    sentiment_value = _clamp(sentiment_value, 20, 80)

    covered_weight = 0.0
    weighted_stock_score = 0.0
    for item in items:
        weight = float(item.get("weight", 0))
        trend = stock_trends.get(str(item.get("code", "")), {})
        if trend.get("source") not in live_stock_sources:
            continue
        covered_weight += weight
        trend_score = _clamp(
            55
            + float(trend.get("ma60_gap", 0)) * 220
            + float(trend.get("ma120_gap", 0)) * 140
            + float(trend.get("max_drawdown_63", 0)) * 100,
            0,
            100,
        )
        weighted_stock_score += weight * trend_score
    stock_trend_coverage = covered_weight / max(concentration, 1e-9)
    uncovered_weight = max(concentration - covered_weight, 0.0)
    stock_trend_score = (
        (weighted_stock_score + uncovered_weight * 20.0) / concentration
        if concentration
        else 20.0
    )
    score = (
        stock_trend_score * 0.40
        + sentiment_value * 0.25
        + concentration_score * 0.20
        + recency_score * 0.15
    )
    top_names = "、".join(str(item.get("name", "--")) for item in items[:3])
    reasons = [
        f"最新披露前十大持仓包括 {top_names}，合计占净值 {concentration:.1%}。",
        f"有效重仓股行情覆盖 {stock_trend_coverage:.1%}，趋势评分 {stock_trend_score:.1f}。",
        f"持仓披露距规则日 {holding_age} 天；新闻直接匹配权重约 {matched_weight:.1%}，情绪评分 {sentiment_value:.1f}。",
    ]
    return (
        score,
        reasons,
        concentration,
        holding_age,
        stock_trend_coverage,
        sentiment_value,
        matched_weight,
    )


def _portfolio_fit(candidate: dict | None, current: dict | None) -> tuple[float, float, str]:
    if not candidate or not current:
        return 55.0, 0.0, "持仓重合数据不完整，组合适配采用中性分。"
    current_weights = {item["code"]: item["weight"] for item in current.get("holdings", [])}
    overlap = sum(
        min(item["weight"], current_weights.get(item["code"], 0))
        for item in candidate.get("holdings", [])
    )
    score = _clamp(100 - overlap / 0.65 * 100, 0, 100)
    return score, overlap, f"与现有018229前十大持仓重合度约 {overlap:.1%}。"


def _confidence_score(
    quote: object,
    history: dict,
    holding: dict | None,
    holding_age: int,
    market_snapshot: dict,
    news: List[object],
    quality_item: dict | None = None,
) -> float:
    if quality_item and quality_item.get("confidence") is not None:
        return _clamp(float(quality_item["confidence"]), 0, 100)
    history_count = len(history.get("records", []))
    history_points = min(history_count / 700, 1) * 25
    history_source = 10 if is_live_source(history.get("source", "fallback"), "fund_history") else 3
    quote_score = (
        10
        if is_live_source(getattr(quote, "source", "fallback"), "fund_nav")
        else 2
    )
    indexes = market_snapshot.get("indexes", [])
    fresh_indexes = sum(
        1
        for item in indexes
        if is_live_source(item.get("source", "fallback"), "indexes")
    )
    market_score = (fresh_indexes / max(len(indexes), 1)) * 15
    real_news = sum(1 for item in news if getattr(item, "source", "").startswith("东方财富"))
    news_score = min(real_news / 6, 1) * 10
    holding_source = (
        15
        if holding and is_live_source(holding.get("source", "fallback"), "holdings")
        else 3
    )
    holding_recency = _clamp(10 - max(holding_age - 90, 0) / 18, 0, 10)
    metadata_score = 10
    raw_score = (
        history_points
        + history_source
        + quote_score
        + market_score
        + news_score
        + holding_source
        + holding_recency
        + metadata_score
    )
    return _clamp(raw_score, 0, 100)


def _risk_penalty(
    metrics: dict, concentration: float, confidence: float
) -> tuple[float, float]:
    target_volatility = 0.15
    volatility_multiplier = _clamp(
        target_volatility / max(metrics["annual_volatility"], 0.01), 0.35, 1.0
    )
    cvar_penalty = _scale(abs(metrics["cvar_95"]), 0.015, 0.06) * 0.08
    drawdown_penalty = _scale(abs(metrics["max_drawdown"]), 0.15, 0.60) * 0.07
    concentration_penalty = _scale(concentration, 0.35, 0.75) * 0.04
    uncertainty_penalty = (100 - confidence) * 0.05
    volatility_penalty = (1 - volatility_multiplier) * 6
    penalty = (
        cvar_penalty
        + drawdown_penalty
        + concentration_penalty
        + uncertainty_penalty
        + volatility_penalty
    )
    return penalty, volatility_multiplier


def _update_signal_streak(candidate: dict, eligible: bool, market_snapshot: dict) -> int:
    QUANT_SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    previous = {}
    try:
        previous = json.loads(QUANT_SIGNAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    prices = ",".join(
        f"{item.get('symbol')}:{item.get('price')}" for item in market_snapshot.get("indexes", [])
    )
    signature = f"{MODEL_VERSION}:{candidate['data_signature']}:{prices}"
    streak = 0
    if eligible:
        if (
            previous.get("model_version") == MODEL_VERSION
            and previous.get("code") == candidate["code"]
        ):
            streak = int(previous.get("streak", 0))
            if previous.get("signature") != signature:
                streak += 1
        else:
            streak = 1
    payload = {
        "model_version": MODEL_VERSION,
        "code": candidate["code"] if eligible else "",
        "signature": signature if eligible else "",
        "streak": streak,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    atomic_write_json(QUANT_SIGNAL_FILE, payload)
    return streak


def _days_since(value: str, as_of: str | None = None) -> int:
    try:
        reference = (
            datetime.strptime(as_of, "%Y-%m-%d").date()
            if as_of
            else date.today()
        )
        return max((reference - datetime.strptime(value, "%Y-%m-%d").date()).days, 0)
    except Exception:
        return 999


def _scale(value: float, low: float, high: float) -> float:
    if high == low:
        return 50.0
    return _clamp((value - low) / (high - low) * 100, 0, 100)


def _triangle(value: float, low: float, ideal: float, high: float) -> float:
    if value <= low or value >= high:
        return 0.0
    if value == ideal:
        return 100.0
    if value < ideal:
        return (value - low) / (ideal - low) * 100
    return (high - value) / (high - ideal) * 100


def _softmax(values: Dict[str, float]) -> Dict[str, float]:
    maximum = max(values.values())
    exponentials = {key: math.exp(value - maximum) for key, value in values.items()}
    total = sum(exponentials.values())
    return {key: value / total for key, value in exponentials.items()}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _empty_result(reasons: List[str]) -> dict:
    return {
        "model_version": MODEL_VERSION,
        "is_recommended": False,
        "is_strong_buy": False,
        "verdict": "当前无强烈建议买入标的",
        "total_score": 0.0,
        "confidence": 0.0,
        "failed_conditions": reasons,
        "all_candidates": [],
        "scored_candidate_count": 0,
        "data_errors": [],
        "portfolio_risk_budget": {},
    }
