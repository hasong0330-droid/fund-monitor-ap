from __future__ import annotations

import math
import statistics
from datetime import date
from typing import Iterable


FACTOR_WEIGHTS = {
    "fundamental": 0.40,
    "macro": 0.20,
    "technical": 0.20,
    "industry": 0.10,
    "other": 0.10,
}

INDUSTRY_TAGS = {
    "018229": {"AI与科技龙头", "先进制造"},
    "005698": {"AI与科技龙头"},
    "118001": {"AI与科技龙头", "港股互联网", "先进制造"},
    "005534": {"AI与科技龙头", "先进制造"},
    "161815": {"美元利率", "汇率变化"},
    "016701": {"AI与科技龙头", "美元利率"},
    "162415": {"美元利率", "汇率变化"},
    "017653": {"AI与科技龙头"},
    # Current designated analysis pool. Tags drive industry-news matching;
    # keep them explicit so replacing the pool cannot silently lose coverage.
    "016702": {"AI与科技龙头", "先进制造", "美元利率"},
    "378006": {"新兴市场", "AI与科技龙头", "港股互联网", "美元利率"},
    "270023": {"AI与科技龙头", "先进制造", "美元利率"},
    "378546": {"大宗商品", "能源资源", "美元利率", "汇率变化"},
    "160216": {"大宗商品", "能源资源", "美元利率"},
    "005976": {"资产配置", "美元利率"},
}

# Metric profiles avoid applying one-size-fits-all thresholds to very
# different fund styles.  The ranges remain deliberately broad until a
# point-in-time calibration dataset is available.
FUNDAMENTAL_PROFILES = {
    "technology": {
        "revenue_yoy": (0.20, -0.10, 0.35),
        "net_income_yoy": (0.25, -0.25, 0.50),
    },
    "resource": {
        "revenue_yoy": (0.15, -0.25, 0.35),
        "net_income_yoy": (0.20, -0.45, 0.60),
        "net_margin": (0.10, -0.05, 0.30),
        "operating_margin": (0.15, -0.05, 0.35),
        "free_cash_flow_margin": (0.20, -0.15, 0.30),
        "roe": (0.20, -0.05, 0.35),
    },
    "balanced": {},
}

FUNDAMENTAL_PROFILE_BY_FUND = {
    "016702": "technology",
    "005534": "technology",
    "270023": "balanced",
    "378006": "balanced",
    "018229": "balanced",
    "378546": "resource",
    "160216": "resource",
    "005976": "fof",
}

SEVERE_EVENT_TERMS = (
    "财务造假",
    "监管立案",
    "暂停赎回",
    "下调指引",
    "业绩预警",
    "制裁",
    "出口管制",
    "断供",
    "基金经理离任",
)


def evaluate_fund_factors(
    code: str,
    history: dict,
    holding: dict | None,
    market_snapshot: dict,
    news: Iterable[object],
    stock_data: dict[str, dict] | None = None,
) -> dict:
    news_items = list(news)
    stock_data = stock_data or {}
    fundamental = _fundamental_factor(holding or {}, stock_data, code)
    macro = _macro_factor(market_snapshot, news_items)
    technical = _technical_factor(history)
    industry = _industry_factor(code, holding or {}, news_items)
    other = _other_factor(code, holding or {}, news_items)
    factors = {
        "fundamental": fundamental,
        "macro": macro,
        "technical": technical,
        "industry": industry,
        "other": other,
    }
    total_score = sum(
        factors[key]["score"] * weight for key, weight in FACTOR_WEIGHTS.items()
    )
    confidence = sum(
        factors[key]["coverage"] * weight for key, weight in FACTOR_WEIGHTS.items()
    ) * 100
    return {
        "model": "Five-Factor Fundamental-Led v4.0",
        "weights": FACTOR_WEIGHTS,
        "scores": {key: round(value["score"], 1) for key, value in factors.items()},
        "coverage": {key: round(value["coverage"], 4) for key, value in factors.items()},
        "contributions": {
            key: round(factors[key]["score"] * weight, 1)
            for key, weight in FACTOR_WEIGHTS.items()
        },
        "total_score": round(_clamp(total_score, 0, 100), 1),
        "confidence": round(_clamp(confidence, 0, 100), 1),
        "details": factors,
        "hard_veto": bool(other.get("hard_veto")),
        "hard_veto_reason": other.get("hard_veto_reason", ""),
        "technical_flags": technical.get("flags", {}),
    }


