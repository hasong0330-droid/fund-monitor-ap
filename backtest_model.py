from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import statistics
import time
import urllib.parse
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List

from config import (
    EASTMONEY_INDEX_KLINE_URL,
    EASTMONEY_INDEX_SECIDS,
    FUND_HOLDINGS_URL,
    NASDAQ_INDEX_HISTORY_URL,
    RECOMMENDATION_POOL,
    REPORTS_DIR,
    YAHOO_CHART_URL,
)
from fetcher import (
    _clean_html_cell,
    _fetch_fund_history,
    _http_get,
    _powershell_http_get,
)
from quant_model import (
    MODEL_VERSION,
    STRONG_BUY_THRESHOLD,
    _clamp,
    _defense_score,
    _history_metrics,
    _market_regime_score,
    _market_defense_score,
    _midterm_trend_score,
    _quality_score,
    _risk_adjusted_score,
    _portfolio_fit,
    _pullback_score,
    _risk_penalty,
    _scale,
    _stability_score,
    _trend_score,
    _triangle,
    _holding_score,
)
from storage import atomic_write_json, atomic_write_text


BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
SAMPLE_DIR = BASE_DIR / "backtest_samples"
BACKTEST_CACHE = CACHE_DIR / "backtest_holdings_history.json"
BACKTEST_INDEX_CACHE = CACHE_DIR / "backtest_index_history.json"
RESEARCH_HISTORY_CACHE = CACHE_DIR / "backtest_research_history.json"
# Keep a materially longer history than the formal 3-9 month holding period.
# The fund API may still truncate a fund at its inception date; that is valid
# sample censoring and is reported per fund rather than filled synthetically.
RESEARCH_HISTORY_DAYS = 3500
RESEARCH_MIN_HISTORY = 600
INDEX_MIN_HISTORY = 1200
INDEX_FETCH_LIMIT = 800
POINT_IN_TIME_CACHE = CACHE_DIR / "backtest_point_in_time.json"
DEFAULT_TRANSACTION_COST_BPS = 50
DEFAULT_EXECUTION_LAG = 1
MIN_POINT_IN_TIME_FUNDAMENTAL_COVERAGE = 0.60
POINT_IN_TIME_NEWS_LOOKBACK_DAYS = 45

# 研究样本池与正式推荐池分离。这里仅用于扩大同类主动QDII的统计样本，
# 不代表这些基金已通过申购限额、排名或当日数据核验，也不会进入页面推荐。
RESEARCH_CANDIDATES = {
    "000041": "华夏全球股票(QDII)(人民币)",
    "000043": "嘉实美国成长股票人民币",
    "000934": "国富大中华精选混合",
    "000988": "嘉实全球互联网股票人民币",
    "001668": "汇添富全球移动互联混合(QDII)人民币A",
    "002230": "华夏大中华混合(QDII)",
    "002379": "工银香港中小盘人民币",
    "003243": "摩根中国世纪混合(QDII)人民币",
    "004877": "汇添富全球医疗混合(QDII)人民币",
    "005534": "华夏新时代混合人民币(QDII)",
    "005698": "华夏全球科技先锋混合(QDII)A",
    "005699": "工银新经济混合(QDII)人民币A",
    "006282": "摩根欧洲动力策略股票(QDII)A",
    "006308": "汇添富全球消费混合(QDII)人民币A",
    "006373": "国富全球科技互联混合(QDII)人民币A",
    "006555": "浦银安盛全球智能科技(QDII)A",
    "007280": "摩根日本精选股票(QDII)A",
    "007455": "富国蓝筹精选股票(QDII)人民币",
    "008253": "华宝致远混合(QDII)A",
    "008284": "易方达全球医药行业混合发起式(QDII)A",
    "009108": "富国红利精选混合(QDII)人民币",
    "012060": "富国全球消费精选混合(QDII)人民币A",
    "012920": "易方达全球成长精选混合(QDII)人民币A",
    "013328": "嘉实全球价值股票(QDII)人民币",
    "017429": "嘉实全球创新龙头股票(QDII)人民币A",
    "017437": "华宝纳斯达克精选股票发起式(QDII)A",
    "018155": "创金合信全球医药生物股票发起(QDII)A",
    "018229": "易方达全球优质企业混合(QDII)A",
    "270023": "广发全球精选股票(QDII)人民币A",
    "486002": "工银全球精选股票(QDII)",
    # Additional calibration-only peers. They are deliberately absent from
    # config.TARGET_FUNDS and config.RECOMMENDATION_POOL.
    "160216": "国泰大宗商品(QDII)A",
    "005976": "长信稳进资产配置(FOF)",
}

# run_backtest 使用研究池；正式推荐资格仍只由 config.RECOMMENDATION_POOL 控制。
CANDIDATES = RESEARCH_CANDIDATES
CURRENT_HOLDING_CODE = "018229"
INDEX_CODES = {"IXIC": EASTMONEY_INDEX_SECIDS["IXIC"], "DJI": EASTMONEY_INDEX_SECIDS["DJI"]}
HORIZONS = (63, 126, 189)
DEFENSIVE_OBJECTIVE_WEIGHTS = {
    "return": 1.00,
    "max_drawdown": 0.75,
    "cvar": 0.50,
}
MIN_INDEPENDENT_EVENTS_PER_SPLIT = 15

ARCHITECTURES = {
    "v2.0_original": {
        "regime_score": 0.20,
        "trend_score": 0.20,
        "quality_score": 0.20,
        "pullback_score": 0.15,
        "holding_score": 0.15,
        "fit_score": 0.10,
        "risk_penalty": 1.0,
    },
    "v2.1_pullback_balanced": {
        "regime_score": 0.05,
        "trend_score": 0.15,
        "quality_score": 0.10,
        "pullback_score": 0.40,
        "holding_score": 0.20,
        "fit_score": 0.10,
        "risk_penalty": 0.0,
    },
    "v2.1_calibrated": {
        "trend_score": 0.15,
        "quality_score": 0.05,
        "pullback_score": 0.50,
        "holding_score": 0.20,
        "fit_score": 0.10,
        "risk_penalty": 0.0,
    },
    "v2.2_defensive": {
        "defense_score": 0.30,
        "midterm_trend_score": 0.20,
        "stability_score": 0.15,
        "pullback_score": 0.15,
        "market_score": 0.10,
        "portfolio_score": 0.10,
    },
    "v3.0_multi_objective": {
        "defense_score": 0.30,
        "midterm_trend_score": 0.20,
        "stability_score": 0.15,
        "pullback_score": 0.10,
        "quality_score": 0.10,
        "risk_adjusted_score": 0.05,
        "portfolio_score": 0.10,
    },
    "v2.1_defensive": {
        "regime_score": 0.0,
        "trend_score": 0.10,
        "quality_score": 0.15,
        "pullback_score": 0.45,
        "holding_score": 0.20,
        "fit_score": 0.10,
        "risk_penalty": 0.0,
    },
}


def load_fund_histories() -> Dict[str, dict]:
    payload = json.loads(
        (CACHE_DIR / "fund_history.json").read_text(encoding="utf-8")
    )
    funds = payload.get("funds", {})
    missing = [code for code in CANDIDATES if code not in funds]
    if missing:
        raise RuntimeError(f"缺少候选基金历史净值：{', '.join(missing)}")
    return {code: funds[code] for code in CANDIDATES}


def load_research_histories(use_cache: bool = False) -> Dict[str, dict]:
    """Load or refresh the expanded research universe without changing app data."""
    cached = _read_json(RESEARCH_HISTORY_CACHE) or {}
    cached_funds = cached.get("funds", {})
    peer_payload = _read_json(CACHE_DIR / "holding_alert_peer_histories.json") or {}
    peer_funds = peer_payload.get("funds", {})
    # Peer NAVs are refreshed by refresh_holding_alert_research_data.py and
    # are valid calibration inputs even when the live endpoint is unavailable.
    available_funds = {**peer_funds, **cached_funds}
    if use_cache and cached.get("history_days") == RESEARCH_HISTORY_DAYS and all(
        len(available_funds.get(code, {}).get("records", [])) >= RESEARCH_MIN_HISTORY
        for code in CANDIDATES
    ):
        return {code: available_funds[code] for code in CANDIDATES}

    # Audited peer caches are refreshed by their dedicated collector. Keep
    # using them when present so a blocked live endpoint cannot erase a valid
    # calibration sample or add needless network latency.
    histories: Dict[str, dict] = {
        code: peer_funds[code]
        for code in CANDIDATES
        if code in peer_funds
        and len(peer_funds.get(code, {}).get("records", [])) >= RESEARCH_MIN_HISTORY
    }
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(
                _fetch_fund_history,
                code,
                cached_funds.get(code),
                RESEARCH_HISTORY_DAYS,
            ): code
            for code in CANDIDATES
            if code not in histories
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                item = future.result()
            except Exception:
                item = available_funds.get(code)
            if not item:
                item = available_funds.get(code)
            if item and len(item.get("records", [])) >= RESEARCH_MIN_HISTORY:
                histories[code] = item

    missing = [code for code in CANDIDATES if code not in histories]
    if missing:
        raise RuntimeError(
            f"研究样本历史净值不足600条：{', '.join(missing)}"
        )
    _write_json(
        RESEARCH_HISTORY_CACHE,
        {
            "ts": int(time.time()),
            "history_days": RESEARCH_HISTORY_DAYS,
            "funds": histories,
        },
    )
    return histories


def fetch_index_histories(use_cache: bool = False) -> Dict[str, List[dict]]:
    cached = _read_json(BACKTEST_INDEX_CACHE) or {}
    if use_cache and all(
        len(cached.get(symbol, [])) >= INDEX_MIN_HISTORY for symbol in INDEX_CODES
    ):
        return {symbol: cached[symbol] for symbol in INDEX_CODES}

    results: Dict[str, List[dict]] = {}
    for symbol, secid in INDEX_CODES.items():
        try:
            results[symbol] = _fetch_index_history(symbol, secid)
        except Exception:
            if len(cached.get(symbol, [])) >= INDEX_MIN_HISTORY:
                results[symbol] = cached[symbol]
        if symbol in results:
            _write_json(BACKTEST_INDEX_CACHE, {**cached, **results})
    if len(results) != len(INDEX_CODES):
        missing = sorted(set(INDEX_CODES) - set(results))
        raise RuntimeError(f"无法取得指数历史样本：{', '.join(missing)}")
    _write_json(BACKTEST_INDEX_CACHE, results)
    return results


