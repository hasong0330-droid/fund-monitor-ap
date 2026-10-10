"""Secure, app-owned adapter for selected iFinD data services.

The adapter deliberately covers only the data families whose returned fields
meet this application's publication and completeness requirements. It does not
read Codex's skill configuration, so the desktop app remains independent.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from typing import Any


IFIND_TOKEN_ENV = "FUND_MONITOR_IFIND_AUTH_TOKEN"
IFIND_BASE_URL = "https://api-mcp.51ifind.com:8643/ds-mcp-servers"
IFIND_SERVERS = {
    "fund": f"{IFIND_BASE_URL}/hexin-ifind-ds-fund-mcp",
    "edb": f"{IFIND_BASE_URL}/hexin-ifind-ds-edb-mcp",
    "global_stock": f"{IFIND_BASE_URL}/hexin-ifind-ds-global-stock-mcp",
    "news": f"{IFIND_BASE_URL}/hexin-ifind-ds-news-mcp",
}
_REQUEST_INTERVAL_SECONDS = 0.55
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0


class IFindError(RuntimeError):
    """The iFinD service is unavailable or returned unusable data."""


def ifind_is_configured() -> bool:
    return bool(os.environ.get(IFIND_TOKEN_ENV, "").strip())


def _throttle() -> None:
    global _LAST_REQUEST_AT
    with _REQUEST_LOCK:
        wait_seconds = _REQUEST_INTERVAL_SECONDS - (time.monotonic() - _LAST_REQUEST_AT)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        _LAST_REQUEST_AT = time.monotonic()


def _parse_json_response(raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace").strip()
    if text.startswith("data:"):
        text = "\n".join(
            line.removeprefix("data:").strip()
            for line in text.splitlines()
            if line.startswith("data:")
        )
    try:
        result = json.loads(text)
    except json.JSONDecodeError as error:
        raise IFindError("iFinD 返回了无法解析的响应") from error
    if not isinstance(result, dict):
        raise IFindError("iFinD 返回结构不是 JSON 对象")
    return result


def unpack_ifind_content(payload: dict[str, Any]) -> dict[str, Any]:
    """Unwrap the nested MCP content payload into the vendor's data object."""
    if payload.get("error"):
        raise IFindError(f"iFinD MCP 错误：{payload['error']}")
    content = ((payload.get("result") or {}).get("content") or [])
    for item in content:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        try:
            envelope = json.loads(item["text"])
        except json.JSONDecodeError:
            continue
        if int(envelope.get("code", 1)) != 1:
            raise IFindError(str(envelope.get("msg") or "iFinD 查询失败"))
        data = envelope.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError as error:
                raise IFindError("iFinD 数据内容无法解析") from error
        if isinstance(data, dict):
            return data
    raise IFindError("iFinD 未返回可用数据内容")