def _fundamental_factor(
    holding: dict, stock_data: dict[str, dict], fund_code: str | None = None
) -> dict:
    if holding.get("allocation_only"):
        return {
            "score": 50.0,
            "coverage": 0.0,
            "covered_weight": 0.0,
            "top10_weight": 0.0,
            "latest_filed": "--",
            "companies": [],
            "applicable": False,
            "reason": (
                "FOF资产配置基金不直接披露个股企业暴露，企业财报覆盖不适用；"
                "仅按资产配置、宏观和净值趋势保守分析。"
            ),
        }
    items = holding.get("holdings", [])[:10]
    profile_name = FUNDAMENTAL_PROFILE_BY_FUND.get(fund_code or "", "balanced")
    total_weight = sum(float(item.get("weight", 0)) for item in items)
    covered_weight = 0.0
    weighted_score = 0.0
    companies = []
    latest_filed = ""
    for item in items:
        weight = float(item.get("weight", 0))
        fundamentals = (stock_data.get(str(item.get("code", ""))) or {}).get(
            "fundamentals", {}
        )
        metrics = fundamentals.get("metrics") or {}
        score, metric_coverage = _company_fundamental_score(metrics, profile_name)
        source = fundamentals.get("source")
        if metric_coverage < 0.50 or source not in {
            "sec-companyfacts",
            "eastmoney-financials",
            "naver-financials",
            "ifind-financials",
            "cache",
        }:
            continue
        covered_weight += weight
        weighted_score += weight * score
        latest_filed = max(latest_filed, str(fundamentals.get("filed_at", "")))
        companies.append(
            {
                "code": str(item.get("code", "")),
                "name": str(item.get("name", "")),
                "weight": round(weight, 4),
                "score": round(score, 1),
                "metric_coverage": round(metric_coverage, 4),
                "fiscal_end": fundamentals.get("fiscal_end", "--"),
                "filed_at": fundamentals.get("filed_at", "--"),
                "source": source,
                # Preserve audited inputs so any later reduction decision can
                # cite the actual report-period metrics instead of a score.
                "metrics": {
                    key: round(float(value), 6)
                    for key, value in metrics.items()
                    if isinstance(value, (int, float)) and math.isfinite(float(value))
                },
            }
        )
    coverage = covered_weight / total_weight if total_weight else 0.0
    score = weighted_score / covered_weight if covered_weight else 50.0
    return {
        "score": round(_clamp(score, 0, 100), 1),
        "coverage": round(_clamp(coverage, 0, 1), 4),
        "covered_weight": round(covered_weight, 4),
        "top10_weight": round(total_weight, 4),
        "latest_filed": latest_filed or "--",
        "companies": companies,
        "applicable": True,
        "profile": profile_name,
        "reason": (
            f"已按披露持仓权重分析 {len(companies)} 家企业，"
            f"覆盖前十大持仓权重 {coverage:.1%}。"
        ),
    }


def _company_fundamental_score(
    metrics: dict, profile: str = "balanced"
) -> tuple[float, float]:
    default_definitions = {
        "revenue_yoy": (0.20, -0.10, 0.25),
        "net_income_yoy": (0.25, -0.20, 0.35),
        "net_margin": (0.10, 0.00, 0.25),
        "operating_margin": (0.15, 0.00, 0.30),
        "free_cash_flow_margin": (0.15, -0.05, 0.25),
        "roe": (0.15, 0.00, 0.30),
    }
    overrides = FUNDAMENTAL_PROFILES.get(profile, {})
    definitions = tuple(
        (key, *(overrides.get(key) or default_definitions[key]))
        for key in default_definitions
    )
    weighted = 0.0
    available_weight = 0.0
    available_count = 0
    for key, weight, low, high in definitions:
        value = metrics.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            continue
        weighted += _scale(float(value), low, high) * weight
        available_weight += weight
        available_count += 1
    score = weighted / available_weight if available_weight else 50.0
    return score, available_count / len(definitions)


