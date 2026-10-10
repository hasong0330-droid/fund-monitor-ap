"""Browser-assisted fund detail page parser and cache writer.

The desktop browser may have network access even when the Python process does
not. This module deliberately accepts saved page HTML/DOM text rather than
scraping through an unverified HTTP shortcut. The resulting cache is treated
as a live, browser-observed source only when its page capture timestamp and
published NAV date pass the normal freshness checks in the main fetcher.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path


BROWSER_FUND_SOURCE = "browser-fund-page"
BROWSER_HISTORY_SOURCE = "browser-fund-history"


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = " ".join(str(data).split())
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return "\n".join(self.parts)


def html_to_text(content: str) -> str:
    parser = _TextParser()
    parser.feed(unescape(str(content or "")))
    return parser.text()


def _parse_number(value: str) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def _parse_date(value: str) -> str | None:
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", str(value))
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None


def parse_fund_detail_page(code: str, content: str, *, captured_at: str | None = None) -> dict:
    """Extract a published unit NAV and optional return/history fields.

    The parser targets visible labels used by major Chinese fund detail pages;
    it rejects pages without an explicit NAV date instead of accepting an
    estimate or a stale title-only match.
    """
    text = html_to_text(content)
    nav_date_match = re.search(
        r"单位净值\s*[（(]\s*(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})\s*[）)]",
        text,
    )
    nav_date = _parse_date(nav_date_match.group(1)) if nav_date_match else None
    if not nav_date:
        raise ValueError(f"{code} 浏览器页面缺少明确的单位净值日期")

    nav_match = re.search(
        r"单位净值\s*[（(][^）)]*[）)]\s*([0-9]+(?:\.[0-9]+)?)\s*[↑↓+-]?",
        text,
    )
    nav = _parse_number(nav_match.group(1)) if nav_match else None
    if not nav or nav <= 0:
        raise ValueError(f"{code} 浏览器页面缺少有效单位净值")

    name = code
    title_match = re.search(r"([^\n]{2,100}?)\s*\(\s*" + re.escape(code) + r"\s*\)", text)
    if title_match:
        name = title_match.group(1).strip()

    daily = None
    daily_match = re.search(r"单位净值[^\n]*?([+-]?\d+(?:\.\d+)?)%", text)
    if daily_match:
        daily = _parse_number(daily_match.group(1))
    monthly = None
    monthly_match = re.search(r"近1月[：:]\s*([+-]?\d+(?:\.\d+)?)%", text)
    if monthly_match:
        monthly = _parse_number(monthly_match.group(1))

    captured = captured_at or datetime.now().isoformat(timespec="seconds")
    result = {
        "code": str(code),
        "name": name,
        "nav": nav,
        "date": nav_date,
        "source": BROWSER_FUND_SOURCE,
        "captured_at": captured,
        "daily_return_pct": daily,
        "monthly_return_pct": monthly,
    }
    return result


def write_browser_quote_cache(output: Path, records: list[dict]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "source": BROWSER_FUND_SOURCE,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "records": records,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_browser_quote_cache(path: Path) -> dict[str, dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    records = payload.get("records", []) if isinstance(payload, dict) else []
    return {
        str(item.get("code")): item
        for item in records
        if isinstance(item, dict) and item.get("code") and item.get("nav") and item.get("date")
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse browser-saved fund detail pages")
    parser.add_argument("--html", action="append", nargs=2, metavar=("CODE", "PATH"), default=[])
    parser.add_argument(
        "--text", action="append", nargs=2, metavar=("CODE", "PATH"), default=[],
        help="导入浏览器复制的页面正文文本（每行一个 CODE PATH）",
    )
    parser.add_argument("--output", type=Path, default=Path("cache/browser_fund_quotes.json"))
    args = parser.parse_args()
    records = []
    for code, path_text in [*args.html, *args.text]:
        records.append(parse_fund_detail_page(code, Path(path_text).read_text(encoding="utf-8")))
    if not records:
        parser.error("至少提供一个 --html 或 --text")
    write_browser_quote_cache(args.output, records)
    print(json.dumps({"output": str(args.output), "codes": [item["code"] for item in records]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
