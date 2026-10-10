from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List

from config import FUND_BENCHMARKS, RECOMMENDATION_POOL
from source_router import is_research_reference_source, is_trading_eligible_source, live_source_names


LIVE = "LIVE"
DELAYED = "DELAYED"
CACHED = "CACHED"
STATIC = "STATIC"
STALE = "STALE"
MISSING = "MISSING"

READY = "READY"
LIMITED = "LIMITED"
BLOCKED = "BLOCKED"

STATUS_LABELS = {
    LIVE: "最新",
    DELAYED: "正常延迟",
    CACHED: "缓存",
    STATIC: "静态待核验",
    STALE: "已过期",
    MISSING: "缺失",
}

GATE_LABELS = {
    READY: "可用于分析",
    LIMITED: "仅供观察",
    BLOCKED: "禁止判断",
}

# User-facing labels distinguish a usable fallback from a real-time source.
STATUS_LABELS[CACHED] = "缓存回退"
STATUS_LABELS[STATIC] = "静态参考基线"
GATE_LABELS[LIMITED] = "保守分析"

CONSERVATIVE_DELAYED = "CONSERVATIVE_DELAYED"
CONSERVATIVE_FALLBACK = "CONSERVATIVE_FALLBACK"
STANDARD_ANALYSIS = "STANDARD"

# These are exchange holidays kept locally so freshness checks do not depend on
# another network request. Weekend makeup workdays do not reopen an exchange.
US_FIXED_HOLIDAYS = {(1, 1), (7, 4), (12, 25)}

# Exchange closures for the years used by the application. Unknown future years
# conservatively fall back to weekdays only until the official calendar is added.
CHINA_EXCHANGE_HOLIDAYS = {
    2024: {
        "2024-01-01", "2024-02-09", "2024-02-12", "2024-02-13", "2024-02-14",
        "2024-02-15", "2024-02-16", "2024-04-04", "2024-04-05", "2024-05-01",
        "2024-05-02", "2024-05-03", "2024-06-10", "2024-09-16", "2024-09-17",
        "2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04", "2024-10-07",
    },
    2025: {
        "2025-01-01", "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
        "2025-02-03", "2025-02-04", "2025-04-04", "2025-05-01", "2025-05-02",
        "2025-05-05", "2025-06-02", "2025-10-01", "2025-10-02", "2025-10-03",
        "2025-10-06", "2025-10-07", "2025-10-08",
    },
    2026: {
        "2026-01-01", "2026-01-02", "2026-01-03", "2026-02-16", "2026-02-17",
        "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23", "2026-04-06",
        "2026-05-01", "2026-05-04", "2026-05-05", "2026-06-19", "2026-09-25",
        "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
    },
}

HONG_KONG_EXCHANGE_HOLIDAYS = {
    2024: {
        "2024-01-01", "2024-02-12", "2024-02-13", "2024-03-29", "2024-04-01",
        "2024-04-04", "2024-05-01", "2024-05-15", "2024-06-10", "2024-07-01",
        "2024-09-18", "2024-10-01", "2024-10-11", "2024-12-25", "2024-12-26",
    },
    2025: {
        "2025-01-01", "2025-01-29", "2025-01-30", "2025-01-31", "2025-04-04",
        "2025-04-18", "2025-04-21", "2025-05-01", "2025-05-05", "2025-05-29",
        "2025-07-01", "2025-10-01", "2025-10-07", "2025-12-25", "2025-12-26",
    },
    2026: {
        "2026-01-01", "2026-02-17", "2026-02-18", "2026-02-19", "2026-04-03",
        "2026-04-06", "2026-04-07", "2026-05-01", "2026-05-25", "2026-06-19",
        "2026-07-01", "2026-09-26", "2026-10-01", "2026-10-19", "2026-12-25",
        "2026-12-28",
    },
}


def _observed_fixed_holidays(year: int) -> set[date]:
    holidays = set()
    for month, day in US_FIXED_HOLIDAYS:
        holiday = date(year, month, day)
        if holiday.weekday() == 5:
            holidays.add(holiday - timedelta(days=1))
        elif holiday.weekday() == 6:
            holidays.add(holiday + timedelta(days=1))
        holidays.add(holiday)
    # US equity-market holidays with weekday rules.
    holidays.add(_nth_weekday(year, 1, 0, 3))   # Martin Luther King Jr. Day
    holidays.add(_nth_weekday(year, 2, 0, 3))   # Presidents' Day
    holidays.add(_last_weekday(year, 5, 0))     # Memorial Day
    holidays.add(_nth_weekday(year, 9, 0, 1))   # Labor Day
    holidays.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving
    holidays.add(_good_friday(year))
    holidays.add(date(year, 6, 19) if date(year, 6, 19).weekday() < 5 else _observed_date(date(year, 6, 19)))
    return holidays


def _observed_date(value: date) -> date:
    if value.weekday() == 5:
        return value - timedelta(days=1)
    if value.weekday() == 6:
        return value + timedelta(days=1)
    return value


