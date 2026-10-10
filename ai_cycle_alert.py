"""AI infrastructure spending-cycle warning model.

The model deliberately separates evidence coverage from risk. Missing evidence
is never interpreted as a healthy cycle, and a shorter delivery lead time is
not treated as demand destruction unless another demand/price signal agrees.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from typing import Iterable
from urllib.parse import urlparse


AI_CYCLE_WEIGHTS = {
    "cloud_capex": 0.25,
    "delivery_lead_time": 0.15,
    "memory_price": 0.15,
    "ai_economics": 0.25,
    "earnings_guidance": 0.20,
}

AI_CYCLE_DIMENSIONS = {
    "cloud_capex": {
        "label": "云厂商资本开支",
        "negative": (
            "下调资本开支", "削减资本开支", "缩减资本开支", "放缓资本开支",
            "资本开支下降", "资本支出下调", "capex cut", "reduce capex",
        ),
        "positive": ("上调资本开支", "增加资本开支", "资本开支增长", "capex raised"),
    },
    "delivery_lead_time": {
        "label": "HBM/GPU交期",
        "negative": (
            "交期缩短", "交付周期缩短", "供货周期缩短", "现货供应充足",
            "排队时间缩短", "取消订单", "gpu库存增加", "hbm库存增加",
        ),
        "positive": ("交期延长", "供不应求", "订单积压增加", "现货溢价扩大"),
    },
    "memory_price": {
        "label": "存储价格",
        "negative": (
            "存储价格转跌", "存储价格下跌", "dram价格下跌", "nand价格下跌",
            "hbm价格下跌", "合约价下跌", "现货价下跌", "存储降价",
        ),
        "positive": ("存储价格上涨", "dram价格上涨", "nand价格上涨", "hbm价格上涨", "合约价上涨"),
    },
    "ai_economics": {
        "label": "AI商业回报",
        "negative": (
            "ai收入增速低于", "ai收入不及", "ai收入放缓", "折旧压力",
            "融资成本上升", "投资回报不及", "变现低于预期", "自由现金流承压",
            "depreciation pressure", "financing cost",
        ),
        "positive": ("ai收入超预期", "ai收入增长", "ai变现加速", "自由现金流增长"),
    },
    "earnings_guidance": {
        "label": "龙头盈利指引",
        "negative": (
            "下调指引", "指引下修", "收入指引下调", "盈利指引下调",
            "毛利率指引下调", "订单指引下调", "业绩预警", "低于预期指引",
        ),
        "positive": ("上调指引", "指引上修", "收入指引上调", "盈利指引上调"),
    },
}

AI_CONTEXT_TERMS = (
    "ai", "人工智能", "云计算", "数据中心", "gpu", "hbm", "dram", "nand",
    "英伟达", "微软", "亚马逊", "谷歌", "meta", "甲骨文", "台积电", "博通",
    "美光", "海力士", "三星", "amd", "超威半导体",
)

AI_COMPANIES = {
    "MSFT": ("微软", "microsoft"),
    "AMZN": ("亚马逊", "amazon", "aws"),
    "GOOGL": ("谷歌", "alphabet", "google"),
    "META": ("meta", "脸书"),
    "ORCL": ("甲骨文", "oracle"),
    "NVDA": ("英伟达", "nvidia"),
    "AMD": ("超威半导体", "amd"),
    "AVGO": ("博通", "broadcom"),
    "TSM": ("台积电", "tsmc"),
    "MU": ("美光", "micron"),
    "000660": ("sk海力士", "海力士", "sk hynix"),
    "005930": ("三星电子", "samsung"),
    "285A": ("kioxia", "铠侠"),
}

AI_HOLDING_CODES = {
    "MSFT", "AMZN", "GOOG", "GOOGL", "META", "ORCL", "NVDA", "AMD", "AVGO",
    "TSM", "MU", "INTC", "AMAT", "LRCX", "ASML", "SNDK", "STM", "ALAB",
    "LITE", "ONTO", "000660", "005930", "285A", "00700", "09988", "01810",
    "688256", "688041", "300308", "002371", "603986", "688012", "688008",
}

AI_HOLDING_NAME_TERMS = (
    "英伟达", "微软", "亚马逊", "谷歌", "meta", "台积电", "海力士", "三星电子",
    "美光", "超威半导体", "博通", "英特尔", "阿斯麦", "应用材料", "拉姆研究",
    "闪迪", "kioxia", "铠侠", "寒武纪", "海光", "中际旭创", "光模块", "芯片",
)

OFFICIAL_SOURCE_TERMS = (
    "公司公告", "官方", "投资者关系", "sec filing", "交易所公告",
)

OFFICIAL_LINK_HOSTS = (
    "sec.gov", "microsoft.com", "amazon.com", "abc.xyz", "meta.com",
    "oracle.com", "nvidia.com", "amd.com", "broadcom.com", "tsmc.com",
    "micron.com", "skhynix.com", "samsung.com",
)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _value(item: object, key: str, default: object = "") -> object:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _parse_date(value: object) -> date | None:
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", str(value or ""))
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def _source_family(source: object) -> str:
    text = str(source or "").strip()
    if not text or text.lower() in {"cache", "baseline", "缓存", "离线观察项"}:
        return ""
    # Aggregators retain the original publisher after the separator.
    return text.split("·", 1)[-1].strip().lower()


def _has_ai_context(text: str) -> bool:
    if re.search(r"(?<![a-z0-9])ai(?![a-z0-9])", text):
        return True
    return any(term in text for term in AI_CONTEXT_TERMS if term != "ai")


def _is_official_evidence(source: object, link: object) -> bool:
    source_text = str(source or "").lower()
    if any(term in source_text for term in OFFICIAL_SOURCE_TERMS):
        return True
    try:
        host = (urlparse(str(link or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in OFFICIAL_LINK_HOSTS)


def _companies(text: str) -> list[str]:
    return sorted(
        code for code, aliases in AI_COMPANIES.items() if any(alias in text for alias in aliases)
    )


def _exposure_for_fund(fund: dict) -> dict:
    if fund.get("allocation_only"):
        return {
            "applicable": False,
            "observed_exposure": None,
            "disclosed_top10_weight": 0.0,
            "level": "不适用",
            "reason": "FOF资产配置未直接披露个股，AI周期暴露不可直接判定。",
        }
    items = (fund.get("holdings") or [])[:10]
    total = sum(float(item.get("weight", 0) or 0) for item in items)
    exposed = 0.0
    names = []
    for item in items:
        code = str(item.get("code", "")).strip().upper()
        name = str(item.get("name", "")).strip()
        text = name.lower()
        if code in AI_HOLDING_CODES or any(term in text for term in AI_HOLDING_NAME_TERMS):
            weight = float(item.get("weight", 0) or 0)
            exposed += weight
            if weight > 0:
                names.append(f"{name or code} {weight:.1%}")
    level = "很高" if exposed >= 0.40 else "高" if exposed >= 0.25 else "中" if exposed >= 0.10 else "低"
    return {
        "applicable": True,
        "observed_exposure": round(exposed, 4),
        "disclosed_top10_weight": round(total, 4),
        "level": level,
        "holdings": names[:5],
        "reason": f"按最新披露前十大持仓观察到AI产业链暴露 {exposed:.1%}。",
    }


def build_ai_cycle_alert(
    news: Iterable[object],
    holdings_by_fund: dict[str, dict],
    *,
    today: date | None = None,
) -> dict:
    today = today or date.today()
    cutoff = today - timedelta(days=120)
    dimensions: dict[str, dict] = {}
    all_sources: set[str] = set()
    all_evidence: list[dict] = []

    for key, definition in AI_CYCLE_DIMENSIONS.items():
        evidence = []
        for item in news:
            title = str(_value(item, "title", "") or "")
            summary = str(_value(item, "summary", "") or "")
            text = f"{title} {summary}".lower()
            if not _has_ai_context(text):
                continue
            observed_at = _parse_date(_value(item, "published_at", ""))
            if not observed_at or observed_at < cutoff or observed_at > today:
                continue
            negative = sorted(term for term in definition["negative"] if term in text)
            positive = sorted(term for term in definition["positive"] if term in text)
            if not negative and not positive:
                continue
            source_text = str(_value(item, "source", "") or "")
            family = _source_family(source_text)
            if not family:
                continue
            record = {
                "dimension": key,
                "title": title,
                "source": source_text,
                "source_family": family,
                "published_at": observed_at.isoformat(),
                "direction": "risk" if negative else "support",
                "terms": negative or positive,
                "companies": _companies(text),
                "official": _is_official_evidence(source_text, _value(item, "link", "")),
                "link": str(_value(item, "link", "") or ""),
            }
            evidence.append(record)

        negative_rows = [item for item in evidence if item["direction"] == "risk"]
        positive_rows = [item for item in evidence if item["direction"] == "support"]
        negative_sources = {item["source_family"] for item in negative_rows}
        positive_sources = {item["source_family"] for item in positive_rows}
        companies = {company for item in negative_rows for company in item["companies"]}
        official = any(item["official"] for item in negative_rows)
        available = bool(evidence)
        risk = 0.0
        if available:
            risk = _clamp(
                20
                + 25 * len(negative_sources)
                + 8 * max(0, len(negative_rows) - len(negative_sources))
                + 8 * max(0, len(companies) - 1)
                + (12 if official else 0)
                - 18 * len(positive_sources)
            )
        active = risk >= 45 and bool(negative_rows)
        dimensions[key] = {
            "key": key,
            "label": definition["label"],
            "weight": AI_CYCLE_WEIGHTS[key],
            "available": available,
            "risk_score": round(risk, 1) if available else None,
            "active": active,
            "negative_source_count": len(negative_sources),
            "company_count": len(companies),
            "official_confirmed": official,
            "status": "转弱" if active else "改善" if positive_rows and not negative_rows else "观察" if available else "数据不足",
            "evidence": sorted(evidence, key=lambda item: item["published_at"], reverse=True)[:6],
        }
        all_sources.update(negative_sources)
        all_sources.update(positive_sources)
        all_evidence.extend(evidence)

    available_weight = sum(
        AI_CYCLE_WEIGHTS[key] for key, item in dimensions.items() if item["available"]
    )
    weighted_risk = sum(
        float(item["risk_score"] or 0) * AI_CYCLE_WEIGHTS[key]
        for key, item in dimensions.items()
        if item["available"]
    )
    score = weighted_risk / available_weight if available_weight else 0.0
    active_keys = [key for key, item in dimensions.items() if item["active"]]
    core_active = any(key in active_keys for key in ("cloud_capex", "ai_economics", "earnings_guidance"))
    official_confirmed = any(item["official_confirmed"] for item in dimensions.values())
    raw_level = "RED" if score >= 70 else "ORANGE" if score >= 50 else "YELLOW" if score >= 30 else "GREEN"
    if available_weight < 0.60:
        level = "GRAY"
    elif raw_level == "RED" and not (len(active_keys) >= 3 and core_active and official_confirmed):
        level = "ORANGE"
    elif raw_level == "ORANGE" and not (len(active_keys) >= 2 and core_active and len(all_sources) >= 2):
        level = "YELLOW"
    else:
        level = raw_level

    labels = {"GRAY": "灰色", "GREEN": "绿色", "YELLOW": "黄色", "ORANGE": "橙色", "RED": "红色"}
    if level == "GRAY":
        summary = f"有效维度覆盖 {available_weight:.0%}，不足以判断AI资本开支周期。"
    elif active_keys:
        names = "、".join(dimensions[key]["label"] for key in active_keys[:3])
        summary = f"{names}出现转弱证据，当前需按跨来源确认等级处理。"
    else:
        summary = "当前有效证据尚未形成AI资本开支周期下行共振。"

    fund_impacts = {}
    for code, fund in holdings_by_fund.items():
        exposure = _exposure_for_fund(fund or {})
        observed = exposure.get("observed_exposure")
        gate = "NOT_APPLICABLE" if not exposure["applicable"] else "BLOCKED" if level in {"ORANGE", "RED"} and float(observed or 0) >= 0.20 else "CAUTION" if level in {"YELLOW", "ORANGE", "RED"} and float(observed or 0) >= 0.10 else "NORMAL"
        fund_impacts[str(code)] = {
            **exposure,
            "gate": gate,
            "risk_contribution": round(score * min(float(observed or 0) / 0.40, 1.0), 1),
        }

    latest_date = max((item["published_at"] for item in all_evidence), default="--")
    return {
        "model": "AI Capex Cycle Alert v1.0",
        "level": level,
        "level_label": labels[level],
        "risk_score": round(score, 1) if available_weight else None,
        "confidence": round(available_weight * 100, 1),
        "coverage": round(available_weight, 4),
        "as_of": latest_date,
        "summary": summary,
        "dimensions": dimensions,
        "active_dimensions": active_keys,
        "independent_sources": sorted(all_sources),
        "official_confirmed": official_confirmed,
        "fund_impacts": fund_impacts,
        "evidence": sorted(all_evidence, key=lambda item: item["published_at"], reverse=True)[:12],
        "rules": (
            "交期缩短不得单独判定需求下滑；存储价格必须区分HBM/DRAM/NAND；"
            "AI收入未单独披露时不得推算商业回报；红色必须包含官方证据。"
        ),
    }


def apply_ai_cycle_gates(
    alert: dict,
    holding_alerts: dict[str, dict],
    strong_buy: dict,
) -> None:
    """Apply only buy-side safety gates; never manufacture a sell percentage."""
    impacts = alert.get("fund_impacts", {})
    for code, holding_alert in holding_alerts.items():
        impact = impacts.get(str(code), {})
        holding_alert["ai_cycle"] = impact
        if impact.get("gate") != "BLOCKED":
            continue
        reason = (
            f"AI资本开支周期为{alert.get('level_label', '高风险')}预警，"
            f"该基金已披露AI产业链暴露 {float(impact.get('observed_exposure', 0)):.1%}。"
        )
        holding_alert.setdefault("reasons", []).insert(0, reason)
        holding_alert["buy_gate"] = "BLOCKED"
        holding_alert["buy_gate_label"] = "AI周期风险：暂停加仓"
        holding_alert["decision_reason"] = reason
        if holding_alert.get("level") not in {"RED", "ORANGE"}:
            holding_alert["action"] = "暂停加仓，复核AI资本开支周期风险"

    candidate = str(strong_buy.get("code", ""))
    impact = impacts.get(candidate, {})
    if candidate and impact.get("gate") == "BLOCKED":
        reason = (
            f"AI资本开支周期{alert.get('level_label', '高风险')}预警，候选基金观察到"
            f" {float(impact.get('observed_exposure', 0)):.1%} AI产业链暴露。"
        )
        strong_buy["is_strong_buy"] = False
        strong_buy.setdefault("failed_conditions", []).append(reason)
        strong_buy.setdefault("risk_conditions", []).append(reason)