def _fetch_index_history(symbol: str, secid: str) -> List[dict]:
    cutoff = (date.today() - timedelta(days=RESEARCH_HISTORY_DAYS + 400)).strftime("%Y%m%d")
    end_date = "20500101"
    collected: dict[str, dict] = {}
    last_count = 0

    # Eastmoney limits a single kline response. Walk backwards by the oldest
    # returned date and merge the windows, keeping the source/date audit intact.
    for window in range(8):
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": 101,
            "fqt": 0,
            "beg": cutoff,
            "end": end_date,
            "lmt": INDEX_FETCH_LIMIT,
        }
        url = EASTMONEY_INDEX_KLINE_URL + "?" + urllib.parse.urlencode(params)
        payload = None
        try:
            payload = json.loads(
                _http_get(
                    url,
                    timeout=25,
                    attempts=1,
                    referer="https://quote.eastmoney.com/",
                )
            )
        except Exception:
            pass
        klines = (
            (payload.get("data") or {}).get("klines", [])
            if isinstance(payload, dict)
            else []
        )
        if not klines:
            try:
                payload = json.loads(
                    _powershell_http_get(
                        url, timeout=40, referer="https://quote.eastmoney.com/"
                    )
                )
            except Exception:
                payload = None
            klines = (
                (payload.get("data") or {}).get("klines", [])
                if isinstance(payload, dict)
                else []
            )
        window_records = []
        for line in klines:
            parts = line.split(",")
            if len(parts) >= 3:
                try:
                    window_records.append({"date": parts[0], "close": float(parts[2])})
                except (TypeError, ValueError):
                    continue
        last_count = len(window_records)
        for item in window_records:
            collected[item["date"]] = item
        if not window_records:
            break
        oldest = min(item["date"] for item in window_records)
        if oldest <= cutoff or len(window_records) < INDEX_FETCH_LIMIT:
            break
        previous_end = (datetime.strptime(oldest, "%Y-%m-%d").date() - timedelta(days=1)).strftime(
            "%Y%m%d"
        )
        if previous_end >= end_date:
            break
        end_date = previous_end
        time.sleep(0.35)

    records = sorted(collected.values(), key=lambda item: item["date"])
    if len(records) >= INDEX_MIN_HISTORY:
        candidates = [records]
        try:
            candidates.append(_fetch_yahoo_index_history(symbol))
        except Exception:
            pass
        try:
            candidates.append(_fetch_nasdaq_index_history(symbol, cutoff))
        except Exception:
            pass
        return max(candidates, key=len)
    if symbol == "IXIC":
        try:
            records = _fetch_tencent_index_history("us.IXIC")
            if len(records) >= INDEX_MIN_HISTORY:
                return records
        except Exception:
            pass
    try:
        records = _fetch_nasdaq_index_history(symbol, cutoff)
        if len(records) >= INDEX_MIN_HISTORY:
            return records
    except Exception:
        pass
    raise ValueError(f"{symbol}指数历史不足：{last_count}")


def _fetch_nasdaq_index_history(symbol: str, start_date: str) -> List[dict]:
    """Fetch a long daily index history from Nasdaq's public history endpoint."""
    nasdaq_symbol = {"IXIC": "COMP", "DJI": "DJIA"}.get(symbol)
    if not nasdaq_symbol:
        raise ValueError(f"Nasdaq未配置{symbol}指数代码")
    params = {
        "assetclass": "index",
        "fromdate": (
            datetime.strptime(start_date, "%Y%m%d").date().isoformat()
            if re.fullmatch(r"\d{8}", str(start_date))
            else str(start_date)
        ),
        "todate": date.today().isoformat(),
        "limit": 5000,
    }
    payload = json.loads(
        _http_get(
            NASDAQ_INDEX_HISTORY_URL.format(symbol=nasdaq_symbol)
            + "?"
            + urllib.parse.urlencode(params),
            timeout=30,
            attempts=1,
            referer="https://www.nasdaq.com/",
            extra_headers={
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.nasdaq.com",
            },
        )
    )
    rows = (
        ((payload.get("data") or {}).get("tradesTable") or {}).get("rows") or []
        if isinstance(payload, dict)
        else []
    )
    records = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_date = str(row.get("date") or "").strip()
        raw_close = str(row.get("close") or "").replace(",", "").strip()
        try:
            parsed_date = datetime.strptime(raw_date, "%m/%d/%Y").date().isoformat()
            close = float(raw_close)
        except (TypeError, ValueError):
            continue
        records.append({"date": parsed_date, "close": close})
    return sorted(
        {item["date"]: item for item in records}.values(),
        key=lambda item: item["date"],
    )