def _parse_date(value: object) -> str | None:
    text = str(value or "").strip()
    match = re.search(r"(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})", text)
    if not match:
        return None
    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "")
    if text.lower() in {"", "null", "none", "nan", "-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _markdown_tables(markdown: str) -> list[tuple[list[str], list[list[str]]]]:
    lines = [line.strip() for line in str(markdown or "").splitlines()]
    tables: list[tuple[list[str], list[list[str]]]] = []
    index = 0
    while index + 1 < len(lines):
        if not lines[index].startswith("|") or not lines[index + 1].startswith("|"):
            index += 1
            continue
        headers = [cell.strip() for cell in lines[index].strip("|").split("|")]
        divider = [cell.strip(" :-") for cell in lines[index + 1].strip("|").split("|")]
        if not headers or not all(cell == "" for cell in divider):
            index += 1
            continue
        index += 2
        rows: list[list[str]] = []
        while index < len(lines) and lines[index].startswith("|"):
            cells = [cell.strip() for cell in lines[index].strip("|").split("|")]
            if len(cells) == len(headers):
                rows.append(cells)
            index += 1
        if rows:
            tables.append((headers, rows))
    return tables


def _tables(data: dict[str, Any]) -> list[tuple[list[str], list[list[object]]]]:
    parsed: list[tuple[list[str], list[list[object]]]] = []
    for item in data.get("datas") or []:
        table = ((item or {}).get("data") or {})
        headers = table.get("columns") or []
        rows = table.get("data") or []
        if isinstance(headers, list) and isinstance(rows, list):
            parsed.append(([str(value) for value in headers], rows))
    parsed.extend(_markdown_tables(str(data.get("answer") or "")))
    return parsed


def parse_macro_observations(data: dict[str, Any], key: str) -> list[tuple[str, float]]:
    tables = _tables(data)
    if any("预测" in header for headers, _ in tables for header in headers):
        raise IFindError(f"iFinD {key} 结果含预测值，不能进入模型")
    minimum = 13 if key == "US_M2" else 2
    for headers, rows in tables:
        date_index = next((i for i, value in enumerate(headers) if "日期" in value), None)
        if date_index is None:
            continue
        value_indexes = [i for i in range(len(headers)) if i != date_index]
        for value_index in value_indexes:
            observations: list[tuple[str, float]] = []
            for row in rows:
                if not isinstance(row, list) or len(row) <= max(date_index, value_index):
                    continue
                observation_date = _parse_date(row[date_index])
                value = _number(row[value_index])
                if observation_date and value is not None:
                    observations.append((observation_date, value))
            deduped = {observation_date: value for observation_date, value in observations}
            ordered = sorted(deduped.items())
            if len(ordered) >= minimum:
                return ordered
    raise IFindError(f"iFinD {key} 缺少足够的已发布历史序列")


def parse_fund_nav_quotes(
    data: dict[str, Any], expected_codes: list[str]
) -> dict[str, dict[str, object]]:
    """Parse only published unit NAVs whose code and date can be verified."""
    expected = {str(code).strip() for code in expected_codes}
    if not expected:
        raise IFindError("iFinD 基金净值查询缺少基金代码")
    if any("估算" in header or "估值" in header for headers, _ in _tables(data) for header in headers):
        raise IFindError("iFinD 基金净值结果包含估算字段，不能用于持仓估值")

    values: dict[str, dict[str, object]] = {}
    for headers, rows in _tables(data):
        code_index = next(
            (index for index, header in enumerate(headers) if "证券代码" in header),
            None,
        )
        if code_index is None:
            continue
        date_index = next(
            (index for index, header in enumerate(headers) if "净值日期" in header),
            None,
        )
        nav_index = next(
            (
                index
                for index, header in enumerate(headers)
                if header.strip().startswith("单位净值")
                and "增长率" not in header
                and "累计" not in header
            ),
            None,
        )
        name_index = next(
            (index for index, header in enumerate(headers) if "证券简称" in header),
            None,
        )
        for row in rows:
            if not isinstance(row, list) or len(row) <= code_index:
                continue
            code = str(row[code_index]).strip().upper().removesuffix(".OF")
            if code not in expected:
                continue
            item = values.setdefault(code, {"code": code})
            if name_index is not None and len(row) > name_index:
                item["name"] = str(row[name_index]).strip()
            if date_index is not None and len(row) > date_index:
                nav_date = _parse_date(row[date_index])
                if nav_date and nav_date <= date.today().isoformat():
                    item["date"] = nav_date
            if nav_index is not None and len(row) > nav_index:
                nav = _number(row[nav_index])
                if nav is not None and nav > 0:
                    item["nav"] = nav

    valid = {
        code: item
        for code, item in values.items()
        if item.get("date") and isinstance(item.get("nav"), (int, float))
    }
    if not valid:
        raise IFindError("iFinD 未返回代码、日期和单位净值均完整的基金记录")
    return valid


def _first_table_value(
    tables: list[tuple[list[str], list[list[object]]]], keyword: str
) -> object | None:
    for headers, rows in tables:
        column = next((index for index, value in enumerate(headers) if keyword in value), None)
        if column is None:
            continue
        for row in rows:
            if isinstance(row, list) and len(row) > column and str(row[column]).strip():
                return row[column]
    return None


def _first_exact_table_value(
    tables: list[tuple[list[str], list[list[object]]]], header: str
) -> object | None:
    for headers, rows in tables:
        column = next(
            (index for index, value in enumerate(headers) if value.strip() == header),
            None,
        )
        if column is None:
            continue
        for row in rows:
            if isinstance(row, list) and len(row) > column and str(row[column]).strip():
                return row[column]
    return None


def parse_global_fundamentals(data: dict[str, Any], ticker: str) -> dict[str, Any]:
    tables = _tables(data)
    fiscal_end = _parse_date(_first_table_value(tables, "报告截止日期")) or _parse_date(
        _first_exact_table_value(tables, "日期")
    )
    if not fiscal_end or fiscal_end > date.today().isoformat():
        raise IFindError(f"iFinD {ticker} 缺少已披露报告期截止日")

    metric_map = {
        "营业收入(同比增长率)": "revenue_yoy",
        "净利润(同比增长率)": "net_income_yoy",
        "净资产收益率ROE": "roe",
        "销售毛利率": "gross_margin",
        "市盈率PE": "pe_ttm",
    }
    metrics: dict[str, float | None] = {
        "revenue": None,
        "revenue_yoy": None,
        "net_income": None,
        "net_income_yoy": None,
        "net_margin": None,
        "operating_margin": None,
        "free_cash_flow_margin": None,
        "roe": None,
        "gross_margin": None,
        "pe_ttm": None,
    }
    for header_fragment, metric_key in metric_map.items():
        value = _number(_first_table_value(tables, header_fragment))
        if value is not None:
            metrics[metric_key] = value / 100 if metric_key in {
                "revenue_yoy", "net_income_yoy", "roe", "gross_margin"
            } else value
    available = sum(value is not None for value in metrics.values())
    if available < 3:
        raise IFindError(f"iFinD {ticker} 财报关键指标不足")
    return {
        "ticker": ticker,
        "symbol": ticker,
        "company": ticker,
        "fiscal_end": fiscal_end,
        "filed_at": "",
        "metrics": metrics,
        "metric_coverage": round(available / len(metrics), 4),
        "source": "ifind-financials",
        "provider": "同花顺iFinD 全球股票财报",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def parse_news_items(data: dict[str, Any]) -> list[dict[str, str]]:
    raw = data.get("data")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as error:
            raise IFindError("iFinD 新闻内容无法解析") from error
    if not isinstance(raw, list):
        raise IFindError("iFinD 未返回新闻列表")
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("资讯标题") or "").strip()
        summary = str(item.get("资讯内容") or "").strip()
        published_at = _parse_date(item.get("日期")) or ""
        link = str(item.get("URL") or "").strip()
        if title and summary and published_at:
            result.append(
                {
                    "title": title,
                    "summary": summary,
                    "published_at": published_at,
                    "link": link,
                }
            )
    if not result:
        raise IFindError("iFinD 新闻没有可审计的标题、日期和内容")
    return result


class IFindClient:
    def __init__(self, auth_token: str | None = None) -> None:
        self.auth_token = (auth_token or os.environ.get(IFIND_TOKEN_ENV, "")).strip()
        self._sessions: dict[str, str] = {}
        self._request_ids: dict[str, int] = {}
        self._lock = threading.RLock()
        self._context = ssl.create_default_context()

    @property
    def configured(self) -> bool:
        return bool(self.auth_token)

    def _next_id(self, service: str) -> int:
        self._request_ids[service] = self._request_ids.get(service, 0) + 1
        return self._request_ids[service]

    def _headers(self, service: str) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": self.auth_token,
        }
        if service in self._sessions:
            headers["Mcp-Session-Id"] = self._sessions[service]
        return headers

    def _post(self, service: str, payload: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
        if service not in IFIND_SERVERS:
            raise IFindError(f"未配置 iFinD 服务：{service}")
        _throttle()
        request = urllib.request.Request(
            IFIND_SERVERS[service],
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(service),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self._context) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self._sessions[service] = session_id
                return _parse_json_response(response.read())
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")[:240]
            raise IFindError(f"iFinD HTTP {error.code}: {body}") from error
        except Exception as error:
            raise IFindError(f"iFinD 连接失败：{error}") from error

    def _initialize(self, service: str) -> None:
        if service in self._sessions:
            return
        response = self._post(
            service,
            {
                "jsonrpc": "2.0",
                "id": self._next_id(service),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "fund-monitor", "version": "1.0"},
                },
            },
        )
        if not self._sessions.get(service):
            raise IFindError(f"iFinD 初始化未返回会话：{response}")
        self._post(
            service,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            timeout=10,
        )

    def call(self, service: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise IFindError(f"未设置环境变量 {IFIND_TOKEN_ENV}")
        with self._lock:
            self._initialize(service)
            response = self._post(
                service,
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(service),
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                },
                timeout=45,
            )
        return unpack_ifind_content(response)

    def macro_observations(self, key: str) -> list[tuple[str, float]]:
        queries = {
            "US_GDP": "美国实际GDP季度环比折年率，2018Q1至最新，不含预测值",
            "US_HY_SPREAD": "美国高收益债期权调整利差OAS日度值，2025-01-01至最新，不含预测值",
            "US_M2": "美国M2货币供应量月度值，2018-01至最新，不含预测值",
        }
        query = queries.get(key)
        if not query:
            raise IFindError(f"iFinD 未配置宏观指标：{key}")
        return parse_macro_observations(
            self.call("edb", "get_edb_data", {"query": query}), key
        )

    def fund_nav_quotes(self, codes: list[str]) -> dict[str, dict[str, object]]:
        normalized = list(dict.fromkeys(str(code).strip() for code in codes if code))
        if not normalized or len(normalized) > 5:
            raise IFindError("iFinD 单次基金净值查询必须包含1至5只基金")
        query = (
            "、".join(normalized)
            + "最新已公布单位净值、净值日期和证券简称，不含盘中估算净值"
        )
        return parse_fund_nav_quotes(
            self.call("fund", "get_fund_market_performance", {"query": query}),
            normalized,
        )

    def global_fundamentals(self, ticker: str) -> dict[str, Any]:
        today = date.today()
        quarter_start_month = ((today.month - 1) // 3) * 3 + 1
        quarter_start = date(today.year, quarter_start_month, 1)
        latest_completed_quarter = quarter_start - timedelta(days=1)
        quarter_text = latest_completed_quarter.strftime("%Y%m%d")
        query = (
            f"{ticker} 截至{quarter_text}最新一期已披露财报的报告截止日期和指标对应日期、"
            "营业收入同比增长率、净利润同比增长率、净资产收益率ROE、销售毛利率和"
            "市盈率PE(TTM)，不含预测值"
        )
        return parse_global_fundamentals(
            self.call("global_stock", "global_stock_financial", {"query": query}), ticker
        )

    def relevant_news(self) -> list[dict[str, str]]:
        today = date.today()
        return self.historical_relevant_news(
            (today - timedelta(days=7)).isoformat(), today.isoformat(), size=8
        )

    def historical_relevant_news(
        self,
        start_date: str,
        end_date: str,
        size: int = 50,
        query: str | None = None,
    ) -> list[dict[str, str]]:
        """Return dated fund-relevant news for point-in-time archiving."""
        query = query or (
            "美联储利率、通胀、美债收益率、AI、半导体、出口管制、人民币汇率"
            "对全球科技、全球成长与亚洲股票基金的影响"
        )
        return parse_news_items(
            self.call(
                "news",
                "search_news",
                {
                    "query": query,
                    "time_start": start_date,
                    "time_end": end_date,
                    "size": max(1, min(int(size), 100)),
                },
            )
        )


_DEFAULT_CLIENT: IFindClient | None = None
_DEFAULT_CLIENT_LOCK = threading.Lock()


def get_ifind_client() -> IFindClient:
    global _DEFAULT_CLIENT
    with _DEFAULT_CLIENT_LOCK:
        token = os.environ.get(IFIND_TOKEN_ENV, "").strip()
        if _DEFAULT_CLIENT is None or _DEFAULT_CLIENT.auth_token != token:
            _DEFAULT_CLIENT = IFindClient(token)
        return _DEFAULT_CLIENT