def _good_friday(year: int) -> date:
    """Return Good Friday using the Gregorian computus algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day) - timedelta(days=2)


def _nth_weekday(year: int, month: int, weekday: int, ordinal: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (ordinal - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    current = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    offset = (current.weekday() - weekday) % 7
    return current - timedelta(days=offset)


def _is_trading_day(value: date, calendar: str = "global") -> bool:
    if value.weekday() >= 5:
        return False
    calendar = str(calendar or "global").lower()
    if calendar in {"global", "us"}:
        return value not in _observed_fixed_holidays(value.year)
    if calendar == "cn":
        return value.isoformat() not in CHINA_EXCHANGE_HOLIDAYS.get(value.year, set())
    if calendar == "hk":
        return value.isoformat() not in HONG_KONG_EXCHANGE_HOLIDAYS.get(value.year, set())
    return True


def _trading_day_age(as_of: date, today: date, calendar: str = "global") -> int:
    """Count elapsed exchange sessions, avoiding weekend/time-zone false staleness."""
    if as_of >= today:
        return 0
    current = as_of + timedelta(days=1)
    age = 0
    while current <= today:
        if _is_trading_day(current, calendar):
            age += 1
        current += timedelta(days=1)
    return age


def _calendar_for_index(symbol: str) -> str:
    if symbol in {"IXIC", "DJI"}:
        return "us"
    if symbol == "^HSI":
        return "hk"
    return "cn"

STATUS_FACTOR = {
    LIVE: 1.0,
    DELAYED: 0.80,
    CACHED: 0.45,
    STATIC: 0.25,
    STALE: 0.10,
    MISSING: 0.0,
}

STATUS_SEVERITY = {
    LIVE: 0,
    DELAYED: 1,
    CACHED: 2,
    STATIC: 3,
    STALE: 4,
    MISSING: 5,
}

LIVE_SOURCES = set(live_source_names())
DIRECT_CANDIDATE_SOURCE = "direct-config"

# A fallback is an explicitly labelled last-known or static reference. It may
# support a conservative display, but never an executable real-time signal.
FALLBACK_SOURCES = {"cache", "fallback", "baseline"}

FUND_INDEX_SYMBOL = {
    code: specs[0][0] for code, specs in FUND_BENCHMARKS.items()
}


@dataclass
class QualityRecord:
    key: str
    label: str
    as_of: str
    fetched_at: str
    source: str
    status: str
    status_label: str
    age_days: int | None
    required: bool
    message: str
    details: dict = field(default_factory=dict)


def resolve_fund_navs(quotes: Dict[str, object], histories: Dict[str, dict]) -> None:
    """Select the newest official NAV; cache can fill a gap but cannot replace newer data."""
    for code, quote in quotes.items():
        history = histories.get(code) or {}
        records = sorted(history.get("records", []), key=lambda item: item.get("date", ""))
        latest_history = records[-1] if records else None
        candidates = []
        if getattr(quote, "date", None) and getattr(quote, "nav", None):
            candidates.append(
                {
                    "date": str(quote.date),
                    "nav": float(quote.nav),
                    "source": str(getattr(quote, "source", "fallback")),
                    "kind": "quote",
                }
            )
        if latest_history and latest_history.get("date") and latest_history.get("nav"):
            candidates.append(
                {
                    "date": str(latest_history["date"]),
                    "nav": float(latest_history["nav"]),
                    "source": str(history.get("source", "cache")),
                    "kind": "history",
                }
            )
        if not candidates:
            continue
        selected = max(
            candidates,
            key=lambda item: (
                _date_value(item["date"]),
                _source_priority(item["source"]),
                item["kind"] == "quote",
            ),
        )
        quote.nav = selected["nav"]
        quote.date = selected["date"]
        quote.source = selected["source"]

        if (
            selected["kind"] == "quote"
            and selected["source"] in LIVE_SOURCES
            and (not latest_history or selected["date"] > str(latest_history.get("date", "")))
        ):
            history.setdefault("records", []).append(
                {"date": selected["date"], "nav": selected["nav"]}
            )
            history["records"] = sorted(
                {item["date"]: item for item in history["records"]}.values(),
                key=lambda item: item["date"],
            )
            history["latest_date"] = selected["date"]


def build_data_quality_report(
    target_codes: Iterable[str],
    recommendation_codes: Iterable[str],
    quotes: Dict[str, object],
    histories: Dict[str, dict],
    holdings: Dict[str, dict],
    market_snapshot: dict,
    news: Iterable[object],
    recommendation_qualifications: Dict[str, dict] | None = None,
    today: date | None = None,
    stock_data: Dict[str, dict] | None = None,
) -> dict:
    today = today or date.today()
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    target_codes = list(target_codes)
    recommendation_codes = list(recommendation_codes)
    recommendation_qualifications = recommendation_qualifications or {}
    fundamentals_enabled = stock_data is not None
    stock_data = stock_data or {}
    all_codes = list(dict.fromkeys([*target_codes, *recommendation_codes]))
    indexes = {
        item.get("symbol"): item for item in market_snapshot.get("indexes", [])
    }

    fund_quality = {}
    nav_records: List[QualityRecord] = []
    history_records: List[QualityRecord] = []
    for code in all_codes:
        quote = quotes.get(code)
        history = histories.get(code) or {}
        max_age = 2 if code == "005534" else 3
        nav_record = _quality_record(
            f"nav:{code}",
            f"{code} 官方净值",
            getattr(quote, "date", "") if quote else "",
            getattr(quote, "source", "missing") if quote else "missing",
            max_age,
            fetched_at,
            today,
            required=True,
            error=getattr(quote, "error", "") if quote else "",
            calendar="cn",
        )
        history_date = history.get("latest_date") or _latest_record_date(history)
        history_record = _quality_record(
            f"history:{code}",
            f"{code} 历史净值",
            history_date,
            history.get("source", "missing"),
            max_age,
            fetched_at,
            today,
            required=True,
            missing_message="历史净值缺失或不足，不能计算动态买点。",
            calendar="cn",
        )
        nav_records.append(nav_record)
        history_records.append(history_record)
        gate, reasons = _fund_gate(nav_record, history_record, history)
        fund_quality[code] = {
            "gate": gate,
            "gate_label": GATE_LABELS[gate],
            "nav": asdict(nav_record),
            "history": asdict(history_record),
            "reasons": reasons,
        }

    index_records = []
    market_errors = market_snapshot.get("fetch_errors", {})
    for symbol, item in indexes.items():
        record = _quality_record(
            f"index:{symbol}",
            f"{item.get('name', symbol)}",
            item.get("date", ""),
            item.get("source", "missing"),
            1,
            fetched_at,
            today,
            required=True,
            allow_missing_date_for_live=True,
            calendar=_calendar_for_index(symbol),
        )
        if market_errors.get(symbol):
            record.message = f"{record.message} 本次抓取失败：{market_errors[symbol]}"
        index_records.append(record)
    index_summary = _aggregate_record(
        "indexes", "全球指数", index_records, fetched_at, required=True
    )
    macro_summary, macro_detail = _macro_quality_summary(
        market_snapshot.get("macro_factors", []), fetched_at, today
    )
    index_by_symbol = {
        record.key.split(":", 1)[1]: record for record in index_records
    }
    for code, item in fund_quality.items():
        benchmark_specs = FUND_BENCHMARKS.get(
            code, ((FUND_INDEX_SYMBOL.get(code, "IXIC"), 1.0),)
        )
        benchmark_records = [
            (symbol, weight, index_by_symbol.get(symbol))
            for symbol, weight in benchmark_specs
        ]
        item["benchmarks"] = [
            {
                "symbol": symbol,
                "weight": weight,
                "status": record.status if record else MISSING,
                "source": record.source if record else "missing",
                "as_of": record.as_of if record else "--",
            }
            for symbol, weight, record in benchmark_records
        ]
        primary_record = benchmark_records[0][2]
        blocked_secondary = [
            symbol
            for symbol, _, record in benchmark_records[1:]
            if not record or record.status in {MISSING, STALE}
        ]
        delayed_secondary = [
            symbol
            for symbol, _, record in benchmark_records[1:]
            if record and record.status in {DELAYED, CACHED, STATIC}
        ]
        if not primary_record:
            item["gate"] = BLOCKED
            item["gate_label"] = GATE_LABELS[BLOCKED]
            item["reasons"].append("主基准指数缺失，不能生成交易性结论。")
        elif primary_record.status in {MISSING, STALE}:
            item["gate"] = BLOCKED
            item["gate_label"] = GATE_LABELS[BLOCKED]
            item["reasons"].append(primary_record.message)
        elif primary_record.status in {CACHED, STATIC} and item["gate"] == READY:
            item["gate"] = LIMITED
            item["gate_label"] = GATE_LABELS[LIMITED]
            item["reasons"].append(primary_record.message)
        elif blocked_secondary and item["gate"] == READY:
            item["gate"] = BLOCKED
            item["gate_label"] = GATE_LABELS[BLOCKED]
            item["reasons"].append(
                f"辅助基准指数缺失、缓存或过期：{', '.join(blocked_secondary)}，未执行分析。"
            )
        elif delayed_secondary and item["gate"] == READY:
            item["gate"] = LIMITED
            item["gate_label"] = GATE_LABELS[LIMITED]
            if delayed_secondary:
                item["reasons"].append(
                    f"辅助基准指数存在缓存或延迟：{', '.join(delayed_secondary)}，仅供观察。"
                )

    holding_records = []
    for code in all_codes:
        item = holdings.get(code) or {}
        holding_records.append(
            _quality_record(
                f"holding:{code}",
                f"{code} 持仓披露",
                item.get("disclosure_date", ""),
                item.get("source", "missing"),
                150,
                fetched_at,
                today,
                required=True,
                missing_message="持仓披露缺失，不能验证集中度与组合重合。",
                error=item.get("error", ""),
                calendar="cn",
            )
        )
    holding_summary = _aggregate_record(
        "holdings", "基金持仓披露", holding_records, fetched_at, required=True
    )

    fundamental_records = (
        [
            _fundamental_quality_record(
                code,
                holdings.get(code) or {},
                stock_data,
                fetched_at,
                today,
            )
            for code in all_codes
        ]
        if fundamentals_enabled
        else []
    )
    fundamental_summary = (
        _aggregate_record(
            "fundamentals",
            "重仓企业财报",
            fundamental_records,
            fetched_at,
            required=True,
        )
        if fundamentals_enabled
        else None
    )
    if fundamental_summary:
        fundamental_summary.details = _fundamental_summary_details(fundamental_records)

    news_items = list(news)
    news_dates = [
        str(getattr(item, "published_at", ""))[:10]
        for item in news_items
        if getattr(item, "published_at", "")
    ]
    real_news = [
        item for item in news_items
        if getattr(item, "source", "") not in {"", "离线观察项", "cache", "fallback", "baseline"}
    ]
    news_source = (
        "finance-news"
        if real_news
        else (
            "cache"
            if any(getattr(item, "source", "") == "cache" for item in news_items)
            else "baseline"
        )
    )
    news_record = _quality_record(
        "news",
        "基金影响新闻",
        max(news_dates) if news_dates else "",
        news_source,
        2,
        fetched_at,
        today,
        required=False,
        missing_message="新闻为离线观察项，只能用于提示，不能用于交易判断。",
    )

    qualification_records = []
    for code in recommendation_codes:
        qualification = recommendation_qualifications.get(code) or {}
        qualification_records.append(
            _quality_record(
                f"qualification:{code}",
                f"{code} 推荐资格",
                qualification.get("as_of", ""),
                qualification.get("source", "missing"),
                5,
                fetched_at,
                today,
                required=True,
                missing_message="推荐资格缺失，不能核验限额、管理类型和三年排名。",
                error=qualification.get("error", ""),
            )
        )
    recommendation_record = _aggregate_record(
        "recommendation_qualification",
        "推荐资格（限额/排名/管理类型）",
        qualification_records,
        fetched_at,
        required=True,
    )

    nav_summary = _aggregate_record(
        "fund_nav", "基金官方净值", nav_records, fetched_at, required=True
    )
    history_summary = _aggregate_record(
        "fund_history", "基金历史净值", history_records, fetched_at, required=True
    )
    module_records = [
        nav_summary,
        history_summary,
        index_summary,
        macro_summary,
        holding_summary,
        news_record,
        recommendation_record,
    ]
    if fundamental_summary:
        module_records.insert(4, fundamental_summary)

    consistency = _consistency_score(quotes, histories)
    overall_components = {
            "nav": (nav_summary.status, 20),
            "history": (history_summary.status, 10),
            "indexes": (index_summary.status, 15 if fundamentals_enabled else 20),
            "macro": (macro_summary.status, 10),
            "holdings": (holding_summary.status, 10 if fundamentals_enabled else 20),
            "recommendation": (recommendation_record.status, 10 if fundamentals_enabled else 15),
            "news": (news_record.status, 5),
    }
    if fundamental_summary:
        overall_components["fundamentals"] = (fundamental_summary.status, 20)
    overall_score, overall_confidence_detail = _confidence_score(
        overall_components,
        consistency,
        macro_detail=macro_detail,
        fundamental_detail=(fundamental_summary.details if fundamental_summary else None),
    )

    index_status = index_summary.status
    for code, item in fund_quality.items():
        holding_record = next(
            (record for record in holding_records if record.key == f"holding:{code}"),
            None,
        )
        qualification_record = next(
            (
                record
                for record in qualification_records
                if record.key == f"qualification:{code}"
            ),
            None,
        )
        fundamental_record = next(
            (
                record
                for record in fundamental_records
                if record.key == f"fundamental:{code}"
            ),
            None,
        )
        qualification_status = (
            qualification_record.status
            if code in recommendation_codes and qualification_record
            else LIVE
        )
        confidence_components = {
                "nav": (item["nav"]["status"], 20),
                "history": (item["history"]["status"], 10),
                "indexes": (index_status, 15 if fundamentals_enabled else 20),
                "macro": (macro_summary.status, 10),
                "holdings": (
                    holding_record.status if holding_record else LIVE,
                    10 if fundamentals_enabled else 20,
                ),
                "recommendation": (qualification_status, 10 if fundamentals_enabled else 15),
                "news": (news_record.status, 5),
        }
        if fundamentals_enabled:
            confidence_components["fundamentals"] = (
                fundamental_record.status if fundamental_record else MISSING,
                20,
            )
        confidence, confidence_detail = _confidence_score(
            confidence_components,
            consistency,
            macro_detail=macro_detail,
            fundamental_detail=(fundamental_record.details if fundamental_record else None),
        )
        item["confidence"] = confidence
        item["confidence_detail"] = confidence_detail
        if fundamental_record:
            item["fundamental_quality"] = fundamental_record.details
        if not holding_record or holding_record.status in {MISSING, STALE}:
            holding_message = (
                holding_record.message
                if holding_record
                else "持仓披露缺失，不能验证行业与重仓股风险。"
            )
            item["reasons"].append(holding_message)
            item["gate"] = BLOCKED
            item["gate_label"] = GATE_LABELS[BLOCKED]
        elif holding_record.status in {CACHED, STATIC} and item["gate"] == READY:
            item["reasons"].append(holding_record.message)
            item["gate"] = LIMITED
            item["gate_label"] = GATE_LABELS[LIMITED]
        if fundamentals_enabled and (
            not fundamental_record or fundamental_record.status in {MISSING, STALE}
        ):
            fundamental_message = (
                fundamental_record.message
                if fundamental_record
                else "重仓企业财报覆盖缺失，不能形成基本面主导结论。"
            )
            item["reasons"].append(fundamental_message)
            item["gate"] = BLOCKED
            item["gate_label"] = GATE_LABELS[BLOCKED]
        elif (
            fundamentals_enabled
            and fundamental_record
            and fundamental_record.status in {CACHED, STATIC}
            and item["gate"] == READY
        ):
            item["reasons"].append(fundamental_record.message)
            item["gate"] = LIMITED
            item["gate_label"] = GATE_LABELS[LIMITED]
        if macro_summary.status != LIVE:
            item["reasons"].append(macro_summary.message)
            if macro_summary.status == DELAYED and item["gate"] == READY:
                item["gate"] = LIMITED
                item["gate_label"] = GATE_LABELS[LIMITED]
            elif macro_summary.status in {CACHED, STATIC} and item["gate"] == READY:
                item["gate"] = LIMITED
                item["gate_label"] = GATE_LABELS[LIMITED]
            elif macro_summary.status not in {DELAYED, CACHED, STATIC}:
                item["gate"] = BLOCKED
                item["gate_label"] = GATE_LABELS[BLOCKED]
        if news_record.status != LIVE:
            item["reasons"].append(
                f"基金影响新闻未通过实时来源校验（{news_record.status_label}），未执行分析。"
            )
            if news_record.status in {CACHED, STATIC} and item["gate"] == READY:
                item["gate"] = LIMITED
                item["gate_label"] = GATE_LABELS[LIMITED]
            elif news_record.status not in {CACHED, STATIC}:
                item["gate"] = BLOCKED
                item["gate_label"] = GATE_LABELS[BLOCKED]
        if code in recommendation_codes:
            direct_candidate = bool((RECOMMENDATION_POOL.get(code) or {}).get("direct_candidate"))
            if item["gate"] == READY and (
                (qualification_status != LIVE and not direct_candidate)
                or not holding_record
                or holding_record.status not in {LIVE, CACHED, STATIC}
                or index_status not in {LIVE, DELAYED, CACHED, STATIC}
            ):
                item["gate"] = LIMITED
                item["gate_label"] = GATE_LABELS[LIMITED]
            if qualification_record and qualification_record.status != LIVE and not direct_candidate:
                item["reasons"].append(qualification_record.message)
        item["reasons"] = _unique_messages(item["reasons"])
        item["analysis_mode"] = _analysis_mode(
            code,
            item,
            index_status,
            holding_records,
            qualification_records,
            news_record,
            macro_summary,
        )
        item["analysis_ready"] = (
            item["gate"] == READY
            or item["analysis_mode"] in {
                CONSERVATIVE_DELAYED,
                CONSERVATIVE_FALLBACK,
            }
        )
        if item["analysis_mode"] == CONSERVATIVE_DELAYED:
            item["reasons"].append(
                "仅存在正常交易日/QDII披露延迟，已允许生成收窄后的保守分析区间。"
            )
            item["reasons"] = _unique_messages(item["reasons"])
        elif item["analysis_mode"] == CONSERVATIVE_FALLBACK:
            item["reasons"].append(
                "实时源已按优先级尝试并回退至缓存或静态基线；允许保守分析，禁止强烈买入与精确减持。"
            )
            item["reasons"] = _unique_messages(item["reasons"])

    target_gates = [fund_quality[code]["gate"] for code in target_codes if code in fund_quality]
    if not target_gates or BLOCKED in target_gates:
        overall_gate = BLOCKED
    elif (
        LIMITED in target_gates
        or recommendation_record.status != LIVE
        or macro_summary.status != LIVE
    ):
        overall_gate = LIMITED
    else:
        overall_gate = READY
    summary_text = {
        READY: "关键数据完整且在有效期内，允许执行分析。",
        LIMITED: "仅存在可解释的官方披露延迟，允许执行收窄后的保守分析。",
        BLOCKED: "关键数据缺失或已明显过期，无法形成保守分析结论。",
    }[overall_gate]
    if overall_gate == LIMITED and any(
        record.status in {CACHED, STATIC} for record in module_records
    ):
        summary_text = (
            "实时源已按优先顺序回退至缓存或静态参考基线；"
            "当前展示保守分析，强烈买入与精确减持保持关闭。"
        )

    return {
        "generated_at": fetched_at,
        "overall_gate": overall_gate,
        "overall_gate_label": GATE_LABELS[overall_gate],
        "overall_score": overall_score,
        "confidence_detail": overall_confidence_detail,
        "summary": summary_text,
        "records": [asdict(record) for record in module_records],
        "funds": fund_quality,
        "macro": macro_detail,
        "recommendation_qualifications": recommendation_qualifications,
        "consistency_score": consistency,
    }


def quality_status_text(report: dict) -> str:
    return (
        f"数据可信度 {float(report.get('overall_score', 0)):.1f}/100；"
        f"{report.get('overall_gate_label', '禁止判断')}。{report.get('summary', '')}"
    )


def _quality_record(
    key: str,
    label: str,
    as_of: str,
    source: str,
    max_age_days: int,
    fetched_at: str,
    today: date,
    required: bool,
    missing_message: str = "数据缺失。",
    allow_missing_date_for_live: bool = False,
    error: str = "",
    calendar: str = "global",
) -> QualityRecord:
    parsed = _parse_date(as_of)
    source = source or "missing"
    research_reference = is_research_reference_source(source)
    if parsed is None:
        if source in LIVE_SOURCES and allow_missing_date_for_live:
            status = DELAYED
            message = "来源已联网，但数据日期未返回，按正常延迟处理。"
        elif source == DIRECT_CANDIDATE_SOURCE:
            status = LIVE
            message = "按用户指定直入备选池，免除外部资格筛选。"
        elif source in {"fallback", "baseline"} or research_reference:
            status = STATIC
            message = (
                "Yahoo 仅作为研究参考，不能单独支持交易结论。"
                if research_reference
                else "实时源和缓存均不可用，当前展示明确标记的静态参考基线。"
            )
        else:
            status = MISSING
            message = missing_message
        age_days = None
    else:
        age_days = _trading_day_age(parsed, today, calendar)
        if source == DIRECT_CANDIDATE_SOURCE:
            status = LIVE
            message = "按用户指定直入备选池，免除外部资格筛选。"
        elif source in {"fallback", "baseline"} or research_reference:
            status = STATIC
            message = (
                "Yahoo 数据日期可供研究参考，但不能单独支持交易结论。"
                if research_reference
                else "实时源和缓存均不可用，当前展示明确标记的静态参考基线。"
            )
        elif source == "cache":
            status = CACHED if age_days <= max_age_days + 7 else STALE
            message = "当前使用缓存数据。" if status == CACHED else "缓存数据已超过有效期。"
        elif source in LIVE_SOURCES:
            if age_days <= max_age_days:
                status = LIVE
                message = "来源和数据日期均在有效期内。"
            elif age_days <= max_age_days + 3:
                status = DELAYED
                message = "数据存在正常披露延迟，只适合谨慎观察。"
            else:
                status = STALE
                message = "数据已超过有效期。"
        else:
            status = STALE
            message = "数据来源未通过可信来源校验。"
    if error:
        message = f"{message} 本次抓取失败：{error}"
    return QualityRecord(
        key=key,
        label=label,
        as_of=str(as_of or "--"),
        fetched_at=fetched_at,
        source=source,
        status=status,
        status_label=STATUS_LABELS[status],
        age_days=age_days,
        required=required,
        message=message,
        details={"calendar": calendar},
    )


def _analysis_mode(
    code: str,
    item: dict,
    index_status: str,
    holding_records: list[QualityRecord],
    qualification_records: list[QualityRecord],
    news_record: QualityRecord,
    macro_record: QualityRecord,
) -> str:
    """Classify delayed and cache/baseline inputs into separate safe modes."""
    if item.get("gate") != LIMITED:
        return STANDARD_ANALYSIS
    statuses = {
        item["nav"]["status"],
        item["history"]["status"],
        index_status,
        macro_record.status,
    }
    holding = next(
        (record for record in holding_records if record.key == f"holding:{code}"),
        None,
    )
    if any(status in {MISSING, STALE} for status in statuses):
        return STANDARD_ANALYSIS
    if holding and holding.status not in {LIVE, CACHED, STATIC}:
        return STANDARD_ANALYSIS
    fallback_statuses = {CACHED, STATIC}
    qualification = next(
        (record for record in qualification_records if record.key == f"qualification:{code}"),
        None,
    )
    uses_fallback = (
        bool(statuses.intersection(fallback_statuses))
        or bool(holding and holding.status in fallback_statuses)
        or news_record.status in fallback_statuses
        or bool(qualification and qualification.status in fallback_statuses)
    )
    if uses_fallback:
        return CONSERVATIVE_FALLBACK
    if not statuses.intersection({DELAYED}):
        return STANDARD_ANALYSIS
    return CONSERVATIVE_DELAYED


def _macro_quality_summary(
    macro_factors: Iterable[dict], fetched_at: str, today: date
) -> tuple[QualityRecord, dict]:
    """Validate field semantics before macro values are allowed to raise confidence."""
    items = {str(item.get("key", "")): item for item in macro_factors}
    economic_definitions = {
        "US_GDP": ("美国实际GDP", 145, "quarterly"),
        "US_CPI": ("美国通胀", 50, "monthly"),
        "US_UNEMPLOYMENT": ("美国就业", 50, "monthly"),
        "US_M2": ("美国M2流动性", 65, "monthly"),
        "US_FED_FUNDS": ("美国政策利率", 50, "monthly"),
    }
    credit_definitions = {
        "US_HY_SPREAD": ("美国高收益债OAS", 5, "daily"),
        "US_CREDIT_STRESS_PROXY": ("美国信用压力代理", 5, "daily"),
    }
    market_definitions = {
        "VIX": ("VIX", 5, "daily"),
        "US2Y": ("美国2年期利率", 5, "daily"),
        "US10Y": ("美国10年期利率", 5, "daily"),
        "US10Y_REAL": ("美国实际利率", 5, "daily"),
        "DXY": ("美元指数", 5, "daily"),
        "USDCNY": ("美元兑人民币", 5, "daily"),
    }
    definitions = {**economic_definitions, **credit_definitions, **market_definitions}

    def build_record(key: str, label: str, max_age: int, frequency: str) -> QualityRecord:
        item = items.get(key, {})
        actual_frequency = str(item.get("frequency", frequency))
        allowed_frequencies = {frequency}
        if key == "US_FED_FUNDS":
            allowed_frequencies.add("daily")
        if actual_frequency == "daily":
            max_age = min(max_age, 5)
        record = _quality_record(
            f"macro:{key}",
            label,
            str(item.get("date", "")),
            str(item.get("source", "missing")),
            max_age,
            fetched_at,
            today,
            required=True,
            missing_message=f"{label}数据缺失，宏观经济判断不完整。",
            calendar="global",
        )
        contract_required_fields = {
            "US_M2": ["value", "change_12_pct"],
        }
        required_fields = list(
            item.get("required_fields")
            or contract_required_fields.get(key, ["value"])
        )
        missing_fields = [
            name for name in required_fields
            if not isinstance(item.get(name), (int, float))
        ]
        semantic_status = str(item.get("semantic_status", "COMPLETE"))
        if item.get("value") is None:
            record.status = MISSING
            record.status_label = STATUS_LABELS[MISSING]
            record.message = f"{label}未返回有效数值，宏观经济判断不完整。"
        elif missing_fields or semantic_status == "INCOMPLETE":
            record.status = MISSING
            record.status_label = STATUS_LABELS[MISSING]
            fields = "、".join(missing_fields or required_fields)
            record.message = f"{label}缺少模型必需字段（{fields}），不计入完整宏观覆盖。"
        elif semantic_status == "BACKGROUND_ONLY" or actual_frequency not in allowed_frequencies:
            record.status = STATIC
            record.status_label = STATUS_LABELS[STATIC]
            record.message = f"{label}仅作低频背景参考，不计入实时宏观模型。"
        elif semantic_status == "PROXY" or bool(item.get("proxy")):
            if record.status in {LIVE, DELAYED}:
                record.status = DELAYED
                record.status_label = STATUS_LABELS[DELAYED]
            record.message = (
                "HYG/LQD 代理可以辅助识别信用压力，但不等同于真实OAS；"
                "仅支持保守分析。"
            )
        record.details = {
            "semantic_status": semantic_status,
            "required_fields": required_fields,
            "missing_fields": missing_fields,
            "field_complete": not missing_fields and semantic_status == "COMPLETE",
            "proxy": bool(item.get("proxy")) or semantic_status == "PROXY",
            "frequency": actual_frequency,
        }
        return record

    records_by_key = {
        key: build_record(key, label, max_age, frequency)
        for key, (label, max_age, frequency) in definitions.items()
    }
    records = list(records_by_key.values())
    oas_record = records_by_key["US_HY_SPREAD"]
    proxy_record = records_by_key["US_CREDIT_STRESS_PROXY"]
    oas_usable = (
        oas_record.details["field_complete"]
        and oas_record.status not in {MISSING, STALE, STATIC}
    )
    proxy_usable = (
        proxy_record.details["required_fields"]
        and not proxy_record.details["missing_fields"]
        and proxy_record.status not in {MISSING, STALE, STATIC}
    )
    credit_record = oas_record if oas_usable else proxy_record if proxy_usable else oas_record
    proxy_in_use = credit_record.key.endswith("US_CREDIT_STRESS_PROXY")

    economic_records = [
        records_by_key[key] for key in economic_definitions
    ] + [credit_record]
    market_records = [records_by_key[key] for key in market_definitions]
    usable_statuses = {LIVE, DELAYED}
    economic_coverage = (
        sum(record.status in usable_statuses for record in economic_records)
        / len(economic_records)
    )
    market_coverage = (
        sum(record.status in usable_statuses for record in market_records)
        / len(market_records)
    )
    cached_economic = sum(record.status == CACHED for record in economic_records)
    cached_market = sum(record.status == CACHED for record in market_records)
    static_economic = sum(record.status == STATIC for record in economic_records)
    static_market = sum(record.status == STATIC for record in market_records)
    semantic_complete = sum(
        record.details["field_complete"]
        for record in economic_records
        if not record.details["proxy"]
    )
    field_completeness = semantic_complete / len(economic_records)
    source_integrity = sum(
        STATUS_FACTOR.get(record.status, 0.0)
        for record in [*economic_records, *market_records]
    ) / (len(economic_records) + len(market_records))
    critical_missing_records = [
        record
        for record in [*economic_records, *market_records]
        if (
            record.status in {MISSING, STALE, STATIC}
            or (
                not record.details["field_complete"]
                and not record.details["proxy"]
            )
        )
    ]
    effective_economic_coverage = economic_coverage + (
        cached_economic + static_economic
    ) / len(economic_records)

    if (
        field_completeness == 1.0
        and not proxy_in_use
        and economic_coverage >= 5 / 6
        and market_coverage >= 4 / 6
    ):
        status = LIVE
        message = (
            f"增长、通胀、就业、流动性和真实信用数据覆盖 {economic_coverage:.0%}；"
            f"市场风险指标覆盖 {market_coverage:.0%}。"
        )
    elif economic_coverage >= 4 / 6 and market_coverage >= 4 / 6:
        status = DELAYED
        message = (
            f"宏观字段完整度 {field_completeness:.0%}，实时可用覆盖 "
            f"{economic_coverage:.0%}；"
            + (
                "当前信用数据为HYG/LQD代理，结论已切换为保守模式。"
                if proxy_in_use
                else "存在正常披露延迟，仅供保守分析。"
            )
        )
    elif effective_economic_coverage >= 4 / 6:
        status = STATIC if static_economic else (CACHED if cached_economic else DELAYED)
        message = (
            f"宏观经济实时覆盖仅 {economic_coverage:.0%}，含缓存或背景数据后 "
            f"{effective_economic_coverage:.0%}；仅供保守分析。"
        )
    else:
        status = MISSING
        message = (
            f"宏观经济实时覆盖仅 {economic_coverage:.0%}，低于至少4项核心数据的门槛；"
            "不应把市场行情指标视为完整宏观结论。"
        )
    dated = [record for record in records if _parse_date(record.as_of)]
    oldest = min(dated, key=lambda record: _date_value(record.as_of)) if dated else None
    critical_missing = bool(critical_missing_records)
    detail = {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "economic_coverage": round(economic_coverage, 4),
        "market_coverage": round(market_coverage, 4),
        "economic_cached_coverage": round(cached_economic / len(economic_records), 4),
        "market_cached_coverage": round(cached_market / len(market_records), 4),
        "economic_static_coverage": round(static_economic / len(economic_records), 4),
        "market_static_coverage": round(static_market / len(market_records), 4),
        "field_completeness": round(field_completeness, 4),
        "source_integrity": round(source_integrity, 4),
        "proxy_in_use": proxy_in_use,
        "proxy_penalty": 8.0 if proxy_in_use else 0.0,
        "credit_key": credit_record.key.split(":", 1)[1],
        "critical_missing": critical_missing,
        "critical_missing_fields": [record.label for record in critical_missing_records],
        "records": [asdict(record) for record in records],
    }
    summary = QualityRecord(
        key="macro_economy",
        label="宏观经济（增长/通胀/就业/流动性）",
        as_of=oldest.as_of if oldest else "--",
        fetched_at=fetched_at,
        source=" / ".join(sorted({record.source for record in records})),
        status=status,
        status_label=STATUS_LABELS[status],
        age_days=oldest.age_days if oldest else None,
        required=True,
        message=message,
        details={
            key: detail[key]
            for key in (
                "field_completeness", "source_integrity", "proxy_in_use",
                "proxy_penalty", "credit_key", "critical_missing",
                "critical_missing_fields",
            )
        },
    )
    return summary, detail


def _aggregate_record(
    key: str,
    label: str,
    records: List[QualityRecord],
    fetched_at: str,
    required: bool,
) -> QualityRecord:
    if not records:
        return QualityRecord(
            key, label, "--", fetched_at, "missing", MISSING,
            STATUS_LABELS[MISSING], None, required, "没有可用数据。"
        )
    worst = max(records, key=lambda item: STATUS_SEVERITY[item.status])
    dated = [item for item in records if _parse_date(item.as_of)]
    oldest = min(dated, key=lambda item: _date_value(item.as_of)) if dated else worst
    sources = sorted({item.source for item in records})
    affected = [item.label.split()[0] for item in records if item.status != LIVE]
    message = worst.message
    if affected:
        message = f"异常项：{'、'.join(affected[:5])}。{worst.message}"
    return QualityRecord(
        key=key,
        label=label,
        as_of=oldest.as_of,
        fetched_at=fetched_at,
        source=" / ".join(sources),
        status=worst.status,
        status_label=STATUS_LABELS[worst.status],
        age_days=oldest.age_days,
        required=required,
        message=message,
    )


def _fundamental_quality_record(
    code: str,
    holding: dict,
    stock_data: Dict[str, dict],
    fetched_at: str,
    today: date,
) -> QualityRecord:
    if holding.get("allocation_only"):
        return QualityRecord(
            key=f"fundamental:{code}",
            label=f"{code} 企业财报适用性",
            as_of=str(holding.get("disclosure_date") or "--"),
            fetched_at=fetched_at,
            source=str(holding.get("source") or "eastmoney-asset-allocation"),
            status=LIVE,
            status_label=STATUS_LABELS[LIVE],
            age_days=None,
            required=False,
            message=(
                "FOF资产配置基金，企业财报覆盖不适用；"
                "仅按资产配置、宏观和净值趋势保守分析。"
            ),
            details={
                "applicable": False,
                "fundamental_coverage": 0.0,
                "freshness_score": 100.0,
                "weighted_fiscal_age_days": None,
                "source_integrity": 1.0,
                "metric_completeness": 1.0,
                "stale_financials": False,
            },
        )
    items = holding.get("holdings", [])[:10]
    total_weight = sum(float(item.get("weight", 0)) for item in items)
    covered_weight = 0.0
    weighted_fiscal_age = 0.0
    weighted_metric_completeness = 0.0
    live_source_weight = 0.0
    filed_dates: list[str] = []
    fiscal_dates: list[str] = []
    sources = set()
    metric_keys = {
        "revenue_yoy",
        "net_income_yoy",
        "net_margin",
        "operating_margin",
        "free_cash_flow_margin",
        "roe",
    }
    for item in items:
        weight = float(item.get("weight", 0))
        fundamentals = (
            stock_data.get(str(item.get("code", "")), {}).get("fundamentals") or {}
        )
        metrics = fundamentals.get("metrics") or {}
        available = sum(
            isinstance(value, (int, float))
            for key, value in metrics.items()
            if key in metric_keys
        )
        source = str(fundamentals.get("source", ""))
        if available < 3 or source not in {
            "sec-companyfacts",
            "eastmoney-financials",
            "naver-financials",
            "ifind-financials",
            "cache",
        }:
            continue
        fiscal_end = str(fundamentals.get("fiscal_end", ""))[:10]
        filed_at = str(fundamentals.get("filed_at", ""))[:10]
        fiscal_date = _parse_date(fiscal_end) or _parse_date(filed_at)
        if not fiscal_date:
            continue
        covered_weight += weight
        weighted_fiscal_age += weight * max(0, (today - fiscal_date).days)
        weighted_metric_completeness += weight * (available / len(metric_keys))
        if source != "cache" and is_trading_eligible_source(source):
            live_source_weight += weight
        if filed_at:
            filed_dates.append(filed_at)
        if fiscal_end:
            fiscal_dates.append(fiscal_end)
        sources.add(source)
    coverage = covered_weight / total_weight if total_weight else 0.0
    fiscal_age = weighted_fiscal_age / covered_weight if covered_weight else None
    metric_completeness = (
        weighted_metric_completeness / covered_weight if covered_weight else 0.0
    )
    source_integrity = live_source_weight / covered_weight if covered_weight else 0.0
    if fiscal_age is None:
        freshness_score = 0.0
    elif fiscal_age <= 180:
        freshness_score = 100.0
    elif fiscal_age >= 550:
        freshness_score = 40.0
    else:
        freshness_score = 100.0 - (fiscal_age - 180) / 370 * 60.0
    stale_financials = fiscal_age is None or fiscal_age > 550 or freshness_score < 60.0
    if (
        coverage >= 0.70
        and not stale_financials
        and source_integrity >= 0.80
    ):
        status = LIVE
        message = (
            f"公开财报源覆盖前十大持仓权重 {coverage:.1%}，"
            f"加权财报账龄 {fiscal_age:.0f} 天。"
        )
    elif coverage >= 0.50 and not stale_financials:
        status = DELAYED
        message = (
            f"企业财报覆盖前十大持仓权重 {coverage:.1%}，"
            "未达到70%完整覆盖门槛，仅供保守分析。"
        )
    elif coverage >= 0.30:
        status = CACHED
        message = (
            f"企业财报仅覆盖前十大持仓权重 {coverage:.1%}，"
            "覆盖不足或财报偏旧，仅供观察。"
        )
    else:
        status = STATIC
        message = (
            f"企业财报仅覆盖前十大持仓权重 {coverage:.1%}，当前仅保留静态参考，"
            "不生成基本面主导的精确交易结论。"
        )
    return QualityRecord(
        key=f"fundamental:{code}",
        label=f"{code} 重仓企业财报",
        as_of=max(filed_dates, default="") or max(fiscal_dates, default="") or "--",
        fetched_at=fetched_at,
        source=" / ".join(sorted(sources)) or "missing",
        status=status,
        status_label=STATUS_LABELS[status],
        age_days=round(fiscal_age) if fiscal_age is not None else None,
        required=True,
        message=message,
        details={
            "applicable": True,
            "fundamental_coverage": round(coverage, 4),
            "freshness_score": round(max(0.0, min(freshness_score, 100.0)), 1),
            "weighted_fiscal_age_days": round(fiscal_age, 1) if fiscal_age is not None else None,
            "source_integrity": round(source_integrity, 4),
            "metric_completeness": round(metric_completeness, 4),
            "stale_financials": stale_financials,
        },
    )


def _fundamental_summary_details(records: Iterable[QualityRecord]) -> dict:
    details = [
        record.details
        for record in records
        if record.details and record.details.get("applicable", True)
    ]
    if not details:
        return {
            "fundamental_coverage": 0.0,
            "freshness_score": 0.0,
            "source_integrity": 0.0,
            "metric_completeness": 0.0,
            "stale_financials": True,
        }
    # The global figure uses the weakest target fund, so a strong disclosure for
    # one fund cannot hide insufficient fundamentals for another.
    return {
        "fundamental_coverage": min(
            float(item.get("fundamental_coverage", 0.0)) for item in details
        ),
        "freshness_score": min(
            float(item.get("freshness_score", 0.0)) for item in details
        ),
        "source_integrity": min(
            float(item.get("source_integrity", 0.0)) for item in details
        ),
        "metric_completeness": min(
            float(item.get("metric_completeness", 0.0)) for item in details
        ),
        "weighted_fiscal_age_days": max(
            float(item.get("weighted_fiscal_age_days", 0.0) or 0.0)
            for item in details
        ),
        "stale_financials": any(bool(item.get("stale_financials")) for item in details),
    }


def _fund_gate(
    nav_record: QualityRecord, history_record: QualityRecord, history: dict
) -> tuple[str, List[str]]:
    reasons = []
    statuses = {nav_record.status, history_record.status}
    record_count = len(history.get("records", []))
    if record_count < 60:
        reasons.append(f"历史净值只有 {record_count} 条，少于60条。")
        if statuses.intersection({STATIC}) and not statuses.intersection({MISSING, STALE}):
            return LIMITED, reasons
        return BLOCKED, reasons
    if MISSING in statuses or STALE in statuses:
        reasons.extend(
            item.message for item in (nav_record, history_record)
            if item.status in {MISSING, STALE}
        )
        return BLOCKED, reasons
    if CACHED in statuses or STATIC in statuses:
        reasons.extend(
            item.message for item in (nav_record, history_record)
            if item.status in {CACHED, STATIC}
        )
        return LIMITED, reasons
    if DELAYED in statuses:
        reasons.extend(
            item.message for item in (nav_record, history_record)
            if item.status in {CACHED, DELAYED}
        )
        return LIMITED, reasons
    return READY, ["净值与历史净值均已通过来源和时效校验。"]


def _confidence_score(
    components: dict,
    consistency: float,
    *,
    macro_detail: dict | None = None,
    fundamental_detail: dict | None = None,
) -> tuple[float, dict]:
    """Score freshness and semantic completeness separately from source status."""
    component_score = sum(
        STATUS_FACTOR.get(status, 0.0) * weight
        for status, weight in components.values()
    )
    consistency_score = max(0.0, min(consistency, 10.0))
    penalties: list[dict] = []
    macro_detail = macro_detail or {}
    field_completeness = float(macro_detail.get("field_completeness", 1.0))
    macro_integrity = float(macro_detail.get("source_integrity", 1.0))
    proxy_penalty = float(macro_detail.get("proxy_penalty", 0.0))
    macro_penalty = min(
        12.0,
        max(0.0, 1.0 - field_completeness) * 8.0
        + max(0.0, 1.0 - macro_integrity) * 4.0
        + proxy_penalty,
    )
    if macro_penalty:
        penalties.append({"name": "宏观语义/来源", "score": round(macro_penalty, 1)})

    fundamental_penalty = 0.0
    if fundamental_detail is not None and fundamental_detail.get("applicable", True):
        coverage = float(fundamental_detail.get("fundamental_coverage", 0.0))
        freshness = float(fundamental_detail.get("freshness_score", 0.0)) / 100.0
        source_integrity = float(fundamental_detail.get("source_integrity", 0.0))
        metric_completeness = float(fundamental_detail.get("metric_completeness", 0.0))
        fundamental_penalty = min(
            12.0,
            max(0.0, 0.70 - coverage) * 12.0
            + max(0.0, 0.60 - freshness) * 8.0
            + max(0.0, 0.80 - source_integrity) * 4.0
            + max(0.0, 0.50 - metric_completeness) * 3.0,
        )
        if fundamental_penalty:
            penalties.append({"name": "基本面覆盖/新鲜度", "score": round(fundamental_penalty, 1)})

    pre_cap_score = max(0.0, min(component_score + consistency_score - macro_penalty - fundamental_penalty, 100.0))
    cap = 100.0
    cap_reasons: list[str] = []
    if macro_detail.get("critical_missing"):
        cap = min(cap, 79.9)
        cap_reasons.append("关键宏观模型字段缺失或语义不完整，可信度上限79.9分")
    elif macro_detail.get("proxy_in_use"):
        cap = min(cap, 89.9)
        cap_reasons.append("信用数据使用HYG/LQD代理，可信度上限89.9分")
    if fundamental_detail is not None and fundamental_detail.get("applicable", True):
        coverage = float(fundamental_detail.get("fundamental_coverage", 0.0))
        freshness = float(fundamental_detail.get("freshness_score", 0.0))
        if coverage < 0.70 or bool(fundamental_detail.get("stale_financials")) or freshness < 60.0:
            cap = min(cap, 84.9)
            cap_reasons.append("基本面覆盖低于70%或财报加权新鲜度不足，可信度上限84.9分")
    score = round(min(pre_cap_score, cap), 1)
    return score, {
        "component_score": round(component_score, 1),
        "consistency_score": round(consistency_score, 1),
        "penalties": penalties,
        "pre_cap_score": round(pre_cap_score, 1),
        "score_cap": cap,
        "cap_reasons": cap_reasons,
        "field_completeness": round(field_completeness, 4),
        "source_integrity": round(macro_integrity, 4),
        "proxy_penalty": round(proxy_penalty, 1),
        "fundamental_coverage": (
            round(float(fundamental_detail.get("fundamental_coverage", 0.0)), 4)
            if fundamental_detail is not None else None
        ),
        "freshness_score": (
            round(float(fundamental_detail.get("freshness_score", 0.0)), 1)
            if fundamental_detail is not None else None
        ),
    }


def _weighted_score(components: dict, consistency: float) -> float:
    """Compatibility wrapper for callers that only need the numeric score."""
    return _confidence_score(components, consistency)[0]


def _consistency_score(quotes: Dict[str, object], histories: Dict[str, dict]) -> float:
    checks = []
    for code, quote in quotes.items():
        history = histories.get(code) or {}
        records = sorted(history.get("records", []), key=lambda item: item.get("date", ""))
        if not records or not getattr(quote, "date", None):
            continue
        latest = records[-1]
        if str(latest.get("date")) != str(quote.date):
            continue
        history_nav = float(latest.get("nav") or 0)
        quote_nav = float(getattr(quote, "nav", 0) or 0)
        if history_nav <= 0 or quote_nav <= 0:
            checks.append(False)
        else:
            checks.append(abs(history_nav / quote_nav - 1) <= 0.002)
    if not checks:
        return 5.0
    return round(sum(checks) / len(checks) * 10, 1)


def _latest_record_date(history: dict) -> str:
    dates = [str(item.get("date", "")) for item in history.get("records", [])]
    return max(dates) if dates else ""


def _unique_messages(messages: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(message for message in messages if message))


def _source_priority(source: str) -> int:
    if source == "eastmoney":
        return 5
    if source == "eastmoney-js":
        return 4
    if source == "eastmoney-history":
        return 4
    if source == "eastmoney-pingzhongdata":
        return 4
    if source in {
        "eastmoney-index",
        "tencent-index",
        "eastmoney-holdings",
    }:
        return 3
    if source == "cache":
        return 2
    return 1


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _date_value(value: str | None) -> date:
    return _parse_date(value) or date.min