def _macro_factor(market_snapshot: dict, news: list[object]) -> dict:
    factors = {item.get("key"): item for item in market_snapshot.get("macro_factors", [])}
    scores: list[tuple[float, float, str]] = []

    def add(key: str, weight: float, scorer, field: str = "value") -> None:
        item = factors.get(key) or {}
        value = item.get(field)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            scores.append((float(scorer(float(value))), weight, key))

    # 金融市场变量用于识别估值压力和风险偏好。
    add("VIX", 0.12, lambda value: 100 - _scale(value, 15, 35))
    add("US2Y", 0.07, lambda value: 100 - _scale(value, 3.0, 5.0))
    add("US10Y", 0.06, lambda value: 100 - _scale(value, 3.5, 5.0))
    add("US10Y_REAL", 0.07, lambda value: 100 - _scale(value, 1.0, 2.5))
    add("DXY", 0.05, lambda value: 100 - _scale(value, 0.5, 3.0), "change_20_pct")
    add("USDCNY", 0.03, lambda value: 100 - _scale(abs(value), 0.5, 3.0), "change_20_pct")

    # 经济数据用于判断增长、通胀、就业和流动性，权重高于单一技术指标。
    gdp = factors.get("US_GDP") or {}
    if gdp.get("frequency") == "quarterly":
        add("US_GDP", 0.15, lambda value: _band_score(value, 1.0, 3.0, -2.0, 5.0))
    add(
        "US_CPI",
        0.10,
        lambda value: _band_score(value, 1.5, 3.0, 0.0, 6.0),
        "change_12_pct",
    )
    add(
        "US_UNEMPLOYMENT",
        0.08,
        lambda value: _band_score(value, 3.5, 5.0, 3.0, 8.0),
    )
    add(
        "US_M2",
        0.10,
        lambda value: _band_score(value, 2.0, 7.0, -2.0, 12.0),
        "change_12_pct",
    )
    add(
        "US_FED_FUNDS",
        0.05,
        lambda value: _band_score(value, 2.0, 4.5, 0.0, 6.0),
    )
    credit = factors.get("US_HY_SPREAD") or {}
    credit_proxy = factors.get("US_CREDIT_STRESS_PROXY") or {}
    if _numeric(credit.get("value")) and not credit.get("proxy"):
        add(
            "US_HY_SPREAD",
            0.10,
            lambda value: 100 - _scale(value, 2.5, 6.5),
        )
    elif _numeric(credit_proxy.get("value")):
        # The proxy is directionally useful but not comparable to OAS, so it
        # receives less weight and a separate monotonic stress score.
        add(
            "US_CREDIT_STRESS_PROXY",
            0.06,
            lambda value: 100 - _scale(value, 1.0, 8.0),
        )
    relevant = [
        item
        for item in news
        if getattr(item, "tag", "") in {"美元利率", "汇率变化", "政策与地缘风险"}
    ]
    news_score = _direction_score(relevant)
    if relevant:
        scores.append((news_score, 0.02, "macro_news"))
    available_weight = sum(weight for _, weight, _ in scores)
    score = (
        sum(value * weight for value, weight, _ in scores) / available_weight
        if available_weight
        else 50.0
    )
    core_keys = {
        "US_GDP", "US_CPI", "US_UNEMPLOYMENT", "US_M2", "US_FED_FUNDS",
        "US_HY_SPREAD", "US_CREDIT_STRESS_PROXY",
    }
    core_weight = sum(weight for _, weight, key in scores if key in core_keys)
    core_total_weight = 0.15 + 0.10 + 0.08 + 0.10 + 0.05 + 0.10
    market_weight = sum(weight for _, weight, key in scores if key not in core_keys and key != "macro_news")
    market_total_weight = 0.12 + 0.07 + 0.06 + 0.07 + 0.05 + 0.03
    return {
        "score": round(_clamp(score, 0, 100), 1),
        "coverage": round(_clamp(available_weight, 0, 1), 4),
        "economic_coverage": round(_clamp(core_weight / core_total_weight, 0, 1), 4),
        "market_coverage": round(_clamp(market_weight / market_total_weight, 0, 1), 4),
        "available_keys": [key for _, _, key in scores],
        "reason": (
            f"宏观总指标覆盖 {available_weight:.0%}；经济数据覆盖 "
            f"{core_weight / core_total_weight:.0%}，市场风险指标覆盖 "
            f"{market_weight / market_total_weight:.0%}；相关政策与利率新闻 {len(relevant)} 条。"
        ),
    }