def _fetch_yahoo_index_history(symbol: str) -> List[dict]:
    """Fetch a longer daily index history without replacing the primary source."""
    yahoo_symbol = {"IXIC": "^IXIC", "DJI": "^DJI"}.get(symbol)
    if not yahoo_symbol:
        raise ValueError(f"Yahoo未配置{symbol}指数代码")
    yahoo_start = date.today() - timedelta(days=RESEARCH_HISTORY_DAYS + 400)
    start = int(
        datetime(
            yahoo_start.year,
            yahoo_start.month,
            yahoo_start.day,
            tzinfo=timezone.utc,
        ).timestamp()
    )
    end = int(datetime.now(timezone.utc).timestamp())
    params = {
        "period1": start,
        "period2": end,
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
    }
    url = YAHOO_CHART_URL.format(symbol=urllib.parse.quote(yahoo_symbol, safe=""))
    payload = json.loads(
        _http_get(
            url + "?" + urllib.parse.urlencode(params),
            timeout=25,
            attempts=1,
            referer="https://finance.yahoo.com/",
        )
    )
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        raise ValueError(f"Yahoo未返回{symbol}指数历史")
    timestamps = result.get("timestamp") or []
    closes = (((result.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    records = []
    for timestamp, close in zip(timestamps, closes):
        if not isinstance(timestamp, (int, float)) or not isinstance(close, (int, float)):
            continue
        records.append({
            "date": datetime.fromtimestamp(float(timestamp), tz=timezone.utc).date().isoformat(),
            "close": float(close),
        })
    records = sorted(
        {item["date"]: item for item in records}.values(),
        key=lambda item: item["date"],
    )
    if len(records) < 700:
        raise ValueError(f"Yahoo {symbol}指数历史不足：{len(records)}")
    return records


def _fetch_tencent_index_history(tencent_symbol: str) -> List[dict]:
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="
        f"{tencent_symbol},day,,,800,qfq"
    )
    payload = json.loads(
        _http_get(url, timeout=30, attempts=2, referer="https://gu.qq.com/")
    )
    rows = (payload.get("data", {}).get(tencent_symbol, {}) or {}).get("day", [])
    records = [
        {"date": row[0], "close": float(row[2])}
        for row in rows
        if len(row) >= 3 and row[0] and row[2]
    ]
    return sorted(records, key=lambda item: item["date"])


def fetch_holdings_histories(
    start_date: str, end_date: str, use_cache: bool = False
) -> Dict[str, List[dict]]:
    cached = _read_json(BACKTEST_CACHE) or {}
    codes = [*CANDIDATES, CURRENT_HOLDING_CODE]
    if use_cache and all(len(cached.get(code, [])) >= 6 for code in codes):
        return {code: cached[code] for code in codes}

    start_year = datetime.strptime(start_date, "%Y-%m-%d").year
    end_year = datetime.strptime(end_date, "%Y-%m-%d").year
    requests = [
        (code, year)
        for code in codes
        for year in range(start_year, end_year + 1)
    ]
    collected: Dict[str, Dict[str, dict]] = {code: {} for code in codes}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_fetch_holding_year, code, year): code
            for code, year in requests
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                items = future.result()
            except Exception:
                continue
            for item in items:
                collected[code][item["disclosure_date"]] = item

    results: Dict[str, List[dict]] = {}
    for code in codes:
        fresh = sorted(collected[code].values(), key=lambda item: item["effective_date"])
        results[code] = fresh if fresh else cached.get(code, [])
    _write_json(BACKTEST_CACHE, results)
    return results


def load_point_in_time_history() -> dict:
    """Load optional historical fundamentals/news with publication dates."""
    payload = _read_json(POINT_IN_TIME_CACHE)
    return payload if isinstance(payload, dict) else {}


def point_in_time_acceptance_status(point_in_time: dict | None) -> dict:
    """Return the raw-data gate required for formal out-of-sample acceptance."""
    acceptance = (point_in_time or {}).get("acceptance", {})
    if not isinstance(acceptance, dict):
        acceptance = {}
    blockers = list(acceptance.get("blocking_reasons") or [])
    required_fields = (
        "fundamental_events",
        "mapped_news_events",
        "fundamental_span_days",
        "news_span_days",
        "fundamental_coverage_ready",
        "funds_with_60pct_fundamental_coverage",
        "usable_fundamental_snapshots",
    )
    if not all(field in acceptance for field in required_fields):
        blockers.append("点时面板缺少原始归档覆盖证明，正式样本外验收不可运行")
    if acceptance.get("fundamental_coverage_ready") is False:
        blockers.extend(acceptance.get("coverage_blocking_reasons") or [
            "基金级点时财报覆盖未通过正式验收门槛"
        ])
    return {
        "ready": bool(acceptance.get("acceptance_ready")) and not blockers,
        "fundamental_events": int(acceptance.get("fundamental_events", 0) or 0),
        "mapped_news_events": int(acceptance.get("mapped_news_events", 0) or 0),
        "fundamental_span_days": int(acceptance.get("fundamental_span_days", 0) or 0),
        "news_span_days": int(acceptance.get("news_span_days", 0) or 0),
        "fundamental_coverage_ready": bool(acceptance.get("fundamental_coverage_ready")),
        "funds_with_60pct_fundamental_coverage": int(
            acceptance.get("funds_with_60pct_fundamental_coverage", 0) or 0
        ),
        "required_funds_with_60pct_fundamental_coverage": int(
            acceptance.get("required_funds_with_60pct_fundamental_coverage", 0) or 0
        ),
        "usable_fundamental_snapshots": int(
            acceptance.get("usable_fundamental_snapshots", 0) or 0
        ),
        "required_usable_fundamental_snapshots": int(
            acceptance.get("required_usable_fundamental_snapshots", 0) or 0
        ),
        "core_fund_snapshots_at_or_above_60pct": acceptance.get(
            "core_fund_snapshots_at_or_above_60pct", {}
        ),
        "blocking_reasons": list(dict.fromkeys(blockers)),
    }


def _record_value(record: object, key: str, default=None):
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _pit_records(point_in_time: dict | None, category: str, code: str) -> List[dict]:
    if not isinstance(point_in_time, dict):
        return []
    source = point_in_time.get(category, {})
    if isinstance(source, dict):
        records = source.get(code, [])
    else:
        records = source
    return [item for item in records if isinstance(item, dict)]


def _available_pit_record(records: List[dict], signal_date: str) -> dict | None:
    available = [
        item for item in records
        if str(item.get("available_date", ""))
        and str(item.get("available_date")) <= signal_date
    ]
    if not available:
        return None
    return max(
        available,
        key=lambda item: (
            str(item.get("available_date", "")),
            str(item.get("as_of_date", "")),
        ),
    )


def _pit_numeric_score(record: dict | None, keys: tuple[str, ...]) -> float | None:
    if not record:
        return None
    for key in keys:
        value = record.get(key)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return _clamp(float(value), 0, 100)
    scores = record.get("scores")
    if isinstance(scores, dict):
        for key in keys:
            value = scores.get(key)
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                return _clamp(float(value), 0, 100)
    return None


def _point_in_time_snapshot(
    code: str,
    signal_date: str,
    point_in_time: dict | None,
    require_point_in_time: bool,
) -> dict:
    if not point_in_time:
        return {
            "status": "missing",
            "eligible": not require_point_in_time,
            "fundamental": None,
            "news": None,
            "fundamental_score": None,
            "news_score": None,
            "fundamental_as_of": "",
            "news_as_of": "",
            "lookahead_check": {"passed": False, "reason": "point-in-time bundle missing"},
        }

    fundamental_records = _pit_records(point_in_time, "fundamentals", code)
    news_records = _pit_records(point_in_time, "news", code)
    fundamental = _available_pit_record(fundamental_records, signal_date)
    available_news = [
        item for item in news_records
        if str(item.get("available_date", ""))
        and str(item.get("available_date")) <= signal_date
    ]
    recent_news = [
        item for item in available_news
        if item.get("available_date", "") >= (
            datetime.strptime(signal_date, "%Y-%m-%d").date() - timedelta(days=POINT_IN_TIME_NEWS_LOOKBACK_DAYS)
        ).isoformat()
    ]
    # An old article is audit evidence, never current signal input. Falling back
    # to it would manufacture news coverage that did not exist at this signal.
    news = max(recent_news, key=lambda item: str(item.get("available_date", "")), default=None)
    future_fundamentals = sum(
        1 for item in fundamental_records
        if str(item.get("available_date", "")) > signal_date
    )
    future_news = sum(
        1 for item in news_records
        if str(item.get("available_date", "")) > signal_date
    )
    fundamental_score = _pit_numeric_score(
        fundamental, ("score", "fundamental_score")
    )
    news_score = _pit_numeric_score(
        news, ("score", "news_score", "other_score", "industry_score")
    )
    lookahead_passed = (
        fundamental is None or str(fundamental.get("available_date", "")) <= signal_date
    ) and (news is None or str(news.get("available_date", "")) <= signal_date)
    fundamental_coverage = float((fundamental or {}).get("coverage", 0.0) or 0.0)
    coverage_ok = fundamental_coverage >= MIN_POINT_IN_TIME_FUNDAMENTAL_COVERAGE
    news_recency_ok = news is not None
    eligible = bool(
        fundamental
        and news
        and fundamental_score is not None
        and news_score is not None
        and coverage_ok
        and news_recency_ok
        and lookahead_passed
    )
    return {
        "status": "ready" if eligible else "insufficient",
        "eligible": eligible,
        "fundamental": fundamental,
        "news": news,
        "fundamental_score": fundamental_score,
        "news_score": news_score,
        "fundamental_as_of": str(
            (fundamental or {}).get("as_of_date")
            or (fundamental or {}).get("available_date")
            or ""
        ),
        "news_as_of": str((news or {}).get("available_date") or ""),
        "fundamental_coverage": fundamental_coverage,
        "fundamental_coverage_ok": coverage_ok,
        "news_recency_ok": news_recency_ok,
        "lookahead_check": {
            "passed": lookahead_passed,
            "future_fundamental_records": future_fundamentals,
            "future_news_records": future_news,
            "information_cutoff_date": signal_date,
        },
    }


def _net_forward_return(entry_nav: float, exit_nav: float, cost_bps: float) -> float:
    one_way_cost = max(float(cost_bps), 0.0) / 10000
    gross_multiple = float(exit_nav) / float(entry_nav)
    return gross_multiple * (1 - one_way_cost) ** 2 - 1


def _fetch_holding_year(code: str, year: int) -> List[dict]:
    params = {
        "type": "jjcc",
        "code": code,
        "topline": 10,
        "year": year,
        "month": 12,
    }
    url = FUND_HOLDINGS_URL + "?" + urllib.parse.urlencode(params)
    try:
        text = _http_get(
            url,
            timeout=15,
            attempts=1,
            referer="https://fundf10.eastmoney.com/",
        )
    except Exception:
        text = _powershell_http_get(
            url, timeout=25, referer="https://fundf10.eastmoney.com/"
        )
    return _parse_holding_year_payload(text)


def _parse_holding_year_payload(text: str) -> List[dict]:
    match = re.search(r'content:"(.*)",arryear:', text, re.S)
    content = match.group(1) if match else text
    content = content.replace('\\"', '"').replace('\\/', '/')
    results = []
    for block in re.split(r"<h4", content)[1:]:
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", block[:1200])
        table_match = re.search(r"<table[^>]*>(.*?)</table>", block, re.S)
        if not date_match or not table_match:
            continue
        disclosure_date = date_match.group(1)
        table = table_match.group(1)
        period_holdings = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if len(cells) < 5:
                continue
            cleaned = [_clean_html_cell(cell) for cell in cells]
            weight_match = next(
                (re.search(r"([\d.]+)%", cell) for cell in cleaned[3:] if "%" in cell),
                None,
            )
            if not weight_match:
                continue
            period_holdings.append(
                {
                    "code": cleaned[1],
                    "name": cleaned[2],
                    "weight": float(weight_match.group(1)) / 100,
                }
            )
        if not period_holdings:
            continue
        effective = (
            datetime.strptime(disclosure_date, "%Y-%m-%d").date()
            + timedelta(days=30)
        )
        results.append(
            {
                "disclosure_date": disclosure_date,
                "holdings": period_holdings[:10],
                "effective_date": effective.isoformat(),
                "source": "eastmoney-holdings-history",
            }
        )
    return results


def run_backtest(
    histories: Dict[str, dict],
    indexes: Dict[str, List[dict]],
    holdings: Dict[str, List[dict]],
    point_in_time: dict | None = None,
    transaction_cost_bps: float = DEFAULT_TRANSACTION_COST_BPS,
    execution_lag: int = DEFAULT_EXECUTION_LAG,
    require_point_in_time: bool = True,
) -> dict:
    execution_lag = max(int(execution_lag), 1)
    index_lookup = {
        symbol: _build_index_lookup(records) for symbol, records in indexes.items()
    }
    all_rows: List[dict] = []
    sample_ranges = {}

    for code, name in CANDIDATES.items():
        records = sorted(histories[code]["records"], key=lambda item: item["date"])
        sample_ranges[code] = {
            "name": name,
            "observations": len(records),
            "start": records[0]["date"],
            "end": records[-1]["date"],
        }
        for index in range(251, len(records) - execution_lag):
            signal_date = records[index]["date"]
            market_snapshot = _market_snapshot_at(signal_date, index_lookup)
            if market_snapshot is None:
                continue
            entry_index = index + execution_lag
            if entry_index >= len(records):
                continue
            point_in_time_snapshot = _point_in_time_snapshot(
                code,
                signal_date,
                point_in_time,
                require_point_in_time,
            )
            metrics = _history_metrics(records[: index + 1])
            quality_score = _quality_score(metrics, code)
            trend_score = _trend_score(metrics)
            pullback_score = _pullback_score(metrics)
            regime_score, regime_state, regime_probabilities, _ = _market_regime_score(
                market_snapshot, []
            )
            candidate_holding = _holding_as_of(holdings.get(code, []), signal_date)
            current_holding = _holding_as_of(
                holdings.get(CURRENT_HOLDING_CODE, []), signal_date
            )
            holding_score, _, concentration, _, _, _, _ = _holding_score(
                candidate_holding, [], {}, signal_date
            )
            fit_score, overlap, _ = _portfolio_fit(candidate_holding, current_holding)
            holdings_available = bool(candidate_holding and current_holding)
            risk_penalty, volatility_multiplier = _risk_penalty(
                metrics, concentration, 100.0
            )
            defense_score = _defense_score(metrics)
            midterm_trend_score = _midterm_trend_score(metrics)
            stability_score = _stability_score(metrics)
            risk_adjusted_score = _risk_adjusted_score(metrics)
            market_score = _market_defense_score(regime_probabilities)
            portfolio_score = holding_score * 0.5 + fit_score * 0.5
            base_score = _architecture_score(
                {
                    "regime_score": regime_score,
                    "trend_score": trend_score,
                    "quality_score": quality_score,
                    "pullback_score": pullback_score,
                    "holding_score": holding_score,
                    "fit_score": fit_score,
                    "risk_penalty": risk_penalty,
                    "defense_score": defense_score,
                    "midterm_trend_score": midterm_trend_score,
                    "stability_score": stability_score,
                    "risk_adjusted_score": risk_adjusted_score,
                    "market_score": market_score,
                    "portfolio_score": portfolio_score,
                },
                "v3.0_multi_objective",
            )
            pit_score = None
            if (
                point_in_time_snapshot["fundamental_score"] is not None
                and point_in_time_snapshot["news_score"] is not None
            ):
                # Keep the historical score comparable while using the same
                # fundamental-led allocation as the production model.
                pit_score = (
                    point_in_time_snapshot["fundamental_score"] * 0.40
                    + market_score * 0.20
                    + trend_score * 0.20
                    + point_in_time_snapshot["news_score"] * 0.20
                )
                total_score = _clamp(base_score * 0.40 + pit_score * 0.60, 0, 100)
            else:
                total_score = base_score
            risk_ok = (
                holdings_available
                and point_in_time_snapshot["eligible"]
                and quality_score >= 50
                and midterm_trend_score >= 50
                and defense_score >= 55
                and stability_score >= 50
                and risk_adjusted_score >= 50
                and pullback_score >= 45
                and not (
                    regime_state == "压力/危机"
                    and regime_probabilities["stress"] >= 0.55
                )
                and holding_score >= 50
                and fit_score >= 35
                and overlap <= 0.65
                and metrics["zscore_60"] <= 1.8
                and metrics["momentum_20"] <= 0.12
                and metrics["annual_volatility"] <= 0.45
                and metrics["max_drawdown"] >= -0.45
                and metrics["cvar_95"] >= -0.04
                and metrics["max_drawdown_126"] >= -0.20
                and metrics["downside_volatility_126"] <= 0.28
                and metrics["cvar_95_126"] >= -0.04
                and metrics["recovery_days_126"] <= 126
            )
            hard_pass = risk_ok and total_score >= STRONG_BUY_THRESHOLD
            row = {
                "date": signal_date,
                "signal_date": signal_date,
                "information_cutoff_date": signal_date,
                "code": code,
                "name": name,
                "score": round(total_score, 4),
                "regime_score": round(regime_score, 4),
                "trend_score": round(trend_score, 4),
                "quality_score": round(quality_score, 4),
                "pullback_score": round(pullback_score, 4),
                "holding_score": round(holding_score, 4),
                "fit_score": round(fit_score, 4),
                "defense_score": round(defense_score, 4),
                "midterm_trend_score": round(midterm_trend_score, 4),
                "stability_score": round(stability_score, 4),
                "risk_adjusted_score": round(risk_adjusted_score, 4),
                "market_score": round(market_score, 4),
                "portfolio_score": round(portfolio_score, 4),
                "risk_penalty": round(risk_penalty, 4),
                "volatility_multiplier": round(volatility_multiplier, 4),
                "regime_state": regime_state,
                "hard_pass": hard_pass,
                "risk_ok": risk_ok,
                "holdings_available": holdings_available,
                "point_in_time_status": point_in_time_snapshot["status"],
                "point_in_time_eligible": point_in_time_snapshot["eligible"],
                "point_in_time_score": round(pit_score, 4) if pit_score is not None else None,
                "fundamental_score_pit": point_in_time_snapshot["fundamental_score"],
                "news_score_pit": point_in_time_snapshot["news_score"],
                "fundamental_as_of": point_in_time_snapshot["fundamental_as_of"],
                "news_as_of": point_in_time_snapshot["news_as_of"],
                "lookahead_check": point_in_time_snapshot["lookahead_check"],
                "holding_effective_date": records[entry_index]["date"],
                "entry_date": records[entry_index]["date"],
                "entry_nav": float(records[entry_index]["nav"]),
                "execution_lag_observations": execution_lag,
                "transaction_cost_bps": float(transaction_cost_bps),
            }
            for horizon in HORIZONS:
                target_index = entry_index + horizon
                raw_return = (
                    float(records[target_index]["nav"]) / row["entry_nav"] - 1
                    if target_index < len(records)
                    else None
                )
                row[f"return_{horizon}"] = (
                    _net_forward_return(
                        row["entry_nav"],
                        float(records[target_index]["nav"]),
                        transaction_cost_bps,
                    )
                    if target_index < len(records)
                    else None
                )
                row[f"gross_return_{horizon}"] = raw_return
                row[f"exit_date_{horizon}"] = (
                    records[target_index]["date"] if target_index < len(records) else ""
                )
                row[f"max_drawdown_{horizon}"] = _forward_max_drawdown(
                    records[index + 1 : target_index + 1]
                ) if target_index < len(records) else None
                if target_index < len(records):
                    forward_navs = [
                        float(item["nav"])
                        for item in records[index + 1 : target_index + 1]
                    ]
                    forward_risk = _forward_risk_metrics(forward_navs)
                    row[f"forward_cvar_{horizon}"] = forward_risk["cvar_95"]
                    row[f"forward_downside_volatility_{horizon}"] = forward_risk[
                        "downside_volatility"
                    ]
                    row[f"defensive_utility_{horizon}"] = _defensive_utility(
                        row[f"return_{horizon}"],
                        row[f"max_drawdown_{horizon}"],
                        row[f"forward_cvar_{horizon}"],
                    )
                    if horizon == 126:
                        row["forward_navs_126"] = forward_navs
                else:
                    row[f"forward_cvar_{horizon}"] = None
                    row[f"forward_downside_volatility_{horizon}"] = None
                    row[f"defensive_utility_{horizon}"] = None
            all_rows.append(row)

    top_by_date = {}
    for row in all_rows:
        previous = top_by_date.get(row["date"])
        if previous is None or row["score"] > previous["score"]:
            top_by_date[row["date"]] = row
    top_rows = sorted(top_by_date.values(), key=lambda item: item["date"])
    # Formal validation must not evaluate a base/technical score for dates where
    # the point-in-time filing or news evidence was unavailable.
    evaluation_rows = (
        [row for row in all_rows if row.get("point_in_time_eligible")]
        if require_point_in_time
        else all_rows
    )
    evaluation_top_by_date = {}
    for row in evaluation_rows:
        previous = evaluation_top_by_date.get(row["date"])
        if previous is None or row["score"] > previous["score"]:
            evaluation_top_by_date[row["date"]] = row
    evaluation_top_rows = sorted(
        evaluation_top_by_date.values(), key=lambda item: item["date"]
    )
    architecture_comparison = evaluate_architectures(evaluation_rows)
    architecture_comparison["evaluation_rows"] = len(evaluation_rows)
    architecture_comparison["excluded_rows_without_point_in_time_evidence"] = (
        len(all_rows) - len(evaluation_rows)
    )
    architecture_comparison["strict_point_in_time_only"] = require_point_in_time
    point_in_time_acceptance = point_in_time_acceptance_status(point_in_time)
    formal_acceptance_passed = bool(
        point_in_time_acceptance["ready"]
        and architecture_comparison["production_eligible"]
    )

    return {
        "model_version": MODEL_VERSION,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "research_universe_size": len(CANDIDATES),
        "research_universe": list(CANDIDATES),
        "sample_selection": (
            "主动管理型QDII研究池；排除指数联接、ETF、债券和重复外币份额；"
            "研究池不等于正式推荐池。"
        ),
        "sample_ranges": sample_ranges,
        "index_ranges": {
            symbol: {
                "observations": len(records),
                "start": records[0]["date"],
                "end": records[-1]["date"],
            }
            for symbol, records in indexes.items()
        },
        "holding_periods": {code: len(items) for code, items in holdings.items()},
        "point_in_time": {
            "required": require_point_in_time,
            "bundle_present": bool(point_in_time),
            "fundamental_news_cutoff": (
                "财报 available_date <= signal_date 且覆盖率不低于60%；"
                "新闻发布时间距信号日不超过45日"
            ),
            "acceptance": point_in_time_acceptance,
            "execution_rule": "next available fund NAV after signal_date",
            "transaction_cost_bps_one_way": float(transaction_cost_bps),
            "execution_lag_observations": execution_lag,
        },
        "rows": all_rows,
        "top_rows": top_rows,
        "evaluation_rows": evaluation_rows,
        "evaluation_top_rows": evaluation_top_rows,
        "summary_scope": (
            "strict-point-in-time" if require_point_in_time else "all-observations"
        ),
        "summary": summarize_results(evaluation_rows, evaluation_top_rows, sample_ranges),
        "descriptive_summary": summarize_results(all_rows, top_rows, sample_ranges),
        "architecture_comparison": architecture_comparison,
        "formal_acceptance": {
            "dataset_ready": point_in_time_acceptance["ready"],
            "model_statistics_passed": architecture_comparison["production_eligible"],
            "passed": formal_acceptance_passed,
            "blocking_reasons": point_in_time_acceptance["blocking_reasons"],
        },
        "scenario_tests": run_scenario_tests(),
        "limitations": [
            "历史新闻情绪未回填；新闻仅用于解释和风险提示，不参与量化分值。",
            "季度持仓按报告期末后30天生效，属于保守近似，不等于真实公告时刻。",
            "历史限购状态和历史同类排名无法完整还原；同类排名只作为当前资格过滤，不进入历史评分。",
            "基金信号在T日净值后生成，回测统一按下一个可用净值日成交。",
            "架构验收使用基金内非重叠持有期；阈值敏感性仍保留重叠事件并单列非重叠交易。",
        ],
    }


def _architecture_score(row: dict, architecture_name: str) -> float:
    weights = ARCHITECTURES[architecture_name]
    score = sum(
        float(row.get(field, 0)) * weight
        for field, weight in weights.items()
        if field != "risk_penalty"
    )
    score -= float(row.get("risk_penalty", 0)) * weights.get("risk_penalty", 0)
    return _clamp(score, 0, 100)


def _forward_max_drawdown(records: List[dict]) -> float:
    if not records:
        return None
    return _max_drawdown([float(item["nav"]) for item in records])


def _max_drawdown(values: List[float]) -> float:
    peak = values[0]
    drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    return drawdown


def _forward_risk_metrics(navs: List[float]) -> dict:
    returns = [navs[index] / navs[index - 1] - 1 for index in range(1, len(navs))]
    downside = [min(value, 0.0) for value in returns]
    tail_count = max(1, math.ceil(len(returns) * 0.05))
    return {
        "cvar_95": statistics.fmean(sorted(returns)[:tail_count]) if returns else 0.0,
        "downside_volatility": (
            statistics.pstdev(downside) * math.sqrt(252)
            if len(downside) > 1
            else 0.0
        ),
    }


def _defensive_utility(
    forward_return: float | None,
    forward_max_drawdown: float | None,
    forward_cvar: float | None,
) -> float | None:
    if None in (forward_return, forward_max_drawdown, forward_cvar):
        return None
    return (
        float(forward_return) * DEFENSIVE_OBJECTIVE_WEIGHTS["return"]
        + float(forward_max_drawdown) * DEFENSIVE_OBJECTIVE_WEIGHTS["max_drawdown"]
        + float(forward_cvar) * DEFENSIVE_OBJECTIVE_WEIGHTS["cvar"]
    )


def _mean_field(rows: List[dict], field: str) -> float | None:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    return statistics.fmean(values) if values else None


def evaluate_architectures(rows: List[dict]) -> dict:
    complete_126 = [row for row in rows if row.get("return_126") is not None]
    dates = sorted({row["date"] for row in complete_126})
    if len(dates) < 4:
        return {
            "split_boundaries": {"train": "--", "validation": "--", "test": "--"},
            "selection_rule": "候选权重预先固定；测试样本仅用于最终验收。",
            "selected_architecture": "v3.0_multi_objective",
            "production_eligible": False,
            "walk_forward": {"passed": False, "positive_window_ratio": 0.0, "windows": []},
            "architectures": {},
            "reason": "严格点时证据下的可用样本不足，不能进行样本外统计验收",
        }
    train_end = dates[len(dates) // 2]
    validation_end = dates[len(dates) * 3 // 4]
    periods = {
        "train": lambda value: value < train_end,
        "validation": lambda value: train_end <= value < validation_end,
        "test": lambda value: value >= validation_end,
    }
    results = {}
    for architecture_name, weights in ARCHITECTURES.items():
        scored = [
            {**row, "score": round(_architecture_score(row, architecture_name), 6)}
            for row in rows
        ]
        training_rows = _non_overlapping_panel(
            [row for row in scored if row.get("return_126") is not None and row["date"] < train_end],
            126,
        )
        training_scores = sorted(row["score"] for row in training_rows)
        frozen_threshold = _percentile(training_scores, 0.75)
        split_results = {
            name: _architecture_period_summary(
                [row for row in scored if predicate(row["date"])],
                frozen_threshold,
            )
            for name, predicate in periods.items()
        }
        results[architecture_name] = {
            "weights": weights,
            "frozen_training_threshold": frozen_threshold,
            "splits": split_results,
        }

    selected = results["v3.0_multi_objective"]
    selected_rows = [
        {**row, "score": round(_architecture_score(row, "v3.0_multi_objective"), 6)}
        for row in rows
    ]
    walk_forward = _walk_forward_summary(selected_rows)
    production_eligible = all(
        selected["splits"][split]["horizons"]["126"]["score_objective_correlation"]
        is not None
        and selected["splits"][split]["horizons"]["126"]["score_objective_correlation"] > 0
        and selected["splits"][split]["horizons"]["126"]["quartile_spread"] > 0
        and selected["splits"][split]["horizons"]["126"]["objective_spread"] > 0
        and selected["splits"][split]["horizons"]["126"]["top_quartile_max_drawdown"]
        >= selected["splits"][split]["horizons"]["126"]["bottom_quartile_max_drawdown"]
        and selected["splits"][split]["horizons"]["126"]["top_quartile_forward_cvar"]
        >= selected["splits"][split]["horizons"]["126"]["bottom_quartile_forward_cvar"]
        and selected["splits"][split]["independent_observations"]
        >= MIN_INDEPENDENT_EVENTS_PER_SPLIT
        for split in ("train", "validation", "test")
    ) and walk_forward["passed"]
    return {
        "split_boundaries": {
            "train": f"{dates[0]} to {dates[dates.index(train_end) - 1]}",
            "validation": f"{train_end} to {dates[dates.index(validation_end) - 1]}",
            "test": f"{validation_end} to {dates[-1]}",
        },
        "selection_rule": (
            "候选权重预先固定；只用训练集和验证集确定架构，测试集仅用于最终验收。"
        ),
        "selected_architecture": "v3.0_multi_objective",
        "production_eligible": production_eligible,
        "walk_forward": walk_forward,
        "architectures": results,
    }


def _walk_forward_summary(rows: List[dict]) -> dict:
    complete = [row for row in rows if row.get("return_126") is not None]
    dates = sorted({row["date"] for row in complete})
    if len(dates) < 120:
        return {"passed": False, "positive_window_ratio": 0.0, "windows": []}
    start_index = len(dates) // 2
    window_size = max(40, len(dates) // 8)
    windows = []
    for index in range(start_index, len(dates), window_size):
        test_dates = set(dates[index : index + window_size])
        if not test_dates:
            continue
        train_rows = _non_overlapping_panel(
            [row for row in complete if row["date"] < dates[index]], 126
        )
        threshold = _percentile(sorted(row["score"] for row in train_rows), 0.75)
        summary = _architecture_period_summary(
            [row for row in complete if row["date"] in test_dates], threshold
        )
        horizon = summary["horizons"]["126"]
        windows.append(
            {
                "start": min(test_dates),
                "end": max(test_dates),
                "independent_observations": summary["independent_observations"],
                "score_objective_correlation": horizon["score_objective_correlation"],
                "objective_spread": horizon["objective_spread"],
                "drawdown_advantage": (
                    horizon["top_quartile_max_drawdown"]
                    >= horizon["bottom_quartile_max_drawdown"]
                ),
                "cvar_advantage": (
                    horizon["top_quartile_forward_cvar"]
                    >= horizon["bottom_quartile_forward_cvar"]
                ),
            }
        )
    valid = [item for item in windows if item["score_objective_correlation"] is not None]
    positive = [
        item for item in valid
        if item["score_objective_correlation"] > 0
        and item["objective_spread"] is not None
        and item["objective_spread"] > 0
        and item["drawdown_advantage"]
        and item["cvar_advantage"]
    ]
    ratio = len(positive) / len(valid) if valid else 0.0
    return {
        "passed": len(valid) >= 3 and ratio >= 2 / 3,
        "positive_window_ratio": ratio,
        "windows": windows,
    }


def _architecture_period_summary(rows: List[dict], threshold: float) -> dict:
    complete_dates = [row["date"] for row in rows if row.get("return_126") is not None]
    horizons = {}
    for horizon in HORIZONS:
        field = f"return_{horizon}"
        eligible = _non_overlapping_panel(
            [row for row in rows if row.get(field) is not None], horizon
        )
        ordered = sorted(eligible, key=lambda item: item["score"])
        size = max(1, len(ordered) // 4)
        top = ordered[-size:]
        bottom = ordered[:size]
        top_average = _mean_return(top, field)
        bottom_average = _mean_return(bottom, field)
        horizons[str(horizon)] = {
            "observations": len(eligible),
            "score_return_correlation": _pearson(
                [row["score"] for row in eligible],
                [row[field] for row in eligible],
            ),
            "score_objective_correlation": _pearson(
                [row["score"] for row in eligible],
                [row[f"defensive_utility_{horizon}"] for row in eligible],
            ),
            "top_quartile": _return_summary(top, field),
            "bottom_quartile": _return_summary(bottom, field),
            "quartile_spread": (
                top_average - bottom_average
                if top_average is not None and bottom_average is not None
                else None
            ),
            "top_quartile_max_drawdown": _mean_field(top, f"max_drawdown_{horizon}"),
            "bottom_quartile_max_drawdown": _mean_field(bottom, f"max_drawdown_{horizon}"),
            "top_quartile_forward_cvar": _mean_field(top, f"forward_cvar_{horizon}"),
            "bottom_quartile_forward_cvar": _mean_field(bottom, f"forward_cvar_{horizon}"),
            "objective_spread": (
                _mean_field(top, f"defensive_utility_{horizon}")
                - _mean_field(bottom, f"defensive_utility_{horizon}")
                if _mean_field(top, f"defensive_utility_{horizon}") is not None
                and _mean_field(bottom, f"defensive_utility_{horizon}") is not None
                else None
            ),
        }

    top_by_date = {}
    for row in rows:
        previous = top_by_date.get(row["date"])
        if previous is None or row["score"] > previous["score"]:
            top_by_date[row["date"]] = row
    signals = [
        row for row in top_by_date.values()
        if row["score"] >= threshold and row.get("return_126") is not None
    ]
    non_overlap = _non_overlapping_trades(signals, 126)
    risk = _trade_risk_summary(non_overlap, "return_126")
    return {
        "start": min(complete_dates) if complete_dates else "--",
        "end": max(complete_dates) if complete_dates else "--",
        "independent_observations": len(
            _non_overlapping_panel(
                [row for row in rows if row.get("return_126") is not None], 126
            )
        ),
        "horizons": horizons,
        "signal_count": len(signals),
        "signal_126": _return_summary(signals, "return_126"),
        "non_overlap_trades": len(non_overlap),
        "non_overlap_average_return": _mean_return(non_overlap, "return_126"),
        "non_overlap_hit_rate": _hit_rate(non_overlap, "return_126"),
        "trade_max_drawdown": risk["max_drawdown"],
        "trade_cvar_95": risk["cvar_95"],
    }


def summarize_results(
    rows: List[dict], top_rows: List[dict], sample_ranges: dict
) -> dict:
    if not rows:
        empty_return = _return_summary([], "return_126")
        empty_horizon = {
            "all": empty_return,
            "top_quartile": empty_return,
            "bottom_quartile": empty_return,
            "score_return_correlation": None,
        }
        return {
            "statistics_available": False,
            "candidate_day_observations": 0,
            "top_candidate_days": 0,
            "backtest_start": "--",
            "backtest_end": "--",
            "holdings_coverage": 0.0,
            "point_in_time_coverage": 0.0,
            "lookahead_failures": 0,
            "all_events_126": empty_return,
            "non_overlap_panel_126": empty_return,
            "non_overlap_counts_by_fund_126": {},
            "top_score_quartile_126": empty_return,
            "bottom_score_quartile_126": empty_return,
            "score_return_correlation_126": None,
            "score_objective_correlation_126": None,
            "score_distribution": {"minimum": 0.0, "median": 0.0, "maximum": 0.0},
            "horizon_analysis": {str(horizon): dict(empty_horizon) for horizon in HORIZONS},
            "component_correlations_126": {
                field: None
                for field in (
                    "regime_score", "trend_score", "quality_score", "pullback_score",
                    "holding_score", "fit_score", "defense_score", "midterm_trend_score",
                    "stability_score", "risk_adjusted_score", "market_score",
                    "portfolio_score", "risk_penalty",
                )
            },
            "raw_hard_pass_days": 0,
            "confirmed_signal_days": 0,
            "confirmed_signal_126": empty_return,
            "threshold_tests": [],
            "buy_hold": {},
        }
    complete_126 = [row for row in rows if row["return_126"] is not None]
    non_overlap_panel = _non_overlapping_panel(complete_126, 126)
    non_overlap_counts_by_fund = _non_overlap_counts_by_fund(complete_126, 126)
    ordered = sorted(non_overlap_panel, key=lambda item: item["score"])
    quartile_size = max(1, len(ordered) // 4)
    bottom = ordered[:quartile_size]
    top = ordered[-quartile_size:]
    production_raw = [row for row in top_rows if row["hard_pass"]]
    production_confirmed = [
        current
        for previous, current in zip(top_rows, top_rows[1:])
        if previous["hard_pass"]
        and current["hard_pass"]
        and previous["code"] == current["code"]
    ]

    threshold_tests = []
    for threshold in (55, 60, 65, 68, 70, 75, 80, 82):
        selected = [
            row for row in top_rows
            if row["score"] >= threshold and row["return_126"] is not None
        ]
        non_overlap = _non_overlapping_trades(selected, 126)
        threshold_tests.append(
            {
                "threshold": threshold,
                **_return_summary(selected, "return_126"),
                "non_overlap_trades": len(non_overlap),
                "non_overlap_average_return": _mean_return(non_overlap, "return_126"),
                "non_overlap_hit_rate": _hit_rate(non_overlap, "return_126"),
            }
        )

    buy_hold = {}
    for code, info in sample_ranges.items():
        code_rows = sorted(
            [row for row in rows if row["code"] == code], key=lambda item: item["date"]
        )
        if code_rows:
            first = code_rows[0]
            last = code_rows[-1]
            buy_hold[code] = {
                "name": info["name"],
                "start": first["entry_date"],
                "end": last["entry_date"],
                "return": last["entry_nav"] / first["entry_nav"] - 1,
            }

    horizon_analysis = {}
    for horizon in HORIZONS:
        field = f"return_{horizon}"
        horizon_rows = _non_overlapping_panel(
            [row for row in rows if row[field] is not None], horizon
        )
        horizon_ordered = sorted(horizon_rows, key=lambda item: item["score"])
        size = max(1, len(horizon_ordered) // 4)
        horizon_analysis[str(horizon)] = {
            "all": _return_summary(horizon_rows, field),
            "top_quartile": _return_summary(horizon_ordered[-size:], field),
            "bottom_quartile": _return_summary(horizon_ordered[:size], field),
            "score_return_correlation": _pearson(
                [row["score"] for row in horizon_rows],
                [row[field] for row in horizon_rows],
            ),
        }

    component_correlations = {}
    for field in (
        "regime_score",
        "trend_score",
        "quality_score",
        "pullback_score",
        "holding_score",
        "fit_score",
        "defense_score",
        "midterm_trend_score",
        "stability_score",
        "risk_adjusted_score",
        "market_score",
        "portfolio_score",
        "risk_penalty",
    ):
        component_correlations[field] = _pearson(
            [row[field] for row in non_overlap_panel],
            [row["defensive_utility_126"] for row in non_overlap_panel],
        )
    scores = sorted(row["score"] for row in rows)

    return {
        "statistics_available": True,
        "candidate_day_observations": len(rows),
        "top_candidate_days": len(top_rows),
        "backtest_start": min(row["date"] for row in rows),
        "backtest_end": max(row["date"] for row in rows),
        "holdings_coverage": sum(1 for row in rows if row["holdings_available"]) / len(rows),
        "point_in_time_coverage": (
            sum(1 for row in rows if row.get("point_in_time_eligible")) / len(rows)
            if rows else 0.0
        ),
        "lookahead_failures": sum(
            1
            for row in rows
            if not (row.get("lookahead_check") or {}).get("passed", False)
        ),
        "all_events_126": _return_summary(complete_126, "return_126"),
        "non_overlap_panel_126": _return_summary(non_overlap_panel, "return_126"),
        "non_overlap_counts_by_fund_126": non_overlap_counts_by_fund,
        "top_score_quartile_126": _return_summary(top, "return_126"),
        "bottom_score_quartile_126": _return_summary(bottom, "return_126"),
        "score_return_correlation_126": _pearson(
            [row["score"] for row in non_overlap_panel],
            [row["return_126"] for row in non_overlap_panel],
        ),
        "score_objective_correlation_126": _pearson(
            [row["score"] for row in non_overlap_panel],
            [row["defensive_utility_126"] for row in non_overlap_panel],
        ),
        "score_distribution": {
            "minimum": scores[0],
            "median": statistics.median(scores),
            "maximum": scores[-1],
        },
        "horizon_analysis": horizon_analysis,
        "component_correlations_126": component_correlations,
        "raw_hard_pass_days": len(production_raw),
        "confirmed_signal_days": len(production_confirmed),
        "confirmed_signal_126": _return_summary(
            [row for row in production_confirmed if row["return_126"] is not None],
            "return_126",
        ),
        "threshold_tests": threshold_tests,
        "buy_hold": buy_hold,
    }


def run_scenario_tests() -> List[dict]:
    start = date(2025, 1, 1)
    stable = [
        {"date": (start + timedelta(days=index)).isoformat(), "nav": 1 + index * 0.001}
        for index in range(300)
    ]
    falling = [
        {"date": (start + timedelta(days=index)).isoformat(), "nav": 1.4 - index * 0.001}
        for index in range(300)
    ]
    volatile = [
        {
            "date": (start + timedelta(days=index)).isoformat(),
            "nav": 1 + index * 0.0003 + (0.08 if index % 2 else -0.08),
        }
        for index in range(300)
    ]
    stable_metrics = _history_metrics(stable)
    falling_metrics = _history_metrics(falling)
    volatile_metrics = _history_metrics(volatile)
    _, stable_multiplier = _risk_penalty(stable_metrics, 0.38, 100)
    _, volatile_multiplier = _risk_penalty(volatile_metrics, 0.38, 100)
    tests = [
        (
            "上升趋势评分高于下降趋势",
            _trend_score(stable_metrics) > _trend_score(falling_metrics),
        ),
        (
            "高波动样本仓位系数更低",
            volatile_multiplier < stable_multiplier,
        ),
        (
            "过热Z-Score不会获得理想回撤分",
            _pullback_score(stable_metrics) < 80,
        ),
        (
            "相同收益下较小回撤具有更高防御效用",
            _defensive_utility(0.10, -0.08, -0.02)
            > _defensive_utility(0.10, -0.20, -0.04),
        ),
        (
            "v3.0排序权重合计为100%",
            abs(sum(ARCHITECTURES["v3.0_multi_objective"].values()) - 1.0) < 1e-9,
        ),
    ]
    return [{"name": name, "passed": passed} for name, passed in tests]


def _backtest_quality_score(metrics: dict) -> float:
    neutral_rank_score = 50.0
    return (
        _scale(metrics["annual_return"], -0.10, 0.25) * 0.20
        + _scale(metrics["sharpe"], -0.3, 1.5) * 0.20
        + _scale(metrics["sortino"], -0.3, 2.0) * 0.15
        + _scale(metrics["calmar"], -0.2, 1.5) * 0.15
        + _scale(metrics["max_drawdown"], -0.65, -0.10) * 0.15
        + neutral_rank_score * 0.15
    )


def _backtest_holding_score(holding: dict | None, as_of: str) -> tuple[float, float]:
    if not holding or not holding.get("holdings"):
        return 50.0, 0.38
    items = holding["holdings"]
    concentration = sum(item["weight"] for item in items)
    age = (
        datetime.strptime(as_of, "%Y-%m-%d").date()
        - datetime.strptime(holding["disclosure_date"], "%Y-%m-%d").date()
    ).days
    recency_score = math.exp(-max(age, 0) / 120) * 100
    concentration_score = _triangle(concentration, 0.18, 0.38, 0.70)
    score = 50 * 0.45 + concentration_score * 0.30 + recency_score * 0.25
    return score, concentration


def _holding_as_of(periods: List[dict], as_of: str) -> dict | None:
    available = [item for item in periods if item.get("effective_date", "9999") <= as_of]
    return available[-1] if available else None


def _build_index_lookup(records: List[dict]) -> tuple[List[str], List[float]]:
    ordered = sorted(records, key=lambda item: item["date"])
    return [item["date"] for item in ordered], [float(item["close"]) for item in ordered]


def _market_snapshot_at(
    as_of: str, lookups: Dict[str, tuple[List[str], List[float]]]
) -> dict | None:
    indexes = []
    for symbol in ("IXIC", "DJI"):
        dates, closes = lookups[symbol]
        position = bisect_right(dates, as_of)
        if position < 60:
            return None
        series = closes[:position]
        price = series[-1]
        ma20 = statistics.fmean(series[-20:])
        ma60 = statistics.fmean(series[-60:])
        indexes.append(
            {
                "symbol": symbol,
                "price": price,
                "ma20_gap_pct": (price / ma20 - 1) * 100,
                "ma60_gap_pct": (price / ma60 - 1) * 100,
                "source": "eastmoney-index",
            }
        )
    return {"indexes": indexes}


def _return_summary(rows: List[dict], field: str) -> dict:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    if not values:
        return {"count": 0, "average_return": None, "median_return": None, "hit_rate": None}
    return {
        "count": len(values),
        "average_return": statistics.fmean(values),
        "median_return": statistics.median(values),
        "hit_rate": sum(value > 0 for value in values) / len(values),
    }


def _non_overlapping_trades(rows: List[dict], horizon: int) -> List[dict]:
    selected = []
    next_available = "0000-00-00"
    for row in sorted(rows, key=lambda item: item["entry_date"]):
        if row["entry_date"] < next_available:
            continue
        exit_date = row.get(f"exit_date_{horizon}")
        if not exit_date:
            continue
        selected.append(row)
        next_available = exit_date
    return selected


def _non_overlapping_panel(rows: List[dict], horizon: int) -> List[dict]:
    """Keep one forward outcome at a time for each research fund.

    The first eligible entry is used as the deterministic anchor, followed by
    the next entry only after the previous holding period has ended. This is
    intentionally stricter than a rolling daily panel and prevents overlapping
    126-day outcomes from being counted as independent evidence.
    """
    selected: List[dict] = []
    for code in sorted({row["code"] for row in rows}):
        next_available = "0000-00-00"
        fund_rows = sorted(
            (row for row in rows if row["code"] == code),
            key=lambda item: item["entry_date"],
        )
        for row in fund_rows:
            exit_date = row.get(f"exit_date_{horizon}")
            if not exit_date or row["entry_date"] < next_available:
                continue
            selected.append(row)
            next_available = exit_date
    return sorted(selected, key=lambda item: (item["entry_date"], item["code"]))


def _non_overlap_counts_by_fund(rows: List[dict], horizon: int) -> dict[str, int]:
    """Return auditable independent-event counts for every fund in the panel."""
    return {
        code: len(_non_overlapping_panel(
            [row for row in rows if row.get("code") == code], horizon
        ))
        for code in sorted({str(row.get("code")) for row in rows if row.get("code")})
    }


def _mean_return(rows: List[dict], field: str) -> float | None:
    values = [row[field] for row in rows if row.get(field) is not None]
    return statistics.fmean(values) if values else None


def _hit_rate(rows: List[dict], field: str) -> float | None:
    values = [row[field] for row in rows if row.get(field) is not None]
    return sum(value > 0 for value in values) / len(values) if values else None


def _percentile(values: List[float], quantile: float) -> float:
    if not values:
        return 0.0
    position = (len(values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def _trade_risk_summary(rows: List[dict], field: str) -> dict:
    paths = [row.get("forward_navs_126") for row in rows if row.get("forward_navs_126")]
    if not paths:
        return {"max_drawdown": None, "cvar_95": None}
    # Concatenate daily holding paths, not only terminal trade returns.
    daily_returns = []
    for path in paths:
        daily_returns.extend(
            [path[index] / path[index - 1] - 1 for index in range(1, len(path))]
        )
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in daily_returns:
        equity *= 1 + value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    tail_count = max(1, math.ceil(len(daily_returns) * 0.05))
    return {
        "max_drawdown": max_drawdown,
        "cvar_95": statistics.fmean(sorted(daily_returns)[:tail_count]),
    }


def _pearson(left: List[float], right: List[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left)
        * sum((y - right_mean) ** 2 for y in right)
    )
    return numerator / denominator if denominator else 0.0


def export_samples(
    histories: Dict[str, dict],
    indexes: Dict[str, List[dict]],
    holdings: Dict[str, List[dict]],
    result: dict,
) -> None:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    with (SAMPLE_DIR / "fund_nav.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["code", "name", "date", "nav"])
        for code, name in CANDIDATES.items():
            for item in sorted(histories[code]["records"], key=lambda row: row["date"]):
                writer.writerow([code, name, item["date"], item["nav"]])
    with (SAMPLE_DIR / "index_daily.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["symbol", "date", "close"])
        for symbol, records in indexes.items():
            for item in records:
                writer.writerow([symbol, item["date"], item["close"]])
    _write_json(SAMPLE_DIR / "holdings_quarterly.json", holdings)
    with (SAMPLE_DIR / "model_events.csv").open("w", encoding="utf-8-sig", newline="") as file:
        fields = [
            field for field in result["rows"][0].keys()
            if field != "forward_navs_126"
        ]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(
            {field: row.get(field) for field in fields}
            for row in result["rows"]
        )


def write_reports(result: dict) -> tuple[Path, Path, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORTS_DIR / "model_backtest_result.json"
    txt_path = REPORTS_DIR / "model_backtest_report.txt"
    html_path = REPORTS_DIR / "model_backtest_report.html"
    serializable = {key: value for key, value in result.items() if key not in {"rows", "top_rows"}}
    _write_json(json_path, serializable)
    report_text = format_report(result)
    atomic_write_text(txt_path, report_text)
    atomic_write_text(
        html_path,
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>GAF-QM模型回测报告</title>"
        "<style>body{margin:0;background:#eef2f6;color:#142033;font-family:'Microsoft YaHei UI',sans-serif}"
        ".wrap{max-width:1080px;margin:28px auto;padding:24px;background:#fff;border-top:5px solid #1677b8}"
        "h1{font-size:24px;margin:0 0 14px}pre{white-space:pre-wrap;line-height:1.75;font-size:14px;font-family:Consolas,'Microsoft YaHei UI',sans-serif}</style>"
        f"<body><main class='wrap'><h1>{html.escape(MODEL_VERSION)} 模型回测报告</h1>"
        f"<pre>{html.escape(report_text)}</pre></main></body></html>",
    )
    return html_path, txt_path, json_path


def format_report(result: dict) -> str:
    summary = result["summary"]
    descriptive_summary = result.get("descriptive_summary", {})
    all_events = summary["all_events_126"]
    top = summary["top_score_quartile_126"]
    bottom = summary["bottom_score_quartile_126"]
    non_overlap_counts = summary.get("non_overlap_counts_by_fund_126", {})
    non_overlap_count_text = "；".join(
        f"{code}:{count}"
        for code, count in sorted(non_overlap_counts.items())
    ) or "--"
    lines = [
        f"模型：{result['model_version']}",
        f"生成时间：{result['generated_at']}",
        (
            "核心统计口径：严格点时样本"
            if result.get("summary_scope") == "strict-point-in-time"
            else "核心统计口径：全部可用样本（非正式验收）"
        ),
        f"严格样本外区间：{summary['backtest_start']} 至 {summary['backtest_end']}",
        f"研究基金池：{result.get('research_universe_size', len(result['sample_ranges']))}只；"
        f"{result.get('sample_selection', '研究样本与正式推荐池分离')}",
        f"候选基金日样本：{summary['candidate_day_observations']}；每日最高候选样本：{summary['top_candidate_days']}",
        f"基金独立非重叠126日样本：{summary['non_overlap_panel_126']['count']}条；"
        f"平均收益 {_fmt_pct(summary['non_overlap_panel_126']['average_return'])}；"
        f"胜率 {_fmt_pct(summary['non_overlap_panel_126']['hit_rate'])}",
        f"非重叠样本按基金计数：{non_overlap_count_text}",
        "非重叠规则：每只基金从最早有效入场点开始，前一笔126日持有期结束后才允许下一笔；不把重叠日计作独立样本。",
        f"历史持仓覆盖率：{summary['holdings_coverage']:.1%}",
        f"点时财报与新闻覆盖率：{summary.get('point_in_time_coverage', 0.0):.1%}"
        f"；未来信息检查失败样本：{summary.get('lookahead_failures', 0)} 条",
        f"执行假设：信号后 {result.get('point_in_time', {}).get('execution_lag_observations', 1)} 个可用净值日成交"
        f"；单边成本 {result.get('point_in_time', {}).get('transaction_cost_bps_one_way', 0):.0f}bp",
        "",
        "一、样本范围",
    ]
    formal = result.get("formal_acceptance", {})
    dataset = result.get("point_in_time", {}).get("acceptance", {})
    lines.insert(7,
        f"正式样本外验收：{'通过' if formal.get('passed') else '未通过/未就绪'}；"
        f"点时数据集{'已就绪' if formal.get('dataset_ready') else '未就绪'}；"
        f"统计验收{'通过' if formal.get('model_statistics_passed') else '未通过'}"
    )
    lines.insert(8,
        f"点时归档：财报事件 {dataset.get('fundamental_events', 0)} 条、"
        f"可映射新闻 {dataset.get('mapped_news_events', 0)} 条；"
        f"财报跨度 {dataset.get('fundamental_span_days', 0)} 天、新闻跨度 {dataset.get('news_span_days', 0)} 天"
    )
    lines.insert(9,
        f"基金级财报覆盖：{dataset.get('funds_with_60pct_fundamental_coverage', 0)}/"
        f"{dataset.get('required_funds_with_60pct_fundamental_coverage', 0) or '--'}只基金达到60%；"
        f"有效快照 {dataset.get('usable_fundamental_snapshots', 0)}/"
        f"{dataset.get('required_usable_fundamental_snapshots', 0) or '--'}条"
    )
    if not summary.get("statistics_available", False):
        lines.insert(10,
            "严格点时样本不足，以下核心统计不生成结论；描述性全样本仅保留在JSON中，不构成验收证据。"
        )
    for code, item in result["sample_ranges"].items():
        lines.append(
            f"{code} {item['name']}：{item['start']} 至 {item['end']}，{item['observations']}条净值"
        )
    for symbol, item in result["index_ranges"].items():
        lines.append(
            f"{symbol}指数：{item['start']} 至 {item['end']}，{item['observations']}条日线"
        )
    lines.extend(
        [
            "",
            "二、63/126/189净值日事件测试（对应约3/6/9个月）",
        ]
    )
    for horizon in HORIZONS:
        item = summary["horizon_analysis"][str(horizon)]
        lines.append(
            f"{horizon}日：相关系数 {_fmt_number(item['score_return_correlation'])}；"
            f"高评分25%平均 {_fmt_pct(item['top_quartile']['average_return'])}；"
            f"低评分25%平均 {_fmt_pct(item['bottom_quartile']['average_return'])}"
        )
    distribution = summary["score_distribution"]
    lines.extend(
        [
            f"综合分分布：最低 {distribution['minimum']:.1f}，中位数 {distribution['median']:.1f}，最高 {distribution['maximum']:.1f}",
            "",
            "三、子模块与未来126日防御效用相关系数",
        ]
    )
    component_names = {
        "regime_score": "市场状态",
        "trend_score": "趋势动量",
        "quality_score": "基金质量",
        "pullback_score": "回撤估值",
        "holding_score": "持仓景气",
        "fit_score": "组合分散",
        "risk_penalty": "风险扣分",
        "defense_score": "下行防御",
        "midterm_trend_score": "中期趋势",
        "stability_score": "收益稳定",
        "risk_adjusted_score": "风险调整收益",
        "market_score": "市场防御",
        "portfolio_score": "组合适配",
    }
    for field, value in summary["component_correlations_126"].items():
        lines.append(f"{component_names[field]}：{_fmt_number(value)}")
    lines.extend(
        [
            "",
            "四、生产强买门槛",
            f"单日通过全部硬门槛：{summary['raw_hard_pass_days']}次",
            f"连续两个数据日确认：{summary['confirmed_signal_days']}次",
            _summary_line("确认信号126日表现", summary["confirmed_signal_126"]),
            "",
            "五、阈值敏感性（每日最高候选）",
        ]
    )
    for item in summary["threshold_tests"]:
        lines.append(
            f"分数≥{item['threshold']}：事件{item['count']}次，平均 {_fmt_pct(item['average_return'])}，"
            f"胜率 {_fmt_pct(item['hit_rate'])}；非重叠交易{item['non_overlap_trades']}次，"
            f"平均 {_fmt_pct(item['non_overlap_average_return'])}，胜率 {_fmt_pct(item['non_overlap_hit_rate'])}"
        )
    comparison = result["architecture_comparison"]
    boundaries = comparison["split_boundaries"]
    lines.extend(
        [
            "",
            "六、预定义架构的时间切分检验",
            f"训练集：{boundaries['train']}",
            f"验证集：{boundaries['validation']}",
            f"测试集：{boundaries['test']}",
            f"最终架构：{comparison['selected_architecture']}；"
            f"生产验收：{'通过' if comparison['production_eligible'] else '不通过'}",
            f"严格点时统计样本：{comparison.get('evaluation_rows', 0)} 条；"
            f"剔除无点时证据样本：{comparison.get('excluded_rows_without_point_in_time_evidence', 0)} 条",
        ]
    )
    if comparison.get("reason"):
        lines.append(f"统计验收限制：{comparison['reason']}")
    for architecture_name, architecture in comparison["architectures"].items():
        lines.append(
            f"{architecture_name}（训练集75%分位阈值 {architecture['frozen_training_threshold']:.1f}）"
        )
        for split_name in ("train", "validation", "test"):
            item = architecture["splits"][split_name]
            horizon = item["horizons"]["126"]
            lines.append(
                f"  {split_name}：独立样本 {item['independent_observations']} 条，"
                f"126日收益相关 {_fmt_number(horizon['score_return_correlation'])}，"
                f"防御效用相关 {_fmt_number(horizon['score_objective_correlation'])}，"
                f"收益高低组差 {_fmt_pct(horizon['quartile_spread'])}，"
                f"防御效用差 {_fmt_number(horizon['objective_spread'])}，信号 {item['signal_count']} 次，"
                f"非重叠 {item['non_overlap_trades']} 次/胜率 {_fmt_pct(item['non_overlap_hit_rate'])}，"
                f"交易序列最大回撤 {_fmt_pct(item['trade_max_drawdown'])}，"
                f"CVaR95 {_fmt_pct(item['trade_cvar_95'])}"
            )
    walk_forward = comparison["walk_forward"]
    lines.append(
        f"滚动样本外窗口：{len(walk_forward['windows'])}个；完整通过比例 "
        f"{walk_forward['positive_window_ratio']:.1%}；"
        f"{'通过' if walk_forward['passed'] else '不通过'}"
    )
    for item in walk_forward["windows"]:
        lines.append(
            f"  {item['start']}至{item['end']}：独立样本{item['independent_observations']}条，"
            f"防御效用相关 {_fmt_number(item['score_objective_correlation'])}，"
            f"效用差 {_fmt_number(item['objective_spread'])}，"
            f"回撤{'通过' if item['drawdown_advantage'] else '失败'}，"
            f"CVaR{'通过' if item['cvar_advantage'] else '失败'}"
        )
    lines.extend(["", "七、同期买入并持有"])
    for code, item in summary["buy_hold"].items():
        lines.append(
            f"{code} {item['name']}：{item['start']} 至 {item['end']}，收益 {_fmt_pct(item['return'])}"
        )
    lines.extend(["", "八、数学逻辑测试"])
    for item in result["scenario_tests"]:
        lines.append(f"{'通过' if item['passed'] else '失败'}：{item['name']}")
    lines.extend(["", "九、结论"])
    if summary["confirmed_signal_days"] < 30:
        lines.append("当前生产强买信号样本不足30次，不能证明强买规则具有统计有效性。")
    if top["average_return"] is not None and bottom["average_return"] is not None:
        if top["average_return"] > bottom["average_return"]:
            lines.append("高评分组未来126日平均收益高于低评分组，排序方向初步有效。")
        else:
            lines.append("高评分组未跑赢低评分组，当前权重或阈值需要重新校准。")
    if summary["score_return_correlation_126"] is not None and summary["score_return_correlation_126"] < 0:
        lines.append("当前版本未通过统计有效性测试，暂不应把评分用于真实资金的强买决策。")
    lines.append("本报告只能验证历史样本内的统计表现，不代表未来收益或本金安全。")
    if formal.get("passed"):
        lines.append("防御架构在训练、验证和最终测试三段的126日收益、防御效用、回撤和CVaR均通过验收，允许以严格硬门槛上线观察。")
    else:
        lines.append("正式验收未通过或点时数据集未就绪，生产强买信号和精确减持应保持关闭。")
    lines.extend(["", "十、未覆盖与限制"])
    if not result.get("point_in_time", {}).get("bundle_present"):
        lines.append(
            "- 未找到点时财报/新闻历史包；严格回测仅保留审计记录，不可据此通过生产强买验收。"
        )
    for item in formal.get("blocking_reasons", []):
        lines.append(f"- 正式验收阻断：{item}")
    lines.extend(f"- {item}" for item in result["limitations"])
    return "\n".join(lines)


def _summary_line(label: str, summary: dict) -> str:
    return (
        f"{label}：{summary['count']}次，平均 {_fmt_pct(summary['average_return'])}，"
        f"中位数 {_fmt_pct(summary['median_return'])}，正收益率 {_fmt_pct(summary['hit_rate'])}"
    )


def _fmt_pct(value: float | None) -> str:
    return "--" if value is None else f"{value:.2%}"


def _fmt_number(value: float | None) -> str:
    return "--" if value is None else f"{value:.3f}"


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, payload: object) -> None:
    atomic_write_json(path, payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="GAF-QM混合模型历史回测")
    parser.add_argument("--use-cache", action="store_true", help="优先使用已保存的指数和持仓样本")
    parser.add_argument(
        "--formal-acceptance",
        action="store_true",
        help="仅在点时数据集通过硬门槛后运行正式样本外验收",
    )
    args = parser.parse_args()
    point_in_time = None

    if args.formal_acceptance:
        from point_in_time_archive import build_backtest_panel, load_historical_holdings

        point_in_time = build_backtest_panel(load_historical_holdings())
        readiness = point_in_time_acceptance_status(point_in_time)
        readiness_payload = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "model_version": MODEL_VERSION,
            "formal_out_of_sample_acceptance": readiness,
            "next_action": (
                "运行严格点时样本外回测"
                if readiness["ready"]
                else "继续归档历史财报披露日与带发布时间的可映射新闻；强烈买入与精确减持保持关闭"
            ),
        }
        atomic_write_json(REPORTS_DIR / "model_formal_acceptance.json", readiness_payload)
        if not readiness["ready"]:
            print(json.dumps(readiness_payload, ensure_ascii=False, indent=2))
            return

    histories = load_research_histories(use_cache=args.use_cache)
    sample_dates = [
        item["date"]
        for history in histories.values()
        for item in history.get("records", [])
    ]
    start_date = min(sample_dates)
    end_date = max(sample_dates)
    indexes = fetch_index_histories(use_cache=args.use_cache)
    holdings = fetch_holdings_histories(start_date, end_date, use_cache=args.use_cache)
    point_in_time = point_in_time or load_point_in_time_history()
    result = run_backtest(
        histories,
        indexes,
        holdings,
        point_in_time=point_in_time,
        require_point_in_time=True,
    )
    export_samples(histories, indexes, holdings, result)
    html_path, txt_path, json_path = write_reports(result)
    print(
        json.dumps(
            {
                "generated_at": result["generated_at"],
                "html": str(html_path),
                "text": str(txt_path),
                "json": str(json_path),
                "summary": result["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
