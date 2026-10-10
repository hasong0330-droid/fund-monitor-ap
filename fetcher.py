import csv
import io
import json
import math
import re
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from config import (
    AI_CYCLE_NEWS_CACHE_FILE,
    AI_CYCLE_NEWS_QUERIES,
    CACHE_DIR,
    BEA_NIPA_ANNUAL_URL,
    CBOE_VIX_HISTORY_URL,
    EASTMONEY_FINANCIALS_URL,
    EASTMONEY_FUND_NEWS_QUERIES,
    EASTMONEY_INDEX_KLINE_URL,
    EASTMONEY_INDEX_SECIDS,
    EASTMONEY_NEWS_SEARCH_URL,
    GDELT_DOC_API_URL,
    EASTMONEY_JS_URL,
    FUND_CACHE_FILE,
    FUND_FEE_PAGE_URL,
    FUND_HISTORY_CACHE_FILE,
    FUND_HISTORY_BACKUP_URL,
    FUND_HISTORY_URL,
    FUND_BROWSER_CACHE_FILE,
    FUND_MANAGER_HISTORY_URL_TEMPLATE,
    FUND_MANAGER_NAV_URL_TEMPLATE,
    FUND_HOLDINGS_CACHE_FILE,
    FUND_HOLDINGS_URL,
    FUND_HOLDING_SOURCE_ORDER,
    FUND_QUOTE_URL,
    FRED_CSV_URL,
    FRED_MACRO_META,
    FRED_MACRO_SERIES,
    HOLDING_STOCK_TRENDS_CACHE_FILE,
    FUND_NAV_SOURCE_ORDER,
    HTTP_HEADERS,
    INDEX_SOURCE_ORDER,
    INDEX_SYMBOLS,
    MARKET_CACHE_FILE,
    MIN_RECOMMENDATION_DAILY_LIMIT,
    NAVER_FINANCIAL_URL,
    NY_FED_EFFR_URL,
    NEWS_CACHE_FILE,
    NEWS_KEYWORD_RULES,
    NEWS_SOURCES,
    PORTFOLIO_HOLDINGS,
    RECOMMENDATION_POOL,
    RECOMMENDATION_QUALIFICATION_CACHE_FILE,
    SOURCE_PRIORITY,
    SEC_COMPANY_FACTS_URL,
    SEC_TICKERS_CACHE_FILE,
    SEC_TICKERS_URL,
    SINA_FOREX_KLINE_URL,
    SINA_US_KLINE_URL,
    TARGET_FUND_NAMES,
    TARGET_FUNDS,
    TENCENT_INDEX_KLINE_URL,
    TRADING_ECONOMICS_INDICATOR_URLS,
    TENCENT_INDEX_SYMBOLS,
    US_TREASURY_YIELD_URL,
    YAHOO_CHART_URL,
    YAHOO_FUNDAMENTALS_URL,
    SINA_FUND_HOLDINGS_URL,
    SINA_FUND_PROFILE_URL,
    THS_FUND_HOLDINGS_URL,
)
from storage import atomic_write_json
from source_router import (
    ordered_source_handlers,
    source_policy_snapshot,
    source_provenance,
)
from ifind_client import IFindError, get_ifind_client, ifind_is_configured
from investoday_client import InvestodayError, get_investoday_client, investoday_is_configured
from browser_source import BROWSER_FUND_SOURCE, load_browser_quote_cache


_SOURCE_HEALTH_LOCK = threading.Lock()
_SOURCE_HEALTH: Dict[str, dict] = {}
_FETCH_AUDIT: list[dict] = []
_NETWORK_PREFLIGHT: dict = {"status": "NOT_RUN", "available": None}
_SOURCE_FAILURE_THRESHOLD = 2
_FRED_REQUEST_LOCK = threading.Lock()
_SEC_TICKER_LOCK = threading.Lock()
_SEC_TICKER_MAP: dict[str, int] | None = None
_GDELT_REQUEST_LOCK = threading.Lock()
_GDELT_LAST_REQUEST_AT = 0.0
_GDELT_MIN_REQUEST_INTERVAL = 5.5


def reset_source_health() -> None:
    with _SOURCE_HEALTH_LOCK:
        _SOURCE_HEALTH.clear()
        _FETCH_AUDIT.clear()
        _NETWORK_PREFLIGHT.clear()
        _NETWORK_PREFLIGHT.update(status="NOT_RUN", available=None, checked_at="")


def source_health_snapshot() -> dict[str, dict]:
    with _SOURCE_HEALTH_LOCK:
        return {key: dict(value) for key, value in _SOURCE_HEALTH.items()}


def fetch_audit_snapshot() -> list[dict]:
    with _SOURCE_HEALTH_LOCK:
        return [dict(item) for item in _FETCH_AUDIT]


def network_preflight_snapshot() -> dict:
    with _SOURCE_HEALTH_LOCK:
        return dict(_NETWORK_PREFLIGHT)


def run_network_preflight() -> dict:
    """Collect Windows network diagnostics without blocking source fallbacks."""
    probes = (
        ("东方财富", "fund.eastmoney.com"),
        ("Yahoo Finance", "query1.finance.yahoo.com"),
        ("FRED", "fred.stlouisfed.org"),
        ("iFinD", "api-mcp.51ifind.com"),
        ("Investoday", "data-api.investoday.net"),
        ("BEA", "apps.bea.gov"),
    )
    results = []
    for name, host in probes:
        item = {"source": name, "host": host, "dns": False, "tcp": False, "error": ""}
        try:
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            item["dns"] = True
            connection = socket.create_connection((host, 443), timeout=3)
            connection.close()
            item["tcp"] = True
        except Exception as error:
            item["error"] = _short_fetch_error(error)
        results.append(item)
    available = any(item["tcp"] for item in results)
    with _SOURCE_HEALTH_LOCK:
        _NETWORK_PREFLIGHT.clear()
        _NETWORK_PREFLIGHT.update(
            status="AVAILABLE" if available else "UNAVAILABLE",
            available=available,
            checked_at=datetime.now().isoformat(timespec="seconds"),
            probes=results,
            ifind_configured=ifind_is_configured(),
            investoday_configured=investoday_is_configured(),
            official_fund_nav_configured=bool(FUND_MANAGER_NAV_URL_TEMPLATE),
            official_fund_history_configured=bool(FUND_MANAGER_HISTORY_URL_TEMPLATE),
        )
    return network_preflight_snapshot()


def source_priority_snapshot() -> dict[str, list[str]]:
    return source_policy_snapshot()


def _record_fetch_audit(
    source: str, status: str, error: str = "", category: str = ""
) -> None:
    provenance = source_provenance(category, source) if category else {}
    with _SOURCE_HEALTH_LOCK:
        _FETCH_AUDIT.append(
            {
                "source": source,
                "status": status,
                "error": error[:300],
                "at": datetime.now().isoformat(timespec="seconds"),
                **provenance,
            }
        )


def _source_is_open(source: str) -> bool:
    with _SOURCE_HEALTH_LOCK:
        return bool(_SOURCE_HEALTH.get(source, {}).get("open"))


def _is_systemic_fetch_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(
        term in text
        for term in (
            "winerror 10013",
            "timed out",
            "timeout",
            "无法连接",
            "network stacks failed",
            "network is unreachable",
            "remote end closed",
            "connection reset",
            "name resolution",
            "网络繁忙",
        )
    )


def _record_source_success(source: str) -> None:
    with _SOURCE_HEALTH_LOCK:
        item = _SOURCE_HEALTH.setdefault(source, {})
        item.update(failures=0, open=False, last_error="")
        item["successes"] = int(item.get("successes", 0)) + 1


def _record_source_failure(source: str, error: Exception) -> None:
    if not _is_systemic_fetch_error(error):
        return
    with _SOURCE_HEALTH_LOCK:
        item = _SOURCE_HEALTH.setdefault(source, {})
        failures = int(item.get("failures", 0)) + 1
        item.update(
            failures=failures,
            open=failures >= _SOURCE_FAILURE_THRESHOLD,
            last_error=_short_fetch_error(error),
        )


def _open_source_circuit(source: str, error: Exception) -> None:
    with _SOURCE_HEALTH_LOCK:
        item = _SOURCE_HEALTH.setdefault(source, {})
        item.update(
            failures=max(_SOURCE_FAILURE_THRESHOLD, int(item.get("failures", 0))),
            open=True,
            last_error=_short_fetch_error(error),
        )


def _call_source(
    source: str,
    callback,
    category: str = "",
    circuit_breaker: bool = True,
):
    if circuit_breaker and _source_is_open(source):
        _record_fetch_audit(
            source, "SKIPPED_CIRCUIT", f"{source} 本轮已熔断", category
        )
        raise RuntimeError(f"{source} 本轮已熔断，跳过重复请求")
    try:
        result = callback()
        _record_source_success(source)
        _record_fetch_audit(source, "SUCCESS", category=category)
        return result
    except Exception as error:
        if circuit_breaker:
            _record_source_failure(source, error)
        _record_fetch_audit(source, "FAILED", _short_fetch_error(error), category)
        raise


def _ordered_handlers(category: str, handlers: dict):
    """Yield installed fetch handlers in the configured source order."""
    return ordered_source_handlers(category, handlers)


@dataclass
class FundQuote:
    code: str
    name: str
    nav: float
    date: str
    est_nav: Optional[float] = None
    est_growth: Optional[float] = None
    source: str = "fallback"
    error: str = ""
    daily_return_pct: Optional[float] = None
    monthly_return_pct: Optional[float] = None


@dataclass
class IndexSnapshot:
    symbol: str
    name: str
    price: float
    change_pct: float
    ma20_gap_pct: float
    ma60_gap_pct: float
    source: str = "fallback"
    date: str = "--"


@dataclass
class NewsItem:
    title: str
    source: str
    summary: str
    link: str = ""
    tag: str = "市场动态"
    impact: str = "关注对基金持仓行业的边际影响。"
    affected_funds: str = ""
    direction: str = "中性观察"
    watch: str = "等待更多数据确认。"
    priority: int = 0
    published_at: str = ""


def _ensure_dirs() -> None:
    for path in (CACHE_DIR,):
        path.mkdir(parents=True, exist_ok=True)


