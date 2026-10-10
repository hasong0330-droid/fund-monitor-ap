"""Investoday Streamable HTTP MCP adapter used by the fund monitor."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import Any


INVESTODAY_API_KEY_ENV = "FUND_MONITOR_INVESTODAY_MCP_API_KEY"
INVESTODAY_URL_ENV = "FUND_MONITOR_INVESTODAY_MCP_URL"
DEFAULT_INVESTODAY_MCP_URL = "https://data-api.investoday.net/data/mcp/preset"


class InvestodayError(RuntimeError):
    """The Investoday MCP service is unavailable or returned unusable data."""


def investoday_is_configured() -> bool:
    return bool(os.environ.get(INVESTODAY_API_KEY_ENV, "").strip())


def _endpoint(base_url: str, api_key: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if not any(name.lower() == "apikey" for name, _ in query):
        query.append(("apiKey", api_key))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
    )


def _decode_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("error"):
        raise InvestodayError(str(payload["error"]))
    result = payload.get("result") or {}
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in result.get("content") or []:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        try:
            decoded = json.loads(item["text"])
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            return decoded
    raise InvestodayError("Investoday MCP 未返回可解析的数据")


class InvestodayClient:
    def __init__(self, api_key: str | None = None, url: str | None = None) -> None:
        self.api_key = (api_key or os.environ.get(INVESTODAY_API_KEY_ENV, "")).strip()
        self.url = (url or os.environ.get(INVESTODAY_URL_ENV, DEFAULT_INVESTODAY_MCP_URL)).strip()
        self._context = ssl.create_default_context()
        self._request_id = 0

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise InvestodayError(f"未设置环境变量 {INVESTODAY_API_KEY_ENV}")
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        request = urllib.request.Request(
            _endpoint(self.url, self.api_key),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20, context=self._context) as response:
                decoded = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as error:
            raise InvestodayError(f"Investoday HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise InvestodayError(f"Investoday 连接失败：{error}") from error
        except json.JSONDecodeError as error:
            raise InvestodayError("Investoday 返回了无法解析的响应") from error
        if not isinstance(decoded, dict):
            raise InvestodayError("Investoday 返回结构不是 JSON 对象")
        return _decode_payload(decoded)

    def list_news(
        self,
        *,
        title: str = "",
        begin_time: str | None = None,
        end_time: str | None = None,
        page_size: int = 30,
    ) -> list[dict[str, Any]]:
        today = date.today()
        data = self.call(
            "list_news",
            {
                "title": title,
                "beginTime": begin_time or f"{(today - timedelta(days=120)).isoformat()} 00:00:00",
                "endTime": end_time or f"{today.isoformat()} 23:59:59",
                "pageNum": 1,
                "pageSize": max(1, min(int(page_size), 100)),
            },
        )
        rows = data.get("data")
        if not isinstance(rows, list):
            raise InvestodayError("Investoday 新闻数据不是列表")
        return [row for row in rows if isinstance(row, dict)]


_DEFAULT_CLIENT: InvestodayClient | None = None


def get_investoday_client() -> InvestodayClient:
    global _DEFAULT_CLIENT
    key = os.environ.get(INVESTODAY_API_KEY_ENV, "").strip()
    url = os.environ.get(INVESTODAY_URL_ENV, DEFAULT_INVESTODAY_MCP_URL).strip()
    if _DEFAULT_CLIENT is None or _DEFAULT_CLIENT.api_key != key or _DEFAULT_CLIENT.url != url:
        _DEFAULT_CLIENT = InvestodayClient(key, url)
    return _DEFAULT_CLIENT