def _technical_factor(history: dict) -> dict:
    records = sorted(history.get("records", []), key=lambda item: item.get("date", ""))
    navs = [float(item["nav"]) for item in records if float(item.get("nav", 0)) > 0]
    if len(navs) < 120:
        return {
            "score": 50.0,
            "coverage": 0.0,
            "flags": {"below_ma60": False, "below_ma120": False},
            "reason": "历史净值不足120个有效周期。",
        }
    current = navs[-1]
    ma60 = statistics.fmean(navs[-60:])
    ma120 = statistics.fmean(navs[-120:])
    returns = [navs[index] / navs[index - 1] - 1 for index in range(len(navs) - 62, len(navs))]
    volatility = statistics.pstdev(returns) * math.sqrt(252)
    drawdown = current / max(navs[-63:]) - 1
    momentum20 = current / navs[-21] - 1
    trend = (_scale(current / ma60 - 1, -0.12, 0.08) * 0.55 + _scale(current / ma120 - 1, -0.15, 0.12) * 0.45)
    defense = 100 - _scale(abs(min(drawdown, 0)), 0.05, 0.30)
    stability = 100 - _scale(volatility, 0.12, 0.50)
    heat = 100 - _scale(max(momentum20, 0), 0.05, 0.15)
    technical_score = trend * 0.35 + defense * 0.30 + stability * 0.25 + heat * 0.10
    score = technical_score * 0.75 + 50.0 * 0.25
    return {
        "score": round(_clamp(score, 0, 100), 1),
        "coverage": 0.75,
        "technical_score": round(technical_score, 1),
        "fund_flow_available": False,
        "flags": {
            "below_ma60": current < ma60,
            "below_ma120": current < ma120,
            "ma60_gap": round(current / ma60 - 1, 6),
            "ma120_gap": round(current / ma120 - 1, 6),
        },
        "reason": (
            f"净值相对60/120日均线 {current / ma60 - 1:+.1%}/{current / ma120 - 1:+.1%}，"
            f"近63日距高点 {drawdown:.1%}；尚无可靠开放式基金资金流数据，资金面按中性处理。"
        ),
    }


def _industry_factor(code: str, holding: dict, news: list[object]) -> dict:
    tags = INDUSTRY_TAGS.get(code, set())
    names = [str(item.get("name", "")).lower() for item in holding.get("holdings", [])[:10]]
    relevant = []
    for item in news:
        text = f"{getattr(item, 'title', '')} {getattr(item, 'summary', '')}".lower()
        if getattr(item, "tag", "") in tags or any(name and name in text for name in names):
            relevant.append(item)
    return {
        "score": round(_direction_score(relevant), 1),
        "coverage": min(len(relevant) / 4, 1.0),
        "news_count": len(relevant),
        "reason": f"匹配基金行业与重仓企业的景气新闻 {len(relevant)} 条。",
    }


def _other_factor(code: str, holding: dict, news: list[object]) -> dict:
    names = [str(item.get("name", "")).lower() for item in holding.get("holdings", [])[:10]]
    negative = []
    severe_sources: set[str] = set()
    severe_titles = []
    for item in news:
        if getattr(item, "direction", "") != "偏利空":
            continue
        text = f"{getattr(item, 'title', '')} {getattr(item, 'summary', '')}".lower()
        affected = code in str(getattr(item, "affected_funds", ""))
        company_match = any(name and name in text for name in names)
        if not affected and not company_match:
            continue
        negative.append(item)
        if any(term in text for term in SEVERE_EVENT_TERMS):
            severe_sources.add(_source_family(str(getattr(item, "source", ""))))
            severe_titles.append(str(getattr(item, "title", "")))
    hard_veto = len({source for source in severe_sources if source}) >= 2
    score = _clamp(100 - min(len(negative), 5) * 12 - (35 if hard_veto else 0), 0, 100)
    source_count = len(
        {_source_family(str(getattr(item, "source", ""))) for item in news if getattr(item, "source", "")}
    )
    return {
        "score": round(score, 1),
        "coverage": min(source_count / 2, 1.0),
        "negative_count": len(negative),
        "hard_veto": hard_veto,
        "hard_veto_reason": (
            "重大负面事件已由两个独立来源确认：" + "；".join(severe_titles[:2])
            if hard_veto
            else ""
        ),
        "reason": f"直接影响基金或重仓企业的负面信息 {len(negative)} 条。",
    }


def _direction_score(items: list[object]) -> float:
    positive = sum(1 for item in items if getattr(item, "direction", "") == "偏利多")
    negative = sum(1 for item in items if getattr(item, "direction", "") == "偏利空")
    return _clamp(50 + positive * 8 - negative * 10, 10, 90)


def _source_family(source: str) -> str:
    return source.split("·", 1)[0].strip().lower()


def _scale(value: float, low: float, high: float) -> float:
    if high == low:
        return 50.0
    return _clamp((value - low) / (high - low) * 100, 0, 100)


def _band_score(value: float, ideal_low: float, ideal_high: float, low: float, high: float) -> float:
    """给宏观变量设置中性/健康区间，避免单调函数误判极低增长或极低通胀。"""
    if ideal_low <= value <= ideal_high:
        return 100.0
    if value < ideal_low:
        return _scale(value, low, ideal_low)
    return 100.0 - _scale(value, ideal_high, high)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))