def _http_get(
    url: str,
    timeout: int = 15,
    attempts: int = 3,
    referer: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> str:
    """Fetch through Python first, then retry through the Windows network stack."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            headers = (
                dict(extra_headers)
                if extra_headers is not None
                else dict(HTTP_HEADERS)
            )
            if referer:
                headers["Referer"] = referer
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                content_type = response.headers.get_content_charset()
                if content_type:
                    return raw.decode(content_type, errors="replace")
                for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk"):
                    try:
                        return raw.decode(encoding)
                    except UnicodeDecodeError:
                        continue
                return raw.decode("utf-8", errors="replace")
        except Exception as error:
            last_error = error
            if attempt < attempts - 1:
                time.sleep(0.8 * (attempt + 1))

    # A desktop launch is outside the Codex network sandbox. PowerShell provides
    # an independent Windows HTTP stack when urllib is blocked or rejected.
    try:
        return _powershell_http_get(
            url,
            timeout=timeout,
            referer=referer,
            extra_headers=extra_headers,
        )
    except Exception as windows_error:
        raise RuntimeError(
            f"all network stacks failed; urllib={last_error}; "
            f"windows={windows_error}"
        ) from windows_error


def _powershell_http_get(
    url: str,
    timeout: int = 15,
    referer: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> str:
    """Use the Windows network stack when a site rejects or blocks urllib."""
    safe_url = url.replace("'", "''")
    merged_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        **(extra_headers or {}),
    }
    if referer:
        merged_headers["Referer"] = referer
    header_items = []
    for key, value in merged_headers.items():
        safe_key = str(key).replace("'", "''")
        safe_value = str(value).replace("'", "''")
        header_items.append(f"'{safe_key}'='{safe_value}'")
    headers = " -Headers @{" + ";".join(header_items) + "}"
    command = (
        "$ProgressPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        f"(Invoke-WebRequest -Uri '{safe_url}'{headers} -UseBasicParsing -TimeoutSec {timeout}).Content"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout + 5,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        detail = (completed.stderr or "").strip() or "empty response"
        raise RuntimeError(f"PowerShell request failed: {detail}")
    return completed.stdout.strip()


def _load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_json(path: Path, payload: dict) -> None:
    atomic_write_json(path, payload)


def _parse_estimate_js(payload: str) -> Optional[dict]:
    start = payload.find("{")
    end = payload.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(payload[start : end + 1])
    except Exception:
        return None


def fetch_recommendation_qualifications() -> Dict[str, dict]:
    """Verify purchase limits, active management and 36-month peer ranks."""
    _ensure_dirs()
    cached = _load_json(RECOMMENDATION_QUALIFICATION_CACHE_FILE) or {}
    cached_funds = cached.get("funds", {})
    results: Dict[str, dict] = {}

    # This endpoint rate-limits small bursts. Three sequential requests are more
    # reliable than parallel calls and add little to total launch time.
    for code, meta in RECOMMENDATION_POOL.items():
        if meta.get("direct_candidate"):
            results[code] = {
                "code": code,
                "name": meta["name"],
                "source": "direct-config",
                "as_of": STATIC_REFERENCE_DATE,
                "eligible": True,
                "one_day_limit": int(meta.get("one_day_limit", 0) or 0),
                "manager_type": meta.get("manager_type", "主动管理"),
                "investment_scope": meta.get("investment_scope", "全球"),
                "rank_desc": meta.get("rank_desc", "用户指定直入备选池"),
                "reasons": ["用户指定直入备选池，跳过外部资格筛选。"],
            }
            continue
        try:
            results[code] = _call_source(
                "eastmoney-qualification",
                lambda code=code, meta=meta: _fetch_recommendation_qualification(
                    code, meta
                ),
                category="recommendation_qualification",
            )
        except Exception as error:
            primary_error = _short_fetch_error(error)
            fallback_checks = []
            try:
                fallback_checks.append(
                    _call_source(
                        "sina-profile",
                        lambda code=code: _fetch_sina_qualification_crosscheck(code),
                        category="recommendation_qualification",
                    )
                )
            except Exception as sina_error:
                fallback_checks.append(
                    {
                        "source": "sina-profile",
                        "verified": False,
                        "message": f"新浪基金概况失败：{_short_fetch_error(sina_error)}",
                    }
                )
            cached_item = cached_funds.get(code)
            if cached_item and cached_item.get("source") in {
                "eastmoney-qualification",
                "multi-source-qualification",
            }:
                results[code] = {
                    **cached_item,
                    "source": "cache",
                    "fallback_checks": fallback_checks,
                    "error": primary_error,
                }
            else:
                results[code] = {
                    **_baseline_recommendation_qualification(code, RECOMMENDATION_POOL[code], primary_error),
                    "fallback_checks": fallback_checks,
                }
        time.sleep(0.6)

    live_results = {
        code: item
        for code, item in results.items()
        if item.get("source") in {
            "eastmoney-qualification",
            "multi-source-qualification",
        }
    }
    if live_results:
        merged = dict(cached_funds)
        merged.update(live_results)
        _save_json(
            RECOMMENDATION_QUALIFICATION_CACHE_FILE,
            {
                "ts": int(time.time()),
                "source_order": [
                    *source_priority_snapshot()["recommendation_qualification"],
                ],
                "funds": merged,
            },
        )
    return results


def _fetch_recommendation_qualification(code: str, meta: dict) -> dict:
    fee_page = _http_get(
        FUND_FEE_PAGE_URL.format(code=code),
        timeout=18,
        attempts=1,
        referer="https://fund.eastmoney.com/",
    )
    purchase_status, max_purchase = _parse_purchase_qualification(fee_page)
    rank_url = FUND_HISTORY_BACKUP_URL.format(code=code) + f"?v={int(time.time())}"
    rank_script = _http_get(
        rank_url, timeout=20, attempts=1, referer="https://fund.eastmoney.com/"
    )
    ranks = _parse_peer_rank_months(rank_script)
    if len(ranks) < 30:
        raise ValueError(f"近三年同类月末排名仅有{len(ranks)}个月，少于30个月")

    sina_profile = _fetch_sina_qualification_crosscheck(code)
    fund_type = sina_profile.get("fund_type", "--")
    benchmark = sina_profile.get("benchmark", "")
    scope_text = sina_profile.get("scope", "")
    manager_type = "主动管理" if "指数型" not in fund_type else "被动/指数"
    scope_verified = bool(
        meta.get("is_global_or_us_focus")
        and meta.get("investment_scope") in {"全球", "美股"}
        and _is_global_or_us_focus(meta["name"], benchmark, scope_text)
    )
    average_percentile = sum(item["percentile"] for item in ranks) / len(ranks)
    reasons = []
    if "暂停" in purchase_status or "封闭" in purchase_status:
        reasons.append(f"当前申购状态不可买入（{purchase_status}）")

    latest = ranks[-1]
    return {
        "code": code,
        "name": meta["name"],
        "source": "multi-source-qualification",
        "as_of": latest["date"],
        "one_day_limit": int(max_purchase),
        "purchase_status": purchase_status,
        "fund_type": fund_type,
        "manager_type": manager_type,
        "investment_scope": meta.get("investment_scope", "--"),
        "scope_verified": scope_verified,
        "benchmark": benchmark,
        "fallback_checks": [sina_profile],
        "rank_months": len(ranks),
        "rank_percentile": round(average_percentile, 2),
        "rank_desc": f"近{len(ranks)}个月月均同类百分位 {average_percentile:.2f}%",
        "latest_peer_rank": f"{latest['rank']:.0f}/{latest['total']:.0f}",
        "eligible": not reasons,
        "reasons": reasons,
        "error": "",
    }


def _parse_purchase_qualification(html_text: str) -> tuple[str, float]:
    status_match = re.search(
        r"申购状态</td>\s*<td[^>]*>(.*?)</td>", html_text, re.S
    )
    status = _clean_html_cell(status_match.group(1)) if status_match else "--"
    limit_match = re.search(
        r"日累计申购限额</td>\s*<td[^>]*>(.*?)</td>", html_text, re.S
    )
    if not limit_match:
        limit_match = re.search(r"单日累计购买上限\s*([\d.,]+)\s*(元|万元|亿元)", html_text)
    if not limit_match:
        raise ValueError("东方财富费率页未返回日累计申购限额")
    limit_text = _clean_html_cell(limit_match.group(1))
    if "无限额" in limit_text:
        return status, 1_000_000_000.0
    amount_match = re.search(r"([\d.,]+)\s*(亿元|万元|元)?", limit_text)
    if not amount_match:
        raise ValueError(f"无法解析申购限额：{limit_text}")
    amount = float(amount_match.group(1).replace(",", ""))
    unit = amount_match.group(2) or "元"
    multiplier = {"元": 1, "万元": 10_000, "亿元": 100_000_000}[unit]
    return status, amount * multiplier


def _is_global_or_us_focus(name: str, benchmark: str, scope: str) -> bool:
    global_terms = (
        "全球",
        "世界",
        "world",
        "all country",
        "美国",
        "美股",
        "s&p",
        "标普",
        "纳斯达克",
        "nasdaq",
        "罗素",
        "russell",
    )
    name_text = name.lower()
    benchmark_text = benchmark.lower()
    scope_text = scope.lower()
    name_signal = any(term in name_text for term in global_terms)
    scope_signal = any(term in scope_text for term in global_terms)

    weighted_segments = []
    for segment in re.split(r"[+＋]", benchmark_text):
        weight_match = re.search(r"(\d+(?:\.\d+)?)\s*%", segment)
        if weight_match:
            weighted_segments.append((float(weight_match.group(1)), segment))
    if weighted_segments and sum(weight for weight, _ in weighted_segments) >= 50:
        global_weight = sum(
            weight
            for weight, segment in weighted_segments
            if any(term in segment for term in global_terms)
        )
        return global_weight >= 50

    benchmark_signal = any(term in benchmark_text for term in global_terms)
    return name_signal and (benchmark_signal or scope_signal)


def _fetch_sina_qualification_crosscheck(code: str) -> dict:
    params = {"symbol": code, "format": "json"}
    url = SINA_FUND_PROFILE_URL + "?" + urllib.parse.urlencode(params)
    payload = json.loads(
        _http_get(
            url,
            timeout=12,
            attempts=1,
            referer="https://finance.sina.com.cn/",
        )
    )
    result = payload.get("result", {})
    status = result.get("status", {})
    data = result.get("data") or {}
    if status.get("code") != 0 or not data:
        raise ValueError(status.get("msg") or "新浪未返回基金概况")
    benchmark = str(data.get("bjjz") or "")
    scope = str(data.get("tzfw") or data.get("fxsytz") or "")
    fund_type = "/".join(
        str(data.get(key) or "")
        for key in ("Type2Name", "Type3Name", "NewType2Name", "NewType3Name")
        if data.get(key)
    )
    return {
        "source": "sina-profile",
        "verified": True,
        "fund_type": fund_type,
        "benchmark": benchmark,
        "scope": scope,
        "message": "新浪已交叉验证基金类型和投资范围，但不提供申购限额及36个月排名。",
    }


def _parse_peer_rank_months(script: str) -> List[dict]:
    marker = "var Data_rateInSimilarType"
    start = script.find(marker)
    if start < 0:
        raise ValueError("同类排名序列缺失")
    array_start = script.find("[", start)
    array_end = script.find("];", array_start)
    if array_start < 0 or array_end < 0:
        raise ValueError("同类排名序列格式异常")
    rows = json.loads(script[array_start : array_end + 1])
    monthly: Dict[str, dict] = {}
    for row in rows:
        rank = _to_float(row.get("y"))
        total = _to_float(row.get("sc"))
        timestamp = int(_to_float(row.get("x")))
        if rank <= 0 or total <= 0 or timestamp <= 0:
            continue
        row_date = datetime.fromtimestamp(timestamp / 1000).date().isoformat()
        monthly[row_date[:7]] = {
            "date": row_date,
            "rank": rank,
            "total": total,
            "percentile": rank / total * 100,
        }
    return sorted(monthly.values(), key=lambda item: item["date"])[-36:]


def _to_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch_fund_quotes(portfolio_codes: Iterable[str] | None = None) -> Dict[str, FundQuote]:
    _ensure_dirs()
    portfolio_codes = list(portfolio_codes or PORTFOLIO_HOLDINGS.keys())
    codes = list(
        dict.fromkeys(
            [*TARGET_FUNDS, *RECOMMENDATION_POOL.keys(), *portfolio_codes]
        )
    )
    cached = _load_json(FUND_CACHE_FILE)
    cached_results: Dict[str, FundQuote] = {}
    if cached and cached.get("records"):
        try:
            cached_results = {
                item["code"]: FundQuote(**{**item, "source": "cache"})
                for item in cached.get("records", [])
            }
        except Exception:
            cached_results = {}

    browser_results: Dict[str, FundQuote] = {}
    for code, item in load_browser_quote_cache(FUND_BROWSER_CACHE_FILE).items():
        if code not in codes:
            continue
        try:
            browser_results[code] = FundQuote(
                code=code,
                name=str(item.get("name") or TARGET_FUND_NAMES.get(code, code)),
                nav=float(item["nav"]),
                date=str(item["date"]),
                source=BROWSER_FUND_SOURCE,
                daily_return_pct=(
                    float(item["daily_return_pct"])
                    if item.get("daily_return_pct") is not None
                    else None
                ),
                monthly_return_pct=(
                    float(item["monthly_return_pct"])
                    if item.get("monthly_return_pct") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            continue

    results: Dict[str, FundQuote] = dict(browser_results)
    fetch_errors: Dict[str, List[str]] = {}
    # Optional official fund-manager source. It is configured by URL template
    # and validated before any result is accepted as live data.
    if FUND_MANAGER_NAV_URL_TEMPLATE:
        for code in codes:
            try:
                results[code] = _call_source(
                    "fund-manager",
                    lambda code=code: _fetch_fund_manager_quote(code),
                    category="fund_nav",
                )
            except Exception as error:
                fetch_errors.setdefault(code, []).append(
                    f"基金公司官方净值：{_short_fetch_error(error)}"
                )
    pending_codes = [code for code in codes if code not in results]
    for start in range(0, len(pending_codes), 3):
        batch = pending_codes[start : start + 3]
        try:
            results.update(
                _call_source(
                    "eastmoney-fund-quote",
                    lambda batch=batch: _fetch_fund_quote_batch(batch),
                    category="fund_nav",
                    circuit_breaker=False,
                )
            )
        except Exception as error:
            for code in batch:
                fetch_errors.setdefault(code, []).append(
                    f"东方财富批量接口：{_short_fetch_error(error)}"
                )

    # A failed batch must not open the source circuit. Retry missing codes one by
    # one, with a bounded retry per code; only consecutive single-code failures
    # can open the circuit and move the rest to an independent source.
    for code in codes:
        if code in results:
            continue
        if _source_is_open("eastmoney-fund-quote"):
            fetch_errors.setdefault(code, []).append(
                "东方财富逐只接口：连续逐只请求失败，当前轮次熔断"
            )
            continue
        single_error: Exception | None = None
        for attempt in range(2):
            try:
                results.update(
                    _call_source(
                        "eastmoney-fund-quote",
                        lambda code=code: _fetch_fund_quote_batch([code]),
                        category="fund_nav",
                    )
                )
                single_error = None
                break
            except Exception as error:
                single_error = error
                if attempt == 0 and not _source_is_open("eastmoney-fund-quote"):
                    time.sleep(0.5)
        if single_error is not None:
            fetch_errors.setdefault(code, []).append(
                f"东方财富逐只接口：{_short_fetch_error(single_error)}"
            )

    missing_codes = [code for code in codes if code not in results]
    if missing_codes and ifind_is_configured():
        ifind_codes = [
            *[code for code in portfolio_codes if code in missing_codes],
            *[code for code in missing_codes if code not in portfolio_codes],
        ]
        for start in range(0, len(ifind_codes), 2):
            batch = ifind_codes[start : start + 2]
            ifind_error: Exception | None = None
            for attempt in range(2):
                try:
                    ifind_quotes = _call_source(
                        "ifind-fund-nav",
                        lambda batch=batch: _fetch_ifind_fund_quotes(batch),
                        category="fund_nav",
                    )
                    results.update(ifind_quotes)
                    ifind_error = None
                    break
                except Exception as error:
                    ifind_error = error
                    if attempt == 0 and not _source_is_open("ifind-fund-nav"):
                        time.sleep(0.8)
            if ifind_error is not None:
                for code in batch:
                    fetch_errors.setdefault(code, []).append(
                        f"iFinD 独立净值源：{_short_fetch_error(ifind_error)}"
                    )

    missing_codes = [code for code in codes if code not in results]
    if missing_codes:
        with ThreadPoolExecutor(max_workers=min(4, len(missing_codes))) as executor:
            futures = {
                executor.submit(
                    _call_source,
                    "eastmoney-pingzhongdata",
                    lambda code=code: _fetch_fund_quote_js(code),
                    "fund_nav",
                ): code
                for code in missing_codes
            }
            for future in as_completed(futures):
                code = futures[future]
                try:
                    results[code] = future.result()
                except Exception as js_error:
                    fetch_errors.setdefault(code, []).append(
                        f"pingzhongdata 备用接口：{_short_fetch_error(js_error)}"
                    )

    if results:
        _merge_estimate_quotes(results)
    if results or fetch_errors:
        merged_records = dict(
            (item["code"], item) for item in (cached.get("records", []) if cached else [])
        )
        merged_records.update(
            {item.code: asdict(item) for item in results.values()}
        )
        _save_json(
            FUND_CACHE_FILE,
            {
                "ts": int(time.time()),
                "cache_schema": 2,
                "source_order": list(FUND_NAV_SOURCE_ORDER),
                "fetch_errors": {
                    code: "；".join(messages) for code, messages in fetch_errors.items()
                },
                "source_health": source_health_snapshot(),
                "records": list(merged_records.values()),
            },
        )

    fallback = fallback_fund_quotes()
    fallback.update(cached_results)
    fallback.update(results)
    for code, quote in fallback.items():
        if code in fetch_errors and code not in results:
            quote.error = "；".join(fetch_errors[code])
    return fallback


def _fetch_fund_quote_batch(codes: List[str]) -> Dict[str, FundQuote]:
    params = {
        "pageIndex": 1,
        "pageSize": len(codes),
        "plat": "Wap",
        "deviceId": 1,
        "product": "EFund",
        "version": "2.0.0",
        "Fcodes": ",".join(codes),
    }
    url = FUND_QUOTE_URL + "?" + urllib.parse.urlencode(params, safe=",")
    payload = json.loads(_http_get(url, timeout=10, attempts=1))
    records = payload.get("Datas") or []
    if not payload.get("Success", False) or not records:
        message = payload.get("ErrMsg") or payload.get("ErrorMessage") or "empty response"
        raise RuntimeError(f"Eastmoney fund quote: {message}")

    results: Dict[str, FundQuote] = {}
    for item in records:
        code = item.get("FCODE", "")
        if not code:
            continue
        results[code] = FundQuote(
            code=code,
            name=item.get("SHORTNAME")
            or TARGET_FUND_NAMES.get(code)
            or RECOMMENDATION_POOL.get(code, {}).get("name", code),
            nav=float(item.get("NAV") or 0),
            date=item.get("PDATE") or "--",
            source="eastmoney",
        )
    return results


def _fetch_ifind_fund_quotes(codes: List[str]) -> Dict[str, FundQuote]:
    records = get_ifind_client().fund_nav_quotes(codes)
    results: Dict[str, FundQuote] = {}
    for code in codes:
        item = records.get(code)
        if not item:
            continue
        results[code] = FundQuote(
            code=code,
            name=str(item.get("name") or TARGET_FUND_NAMES.get(code) or code),
            nav=float(item["nav"]),
            date=str(item["date"]),
            source="ifind-fund-nav",
        )
    if not results:
        raise IFindError("iFinD 未返回请求基金的有效官方净值")
    return results


def _fetch_fund_quote_js(code: str) -> FundQuote:
    # fundgz no longer reliably returns its previous JavaScript payload. The
    # pingzhongdata endpoint is also an Eastmoney public source and contains
    # the latest published unit NAV, which is preferable to a stale estimate.
    history = _fetch_fund_history_backup(code, history_days=1100)
    latest = (history.get("records") or [])[0]
    nav = float((latest or {}).get("nav") or 0)
    nav_date = str((latest or {}).get("date") or "--")[:10]
    if nav <= 0 or nav_date == "--":
        raise ValueError("pingzhongdata未返回有效单位净值或净值日期")
    return FundQuote(
        code=code,
        name=TARGET_FUND_NAMES.get(code)
        or RECOMMENDATION_POOL.get(code, {}).get("name", code),
        nav=nav,
        date=nav_date,
        source="eastmoney-pingzhongdata",
    )


def fetch_fund_histories(codes: List[str]) -> Dict[str, dict]:
    _ensure_dirs()
    cached = _load_json(FUND_HISTORY_CACHE_FILE) or {}
    cached_records = cached.get("funds", {})
    histories: Dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=min(3, len(codes))) as executor:
        futures = {
            executor.submit(
                _fetch_fund_history_with_fallback, code, cached_records.get(code)
            ): code
            for code in codes
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                histories[code] = future.result()
            except Exception as error:
                cached_item = cached_records.get(code)
                if cached_item:
                    histories[code] = {
                        **cached_item,
                        "source": "cache",
                        "error": (
                            "历史净值主接口失败、备用接口失败："
                            f"{_short_fetch_error(error)}"
                        ),
                    }
                else:
                    histories[code] = _baseline_fund_history(
                        code, f"history sources failed: {_short_fetch_error(error)}"
                    )

    if histories:
        serializable = {
            code: item for code, item in histories.items() if item.get("source") != "cache"
        }
        if serializable:
            merged = dict(cached_records)
            merged.update(serializable)
            _save_json(
                FUND_HISTORY_CACHE_FILE,
                {"ts": int(time.time()), "funds": merged},
            )
    return histories


def _fetch_fund_history_with_fallback(
    code: str, cached_item: dict | None = None
) -> dict:
    handlers = {
        **(
            {"fund-manager-history": lambda: _fetch_fund_manager_history(code)}
            if FUND_MANAGER_HISTORY_URL_TEMPLATE
            else {}
        ),
        "eastmoney-history": lambda: _fetch_fund_history(code, cached_item),
        "eastmoney-pingzhongdata": lambda: _fetch_fund_history_backup(code),
    }
    errors = []
    for source, handler in _ordered_handlers("fund_history", handlers):
        try:
            result = _call_source(source, handler, category="fund_history")
            if errors:
                result["error"] = "历史净值主接口失败，已切换备用接口：" + "；".join(errors)
            return result
        except Exception as error:
            errors.append(f"{source}: {_short_fetch_error(error)}")
    raise RuntimeError("；".join(errors) or f"{code} no history source is configured")


def _official_rows(payload: object) -> list[dict]:
    """Normalize common official JSON/CSV envelopes to row dictionaries."""
    if isinstance(payload, dict):
        for key in ("data", "records", "rows", "result", "items", "Datas"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = _official_rows(value)
                if nested:
                    return nested
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _official_field(row: dict, *names: str) -> object:
    for name in names:
        if name in row and row[name] not in (None, "", "--", "-"):
            return row[name]
    normalized = {str(key).strip().lower(): value for key, value in row.items()}
    for name in names:
        value = normalized.get(name.lower())
        if value not in (None, "", "--", "-"):
            return value
    return None


def _fetch_official_payload(url: str) -> object:
    raw = _http_get(url, timeout=10, attempts=1, referer="https://fund.eastmoney.com/")
    text = str(raw).lstrip("\ufeff").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows:
            raise ValueError("基金公司官方接口返回内容无法解析为 JSON/CSV")
        return rows


def _parse_official_quote(code: str, payload: object) -> FundQuote:
    rows = _official_rows(payload)
    for row in rows:
        row_code = str(_official_field(row, "code", "fund_code", "基金代码") or code).strip()
        if row_code and row_code != code:
            continue
        nav = _to_float(_official_field(row, "nav", "unit_nav", "net_value", "DWJZ", "单位净值"))
        nav_date = str(_official_field(row, "date", "nav_date", "as_of", "FSRQ", "净值日期") or "")[:10]
        if nav > 0 and re.match(r"^\d{4}-\d{2}-\d{2}$", nav_date):
            return FundQuote(
                code=code,
                name=str(_official_field(row, "name", "fund_name", "基金名称") or TARGET_FUND_NAMES.get(code, code)),
                nav=nav,
                date=nav_date,
                source="fund-manager",
            )
    raise ValueError(f"基金公司官方接口未返回 {code} 的有效单位净值")


def _fetch_fund_manager_quote(code: str) -> FundQuote:
    return _parse_official_quote(
        code, _fetch_official_payload(FUND_MANAGER_NAV_URL_TEMPLATE.format(code=code))
    )


def _fetch_fund_manager_history(code: str, history_days: int = 1100) -> dict:
    payload = _fetch_official_payload(FUND_MANAGER_HISTORY_URL_TEMPLATE.format(code=code))
    rows = _official_rows(payload)
    records = []
    cutoff = date.today() - timedelta(days=history_days)
    for row in rows:
        raw_date = _official_field(row, "date", "nav_date", "as_of", "FSRQ", "净值日期")
        nav = _to_float(_official_field(row, "nav", "unit_nav", "net_value", "DWJZ", "单位净值"))
        try:
            record_date = datetime.fromisoformat(str(raw_date)[:10]).date()
        except (TypeError, ValueError):
            continue
        if nav > 0 and record_date >= cutoff:
            records.append({"date": record_date.isoformat(), "nav": nav})
    records = sorted(
        {item["date"]: item for item in records}.values(),
        key=lambda item: item["date"],
        reverse=True,
    )
    if len(records) < 60:
        raise ValueError(f"基金公司官方历史净值不足60条：{code}")
    return {
        "records": records,
        "source": "fund-manager-history",
        "purchase_status": "--",
        "latest_date": records[0]["date"],
    }


def _fetch_fund_history(
    code: str,
    cached_item: dict | None = None,
    history_days: int = 1100,
) -> dict:
    cached_history = (cached_item or {}).get("records", [])
    has_full_cache = len(cached_history) >= 600 and history_days <= 1100
    request_days = 95 if has_full_cache else history_days
    start_date = date.today() - timedelta(days=request_days)
    base_params = {
        "fundCode": code,
        "pageSize": 20,
        "startDate": start_date.strftime("%Y-%m-%d"),
        "endDate": date.today().strftime("%Y-%m-%d"),
    }

    def fetch_page(page_index: int) -> dict:
        params = {**base_params, "pageIndex": page_index}
        url = FUND_HISTORY_URL + "?" + urllib.parse.urlencode(params)
        try:
            payload = json.loads(
                _http_get(
                    url,
                    timeout=15,
                    attempts=2,
                    referer="https://fundf10.eastmoney.com/",
                )
            )
        except Exception:
            payload = None
        if not isinstance(payload, dict) or not payload.get("Data"):
            payload = json.loads(
                _powershell_http_get(
                    url, timeout=20, referer="https://fundf10.eastmoney.com/"
                )
            )
        if not isinstance(payload, dict):
            raise ValueError(f"invalid history payload for {code}, page {page_index}")
        return payload

    first_payload = fetch_page(1)
    first_rows = (first_payload.get("Data") or {}).get("LSJZList", [])
    total_count = int(first_payload.get("TotalCount") or len(first_rows))
    page_count = max(1, math.ceil(total_count / 20))
    rows = list(first_rows)
    if page_count > 1:
        with ThreadPoolExecutor(max_workers=min(4, page_count - 1)) as executor:
            futures = {
                executor.submit(fetch_page, page_index): page_index
                for page_index in range(2, page_count + 1)
            }
            for future in as_completed(futures):
                payload = future.result()
                rows.extend((payload.get("Data") or {}).get("LSJZList", []))

    unique_rows = {
        row.get("FSRQ"): row
        for row in rows
        if row.get("FSRQ") and row.get("DWJZ")
    }
    live_records = {
        record_date: {"date": record_date, "nav": float(row.get("DWJZ") or 0)}
        for record_date, row in unique_rows.items()
    }
    cutoff = (date.today() - timedelta(days=history_days)).strftime("%Y-%m-%d")
    merged_records = {
        str(item.get("date")): {
            "date": str(item.get("date")),
            "nav": float(item.get("nav") or 0),
        }
        for item in cached_history
        if item.get("date") and item.get("nav") and str(item.get("date")) >= cutoff
    }
    merged_records.update(live_records)
    records = sorted(
        merged_records.values(), key=lambda item: item["date"], reverse=True
    )
    if len(records) < 60:
        raise ValueError(f"insufficient history for {code}")
    latest = max(unique_rows.values(), key=lambda row: row["FSRQ"])
    return {
        "records": records,
        "source": "eastmoney-history",
        "purchase_status": latest.get("SGZT") or "--",
        "latest_date": latest.get("FSRQ") or "--",
    }


def _fetch_fund_history_backup(code: str, history_days: int = 1100) -> dict:
    url = FUND_HISTORY_BACKUP_URL.format(code=code)
    try:
        text = _http_get(url, timeout=15, attempts=1)
    except Exception:
        text = _powershell_http_get(url, timeout=20, referer="https://fund.eastmoney.com/")
    match = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", text, re.S)
    if not match:
        raise ValueError(f"{code} 备用历史接口没有返回净值序列")
    rows = json.loads(match.group(1))
    cutoff = date.today() - timedelta(days=history_days)
    records = []
    for row in rows:
        try:
            record_date = datetime.fromtimestamp(float(row["x"]) / 1000).date()
            nav = float(row["y"])
        except (KeyError, TypeError, ValueError, OSError):
            continue
        if record_date >= cutoff and nav > 0:
            records.append({"date": record_date.isoformat(), "nav": nav})
    records = sorted(
        {item["date"]: item for item in records}.values(),
        key=lambda item: item["date"],
        reverse=True,
    )
    if len(records) < 60:
        raise ValueError(f"{code} 备用历史净值不足60条")
    return {
        "records": records,
        "source": "eastmoney-pingzhongdata",
        "purchase_status": "--",
        "latest_date": records[0]["date"],
    }


def fetch_fund_holdings(
    codes: List[str], portfolio_codes: Iterable[str] | None = None
) -> Dict[str, dict]:
    _ensure_dirs()
    portfolio_codes = list(portfolio_codes or PORTFOLIO_HOLDINGS.keys())
    cached = _load_json(FUND_HOLDINGS_CACHE_FILE) or {}
    cached_records = cached.get("funds", {})
    holdings: Dict[str, dict] = {}
    fetch_errors: Dict[str, str] = {}
    requested_codes = list(dict.fromkeys(str(code) for code in codes if code))
    ordered_codes = [
        *[code for code in portfolio_codes if code in requested_codes],
        *[code for code in requested_codes if code not in portfolio_codes],
    ]
    if not ordered_codes:
        return {}

    def resolve_one(code: str) -> None:
        try:
            holdings[code] = _fetch_fund_holdings(code)
        except Exception as error:
            fetch_errors[code] = _short_fetch_error(error)
            cached_item = cached_records.get(code)
            if cached_item:
                holdings[code] = {
                    **cached_item,
                    "source": "cache",
                    "error": f"本次持仓抓取失败：{fetch_errors[code]}",
                }
            else:
                holdings[code] = _baseline_fund_holding(
                    code, f"holding sources failed: {fetch_errors[code]}"
                )

    # Public disclosure endpoints rate-limit bursts. Resolve actual portfolio
    # positions synchronously first so their alerts are never delayed behind a
    # faster non-portfolio request; remaining funds can still run concurrently.
    priority_codes = [code for code in ordered_codes if code in portfolio_codes]
    for code in priority_codes:
        resolve_one(code)
    remaining_codes = [code for code in ordered_codes if code not in priority_codes]
    if remaining_codes:
        with ThreadPoolExecutor(max_workers=min(2, len(remaining_codes))) as executor:
            futures = {
                executor.submit(_fetch_fund_holdings, code): code
                for code in remaining_codes
            }
            for future in as_completed(futures):
                code = futures[future]
                try:
                    holdings[code] = future.result()
                except Exception as error:
                    fetch_errors[code] = _short_fetch_error(error)
                    cached_item = cached_records.get(code)
                    if cached_item:
                        holdings[code] = {
                            **cached_item,
                            "source": "cache",
                            "error": f"本次持仓抓取失败：{fetch_errors[code]}",
                        }
                    else:
                        holdings[code] = _baseline_fund_holding(
                            code, f"holding sources failed: {fetch_errors[code]}"
                        )

    serializable = {
        code: item for code, item in holdings.items() if item.get("source") != "cache"
    }
    if serializable or fetch_errors:
        merged = dict(cached_records)
        merged.update(serializable)
        _save_json(
            FUND_HOLDINGS_CACHE_FILE,
            {
                "ts": int(time.time()),
                "cache_schema": 2,
                "source_order": list(FUND_HOLDING_SOURCE_ORDER),
                "fetch_errors": fetch_errors,
                "funds": merged,
            },
        )
    return holdings


def fetch_holding_stock_trends(holdings_by_fund: Dict[str, dict]) -> Dict[str, dict]:
    cached_payload = _load_json(HOLDING_STOCK_TRENDS_CACHE_FILE) or {}
    cached = cached_payload.get("stocks", {})
    stocks = {}
    for fund in holdings_by_fund.values():
        for item in fund.get("holdings", [])[:10]:
            code = str(item.get("code", "")).strip()
            if code:
                stocks[code] = str(item.get("name", code))
    results: Dict[str, dict] = {}
    errors: Dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(_fetch_holding_stock_trend, code, name): code
            for code, name in stocks.items()
        }
        for future in as_completed(futures):
            code = futures[future]
            cached_stock = cached.get(code, {})
            try:
                trend = future.result()
            except Exception as error:
                errors[code] = _short_fetch_error(error)
                trend = (
                    {**cached_stock, "source": "cache"}
                    if cached_stock
                    else {
                        "code": code,
                        "name": stocks.get(code, code),
                        "source": "baseline",
                        "date": STATIC_REFERENCE_DATE,
                    }
                )
            # 财报和行情是两个独立数据模块，行情失败不应阻断基本面抓取。
            fundamentals = _resolve_stock_fundamentals(code, cached_stock)
            results[code] = {**trend, "fundamentals": fundamentals}
    _save_json(
        HOLDING_STOCK_TRENDS_CACHE_FILE,
        {"ts": int(time.time()), "stocks": results, "errors": errors},
    )
    return results


def _resolve_stock_fundamentals(code: str, cached_stock: dict) -> dict:
    cached = cached_stock.get("fundamentals") or {}
    if _fundamentals_cache_is_fresh(cached):
        return cached
    fetchers = {}
    sec_ticker = _holding_sec_ticker(code)
    if sec_ticker:
        fetchers["sec-companyfacts"] = lambda: _fetch_sec_company_fundamentals(
            code, sec_ticker
        )
    if _holding_market(code) == "cn":
        fetchers["eastmoney-financials"] = (
            lambda: _fetch_eastmoney_company_fundamentals(code)
        )
    if _holding_market(code) == "kr":
        fetchers["naver-financials"] = lambda: _fetch_naver_company_fundamentals(code)
    if _holding_market(code) == "us" and ifind_is_configured():
        fetchers["ifind-financials"] = lambda: _fetch_ifind_company_fundamentals(code)
    errors = []
    for source_name, fetcher in _ordered_handlers("fundamentals", fetchers):
        try:
            result = _call_source(source_name, fetcher, category="fundamentals")
            if not _fundamentals_report_is_current(result):
                raise ValueError(
                    f"{source_name} 财报报告期超过550天，继续尝试下一独立来源"
                )
            return result
        except Exception as error:
            errors.append(f"{source_name}：{_short_fetch_error(error)}")
    if cached:
        return {
            **cached,
            "source": "cache",
            "error": "本次全球财报刷新失败：" + "；".join(errors),
        }
    return {
        "source": "baseline",
        "ticker": code,
        "error": "全球财报获取失败：" + "；".join(errors),
    }


def _fundamentals_cache_is_fresh(item: dict) -> bool:
    try:
        fetched = datetime.fromisoformat(str(item.get("fetched_at"))).date()
    except (TypeError, ValueError):
        return False
    return item.get("source") in {
        "sec-companyfacts",
        "eastmoney-financials",
        "naver-financials",
        "ifind-financials",
    } and (date.today() - fetched).days < 1 and _fundamentals_report_is_current(item)


def _fundamentals_report_is_current(item: dict) -> bool:
    try:
        fiscal_end = datetime.fromisoformat(str(item.get("fiscal_end"))[:10]).date()
    except (TypeError, ValueError):
        return False
    return 0 <= (date.today() - fiscal_end).days <= 550


def _holding_sec_ticker(code: str) -> str | None:
    # These companies are held through their Hong Kong/Taiwan lines but publish
    # comparable annual reports to the SEC via a US ADR or primary listing.
    overrides = {
        "09988": "BABA",
        "00883": "CEO",
        "01179": "HTHT",
        "2330": "TSM",
    }
    if code in overrides:
        return overrides[code]
    return code.upper() if _holding_market(code) == "us" else None


def _fetch_sec_company_fundamentals(code: str, ticker: str | None = None) -> dict:
    ticker = (ticker or code).upper()
    ticker_map = _load_sec_ticker_map()
    cik = ticker_map.get(ticker)
    if not cik:
        raise ValueError(f"SEC目录未找到股票代码 {ticker}")
    url = SEC_COMPANY_FACTS_URL.format(cik=str(cik).zfill(10))
    payload = json.loads(
        _http_get(
            url,
            timeout=20,
            attempts=2,
            extra_headers={
                "User-Agent": "FundMonitor/4.0 contact@example.com",
            },
        )
    )
    result = _parse_sec_company_facts(payload)
    return {
        **result,
        "ticker": ticker,
        "holding_code": code,
        "cik": str(cik).zfill(10),
        "company": str(payload.get("entityName") or code),
        "source": "sec-companyfacts",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def fetch_sec_company_fundamentals_history(
    code: str, ticker: str | None = None
) -> list[dict]:
    """Return annual SEC report records with their actual filing dates.

    Each record is separately dated.  This is intentionally distinct from the
    latest-fundamentals helper so a later report can never overwrite the
    information set available to an earlier backtest signal.
    """
    ticker = (ticker or _holding_sec_ticker(code) or code).upper()
    ticker_map = _load_sec_ticker_map()
    cik = ticker_map.get(ticker)
    if not cik:
        raise ValueError(f"SEC ticker map does not contain {ticker}")
    url = SEC_COMPANY_FACTS_URL.format(cik=str(cik).zfill(10))
    payload = json.loads(
        _http_get(
            url,
            timeout=20,
            attempts=2,
            extra_headers={"User-Agent": "FundMonitor/4.0 contact@example.com"},
        )
    )
    events = _parse_sec_company_facts_history(payload)
    if not events:
        raise ValueError(f"SEC financial history is incomplete for {ticker}")
    return [
        {
            **event,
            "ticker": ticker,
            "holding_code": code,
            "cik": str(cik).zfill(10),
            "company": str(payload.get("entityName") or code),
            "source": "sec-companyfacts",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
        for event in events
    ]


def _fetch_eastmoney_company_fundamentals(code: str) -> dict:
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"{code} 不是东方财富A股财务报表代码")
    exchange = "SH" if code.startswith(("5", "6", "9")) else "SZ"
    secucode = f"{code}.{exchange}"
    params = {
        "reportName": "RPT_F10_FINANCE_MAINFINADATA",
        "columns": "ALL",
        "filter": f'(SECUCODE="{secucode}")',
        "pageNumber": 1,
        "pageSize": 12,
        "sortTypes": -1,
        "sortColumns": "REPORT_DATE",
        "source": "WEB",
        "client": "WEB",
    }
    payload = json.loads(
        _http_get(
            EASTMONEY_FINANCIALS_URL + "?" + urllib.parse.urlencode(params),
            timeout=12,
            attempts=1,
            referer="https://data.eastmoney.com/",
        )
    )
    rows = (payload.get("result") or {}).get("data") or []
    if not rows:
        raise ValueError(f"东方财富未返回 {secucode} 财务报表")
    current = max(rows, key=lambda item: str(item.get("REPORT_DATE", "")))

    def number(field: str) -> float | None:
        value = current.get(field)
        return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else None

    revenue = number("TOTALOPERATEREVE")
    net_income = number("PARENTNETPROFIT")
    operating_profit = number("OPERATE_PROFIT_PK")
    free_cash_flow = number("FCFF_BACK")
    roe = number("ROEJQ")
    metrics = {
        "revenue": revenue,
        "revenue_yoy": _percent_to_decimal(number("TOTALOPERATEREVETZ")),
        "net_income": net_income,
        "net_income_yoy": _percent_to_decimal(number("PARENTNETPROFITTZ")),
        "net_margin": net_income / revenue if revenue else None,
        "operating_margin": operating_profit / revenue if revenue and operating_profit is not None else None,
        "free_cash_flow_margin": free_cash_flow / revenue if revenue and free_cash_flow is not None else None,
        "roe": _percent_to_decimal(roe),
    }
    available = sum(value is not None for value in metrics.values())
    fiscal_end = str(current.get("REPORT_DATE") or "")[:10]
    if not fiscal_end or revenue is None or net_income is None:
        raise ValueError(f"东方财富 {secucode} 财报缺少营收或归母净利润")
    return {
        "ticker": code,
        "symbol": secucode,
        "company": current.get("SECURITY_NAME_ABBR") or code,
        "fiscal_end": fiscal_end,
        "filed_at": str(current.get("NOTICE_DATE") or fiscal_end)[:10],
        "metrics": metrics,
        "metric_coverage": round(available / len(metrics), 4),
        "source": "eastmoney-financials",
        "provider": "东方财富财务报表",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def _percent_to_decimal(value: float | None) -> float | None:
    return value / 100 if value is not None else None


def _fetch_naver_company_fundamentals(code: str) -> dict:
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"{code} 不是Naver韩国股票代码")
    text = _http_get(
        NAVER_FINANCIAL_URL.format(code=code),
        timeout=12,
        attempts=1,
        referer="https://finance.naver.com/",
    )
    table_start = text.find("th_cop_anal8")
    if table_start < 0:
        raise ValueError(f"Naver未返回 {code} 财务汇总表")
    table_start = text.rfind("<table", 0, table_start)
    tbody_start = text.find("<tbody", table_start)
    if table_start < 0 or tbody_start < 0:
        raise ValueError(f"Naver {code} 财务汇总表结构无效")
    headers = _naver_financial_headers(text[table_start:tbody_start])
    annual_indexes = _naver_actual_annual_indexes(headers)
    if len(annual_indexes) < 2:
        raise ValueError(f"Naver {code} 缺少两个可比年度财报")
    previous_column, current_column = annual_indexes[-2:]
    first_data_column = next(
        (index for index, item in enumerate(headers) if item.get("period")),
        0,
    )
    previous_index = previous_column - first_data_column
    current_index = current_column - first_data_column
    revenue = _naver_financial_row_values(text, "th_cop_anal8")
    operating_profit = _naver_financial_row_values(text, "th_cop_anal9")
    net_income = _naver_financial_row_values(text, "th_cop_anal10")
    roe = _naver_financial_row_values(text, "th_cop_anal13")
    for field, values in {
        "营收": revenue,
        "营业利润": operating_profit,
        "净利润": net_income,
        "ROE": roe,
    }.items():
        if len(values) <= current_index:
            raise ValueError(f"Naver {code} {field}列数不足")
    current_revenue = revenue[current_index]
    previous_revenue = revenue[previous_index]
    current_income = net_income[current_index]
    previous_income = net_income[previous_index]
    current_operating = operating_profit[current_index]
    current_roe = roe[current_index]
    if current_revenue is None or current_income is None:
        raise ValueError(f"Naver {code} 最新年度缺少营收或净利润")
    fiscal_label = headers[current_column]["period"]
    year = int(fiscal_label[:4])
    metrics = {
        "revenue": current_revenue,
        "revenue_yoy": _growth_ratio(current_revenue, previous_revenue),
        "net_income": current_income,
        "net_income_yoy": _growth_ratio(current_income, previous_income),
        "net_margin": current_income / current_revenue if current_revenue else None,
        "operating_margin": (
            current_operating / current_revenue
            if current_operating is not None and current_revenue
            else None
        ),
        "free_cash_flow_margin": None,
        "roe": _percent_to_decimal(current_roe),
    }
    available = sum(value is not None for value in metrics.values())
    return {
        "ticker": code,
        "symbol": f"{code}.KS",
        "fiscal_end": f"{fiscal_label.replace('.', '-')}-31",
        "filed_at": f"{year + 1}-03-31",
        "metrics": metrics,
        "metric_coverage": round(available / len(metrics), 4),
        "source": "naver-financials",
        "provider": "Naver Finance财务汇总表",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def _naver_financial_headers(fragment: str) -> list[dict]:
    headers = []
    for cell in re.findall(r"<th[^>]*scope=[\"']col[\"'][^>]*>(.*?)</th>", fragment, re.I | re.S):
        plain = _strip_html(cell)
        match = re.search(r"(\d{4}\.\d{2})", plain)
        headers.append({
            "period": match.group(1) if match else "",
            "estimated": "(E)" in plain.upper(),
        })
    return headers


def _naver_actual_annual_indexes(headers: list[dict]) -> list[int]:
    indexes = []
    started = False
    for index, item in enumerate(headers):
        period = item.get("period", "")
        if not period:
            continue
        month = period[-2:]
        if started and month != "12":
            break
        if month == "12":
            started = True
            if not item.get("estimated"):
                indexes.append(index)
    return indexes


def _naver_financial_row_values(text: str, class_name: str) -> list[float | None]:
    marker = text.find(class_name)
    if marker < 0:
        raise ValueError(f"Naver财务汇总表缺少{class_name}")
    start = text.rfind("<tr", 0, marker)
    end = text.find("</tr>", marker)
    if start < 0 or end < 0:
        raise ValueError(f"Naver财务汇总表{class_name}行结构无效")
    row = text[start:end]
    return [_html_number(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)]


def _strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", "", unescape(value)).replace("\xa0", " ").strip()


def _html_number(value: str) -> float | None:
    plain = _strip_html(value).replace(",", "")
    if plain in {"", "-", "N/A"}:
        return None
    try:
        return float(plain)
    except ValueError:
        return None


def _growth_ratio(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in {None, 0}:
        return None
    return (current - previous) / abs(previous)


def _fetch_ifind_company_fundamentals(code: str) -> dict:
    """Use iFinD only after official filings fail for a US holding."""
    result = get_ifind_client().global_fundamentals(code)
    return {**result, "holding_code": code}


def _fetch_yahoo_company_fundamentals(code: str) -> dict:
    """Read annual public-company financials for non-SEC listings and ADRs."""
    symbol = _holding_yahoo_symbol(code)
    if not symbol:
        raise ValueError(f"{code} 无法映射到Yahoo股票代码")
    metric_types = (
        "annualTotalRevenue",
        "annualNetIncome",
        "annualOperatingIncome",
        "annualOperatingCashFlow",
        "annualCapitalExpenditure",
        "annualStockholdersEquity",
        "annualCommonStockEquity",
    )
    params = {
        "symbol": symbol,
        "type": ",".join(metric_types),
        "period1": int(datetime.combine(date.today() - timedelta(days=2200), datetime.min.time()).timestamp()),
        "period2": int(datetime.combine(date.today() + timedelta(days=2), datetime.min.time()).timestamp()),
    }
    url = YAHOO_FUNDAMENTALS_URL.format(symbol=urllib.parse.quote(symbol, safe=""))
    payload = json.loads(
        _http_get(
            url + "?" + urllib.parse.urlencode(params),
            timeout=18,
            attempts=1,
            referer="https://finance.yahoo.com/",
        )
    )
    result = payload.get("timeseries", {}).get("result") or []
    series = {
        metric: _yahoo_annual_series(result, metric)
        for metric in metric_types
    }
    revenue = series["annualTotalRevenue"]
    net_income = series["annualNetIncome"]
    if not revenue or not net_income:
        raise ValueError(f"Yahoo财报缺少{code}可比营收或净利润年度数据")
    operating_income = series["annualOperatingIncome"]
    operating_cash = series["annualOperatingCashFlow"]
    capex = series["annualCapitalExpenditure"]
    equity = series["annualStockholdersEquity"] or series["annualCommonStockEquity"]
    current_revenue = revenue[-1]
    previous_revenue = revenue[-2] if len(revenue) > 1 else None
    current_income = _yahoo_value_for_end(net_income, current_revenue["end"]) or net_income[-1]
    previous_income = (
        _yahoo_value_for_end(net_income, previous_revenue["end"])
        if previous_revenue
        else None
    )
    current_operating = _yahoo_value_for_end(operating_income, current_revenue["end"])
    current_cash = _yahoo_value_for_end(operating_cash, current_revenue["end"])
    current_capex = _yahoo_value_for_end(capex, current_revenue["end"])
    current_equity = _yahoo_latest_not_after(equity, current_revenue["end"])
    revenue_value = float(current_revenue["val"])
    income_value = float(current_income["val"])
    free_cash_flow = None
    if current_cash is not None:
        free_cash_flow = float(current_cash["val"]) - abs(float((current_capex or {}).get("val", 0)))
    metrics = {
        "revenue": revenue_value,
        "revenue_yoy": _growth_rate(current_revenue, previous_revenue),
        "net_income": income_value,
        "net_income_yoy": _growth_rate(current_income, previous_income),
        "net_margin": income_value / revenue_value if revenue_value else None,
        "operating_margin": (
            float(current_operating["val"]) / revenue_value
            if current_operating is not None and revenue_value
            else None
        ),
        "free_cash_flow_margin": (
            free_cash_flow / revenue_value
            if free_cash_flow is not None and revenue_value
            else None
        ),
        "roe": (
            income_value / float(current_equity["val"])
            if current_equity is not None and float(current_equity["val"]) > 0
            else None
        ),
    }
    available = sum(value is not None for value in metrics.values())
    fiscal_end = current_revenue["end"]
    return {
        "ticker": code.upper(),
        "symbol": symbol,
        "fiscal_end": fiscal_end,
        "filed_at": fiscal_end,
        "metrics": metrics,
        "metric_coverage": round(available / len(metrics), 4),
        "source": "yahoo-fundamentals",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def _yahoo_annual_series(result: list[dict], metric: str) -> list[dict]:
    rows = []
    for item in result:
        values = item.get(metric) or []
        if not isinstance(values, list):
            continue
        for row in values:
            value = (row.get("reportedValue") or {}).get("raw")
            end = row.get("asOfDate")
            if isinstance(value, (int, float)) and end:
                rows.append({"end": str(end)[:10], "filed": str(end)[:10], "val": value})
    by_end = {row["end"]: row for row in rows}
    return [by_end[key] for key in sorted(by_end)]


def _yahoo_value_for_end(rows: list[dict], end: str) -> dict | None:
    return next((row for row in reversed(rows) if row.get("end") == end), None)


def _yahoo_latest_not_after(rows: list[dict], end: str) -> dict | None:
    return next((row for row in reversed(rows) if str(row.get("end", "")) <= end), None)


def _load_sec_ticker_map() -> dict[str, int]:
    global _SEC_TICKER_MAP
    with _SEC_TICKER_LOCK:
        if _SEC_TICKER_MAP is not None:
            return dict(_SEC_TICKER_MAP)
        cached = _load_json(SEC_TICKERS_CACHE_FILE) or {}
        try:
            cached_at = datetime.fromisoformat(str(cached.get("fetched_at"))).date()
        except (TypeError, ValueError):
            cached_at = date.min
        if cached.get("tickers") and (date.today() - cached_at).days < 7:
            _SEC_TICKER_MAP = {
                key.upper(): int(value) for key, value in cached["tickers"].items()
            }
            return dict(_SEC_TICKER_MAP)

        payload = json.loads(
            _http_get(
                SEC_TICKERS_URL,
                timeout=20,
                attempts=2,
                extra_headers={
                    "User-Agent": "FundMonitor/4.0 contact@example.com",
                },
            )
        )
        tickers = {
            str(item.get("ticker", "")).upper(): int(item["cik_str"])
            for item in payload.values()
            if item.get("ticker") and item.get("cik_str") is not None
        }
        _save_json(
            SEC_TICKERS_CACHE_FILE,
            {
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "tickers": tickers,
            },
        )
        _SEC_TICKER_MAP = tickers
        return dict(_SEC_TICKER_MAP)


def _parse_sec_company_facts(payload: dict) -> dict:
    revenue = _sec_annual_series(
        payload,
        ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "Revenue"),
    )
    net_income = _sec_annual_series(payload, ("NetIncomeLoss", "ProfitLoss"))
    operating_income = _sec_annual_series(
        payload, ("OperatingIncomeLoss", "ProfitLossFromOperatingActivities")
    )
    operating_cash = _sec_annual_series(
        payload,
        ("NetCashProvidedByUsedInOperatingActivities", "CashFlowsFromUsedInOperatingActivities"),
    )
    capex = _sec_annual_series(
        payload,
        ("PaymentsToAcquirePropertyPlantAndEquipment", "PurchaseOfPropertyPlantAndEquipment"),
    )
    equity = _sec_instant_series(
        payload, ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "Equity")
    )
    if not revenue or not net_income:
        raise ValueError("SEC财报缺少可比营收或净利润年度数据")

    current_revenue = revenue[-1]
    previous_revenue = revenue[-2] if len(revenue) > 1 else None
    current_income = _sec_value_for_end(net_income, current_revenue["end"])
    previous_income = (
        _sec_value_for_end(net_income, previous_revenue["end"])
        if previous_revenue
        else None
    )
    if current_income is None:
        current_income = net_income[-1]
    current_operating = _sec_value_for_end(operating_income, current_revenue["end"])
    current_cash = _sec_value_for_end(operating_cash, current_revenue["end"])
    current_capex = _sec_value_for_end(capex, current_revenue["end"])
    current_equity = _sec_latest_not_after(equity, current_revenue["end"])
    revenue_value = float(current_revenue["val"])
    income_value = float(current_income["val"])
    free_cash_flow = None
    if current_cash is not None:
        free_cash_flow = float(current_cash["val"]) - abs(float((current_capex or {}).get("val", 0)))

    metrics = {
        "revenue": revenue_value,
        "revenue_yoy": _growth_rate(current_revenue, previous_revenue),
        "net_income": income_value,
        "net_income_yoy": _growth_rate(current_income, previous_income),
        "net_margin": income_value / revenue_value if revenue_value else None,
        "operating_margin": (
            float(current_operating["val"]) / revenue_value
            if current_operating is not None and revenue_value
            else None
        ),
        "free_cash_flow_margin": (
            free_cash_flow / revenue_value
            if free_cash_flow is not None and revenue_value
            else None
        ),
        "roe": (
            income_value / float(current_equity["val"])
            if current_equity is not None and float(current_equity["val"]) > 0
            else None
        ),
    }
    available = sum(value is not None for value in metrics.values())
    return {
        "fiscal_end": current_revenue["end"],
        "filed_at": max(
            str(current_revenue.get("filed", "")),
            str(current_income.get("filed", "")),
        ),
        "metrics": metrics,
        "metric_coverage": round(available / len(metrics), 4),
    }


def _parse_sec_company_facts_history(payload: dict) -> list[dict]:
    """Build annual SEC events using only information filed on each event date.

    A later amendment is an additional event. It cannot overwrite the record
    visible at the original filing date.
    """
    revenue = _sec_annual_filing_series(
        payload,
        (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
            "Revenue",
        ),
    )
    net_income = _sec_annual_filing_series(payload, ("NetIncomeLoss", "ProfitLoss"))
    operating_income = _sec_annual_filing_series(
        payload, ("OperatingIncomeLoss", "ProfitLossFromOperatingActivities")
    )
    operating_cash = _sec_annual_filing_series(
        payload,
        ("NetCashProvidedByUsedInOperatingActivities", "CashFlowsFromUsedInOperatingActivities"),
    )
    capex = _sec_annual_filing_series(
        payload, ("PaymentsToAcquirePropertyPlantAndEquipment", "PurchaseOfPropertyPlantAndEquipment")
    )
    equity = _sec_instant_filing_series(
        payload,
        ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "Equity"),
    )

    records = []
    fiscal_ends = sorted({str(item.get("end", "")) for item in revenue if item.get("end")})
    for fiscal_end in fiscal_ends:
        filing_dates = sorted(
            {
                str(item.get("filed", ""))
                for series in (revenue, net_income, operating_income, operating_cash, capex)
                for item in series
                if str(item.get("end", "")) == fiscal_end and item.get("filed")
            }
        )
        for filed_at in filing_dates:
            current_revenue = _sec_value_available_as_of(revenue, fiscal_end, filed_at)
            current_income = _sec_value_available_as_of(net_income, fiscal_end, filed_at)
            if current_revenue is None or current_income is None:
                continue
            revenue_value = float(current_revenue["val"])
            income_value = float(current_income["val"])
            if revenue_value == 0:
                continue
            previous_revenue = _sec_latest_period_before(revenue, fiscal_end, filed_at)
            previous_income = (
                _sec_value_available_as_of(
                    net_income, str(previous_revenue.get("end", "")), filed_at
                )
                if previous_revenue
                else None
            )
            current_operating = _sec_value_available_as_of(operating_income, fiscal_end, filed_at)
            current_cash = _sec_value_available_as_of(operating_cash, fiscal_end, filed_at)
            current_capex = _sec_value_available_as_of(capex, fiscal_end, filed_at)
            current_equity = _sec_latest_not_after_available_as_of(equity, fiscal_end, filed_at)
            free_cash_flow = (
                float(current_cash["val"]) - abs(float((current_capex or {}).get("val", 0)))
                if current_cash is not None
                else None
            )
            metrics = {
                "revenue": revenue_value,
                "revenue_yoy": _growth_rate(current_revenue, previous_revenue),
                "net_income": income_value,
                "net_income_yoy": _growth_rate(current_income, previous_income),
                "net_margin": income_value / revenue_value,
                "operating_margin": (
                    float(current_operating["val"]) / revenue_value
                    if current_operating is not None
                    else None
                ),
                "free_cash_flow_margin": (
                    free_cash_flow / revenue_value if free_cash_flow is not None else None
                ),
                "roe": (
                    income_value / float(current_equity["val"])
                    if current_equity is not None and float(current_equity["val"]) > 0
                    else None
                ),
            }
            records.append(
                {
                    "fiscal_end": fiscal_end,
                    "filed_at": filed_at,
                    "metrics": metrics,
                    "metric_coverage": round(
                        sum(value is not None for value in metrics.values()) / len(metrics), 4
                    ),
                }
            )
    return records


def _parse_sec_company_facts_history_legacy(payload: dict) -> list[dict]:
    """Build annual, filing-dated fundamental observations from SEC facts."""
    revenue = _sec_annual_series(
        payload,
        ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "Revenue"),
    )
    net_income = _sec_annual_series(payload, ("NetIncomeLoss", "ProfitLoss"))
    operating_income = _sec_annual_series(
        payload, ("OperatingIncomeLoss", "ProfitLossFromOperatingActivities")
    )
    operating_cash = _sec_annual_series(
        payload,
        ("NetCashProvidedByUsedInOperatingActivities", "CashFlowsFromUsedInOperatingActivities"),
    )
    capex = _sec_annual_series(
        payload,
        ("PaymentsToAcquirePropertyPlantAndEquipment", "PurchaseOfPropertyPlantAndEquipment"),
    )
    equity = _sec_instant_series(
        payload,
        ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "Equity"),
    )
    records = []
    for index, current_revenue in enumerate(revenue):
        previous_revenue = revenue[index - 1] if index else None
        fiscal_end = str(current_revenue.get("end", ""))
        current_income = _sec_value_for_end(net_income, fiscal_end)
        if current_income is None:
            continue
        current_operating = _sec_value_for_end(operating_income, fiscal_end)
        current_cash = _sec_value_for_end(operating_cash, fiscal_end)
        current_capex = _sec_value_for_end(capex, fiscal_end)
        current_equity = _sec_latest_not_after(equity, fiscal_end)
        revenue_value = float(current_revenue["val"])
        income_value = float(current_income["val"])
        if revenue_value == 0:
            continue
        free_cash_flow = (
            float(current_cash["val"]) - abs(float((current_capex or {}).get("val", 0)))
            if current_cash is not None
            else None
        )
        filed_at = max(
            str(current_revenue.get("filed", "")),
            str(current_income.get("filed", "")),
        )
        if not fiscal_end or not filed_at:
            continue
        metrics = {
            "revenue": revenue_value,
            "revenue_yoy": _growth_rate(current_revenue, previous_revenue),
            "net_income": income_value,
            "net_income_yoy": _growth_rate(
                current_income,
                _sec_value_for_end(net_income, previous_revenue["end"]) if previous_revenue else None,
            ),
            "net_margin": income_value / revenue_value,
            "operating_margin": (
                float(current_operating["val"]) / revenue_value
                if current_operating is not None
                else None
            ),
            "free_cash_flow_margin": (
                free_cash_flow / revenue_value if free_cash_flow is not None else None
            ),
            "roe": (
                income_value / float(current_equity["val"])
                if current_equity is not None and float(current_equity["val"]) > 0
                else None
            ),
        }
        records.append(
            {
                "fiscal_end": fiscal_end,
                "filed_at": filed_at,
                "metrics": metrics,
                "metric_coverage": round(
                    sum(value is not None for value in metrics.values()) / len(metrics), 4
                ),
            }
        )
    return records


def _sec_annual_series(payload: dict, concepts: tuple[str, ...]) -> list[dict]:
    rows = _sec_fact_rows(payload, concepts)
    annual = [
        row
        for row in rows
        if str(row.get("form", "")).upper() in {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
        and str(row.get("fp", "")).upper() == "FY"
        and row.get("end")
    ]
    return _dedupe_sec_rows(annual)


def _sec_annual_filing_series(payload: dict, concepts: tuple[str, ...]) -> list[dict]:
    """Return annual facts without collapsing later amendments into the past."""
    annual = [
        row
        for row in _sec_fact_rows(payload, concepts)
        if str(row.get("form", "")).upper() in {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
        and str(row.get("fp", "")).upper() == "FY"
        and row.get("end")
        and row.get("filed")
    ]
    return _dedupe_sec_filing_rows(annual)


def _sec_instant_series(payload: dict, concepts: tuple[str, ...]) -> list[dict]:
    rows = _sec_fact_rows(payload, concepts)
    valid = [
        row
        for row in rows
        if str(row.get("form", "")).upper() in {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A", "6-K", "6-K/A"}
        and row.get("end")
    ]
    return _dedupe_sec_rows(valid)


def _sec_instant_filing_series(payload: dict, concepts: tuple[str, ...]) -> list[dict]:
    """Return balance-sheet facts while retaining each disclosure timestamp."""
    valid = [
        row
        for row in _sec_fact_rows(payload, concepts)
        if str(row.get("form", "")).upper() in {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A", "6-K", "6-K/A"}
        and row.get("end")
        and row.get("filed")
    ]
    return _dedupe_sec_filing_rows(valid)


def _sec_fact_rows(payload: dict, concepts: tuple[str, ...]) -> list[dict]:
    facts = payload.get("facts", {})
    candidates: list[list[dict]] = []
    for taxonomy in ("us-gaap", "ifrs-full"):
        taxonomy_facts = facts.get(taxonomy, {})
        for concept in concepts:
            units = (taxonomy_facts.get(concept) or {}).get("units", {})
            for unit in ("USD", "USD/shares"):
                rows = units.get(unit)
                if rows:
                    valid = [
                        row
                        for row in rows
                        if isinstance(row.get("val"), (int, float))
                    ]
                    if valid:
                        candidates.append(valid)
    if not candidates:
        return []
    return max(
        candidates,
        key=lambda rows: (
            max(str(row.get("end", "")) for row in rows),
            len({str(row.get("end", "")) for row in rows}),
        ),
    )


def _dedupe_sec_rows(rows: list[dict]) -> list[dict]:
    by_end: dict[str, dict] = {}
    for row in sorted(rows, key=lambda item: (str(item.get("end", "")), str(item.get("filed", "")))):
        by_end[str(row["end"])] = row
    return [by_end[key] for key in sorted(by_end)]


def _dedupe_sec_filing_rows(rows: list[dict]) -> list[dict]:
    by_filing: dict[tuple[str, str, str, str], dict] = {}
    for row in sorted(
        rows,
        key=lambda item: (
            str(item.get("end", "")),
            str(item.get("filed", "")),
            str(item.get("form", "")),
            str(item.get("accn", "")),
        ),
    ):
        key = (
            str(row.get("end", "")),
            str(row.get("filed", "")),
            str(row.get("form", "")),
            str(row.get("accn", "")),
        )
        by_filing[key] = row
    return list(by_filing.values())


def _sec_value_for_end(rows: list[dict], end: str) -> dict | None:
    return next((row for row in reversed(rows) if row.get("end") == end), None)


def _sec_value_available_as_of(rows: list[dict], end: str, available_at: str) -> dict | None:
    candidates = [
        row
        for row in rows
        if str(row.get("end", "")) == end
        and str(row.get("filed", ""))
        and str(row.get("filed", "")) <= available_at
    ]
    return max(candidates, key=lambda item: str(item.get("filed", "")), default=None)


def _sec_latest_period_before(rows: list[dict], end: str, available_at: str) -> dict | None:
    candidates = [
        row
        for row in rows
        if str(row.get("end", "")) < end
        and str(row.get("filed", ""))
        and str(row.get("filed", "")) <= available_at
    ]
    return max(
        candidates,
        key=lambda item: (str(item.get("end", "")), str(item.get("filed", ""))),
        default=None,
    )


def _sec_latest_not_after(rows: list[dict], end: str) -> dict | None:
    return next((row for row in reversed(rows) if str(row.get("end", "")) <= end), None)


def _sec_latest_not_after_available_as_of(
    rows: list[dict], end: str, available_at: str
) -> dict | None:
    candidates = [
        row
        for row in rows
        if str(row.get("end", "")) <= end
        and str(row.get("filed", ""))
        and str(row.get("filed", "")) <= available_at
    ]
    return max(
        candidates,
        key=lambda item: (str(item.get("end", "")), str(item.get("filed", ""))),
        default=None,
    )


def _growth_rate(current: dict | None, previous: dict | None) -> float | None:
    if not current or not previous:
        return None
    previous_value = float(previous.get("val", 0))
    if previous_value == 0:
        return None
    return (float(current.get("val", 0)) - previous_value) / abs(previous_value)


def _fetch_holding_stock_trend(code: str, name: str) -> dict:
    market = _holding_market(code)
    fetchers = {}
    if market in {"cn", "hk"}:
        fetchers["tencent-stock"] = lambda: _fetch_tencent_stock_history(code)
    if market == "us":
        fetchers["sina-stock"] = lambda: _fetch_sina_us_stock_history(code)
    if _holding_eastmoney_secid(code):
        fetchers["eastmoney-stock"] = lambda: _fetch_eastmoney_stock_history(code)

    errors = []
    for source_name, fetcher in _ordered_handlers("holding_stock_trends", fetchers):
        try:
            symbol, observations, source = _call_source(
                source_name, fetcher, category="holding_stock_trends"
            )
            return _build_holding_stock_trend(code, name, symbol, observations, source)
        except Exception as error:
            errors.append(f"{source_name}：{_short_fetch_error(error)}")
    raise RuntimeError("；".join(errors))


def _fetch_eastmoney_stock_history(code: str) -> tuple[str, list[tuple[str, float]], str]:
    secid = _holding_eastmoney_secid(code)
    if not secid:
        raise ValueError(f"{code} unsupported by eastmoney mapping")
    return secid, _fetch_eastmoney_series(secid, 180), "eastmoney-stock"


def _fetch_tencent_stock_history(code: str) -> tuple[str, list[tuple[str, float]], str]:
    market = _holding_market(code)
    if market == "cn":
        symbol = ("sh" if code.startswith(("5", "6", "9")) else "sz") + code
    elif market == "hk":
        symbol = "hk" + code.zfill(5)
    else:
        raise ValueError(f"{code} unsupported by tencent stock history")
    params = {"param": f"{symbol},day,,,180,qfq"}
    url = TENCENT_INDEX_KLINE_URL + "?" + urllib.parse.urlencode(params)
    payload = json.loads(_http_get(url, timeout=12, attempts=2, referer="https://gu.qq.com/"))
    stock_data = payload.get("data", {}).get(symbol, {})
    rows = stock_data.get("qfqday") or stock_data.get("day") or []
    observations = [
        (str(row[0]), float(row[2]))
        for row in rows
        if isinstance(row, list) and len(row) >= 3 and row[2] not in {None, ""}
    ]
    return symbol, observations, "tencent-stock"


def _fetch_sina_us_stock_history(code: str) -> tuple[str, list[tuple[str, float]], str]:
    if _holding_market(code) != "us":
        raise ValueError(f"{code} unsupported by sina US history")
    url = SINA_US_KLINE_URL.format(symbol=urllib.parse.quote(code)) + "?" + urllib.parse.urlencode(
        {"symbol": code, "___qn": 3}
    )
    text = _http_get(url, timeout=18, attempts=2, referer="https://finance.sina.com.cn/stock/usstock/")
    return code, _parse_sina_us_stock_history(text), "sina-stock"


def _parse_sina_us_stock_history(text: str) -> list[tuple[str, float]]:
    match = re.search(r"=\((\[.*\])\);?\s*$", text, re.DOTALL)
    if not match:
        raise ValueError("sina US stock payload invalid")
    rows = json.loads(match.group(1))
    observations = []
    for row in rows:
        try:
            observations.append((str(row["d"]), float(row["c"])))
        except (KeyError, TypeError, ValueError):
            continue
    if len(observations) < 120:
        raise ValueError("sina US stock history not enough")
    return observations


def _fetch_yahoo_stock_history(code: str) -> tuple[str, list[tuple[str, float]], str]:
    symbol = _holding_yahoo_symbol(code)
    params = {"range": "8mo", "interval": "1d"}
    url = YAHOO_CHART_URL.format(symbol=urllib.parse.quote(symbol)) + "?" + urllib.parse.urlencode(params)
    payload = json.loads(_http_get(url, timeout=12, attempts=1))
    result = payload.get("chart", {}).get("result", [{}])[0]
    closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    timestamps = result.get("timestamp", [])
    observations = [
        (datetime.fromtimestamp(timestamp).date().isoformat(), float(value))
        for timestamp, value in zip(timestamps, closes)
        if value is not None
    ]
    return symbol, observations, "yahoo-stock"


def _build_holding_stock_trend(
    code: str,
    name: str,
    symbol: str,
    observations: list[tuple[str, float]],
    source: str,
) -> dict:
    if len(observations) < 120:
        raise ValueError(f"{code} stock history not enough")
    values = [value for _, value in observations]
    current = values[-1]
    ma60 = sum(values[-60:]) / 60
    ma120 = sum(values[-120:]) / 120
    window = values[-64:]
    return {
        "code": code,
        "name": name,
        "symbol": symbol,
        "price": round(current, 4),
        "ma60_gap": round(current / ma60 - 1, 6),
        "ma120_gap": round(current / ma120 - 1, 6),
        "max_drawdown_63": round(_max_drawdown_values(window), 6),
        "momentum_20": round(current / values[-21] - 1, 6),
        "date": observations[-1][0],
        "source": source,
    }


def _holding_yahoo_symbol(code: str) -> str:
    market = _holding_market(code)
    if market == "kr":
        return f"{code}.KS"
    if market == "jp":
        return f"{code}.T"
    if market == "hk":
        return f"{code.zfill(5)}.HK"
    if market == "cn":
        return f"{code}.SS" if code.startswith(("5", "6", "9")) else f"{code}.SZ"
    return code


def _holding_eastmoney_secid(code: str) -> str | None:
    nasdaq = {
        "AMD",
        "INTC",
        "MU",
        "SNDK",
        "AMAT",
        "LRCX",
        "ASML",
        "LITE",
        "ALAB",
    }
    nyse = {"ONTO", "GLW", "TSM", "STM"}
    if code in {"000660", "005930"}:
        return f"177.{code}"
    if code == "285A":
        return "176.285A"
    if code in nasdaq:
        return f"105.{code}"
    if code in nyse:
        return f"106.{code}"
    if re.fullmatch(r"\d{6}", code):
        market = "1" if code.startswith(("5", "6", "9")) else "0"
        return f"{market}.{code}"
    return None


def _holding_market(code: str) -> str:
    if code in {"000660", "005930"}:
        return "kr"
    if code == "285A":
        return "jp"
    if re.fullmatch(r"\d{6}", code):
        return "cn"
    if re.fullmatch(r"\d{5}", code):
        return "hk"
    if re.fullmatch(r"[A-Z][A-Z0-9.-]*", code):
        return "us"
    return "other"


def _max_drawdown_values(values: List[float]) -> float:
    peak = values[0]
    drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    return drawdown


def _fetch_fund_holdings(code: str) -> dict:
    if code == "161815":
        try:
            allocation = _fetch_fund_asset_allocation(code)
            _validate_holdings_record(allocation, code)
            return {**allocation, "source": "eastmoney-asset-allocation"}
        except Exception:
            # Fall through to the generic holdings sources if the allocation
            # disclosure is temporarily unavailable.
            pass
    today = date.today()
    quarter_month = ((today.month - 1) // 3) * 3
    year = today.year
    if quarter_month == 0:
        quarter_month = 12
        year -= 1

    errors = []
    for offset in range(4):
        month = quarter_month - offset * 3
        query_year = year
        while month <= 0:
            month += 12
            query_year -= 1
        next_month = date(query_year + (month == 12), month % 12 + 1, 1)
        query_date = (next_month - timedelta(days=1)).isoformat()
        handlers = {
            "eastmoney-holdings": lambda: _fetch_eastmoney_holdings(
                code, query_year, month
            ),
            "sina-holdings": lambda: _fetch_sina_holdings(code, query_date),
            "ths-holdings": lambda: _fetch_ths_holdings(code, query_date),
        }
        for source, handler in _ordered_handlers("fund_holdings", handlers):
            try:
                parsed = _call_source(source, handler, category="fund_holdings")
                _validate_holdings_record(parsed, code)
                return {**parsed, "source": source}
            except Exception as error:
                errors.append(f"{source}：{_short_fetch_error(error)}")
    raise ValueError(f"{code} 持仓披露获取失败：{'；'.join(errors[-2:])}")


def _fetch_fund_asset_allocation(code: str) -> dict:
    """Read non-equity fund allocation from Eastmoney's public fund script.

    QDII-FOF funds such as 161815 may have no individual stock holdings. Their
    disclosed allocation is still valid portfolio evidence, but must not be
    mistaken for company-level fundamentals.
    """
    url = FUND_HISTORY_BACKUP_URL.format(code=code) + "?v=" + date.today().isoformat().replace("-", "")
    text = _http_get(url, timeout=15, attempts=1, referer="https://fund.eastmoney.com/")
    match = re.search(r"var Data_assetAllocation\s*=\s*(\{.*?\});", text, re.S)
    if not match:
        raise ValueError(f"{code} 资产配置披露缺失")
    payload = json.loads(match.group(1))
    categories = payload.get("categories") or []
    series = payload.get("series") or []
    if not categories or not series:
        raise ValueError(f"{code} 资产配置序列为空")
    latest_index = len(categories) - 1
    allocation = []
    for item in series:
        # This line series is the fund's net-asset amount, not a NAV weight.
        if str(item.get("type") or "").lower() == "line":
            continue
        values = item.get("data") or []
        if latest_index >= len(values):
            continue
        value = values[latest_index]
        try:
            weight = float(value) / 100.0
        except (TypeError, ValueError):
            continue
        if weight > 0:
            allocation.append({
                "code": "ASSET-" + str(item.get("name") or "OTHER"),
                "name": str(item.get("name") or "其他资产"),
                "weight": weight,
            })
    if not allocation:
        raise ValueError(f"{code} 资产配置没有有效权重")
    return {
        "disclosure_date": str(categories[latest_index])[:10],
        "holdings": [],
        "asset_allocation": allocation,
        "allocation_only": True,
    }


def _fetch_eastmoney_holdings(code: str, year: int, month: int) -> dict:
    params = {
        "type": "jjcc",
        "code": code,
        "topline": 10,
        "year": year,
        "month": month,
    }
    url = FUND_HOLDINGS_URL + "?" + urllib.parse.urlencode(params)
    text = _http_get(
        url, timeout=15, attempts=1, referer="https://fundf10.eastmoney.com/"
    )
    return _parse_fund_holdings_payload(text)


def _fetch_sina_holdings(code: str, query_date: str) -> dict:
    params = {"symbol": code, "date": query_date}
    url = SINA_FUND_HOLDINGS_URL + "?" + urllib.parse.urlencode(params)
    payload = json.loads(_http_get(url, timeout=12, attempts=1))
    result = payload.get("result") or {}
    rows = result.get("data") or result.get("dataList") or []
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("list") or []
    disclosure_date = (
        result.get("date")
        or result.get("report_date")
        or query_date
    )
    return {
        "disclosure_date": str(disclosure_date)[:10],
        "holdings": _normalize_holdings_rows(rows),
    }


def _fetch_ths_holdings(code: str, query_date: str) -> dict:
    url = THS_FUND_HOLDINGS_URL.format(code=code)
    text = _http_get(url, timeout=12, attempts=1)
    date_match = re.search(r"(?:截至日期|截止日期|报告期|截止至)[^0-9]*(\d{4}-\d{2}-\d{2})", text)
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S):
        cells = [_clean_html_cell(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if cells:
            rows.append(cells)
    return {
        "disclosure_date": date_match.group(1) if date_match else query_date,
        "holdings": _normalize_holdings_rows(rows),
    }


def _normalize_holdings_rows(rows: list) -> list:
    normalized = []
    name_keys = ("stock_name", "name", "sec_name", "股票名称", "证券名称")
    code_keys = ("stock_code", "code", "symbol", "股票代码", "证券代码")
    weight_keys = ("hold_ratio", "ratio", "weight", "占净值比例", "持仓占比")
    for row in rows:
        if isinstance(row, dict):
            name = next((row.get(key) for key in name_keys if row.get(key)), "")
            stock_code = next((row.get(key) for key in code_keys if row.get(key)), "")
            raw_weight = next((row.get(key) for key in weight_keys if row.get(key) is not None), 0)
        else:
            cells = [str(item).strip() for item in row]
            code_match = next((re.search(r"(?<!\d)\d{6}(?!\d)", item) for item in cells), None)
            stock_code = code_match.group(0) if code_match else ""
            weight_match = next((re.search(r"(\d+(?:\.\d+)?)\s*%", item) for item in cells), None)
            raw_weight = float(weight_match.group(1)) / 100 if weight_match else 0
            name = next((item for item in cells if item and item != stock_code and not re.fullmatch(r"\d+", item)), "")
        try:
            weight_text = str(raw_weight).replace(",", "").replace("%", "").strip()
            weight = float(weight_text)
            if weight > 1:
                weight /= 100
        except (TypeError, ValueError):
            continue
        if name and 0 < weight <= 1:
            normalized.append({"code": str(stock_code), "name": str(name), "weight": weight})
    return normalized[:10]


def _validate_holdings_record(record: dict, code: str) -> None:
    disclosure_date = str(record.get("disclosure_date") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", disclosure_date):
        raise ValueError(f"{code} 持仓披露日期缺失")
    items = record.get("holdings") or []
    if record.get("allocation_only"):
        allocation = record.get("asset_allocation") or []
        valid_allocation = [
            float(item.get("weight") or 0)
            for item in allocation
            if item.get("name") and 0 < float(item.get("weight") or 0) <= 1
        ]
        if not valid_allocation or sum(valid_allocation) <= 0:
            raise ValueError(f"{code} 资产配置权重缺失或无效")
        return
    if len(items) < 5:
        raise ValueError(f"{code} 前十大持仓不足5项")
    valid_weights = [
        float(item.get("weight") or 0)
        for item in items
        if item.get("name") and 0 < float(item.get("weight") or 0) <= 1
    ]
    if len(valid_weights) < 5 or sum(valid_weights) <= 0:
        raise ValueError(f"{code} 持仓权重缺失或无效")


def _parse_fund_holdings_payload(text: str) -> dict:
    match = re.search(r'content:"(.*)",arryear:', text, re.S)
    content = match.group(1) if match else text
    content = content.replace('\\"', '"').replace('\\/', '/')
    date_match = re.search(r"截止至：.*?(\d{4}-\d{2}-\d{2})", content, re.S)
    holdings = []
    for row in re.findall(r"<tr>(.*?)</tr>", content, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 7:
            continue
        cleaned = [_clean_html_cell(cell) for cell in cells]
        weight_match = re.search(r"([\d.]+)%", cleaned[6])
        if not weight_match:
            continue
        holdings.append(
            {
                "code": cleaned[1],
                "name": cleaned[2],
                "weight": float(weight_match.group(1)) / 100,
            }
        )
    return {
        "disclosure_date": date_match.group(1) if date_match else "--",
        "holdings": holdings[:10],
    }


def _clean_html_cell(value: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", value)).split()).strip()


def _merge_estimate_quotes(results: Dict[str, FundQuote]) -> None:
    def fetch_one(code: str) -> tuple[str, Optional[dict]]:
        try:
            payload = _http_get(
                EASTMONEY_JS_URL.format(code=code), timeout=6, attempts=1
            )
            return code, _parse_estimate_js(payload)
        except Exception:
            return code, None

    with ThreadPoolExecutor(max_workers=min(6, len(results))) as executor:
        for code, estimate_data in executor.map(fetch_one, results):
            if not estimate_data:
                continue
            quote = results[code]
            estimate = estimate_data.get("gsz")
            growth = estimate_data.get("gszzl")
            if estimate:
                quote.est_nav = float(estimate)
            if growth:
                quote.est_growth = float(growth)


def fetch_market_snapshot() -> Dict[str, object]:
    _ensure_dirs()
    cached = _load_json(MARKET_CACHE_FILE) or {}
    cached_indexes = {
        item["symbol"]: IndexSnapshot(**{**item, "source": "cache"})
        for item in cached.get("indexes", [])
    }
    fallback = {item.symbol: item for item in fallback_indexes()}

    fresh_indexes: Dict[str, IndexSnapshot] = {}
    fetch_errors: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(INDEX_SYMBOLS)) as executor:
        futures = {
            executor.submit(_fetch_index_snapshot, symbol, name): symbol
            for symbol, name in INDEX_SYMBOLS.items()
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                fresh_indexes[symbol] = future.result()
            except Exception as error:
                fetch_errors[symbol] = str(error)[:300]

    if not cached.get("cache_schema"):
        for symbol, item in cached_indexes.items():
            if not item.date or item.date == "--":
                fetch_errors.setdefault(symbol, "旧缓存缺少指数数据日期，不能确认时效")

    indexes: List[IndexSnapshot] = []
    for symbol, name in INDEX_SYMBOLS.items():
        indexes.append(fresh_indexes.get(symbol, cached_indexes.get(symbol, fallback[symbol])))

    macro_factors, macro_errors = _fetch_macro_factors(cached.get("macro_factors", []))

    headline_ready = all(
        item.source in {"eastmoney-index", "tencent-index"}
        for item in indexes
    ) and all(
        item.get("source") not in {"cache", "missing", "fallback"}
        and item.get("value") is not None
        for item in macro_factors
        if item.get("key") != "US_CREDIT_STRESS_PROXY"
    )
    headlines = (
        _build_macro_headlines(indexes)
        if headline_ready
        else ["关键实时数据未完成来源校验，本次不生成市场趋势摘要。"]
    )
    payload = {
        "ts": int(time.time()),
        "cache_schema": 4,
        "source_order": list(INDEX_SOURCE_ORDER),
        "macro_source_order": [
            "美国财政部/CBOE/新浪外汇",
            "FRED",
            "美国劳工统计局BLS",
            "世界银行GDP",
            "东方财富/Yahoo",
            "cache",
        ],
        "fetch_errors": fetch_errors,
        "macro_errors": macro_errors,
        "indexes": [asdict(item) for item in indexes],
        "macro_factors": macro_factors,
        "headlines": headlines,
    }
    _save_json(MARKET_CACHE_FILE, payload)
    return payload


def _fetch_macro_factors(cached_items: list[dict]) -> tuple[list[dict], dict[str, str]]:
    cached = {item.get("key"): item for item in cached_items}
    fresh: dict[str, dict] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(FRED_MACRO_SERIES))) as executor:
        futures = {
            executor.submit(_fetch_macro_factor, key, series_id, name): key
            for key, (series_id, name) in FRED_MACRO_SERIES.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                fresh[key] = future.result()
            except Exception as error:
                errors[key] = _short_fetch_error(error)
    result = []
    for key, (_, name) in FRED_MACRO_SERIES.items():
        if key in fresh:
            result.append(fresh[key])
        elif key in cached and _macro_cache_is_usable(cached[key], key):
            result.append({**cached[key], "source": "cache"})
        else:
            result.append(
                _baseline_macro_factor(
                    key, name, errors.get(key, "macro sources returned no data")
                )
            )
    return result, errors


def _fetch_macro_factor(key: str, series_id: str, name: str) -> dict:
    meta = FRED_MACRO_META.get(key, {})
    primary_fetchers = {
        "US2Y": ("us-treasury", lambda: _fetch_treasury_factor(key, name, "BC_2YEAR", real=False)),
        "US10Y": ("us-treasury", lambda: _fetch_treasury_factor(key, name, "BC_10YEAR", real=False)),
        "US10Y_REAL": ("us-treasury", lambda: _fetch_treasury_factor(key, name, "TC_10YEAR", real=True)),
        "VIX": ("cboe", lambda: _fetch_cboe_vix_factor(name)),
        "DXY": ("sina-forex", lambda: _fetch_sina_forex_factor(key, name, "DINIW")),
        "USDCNY": ("sina-forex", lambda: _fetch_sina_forex_factor(key, name, "fx_susdcny")),
    }
    errors = []
    if key in primary_fetchers:
        source, fetcher = primary_fetchers[key]
        try:
            return _call_source(source, fetcher, category="macro")
        except Exception as error:
            errors.append(f"{source}: {_short_fetch_error(error)}")

    # Low-frequency US data has independent publication paths. Query these
    # first so a slow FRED connection cannot turn the entire macro section
    # into static reference data.
    direct_fetchers = {
        "US_CPI": (
            "tradingeconomics",
            lambda: _fetch_tradingeconomics_macro_factor(key, name),
        ),
        "US_UNEMPLOYMENT": (
            "tradingeconomics",
            lambda: _fetch_tradingeconomics_macro_factor(key, name),
        ),
        "US_FED_FUNDS": ("ny-fed", lambda: _fetch_ny_fed_effr_factor(key, name)),
        "US_CREDIT_STRESS_PROXY": (
            "sina-credit-etf",
            lambda: _fetch_sina_credit_stress_factor(key, name),
        ),
    }
    direct = direct_fetchers.get(key)
    if direct:
        source, fetcher = direct
        try:
            return _call_source(source, fetcher, category="macro")
        except Exception as error:
            errors.append(f"{source}：{_short_fetch_error(error)}")

    if not series_id:
        raise RuntimeError("；".join(errors) or f"{key} 未配置可用的备用序列")
    if key in {"US_GDP", "US_HY_SPREAD"}:
        try:
            return _call_source(
                "fred",
                lambda: _fetch_fred_csv_direct(
                    key, name, series_id, meta, timeout=8, attempts=1
                ),
                category="macro",
                circuit_breaker=False,
            )
        except Exception as direct_error:
            errors.append(f"FRED短区间直连：{_short_fetch_error(direct_error)}")
        if ifind_is_configured():
            try:
                return _call_source(
                    "ifind-edb",
                    lambda: _fetch_ifind_macro_factor(key, name),
                    category="macro",
                )
            except Exception as error:
                errors.append(f"同花顺iFinD：{_short_fetch_error(error)}")
        if key == "US_GDP":
            try:
                return _call_source(
                    "bea", lambda: _fetch_bea_gdp_factor(key, name), category="macro"
                )
            except Exception as error:
                errors.append(f"美国经济分析局：{_short_fetch_error(error)}")
        # US_HY_SPREAD deliberately falls through as missing. The separately
        # fetched US_CREDIT_STRESS_PROXY is then selected by the quality/model
        # layer without being mislabeled as a real OAS observation.
        raise RuntimeError("；".join(errors))
    params = _fred_csv_params(key, series_id)
    url = FRED_CSV_URL + "?" + urllib.parse.urlencode(params)
    try:
        text = _http_get(
            url,
            timeout=12,
            attempts=2,
            referer="https://fred.stlouisfed.org/",
        )
        reader = csv.DictReader(io.StringIO(text))
        observations = []
        for row in reader:
            raw = row.get(series_id)
            if raw in {None, "", "."}:
                continue
            observations.append((row.get("observation_date") or row.get("DATE") or "--", float(raw)))
        return _build_macro_factor(key, name, observations, "fred", meta=meta)
    except Exception as fred_error:
        errors.append(f"FRED：{_short_fetch_error(fred_error)}")
        if key == "US_M2":
            if ifind_is_configured():
                try:
                    return _call_source(
                        "ifind-edb",
                        lambda: _fetch_ifind_macro_factor(key, name),
                        category="macro",
                    )
                except Exception as error:
                    errors.append(f"同花顺iFinD：{_short_fetch_error(error)}")
            try:
                return _call_source(
                    "tradingeconomics",
                    lambda: _fetch_tradingeconomics_macro_factor(key, name),
                    category="macro",
                )
            except Exception as error:
                errors.append(f"Trading Economics：{_short_fetch_error(error)}")
        if key in {"US_CPI", "US_UNEMPLOYMENT"}:
            try:
                observations = _fetch_bls_macro_factor(key)
                return _build_macro_factor(key, name, observations, "bls", meta=meta)
            except Exception as error:
                errors.append(f"美国劳工统计局：{_short_fetch_error(error)}")
        if key == "US_GDP":
            try:
                observations = _fetch_world_bank_gdp()
                return _build_macro_factor(
                    key,
                    name,
                    observations,
                    "world-bank",
                    meta={**meta, "frequency": "annual"},
                )
            except Exception as error:
                errors.append(f"世界银行：{_short_fetch_error(error)}")
        eastmoney_secids = {
            "VIX": "100.VIX",
            "DXY": "100.UDI",
            "US2Y": "171.US2Y",
            "US10Y": "171.US10Y",
            "USDCNY": "133.USDCNH",
        }
        secid = eastmoney_secids.get(key)
        if secid:
            try:
                observations = _fetch_eastmoney_series(secid, 120)
                return _build_macro_factor(key, name, observations, "eastmoney-macro", meta=meta)
            except Exception as error:
                errors.append(f"东方财富：{_short_fetch_error(error)}")
        raise RuntimeError("；".join(errors))


def _fetch_fred_csv_direct(
    key: str,
    name: str,
    series_id: str,
    meta: dict,
    *,
    timeout: float = 12,
    attempts: int = 2,
) -> dict:
    """Fallback FRED request using a TLS context independent of _http_get."""
    url = FRED_CSV_URL + "?" + urllib.parse.urlencode(
        _fred_csv_params(key, series_id)
    )
    context = ssl._create_unverified_context()
    last_error: Exception | None = None
    text = ""
    with _FRED_REQUEST_LOCK:
        for attempt in range(attempts):
            request = urllib.request.Request(
                url,
                headers={
                    **HTTP_HEADERS,
                    "Referer": "https://fred.stlouisfed.org/",
                    "Connection": "close",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                    text = response.read().decode("utf-8", errors="replace")
                break
            except Exception as error:
                last_error = error
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
    if not text:
        raise RuntimeError(
            f"FRED direct request failed after {attempts} attempts: {last_error}"
        )
    reader = csv.DictReader(io.StringIO(text))
    observations = []
    for row in reader:
        raw = row.get(series_id)
        if raw in {None, "", "."}:
            continue
        observations.append(
            (row.get("observation_date") or row.get("DATE") or "--", float(raw))
        )
    return _build_macro_factor(key, name, observations, "fred", meta=meta)


def _fred_csv_params(key: str, series_id: str) -> dict[str, str]:
    """Limit FRED CSV downloads to the history needed by each factor."""
    end = date.today()
    lookback_days = {
        "US_GDP": 5 * 366,
        "US_HY_SPREAD": 2 * 366,
    }.get(key)
    params = {"id": series_id}
    if lookback_days:
        params.update(
            cosd=(end - timedelta(days=lookback_days)).isoformat(),
            coed=end.isoformat(),
        )
    return params


def _fetch_bea_gdp_factor(key: str, name: str) -> dict:
    """Parse BEA's published annual real-GDP growth release.

    The release is authoritative but annual. Its frequency is preserved in the
    payload instead of presenting it as a current quarterly nowcast.
    """
    text = _http_get(
        BEA_NIPA_ANNUAL_URL,
        timeout=20,
        attempts=1,
        referer="https://apps.bea.gov/",
    )
    observations = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 3 or row[0].strip() != "A191RL":
            continue
        try:
            observations.append((f"{int(row[1])}-12-31", float(row[2].replace(",", ""))))
        except (TypeError, ValueError):
            continue
    result = _build_macro_factor(
        key,
        name,
        observations,
        "bea",
        meta={"frequency": "annual", "value_type": "rate", "lookback": 1},
    )
    result["provider"] = "美国经济分析局（BEA）NIPA"
    result["definition"] = "美国实际GDP年度增长率"
    return _apply_macro_contract(result)


def _fetch_ifind_macro_factor(key: str, name: str) -> dict:
    observations = get_ifind_client().macro_observations(key)
    result = _build_macro_factor(
        key,
        name,
        observations,
        "ifind-edb",
        meta=FRED_MACRO_META.get(key, {}),
    )
    result["provider"] = "同花顺iFinD EDB"
    result["publication_verified"] = True
    return result


def _fetch_ny_fed_effr_factor(key: str, name: str) -> dict:
    payload = json.loads(
        _http_get(
            NY_FED_EFFR_URL,
            timeout=12,
            attempts=1,
            referer="https://markets.newyorkfed.org/",
        )
    )
    observations = []
    for item in payload.get("refRates", []):
        try:
            observations.append((str(item["effectiveDate"])[:10], float(item["percentRate"])))
        except (KeyError, TypeError, ValueError):
            continue
    result = _build_macro_factor(
        key,
        name,
        observations,
        "ny-fed",
        meta={"frequency": "daily", "value_type": "rate", "lookback": 20},
    )
    result["provider"] = "纽约联储"
    result["definition"] = "Effective Federal Funds Rate（EFFR）"
    return _apply_macro_contract(result)


def _fetch_tradingeconomics_macro_factor(key: str, name: str) -> dict:
    url = TRADING_ECONOMICS_INDICATOR_URLS[key]
    text = _http_get(
        url,
        timeout=12,
        attempts=1,
        referer="https://tradingeconomics.com/",
    )
    description = _tradingeconomics_dataset_description(text)
    match = re.search(
        r"\b(?:increased|decreased|rose|fell|unchanged)\s+to\s+"
        r"([\d,.]+)\s+(?:percent|USD Billion).*?\s+in\s+([A-Za-z]+)\s+"
        r"from\s+([\d,.]+).*?\s+of\s+(\d{4})",
        description,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError("Trading Economics页面未找到最新指标与上期值")
    current = float(match.group(1).replace(",", ""))
    previous = float(match.group(3).replace(",", ""))
    month = _english_month_number(match.group(2))
    year = int(match.group(4))
    if not month:
        raise ValueError("Trading Economics页面返回了无法解析的指标月份")
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    as_of = (next_month - timedelta(days=1)).isoformat()
    frequency = "monthly"
    result = {
        "key": key,
        "name": name,
        "value": current,
        "change_20": round(current - previous, 4),
        "change_20_pct": round((current / previous - 1) * 100, 4) if previous else None,
        "change_60": None,
        "change_1": round(current - previous, 4),
        "change_1_pct": round((current / previous - 1) * 100, 4) if previous else None,
        "change_lookback": round(current - previous, 4),
        "change_lookback_pct": round((current / previous - 1) * 100, 4) if previous else None,
        "change_12_pct": current if key == "US_CPI" else None,
        "frequency": frequency,
        "value_type": "rate" if key in {"US_CPI", "US_UNEMPLOYMENT"} else "level",
        "lookback_periods": 1,
        "observations": 2,
        "date": as_of,
        "source": "tradingeconomics",
        "provider": "Trading Economics公开指标页",
        "previous_value": previous,
    }
    if key == "US_CPI":
        result["definition"] = "美国CPI同比（页面公布值）"
    elif key == "US_UNEMPLOYMENT":
        result["definition"] = "美国失业率（页面公布值）"
    else:
        result["definition"] = "美国M2余额（页面公布值；同比待官方序列复核）"
    return _apply_macro_contract(result)


def _fetch_sina_credit_stress_factor(key: str, name: str) -> dict:
    """Build a transparent HYG/LQD credit-stress proxy from live ETF prices.

    It is intentionally labelled as a proxy rather than an ICE OAS spread.
    The proxy rises when high-yield bonds underperform investment-grade bonds,
    which is the direction relevant to the portfolio risk gate.
    """
    _, hyg, _ = _fetch_sina_us_stock_history("HYG")
    _, lqd, _ = _fetch_sina_us_stock_history("LQD")
    hyg_by_date = dict(hyg)
    ratios = [
        (as_of, hyg_by_date[as_of] / lqd_price)
        for as_of, lqd_price in lqd
        if as_of in hyg_by_date and lqd_price > 0 and hyg_by_date[as_of] > 0
    ]
    if len(ratios) < 61:
        raise ValueError("HYG/LQD共同交易日不足，无法构造信用压力代理")
    ratios.sort(key=lambda item: item[0])
    values = [value for _, value in ratios]
    current = values[-1]
    prior = values[-21]
    peak_60 = max(values[-61:])
    prior_peak_60 = max(values[-81:-20]) if len(values) >= 81 else max(values[:-20])
    stress = max(0.0, (peak_60 / current - 1) * 100)
    prior_stress = max(0.0, (prior_peak_60 / prior - 1) * 100) if prior else 0.0
    return _apply_macro_contract({
        "key": key,
        "name": name,
        "value": round(stress, 4),
        "change_20": round(stress - prior_stress, 4),
        "change_20_pct": None,
        "change_60": None,
        "change_1": None,
        "change_1_pct": None,
        "change_lookback": round(stress - prior_stress, 4),
        "change_lookback_pct": None,
        "change_12_pct": None,
        "frequency": "daily",
        "value_type": "stress_proxy",
        "lookback_periods": 60,
        "observations": len(ratios),
        "date": ratios[-1][0],
        "source": "sina-credit-etf",
        "provider": "新浪财经美股ETF历史行情",
        "definition": "HYG相对LQD的60日信用压力代理（非ICE高收益债OAS）",
        "proxy": True,
    })


def _tradingeconomics_dataset_description(text: str) -> str:
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        try:
            payload = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        items = payload.get("@graph", []) if isinstance(payload, dict) else []
        for item in items:
            if item.get("@type") == "Dataset" and item.get("description"):
                return str(item["description"])
    match = re.search(r'name=["\']description["\']\s+content=["\']([^"\']+)', text, re.I)
    return unescape(match.group(1)) if match else ""


def _english_month_number(value: str) -> int | None:
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    return months.get(str(value).lower())


def _fetch_treasury_factor(key: str, name: str, field: str, real: bool) -> dict:
    dataset = "daily_treasury_real_yield_curve" if real else "daily_treasury_yield_curve"
    observations = []
    for year in (date.today().year - 1, date.today().year):
        params = {"data": dataset, "field_tdr_date_value": year}
        url = US_TREASURY_YIELD_URL + "?" + urllib.parse.urlencode(params)
        text = _http_get(url, timeout=18, attempts=2, referer="https://home.treasury.gov/")
        observations.extend(_parse_treasury_series(text, field))
    observations = sorted(dict(observations).items())
    return _build_macro_factor(key, name, observations, "us-treasury")


def _parse_treasury_series(text: str, field: str) -> list[tuple[str, float]]:
    root = ET.fromstring(text)
    observations = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "properties":
            continue
        values = {child.tag.rsplit("}", 1)[-1]: child.text for child in element}
        raw_date = values.get("NEW_DATE") or values.get("Date")
        raw_value = values.get(field)
        if not raw_date or raw_value in {None, ""}:
            continue
        observations.append((raw_date[:10], float(raw_value)))
    if len(observations) < 21:
        raise ValueError(f"treasury {field} observations not enough")
    return observations


def _fetch_cboe_vix_factor(name: str) -> dict:
    text = _http_get(CBOE_VIX_HISTORY_URL, timeout=18, attempts=2, referer="https://www.cboe.com/")
    reader = csv.DictReader(io.StringIO(text))
    observations = []
    for row in reader:
        try:
            parsed_date = datetime.strptime(row["DATE"], "%m/%d/%Y").date().isoformat()
            observations.append((parsed_date, float(row["CLOSE"])))
        except (KeyError, TypeError, ValueError):
            continue
    return _build_macro_factor("VIX", name, observations, "cboe")


def _fetch_sina_forex_factor(key: str, name: str, symbol: str) -> dict:
    url = SINA_FOREX_KLINE_URL.format(symbol=urllib.parse.quote(symbol)) + "?" + urllib.parse.urlencode(
        {"symbol": symbol}
    )
    text = _http_get(url, timeout=18, attempts=2, referer="https://finance.sina.com.cn/money/forex/")
    observations = _parse_sina_forex_history(text)
    return _build_macro_factor(key, name, observations, "sina-forex")


def _parse_sina_forex_history(text: str) -> list[tuple[str, float]]:
    match = re.search(r'=\("(.*)"\);?\s*$', text, re.DOTALL)
    if not match:
        raise ValueError("sina forex payload invalid")
    observations = []
    for line in match.group(1).split("|"):
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            observations.append((parts[0], float(parts[4])))
        except ValueError:
            continue
    if len(observations) < 21:
        raise ValueError("sina forex observations not enough")
    return observations


def _fetch_eastmoney_series(secid: str, limit: int) -> list[tuple[str, float]]:
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101,
        "fqt": 1,
        "end": 20500101,
        "lmt": limit,
    }
    url = EASTMONEY_INDEX_KLINE_URL + "?" + urllib.parse.urlencode(params)
    payload = json.loads(_http_get(url, timeout=12, attempts=1))
    klines = (payload.get("data") or {}).get("klines", [])
    observations = []
    for line in klines:
        parts = line.split(",")
        if len(parts) >= 3:
            observations.append((parts[0], float(parts[2])))
    if len(observations) < 21:
        raise ValueError(f"{secid} eastmoney observations not enough")
    return observations


def _fetch_bls_macro_factor(key: str) -> list[tuple[str, float]]:
    series_id = {
        "US_CPI": "CUUR0000SA0",
        "US_UNEMPLOYMENT": "LNS14000000",
    }[key]
    current_year = date.today().year
    payload = json.loads(
        _http_get(
            "https://api.bls.gov/publicAPI/v2/timeseries/data/"
            + series_id
            + "?"
            + urllib.parse.urlencode(
                {"startyear": current_year - 3, "endyear": current_year}
            ),
            timeout=15,
            attempts=1,
            referer="https://www.bls.gov/",
        )
    )
    if payload.get("status") != "REQUEST_SUCCEEDED":
        raise ValueError("BLS API未返回成功状态")
    observations = []
    for row in payload.get("Results", {}).get("series", [{}])[0].get("data", []):
        period = str(row.get("period", ""))
        if not period.startswith("M") or period == "M13":
            continue
        try:
            observations.append((f"{row['year']}-{int(period[1:]):02d}-01", float(row["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    observations.sort()
    if len(observations) < 13:
        raise ValueError("BLS宏观数据不足13个月")
    return observations


def _fetch_world_bank_gdp() -> list[tuple[str, float]]:
    url = "https://api.worldbank.org/v2/country/USA/indicator/NY.GDP.MKTP.KD.ZG?format=json&per_page=20"
    payload = json.loads(_http_get(url, timeout=15, attempts=1, referer="https://data.worldbank.org/"))
    rows = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
    observations = []
    for row in rows:
        try:
            if row.get("value") is not None:
                observations.append((f"{int(row['date'])}-12-31", float(row["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    observations.sort()
    if len(observations) < 2:
        raise ValueError("世界银行GDP数据不足")
    return observations


def _build_macro_factor(
    key: str,
    name: str,
    observations: list[tuple[str, float]],
    source: str,
    meta: dict | None = None,
) -> dict:
    if len(observations) < 21:
        # 低频指标只需满足相应的同比/环比计算窗口。
        minimum = 2 if (meta or {}).get("frequency") == "annual" else 13 if (meta or {}).get("frequency") == "monthly" else 21
        if len(observations) < minimum:
            raise ValueError(f"{key} macro observations not enough")
    observations = sorted(
        [(str(item[0])[:10], float(item[1])) for item in observations],
        key=lambda item: item[0],
    )
    values = [value for _, value in observations]
    current = values[-1]
    frequency = (meta or {}).get("frequency", "daily")
    value_type = (meta or {}).get("value_type", "level")
    lookback = int((meta or {}).get("lookback", 20))
    prior_index = max(0, len(values) - 1 - lookback)
    prior = values[prior_index]
    prior20 = values[-21] if len(values) >= 21 else values[0]
    prior60 = values[-61] if len(values) >= 61 else values[0]
    previous = values[-2] if len(values) >= 2 else current
    change_1 = current - previous
    change_1_pct = (current / previous - 1) * 100 if previous else None
    change_lookback = current - prior
    change_lookback_pct = (current / prior - 1) * 100 if prior else None
    change_12_pct = None
    if frequency == "monthly" and len(values) >= 13 and values[-13]:
        change_12_pct = (current / values[-13] - 1) * 100
    return _apply_macro_contract({
        "key": key,
        "name": name,
        "value": round(current, 4),
        "change_20": round(current - prior20, 4),
        "change_20_pct": round((current / prior20 - 1) * 100, 4) if prior20 else None,
        "change_60": round(current - prior60, 4),
        "change_1": round(change_1, 4),
        "change_1_pct": round(change_1_pct, 4) if change_1_pct is not None else None,
        "change_lookback": round(change_lookback, 4),
        "change_lookback_pct": round(change_lookback_pct, 4) if change_lookback_pct is not None else None,
        "change_12_pct": round(change_12_pct, 4) if change_12_pct is not None else None,
        "frequency": frequency,
        "value_type": value_type,
        "lookback_periods": lookback,
        "observations": len(observations),
        "date": observations[-1][0],
        "source": source,
    })


def _apply_macro_contract(factor: dict) -> dict:
    """Attach the semantic contract required before a macro value enters a model."""
    key = str(factor.get("key", ""))
    frequency = str(factor.get("frequency", ""))
    factor.setdefault("proxy", False)
    factor.setdefault("semantic_status", "COMPLETE")
    if key == "US_GDP":
        factor["definition"] = "美国实际 GDP 季度环比折年率"
        factor["required_fields"] = ["value"]
        if frequency != "quarterly":
            factor["semantic_status"] = "BACKGROUND_ONLY"
            factor["definition"] = "美国实际 GDP 年度增长率（仅作低频背景）"
    elif key == "US_M2":
        factor["definition"] = "美国 M2 余额及基于 13 个月序列计算的同比"
        factor["required_fields"] = ["value", "change_12_pct"]
        if not isinstance(factor.get("change_12_pct"), (int, float)):
            factor["semantic_status"] = "INCOMPLETE"
    elif key == "US_HY_SPREAD":
        factor["definition"] = "ICE BofA 美国高收益债 OAS"
        factor["required_fields"] = ["value"]
        factor["proxy"] = False
    elif key == "US_CREDIT_STRESS_PROXY":
        factor["definition"] = "HYG 相对 LQD 的 60 日信用压力代理，不等同于 OAS"
        factor["required_fields"] = ["value"]
        factor["proxy"] = True
        factor["semantic_status"] = "PROXY"
        factor["reliability_penalty"] = 0.25
    return factor


def fetch_finance_news() -> List[NewsItem]:
    _ensure_dirs()
    items: List[NewsItem] = []
    with ThreadPoolExecutor(max_workers=len(EASTMONEY_FUND_NEWS_QUERIES)) as executor:
        futures = {
            executor.submit(
                _call_source,
                "eastmoney-news",
                lambda keyword=keyword: _fetch_eastmoney_search_news(keyword),
                "news",
            ): keyword
            for keyword in EASTMONEY_FUND_NEWS_QUERIES
        }
        for future in as_completed(futures):
            try:
                items.extend(future.result())
            except Exception:
                continue

    # Eastmoney不足时依次尝试新浪财经和东方财富财经栏目。
    if len(items) < 8:
        for source in NEWS_SOURCES:
            try:
                source_key = (
                    "sina-news" if source["name"] == "新浪财经" else "eastmoney-news"
                )
                text = _call_source(
                    source_key,
                    lambda source=source: _http_get(source["url"], timeout=8),
                    category="news",
                )
                parser = _parse_sina_news if source["name"] == "新浪财经" else _parse_eastmoney_news
                items.extend(parser(text))
            except Exception:
                continue

    if len(items) < 8 and ifind_is_configured():
        try:
            items.extend(
                _call_source(
                    "ifind-news",
                    _fetch_ifind_news,
                    category="news",
                )
            )
        except Exception:
            pass

    if len(items) < 8 and investoday_is_configured():
        try:
            items.extend(
                _call_source(
                    "investoday-news",
                    lambda: _fetch_investoday_news(EASTMONEY_FUND_NEWS_QUERIES),
                    category="news",
                )
            )
        except Exception:
            pass

    normalized = sorted(
        _dedupe_news(items),
        key=lambda item: (item.priority, item.published_at),
        reverse=True,
    )
    if normalized:
        selected = _select_diverse_news(normalized, limit=8)
        _save_json(
            NEWS_CACHE_FILE,
            {
                "ts": int(time.time()),
                "records": [asdict(item) for item in selected],
            },
        )
        return selected

    cached = _load_json(NEWS_CACHE_FILE)
    if cached and cached.get("records"):
        try:
            cached_items = [NewsItem(**item) for item in cached.get("records", [])]
            relevant_items = [item for item in cached_items if item.affected_funds]
            if relevant_items:
                return [
                    NewsItem(
                        **{
                            **asdict(item),
                            "source": "cache",
                        }
                    )
                    for item in relevant_items[:8]
                ]
        except Exception:
            pass

    return fallback_news()


def fetch_ai_cycle_news() -> List[NewsItem]:
    """Fetch a broader evidence set for the AI spending-cycle warning.

    This remains separate from the eight-card general news selection so tag
    diversity cannot silently discard several reports about the same cycle.
    """
    _ensure_dirs()
    items: List[NewsItem] = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(
                _call_source,
                "eastmoney-news",
                lambda query=query: _fetch_eastmoney_search_news(query, page_size=30),
                "news",
                False,
            ): query
            for query in AI_CYCLE_NEWS_QUERIES
        }
        for future in as_completed(futures):
            try:
                items.extend(future.result())
            except Exception:
                continue

    if ifind_is_configured():
        try:
            today = date.today()
            rows = get_ifind_client().historical_relevant_news(
                (today - timedelta(days=120)).isoformat(),
                today.isoformat(),
                size=40,
                query=(
                    "AI云厂商资本开支下调、GPU或HBM交期缩短、DRAM/NAND/HBM价格下跌、"
                    "AI收入增速低于折旧和融资成本、芯片及云计算龙头盈利指引下修"
                ),
            )
            for row in rows:
                text = f"{row.get('title', '')} {row.get('summary', '')}"
                rule = _match_news_rule(text)
                items.append(
                    NewsItem(
                        title=_clean_text(row.get("title") or ""),
                        source="同花顺iFinD",
                        summary=_clean_text(row.get("summary") or ""),
                        link=str(row.get("link") or ""),
                        tag=(rule or {}).get("tag", "AI资本开支周期"),
                        impact=(rule or {}).get(
                            "impact", "关注AI基础设施投入与产业链盈利兑现。"
                        ),
                        affected_funds="016702、378006、270023、018229、005534、002891",
                        direction=_classify_news_direction(text),
                        watch=(rule or {}).get("watch", "等待跨来源与官方披露确认。"),
                        priority=int((rule or {}).get("priority", 8)),
                        published_at=str(row.get("published_at") or ""),
                    )
                )
        except Exception:
            pass

    if investoday_is_configured():
        try:
            items.extend(
                _call_source(
                    "investoday-news",
                    lambda: _fetch_investoday_news(AI_CYCLE_NEWS_QUERIES, page_size=30),
                    category="news",
                )
            )
        except Exception:
            pass

    usable = [item for item in _dedupe_news(items) if item.published_at]
    usable.sort(key=lambda item: (item.published_at, item.priority), reverse=True)
    if usable:
        selected = usable[:60]
        _save_json(
            AI_CYCLE_NEWS_CACHE_FILE,
            {"ts": int(time.time()), "records": [asdict(item) for item in selected]},
        )
        return selected

    cached = _load_json(AI_CYCLE_NEWS_CACHE_FILE) or {}
    cached_items = []
    for item in cached.get("records", []):
        try:
            cached_items.append(NewsItem(**{**item, "source": "cache"}))
        except (TypeError, ValueError):
            continue
    return cached_items


def _fetch_investoday_news(queries: Iterable[str], page_size: int = 20) -> List[NewsItem]:
    """Normalize Investoday MCP news into the existing auditable NewsItem shape."""
    items: List[NewsItem] = []
    client = get_investoday_client()
    for query in queries:
        for row in client.list_news(title=query, page_size=page_size):
            title = _clean_text(row.get("title") or "")
            summary = _clean_text(row.get("summary") or row.get("impactAnalysis") or "")
            rule = _match_news_rule(f"{title} {summary}")
            if not title or not rule:
                continue
            items.append(
                NewsItem(
                    title=title,
                    source="investoday-news",
                    summary=_build_cn_summary(title, summary),
                    tag=rule["tag"],
                    impact=rule["impact"],
                    affected_funds="、".join(rule["funds"]),
                    direction=_classify_news_direction(f"{title} {summary}"),
                    watch=rule["watch"],
                    priority=rule["priority"],
                    published_at=str(row.get("date") or ""),
                )
            )
    if not items:
        raise InvestodayError("Investoday 未返回与基金相关的可用新闻")
    return items


def _fetch_ifind_news() -> List[NewsItem]:
    items = []
    for row in get_ifind_client().relevant_news():
        title = _clean_text(row.get("title") or "")
        summary = _clean_text(row.get("summary") or "")
        rule = _match_news_rule(title + " " + summary)
        if not title or not rule:
            continue
        items.append(
            NewsItem(
                title=title,
                source="同花顺iFinD",
                summary=_build_cn_summary(title, summary),
                link=str(row.get("link") or ""),
                tag=rule["tag"],
                impact=rule["impact"],
                affected_funds="、".join(rule["funds"]),
                direction=_classify_news_direction(title + " " + summary),
                watch=rule["watch"],
                priority=rule["priority"],
                published_at=str(row.get("published_at") or ""),
            )
        )
    if not items:
        raise IFindError("iFinD 未返回与基金相关的可用新闻")
    return items


def _fetch_eastmoney_search_news(
    keyword: str, page_index: int = 1, page_size: int = 100
) -> List[NewsItem]:
    search_param = {
        "uid": "",
        "keyword": keyword,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "time",
                "pageIndex": max(1, int(page_index)),
                "pageSize": max(1, min(int(page_size), 100)),
                "preTag": "",
                "postTag": "",
            }
        },
    }
    params = {
        "cb": "callback",
        "param": json.dumps(search_param, ensure_ascii=False, separators=(",", ":")),
    }
    url = EASTMONEY_NEWS_SEARCH_URL + "?" + urllib.parse.urlencode(params)
    return _parse_eastmoney_search_news(_http_get(url, timeout=8))


def _gdelt_published_at(value: object) -> str:
    text = str(value or "").strip()
    match = re.search(
        r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})(?:[T\s]?(\d{2}):?(\d{2}):?(\d{2})?)?",
        text,
    )
    if not match:
        return ""
    year, month, day, hour, minute, second = match.groups()
    if not all((year, month, day)):
        return ""
    return (
        f"{year}-{month}-{day} "
        f"{hour or '00'}:{minute or '00'}:{second or '00'}"
    )


def _fetch_gdelt_historical_news(
    keyword: str, start_date: str, end_date: str, max_records: int = 250
) -> List[NewsItem]:
    """Fetch dated Chinese-language public news for auditable historical backfill."""
    params = {
        "query": f"{keyword} sourcelang:chinese",
        "mode": "artlist",
        "format": "json",
        "startdatetime": start_date.replace("-", "") + "000000",
        "enddatetime": end_date.replace("-", "") + "235959",
        "maxrecords": max(1, min(int(max_records), 250)),
    }
    url = GDELT_DOC_API_URL + "?" + urllib.parse.urlencode(params)
    # GDELT asks clients to keep requests at least five seconds apart. The
    # archive is windowed and resumable, so a slow, auditable backfill is safer
    # than triggering a 429 storm and recording every window as unavailable.
    global _GDELT_LAST_REQUEST_AT
    last_error: Exception | None = None
    payload = None
    for attempt in range(2):
        with _GDELT_REQUEST_LOCK:
            wait_seconds = _GDELT_MIN_REQUEST_INTERVAL - (
                time.monotonic() - _GDELT_LAST_REQUEST_AT
            )
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            _GDELT_LAST_REQUEST_AT = time.monotonic()
        try:
            payload = json.loads(_http_get(url, timeout=20, attempts=1))
            break
        except Exception as error:
            last_error = error
            if "429" not in str(error):
                raise
            if attempt == 0:
                time.sleep(15)
    if payload is None:
        raise RuntimeError(str(last_error or "GDELT returned no payload"))
    rows = payload.get("articles") or []
    if not isinstance(rows, list):
        return []
    items: List[NewsItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _clean_text(str(row.get("title") or ""))
        summary = _clean_text(str(row.get("snippet") or ""))
        rule = _match_news_rule(f"{title} {summary}")
        published_at = _gdelt_published_at(row.get("seendate") or row.get("date"))
        if not title or not rule or not published_at:
            continue
        domain = _clean_text(str(row.get("domain") or ""))
        items.append(
            NewsItem(
                title=title,
                source=f"GDELT · {domain}" if domain else "GDELT",
                summary=_build_cn_summary(title, summary),
                link=str(row.get("url") or ""),
                tag=rule["tag"],
                impact=rule["impact"],
                affected_funds="、".join(rule["funds"]),
                direction=_classify_news_direction(f"{title} {summary}"),
                watch=rule["watch"],
                priority=rule["priority"],
                published_at=published_at,
            )
        )
    return items


def _parse_eastmoney_search_news(text: str) -> List[NewsItem]:
    start = text.find("(")
    end = text.rfind(")")
    payload_text = text[start + 1 : end] if start != -1 and end > start else text
    payload = json.loads(payload_text)
    rows = payload.get("result", {}).get("cmsArticleWebOld", [])
    items: List[NewsItem] = []
    for row in rows:
        title = _clean_text(row.get("title") or "")
        intro = _clean_text(row.get("content") or "")
        rule = _match_news_rule(title + " " + intro)
        if not title or not rule:
            continue
        media = _clean_text(row.get("mediaName") or "")
        items.append(
            NewsItem(
                title=title,
                source=f"东方财富 · {media}" if media else "东方财富",
                summary=_build_cn_summary(title, intro),
                link=row.get("url") or _eastmoney_news_link(row.get("code") or ""),
                tag=rule["tag"],
                impact=rule["impact"],
                affected_funds="、".join(rule["funds"]),
                direction=_classify_news_direction(title + " " + intro),
                watch=rule["watch"],
                priority=rule["priority"],
                published_at=_clean_text(row.get("date") or ""),
            )
        )
    return items


def _parse_sina_news(text: str) -> List[NewsItem]:
    payload = json.loads(text)
    result = payload.get("result", {})
    data = result.get("data", [])
    items: List[NewsItem] = []
    for row in data[:50]:
        title = _clean_text(row.get("title") or row.get("intro") or "")
        intro = _clean_text(row.get("intro") or row.get("summary") or "")
        link = row.get("url") or row.get("wapurl") or ""
        if not title:
            continue
        rule = _match_news_rule(title + " " + intro)
        if not rule:
            continue
        direction = _classify_news_direction(title + " " + intro)
        summary = _build_cn_summary(title, intro)
        items.append(
            NewsItem(
                title=title,
                source="新浪财经",
                summary=summary,
                link=link,
                tag=rule["tag"],
                impact=rule["impact"],
                affected_funds="、".join(rule["funds"]),
                direction=direction,
                watch=rule["watch"],
                priority=rule["priority"],
            )
        )
    return items


def _parse_eastmoney_news(text: str) -> List[NewsItem]:
    payload = json.loads(text)
    data = payload.get("data", {})
    rows = data.get("list", []) or data.get("news", [])
    items: List[NewsItem] = []
    for row in rows[:50]:
        title = _clean_text(row.get("title") or "")
        media = _clean_text(row.get("mediaName") or row.get("source") or "")
        intro = _clean_text(row.get("summary") or row.get("digest") or "")
        link = row.get("url") or _eastmoney_news_link(row.get("code") or "")
        if not title:
            continue
        rule = _match_news_rule(title + " " + intro)
        if not rule:
            continue
        direction = _classify_news_direction(title + " " + intro)
        summary = _build_cn_summary(title, intro)
        items.append(
            NewsItem(
                title=title,
                source=f"东方财富 · {media}" if media else "东方财富",
                summary=summary,
                link=link,
                tag=rule["tag"],
                impact=rule["impact"],
                affected_funds="、".join(rule["funds"]),
                direction=direction,
                watch=rule["watch"],
                priority=rule["priority"],
                published_at=_clean_text(row.get("showTime") or row.get("date") or ""),
            )
        )
    return items


def _eastmoney_news_link(code: str) -> str:
    code = code.strip()
    if not code:
        return ""
    digits = re.sub(r"\D", "", code)
    if not digits:
        return ""
    return f"https://finance.eastmoney.com/a/{digits}.html"


def _dedupe_news(items: List[NewsItem]) -> List[NewsItem]:
    deduped: List[NewsItem] = []
    seen_titles = set()
    for item in items:
        key = re.sub(r"\s+", "", item.title.lower())
        if not key or key in seen_titles:
            continue
        seen_titles.add(key)
        deduped.append(item)
    return deduped


def _select_diverse_news(items: List[NewsItem], limit: int) -> List[NewsItem]:
    selected: List[NewsItem] = []
    tag_counts: Dict[str, int] = {}
    selected_titles = set()
    for per_tag_limit in (1, 2):
        for item in items:
            if item.title in selected_titles:
                continue
            if tag_counts.get(item.tag, 0) >= per_tag_limit:
                continue
            selected.append(item)
            selected_titles.add(item.title)
            tag_counts[item.tag] = tag_counts.get(item.tag, 0) + 1
            if len(selected) == limit:
                return selected

    for item in items:
        if item.title in selected_titles:
            continue
        selected.append(item)
        if len(selected) == limit:
            break
    return selected


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("\u3000", " ")
    return " ".join(text.split()).strip()


def _match_news_rule(text: str) -> Optional[dict]:
    content = text.lower()
    matches = []
    for rule in NEWS_KEYWORD_RULES:
        keyword_hits = sum(
            1 for keyword in rule["keywords"] if keyword.lower() in content
        )
        if keyword_hits:
            matches.append((rule["priority"], keyword_hits, rule))
    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def _classify_news_direction(text: str) -> str:
    content = text.lower()
    negative_phrases = [
        "降息预期降温", "不及预期", "下调指引", "出口管制", "芯片禁令",
        "科技制裁", "加征关税", "收益率上行", "通胀升温", "加息",
        "暴跌", "大幅下滑", "亏损扩大", "监管调查",
    ]
    positive_phrases = [
        "降息", "超预期", "上调指引", "回购", "订单增长", "盈利增长",
        "资本开支增加", "政策支持", "限制放宽", "获批", "复苏",
    ]
    negative_score = sum(1 for phrase in negative_phrases if phrase in content)
    positive_score = sum(1 for phrase in positive_phrases if phrase in content)
    if negative_score > positive_score:
        return "偏利空"
    if positive_score > negative_score:
        return "偏利多"
    return "中性观察"


def _build_cn_summary(title: str, intro: str) -> str:
    base = intro if intro else title
    base = _clean_text(base)
    if len(base) > 52:
        base = base[:52].rstrip("，。；、 ") + "..."
    if not base:
        base = title
    return base


def _fetch_index_snapshot(symbol: str, name: str) -> IndexSnapshot:
    errors = []
    source_names = {
        "tencent-index": "腾讯财经",
        "eastmoney-index": "东方财富",
    }
    fetchers = {
        "tencent-index": lambda: _fetch_tencent_index_snapshot(symbol, name),
        "eastmoney-index": lambda: _fetch_eastmoney_index_snapshot(symbol, name),
    }
    for source_key, fetcher in _ordered_handlers("indexes", fetchers):
        try:
            return _call_source(
                source_key,
                fetcher,
                category="indexes",
            )
        except Exception as error:
            errors.append(f"{source_names[source_key]}: {_short_fetch_error(error)}")
    raise RuntimeError("；".join(errors))


def _short_fetch_error(error: Exception) -> str:
    text = " ".join(str(error).split())
    return text[:120] if text else error.__class__.__name__


def _fetch_yahoo_index_snapshot(symbol: str, name: str) -> IndexSnapshot:
    params = {"range": "3mo", "interval": "1d"}
    url = YAHOO_CHART_URL.format(symbol=urllib.parse.quote(symbol)) + "?" + urllib.parse.urlencode(params)
    payload = json.loads(_http_get(url, timeout=12, attempts=1))
    result = payload.get("chart", {}).get("result", [{}])[0]
    closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    timestamps = result.get("timestamp", [])
    snapshot_date = (
        datetime.fromtimestamp(timestamps[-1]).date().isoformat()
        if timestamps
        else "--"
    )
    return _build_index_snapshot(symbol, name, closes, "yahoo", snapshot_date)


def _fetch_tencent_index_snapshot(symbol: str, name: str) -> IndexSnapshot:
    remote_symbol = TENCENT_INDEX_SYMBOLS[symbol]
    params = {"param": f"{remote_symbol},day,,,90,qfq"}
    url = TENCENT_INDEX_KLINE_URL + "?" + urllib.parse.urlencode(params)
    payload = json.loads(_http_get(url, timeout=12, attempts=1))
    rows = payload.get("data", {}).get(remote_symbol, {}).get("day", [])
    valid_rows = [row for row in rows if isinstance(row, list) and len(row) >= 3]
    closes = [row[2] for row in valid_rows]
    snapshot_date = str(valid_rows[-1][0]) if valid_rows else "--"
    return _build_index_snapshot(
        symbol, name, closes, "tencent-index", snapshot_date
    )


def _fetch_eastmoney_index_snapshot(symbol: str, name: str) -> IndexSnapshot:
    params = {
        "secid": EASTMONEY_INDEX_SECIDS[symbol],
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101,
        "fqt": 0,
        "end": 20500101,
        "lmt": 90,
    }
    url = EASTMONEY_INDEX_KLINE_URL + "?" + urllib.parse.urlencode(params)
    payload = json.loads(_http_get(url, timeout=12, attempts=1))
    klines = payload.get("data", {}).get("klines", [])
    valid_klines = [line.split(",") for line in klines if len(line.split(",")) >= 3]
    closes = [parts[2] for parts in valid_klines]
    snapshot_date = valid_klines[-1][0] if valid_klines else "--"
    return _build_index_snapshot(
        symbol, name, closes, "eastmoney-index", snapshot_date
    )


def _build_index_snapshot(
    symbol: str,
    name: str,
    closes: List[object],
    source: str,
    snapshot_date: str = "--",
) -> IndexSnapshot:
    series = [float(item) for item in closes if item is not None]
    if len(series) < 65:
        raise ValueError("index data not enough")

    price = series[-1]
    prev = series[-2]
    change_pct = round((price - prev) / prev * 100, 2)
    ma20 = sum(series[-20:]) / 20
    ma60 = sum(series[-60:]) / 60
    ma20_gap_pct = round((price - ma20) / ma20 * 100, 2)
    ma60_gap_pct = round((price - ma60) / ma60 * 100, 2)
    return IndexSnapshot(
        symbol=symbol,
        name=name,
        price=round(price, 2),
        change_pct=change_pct,
        ma20_gap_pct=ma20_gap_pct,
        ma60_gap_pct=ma60_gap_pct,
        source=source,
        date=snapshot_date,
    )


def _build_macro_headlines(indexes: List[IndexSnapshot]) -> List[str]:
    ixic = next((item for item in indexes if item.symbol == "IXIC"), None)
    hsi = next((item for item in indexes if item.symbol == "^HSI"), None)
    hs300 = next((item for item in indexes if item.symbol == "000300.SS"), None)

    phase = "全球权益偏强，但高位波动明显，适合分批而不是一次性重仓。"
    if ixic and ixic.ma20_gap_pct < -3:
        phase = "美股科技处于回撤修复阶段，适合右侧试仓，不适合情绪化抄底。"
    elif ixic and ixic.ma60_gap_pct > 8:
        phase = "美股科技偏热，估值透支较多，更适合等回撤后的二次买点。"

    hsi_view = "港股仍在震荡修复区间，买点应更多依赖回调深度。"
    if hsi and hsi.ma20_gap_pct > 2:
        hsi_view = "港股短线偏强，但追高的性价比下降，适合低吸而非抬价买。"
    elif hsi and hsi.ma20_gap_pct < -2:
        hsi_view = "港股短线承压，先等止跌与成交企稳，再考虑亚洲与港股基金。"

    a_share = "A股整体偏震荡，主动管理基金更依赖选股能力而不是贝塔普涨。"
    if hs300 and hs300.ma60_gap_pct > 4:
        a_share = "A股处于温和修复段，优质主动基金更容易跑出超额收益。"

    return [phase, hsi_view, a_share]


STATIC_REFERENCE_DATE = "2026-07-15"


def _macro_cache_is_usable(item: dict, key: str) -> bool:
    if not isinstance(item.get("value"), (int, float)):
        return False
    try:
        as_of = date.fromisoformat(str(item.get("date", ""))[:10])
    except ValueError:
        return False
    frequency = FRED_MACRO_META.get(key, {}).get("frequency", "daily")
    max_age_days = {"daily": 12, "monthly": 60, "quarterly": 160}.get(
        frequency, 12
    )
    return 0 <= (date.today() - as_of).days <= max_age_days


def _baseline_fund_history(code: str, error: str) -> dict:
    quote = fallback_fund_quotes().get(code)
    return {
        "records": [],
        "latest_date": quote.date if quote else STATIC_REFERENCE_DATE,
        "source": "baseline",
        "purchase_status": "--",
        "error": error,
    }


def _baseline_fund_holding(code: str, error: str) -> dict:
    return {
        "code": code,
        "disclosure_date": STATIC_REFERENCE_DATE,
        "holdings": [],
        "source": "baseline",
        "error": error,
        "note": "No live or cached holding disclosure is available; this is display-only baseline metadata.",
    }


def _baseline_recommendation_qualification(code: str, meta: dict, error: str) -> dict:
    return {
        "code": code,
        "name": meta["name"],
        "source": "baseline",
        "as_of": STATIC_REFERENCE_DATE,
        "eligible": False,
        "reasons": ["实时资格数据和缓存均不可用，静态基线不参与推荐。"],
        "error": error,
    }


def _baseline_macro_factor(key: str, name: str, error: str) -> dict:
    values = {
        "VIX": 20.0,
        "US2Y": 4.2,
        "US10Y": 4.4,
        "US10Y_REAL": 2.0,
        "DXY": 100.0,
        "USDCNY": 7.2,
        "US_GDP": 2.0,
        "US_CPI": 300.0,
        "US_UNEMPLOYMENT": 4.2,
        "US_M2": 21000.0,
        "US_FED_FUNDS": 4.25,
        "US_HY_SPREAD": 3.5,
        "US_CREDIT_STRESS_PROXY": 0.5,
    }
    annual_change = {"US_CPI": 2.5, "US_M2": 3.0}
    return _apply_macro_contract({
        "key": key,
        "name": name,
        "value": values.get(key, 0.0),
        "change_20": 0.0,
        "change_20_pct": 0.0,
        "change_12_pct": annual_change.get(key),
        "date": STATIC_REFERENCE_DATE,
        "source": "baseline",
        "frequency": FRED_MACRO_META.get(key, {}).get("frequency", "daily"),
        "error": error,
    })


def fallback_fund_quotes() -> Dict[str, FundQuote]:
    sample = {
        "016702": FundQuote(
            code="016702",
            name="银华海外数字经济量化选股混合发起式(QDII)C",
            nav=1.0000,
            date=STATIC_REFERENCE_DATE,
            source="fallback",
        ),
        "378006": FundQuote(
            code="378006",
            name="摩根全球新兴市场混合(QDII)",
            nav=1.0000,
            date=STATIC_REFERENCE_DATE,
            source="fallback",
        ),
        "378546": FundQuote(
            code="378546",
            name="摩根全球天然资源混合(QDII)",
            nav=1.0000,
            date=STATIC_REFERENCE_DATE,
            source="fallback",
        ),
        "018229": FundQuote(
            code="018229",
            name="易方达全球优质企业混合(QDII)A",
            nav=2.3892,
            date="2026-07-15",
            est_nav=2.4015,
            est_growth=0.51,
            source="fallback",
        ),
        "005534": FundQuote(
            code="005534",
            name="华夏新时代灵活配置混合A",
            nav=2.5338,
            date="2026-07-15",
            est_nav=2.5281,
            est_growth=-0.22,
            source="fallback",
        ),
        "161815": FundQuote(
            code="161815",
            name="银华抗通胀主题(QDII-FOF-LOF)A",
            nav=1.0240,
            date="2026-07-15",
            est_nav=1.0280,
            est_growth=0.39,
            source="fallback",
        ),
        "016701": FundQuote(
            code="016701",
            name="银华海外数字经济量化选股混合发起式(QDII)A",
            nav=1.9563,
            date="2026-07-15",
            est_nav=1.9640,
            est_growth=0.40,
            source="fallback",
        ),
        "162415": FundQuote(
            code="162415",
            name="华宝标普美国消费人民币A",
            nav=1.4600,
            date="2026-07-15",
            est_nav=1.4660,
            est_growth=0.41,
            source="fallback",
        ),
        "270023": FundQuote(
            code="270023",
            name="广发全球精选股票(QDII)人民币A",
            nav=5.8640,
            date="2026-07-15",
            est_nav=5.8900,
            est_growth=0.44,
            source="fallback",
        ),
    }

    for code, meta in RECOMMENDATION_POOL.items():
        sample[code] = FundQuote(
            code=code,
            name=meta["name"],
            nav=round(1.2 + (hash(code) % 180) / 100, 4),
            date="2026-07-15",
            source="fallback",
        )

    return sample


def fallback_indexes() -> List[IndexSnapshot]:
    return [
        IndexSnapshot(
            symbol="IXIC",
            name="纳斯达克综合指数",
            price=19862.45,
            change_pct=-0.84,
            ma20_gap_pct=-2.1,
            ma60_gap_pct=5.4,
            source="fallback",
            date=STATIC_REFERENCE_DATE,
        ),
        IndexSnapshot(
            symbol="DJI",
            name="道琼斯工业指数",
            price=41035.18,
            change_pct=-0.26,
            ma20_gap_pct=0.8,
            ma60_gap_pct=2.9,
            source="fallback",
            date=STATIC_REFERENCE_DATE,
        ),
        IndexSnapshot(
            symbol="^HSI",
            name="恒生指数",
            price=18422.56,
            change_pct=0.47,
            ma20_gap_pct=1.6,
            ma60_gap_pct=-0.9,
            source="fallback",
            date=STATIC_REFERENCE_DATE,
        ),
        IndexSnapshot(
            symbol="000300.SS",
            name="沪深300",
            price=3658.32,
            change_pct=-0.13,
            ma20_gap_pct=0.3,
            ma60_gap_pct=1.8,
            source="fallback",
            date=STATIC_REFERENCE_DATE,
        ),
    ]


def fallback_news() -> List[NewsItem]:
    return [
        NewsItem(
            title="美债利率维持高位震荡，全球成长资产承压",
            source="baseline",
            summary="若美国长端利率继续上行，全球科技基金应等待估值回撤后的分批买点。",
            tag="美元利率",
            impact="美元利率直接影响全球成长股估值；收益率下行通常利好科技QDII，快速上行则压制净值。",
            affected_funds="018229、161815、016701、162415、270023",
            direction="偏利空",
            watch="重点看美国10年期国债收益率、降息预期和纳斯达克走势是否同向。",
            priority=5,
            published_at=STATIC_REFERENCE_DATE,
        ),
        NewsItem(
            title="AI 与半导体景气仍强，但估值波动加大",
            source="baseline",
            summary="科技龙头盈利预期尚未破坏，但估值偏高时追涨会明显放大基金净值波动。",
            tag="AI与科技龙头",
            impact="核心科技公司业绩、资本开支和订单预期会直接改变全球科技基金的盈利与估值空间。",
            affected_funds="018229、161815、016701、162415、270023",
            direction="中性观察",
            watch="重点看财报收入指引、AI资本开支、毛利率及半导体订单是否超预期。",
            priority=6,
            published_at=STATIC_REFERENCE_DATE,
        ),
        NewsItem(
            title="港股互联网处于修复区间，等待成交与盈利确认",
            source="baseline",
            summary="只有成交、南向资金与平台公司盈利同步改善，亚洲精选基金的修复才更可靠。",
            tag="港股互联网",
            impact="港股互联网龙头的盈利修复、回购和监管变化会影响亚洲精选基金的主要弹性。",
            affected_funds="",
            direction="中性观察",
            watch="重点看恒生科技成交额、南向资金、平台公司盈利和回购强度。",
            priority=5,
            published_at=STATIC_REFERENCE_DATE,
        ),
        NewsItem(
            title="先进制造关注订单改善与价格竞争",
            source="baseline",
            summary="制造成长方向需要订单和利润率共同改善，单纯政策主题上涨不宜作为追买依据。",
            tag="先进制造",
            impact="制造业订单、价格和政策支持会影响A股成长与亚洲制造持仓的盈利预期。",
            affected_funds="005534",
            direction="中性观察",
            watch="重点看行业订单、产能利用率、价格竞争和政策落地后的盈利改善。",
            priority=4,
            published_at=STATIC_REFERENCE_DATE,
        ),
    ]


def market_phase_text(snapshot: Dict[str, object]) -> str:
    indexes = [IndexSnapshot(**item) for item in snapshot.get("indexes", [])]
    ixic = next((item for item in indexes if item.symbol == "IXIC"), None)
    if not ixic:
        return "全球市场处于震荡阶段"
    if ixic.ma60_gap_pct > 8:
        return "全球科技指数偏热，处于高位震荡阶段"
    if ixic.ma20_gap_pct < -3:
        return "全球科技指数处于回撤修复阶段"
    return "全球市场处于上行中的震荡阶段"


def data_freshness_text(sources: List[str]) -> str:
    source_set = set(sources)
    live_sources = {
        "eastmoney",
        "eastmoney-history",
        "eastmoney-index",
        "tencent-index",
    }
    if source_set and source_set <= live_sources:
        return "本次启动已重新抓取基金净值与全球指数"
    if source_set.intersection(live_sources):
        return "本次启动已部分抓取；其余数据为缓存或离线样例"
    return "本次抓取失败，当前为缓存或离线样例数据"
