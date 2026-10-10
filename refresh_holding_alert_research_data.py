"""Refresh auditable peer NAVs and portfolio point-in-time holdings.

This data serves the holding-risk calibration only.  It does not alter the
designated-fund pool, recommendation pool, or the user's actual positions.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

from backtest_model import (
    RESEARCH_HISTORY_CACHE,
    RESEARCH_HISTORY_DAYS,
    _parse_holding_year_payload,
)
from config import CACHE_DIR, PORTFOLIO_HOLDINGS
from fetcher import (
    FUND_HOLDINGS_URL,
    _fetch_fund_history_backup,
    _http_get,
    _powershell_http_get,
)
from storage import atomic_write_json


PEER_HISTORY_FILE = CACHE_DIR / "holding_alert_peer_histories.json"
PORTFOLIO_POINT_IN_TIME_FILE = CACHE_DIR / "portfolio_risk_point_in_time.json"
PEER_FUNDS = {
    "160216": {
        "name": "国泰大宗商品(QDII)A",
        "profile": "resource",
        "rationale": "QDII商品策略，补充全球天然资源基金的资源周期净值样本。",
    },
    "005976": {
        "name": "长信稳进资产配置(FOF)",
        "profile": "fof",
        "rationale": "FOF-均衡型，补充资产配置基金的净值样本。",
    },
}
HISTORY_DAYS = 1100
CONSERVATIVE_DISCLOSURE_LAG_DAYS = 45


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _fallback_peer_history(code: str) -> dict:
    """Use the previous audited cache if a public source is temporarily down."""
    previous = _read_json(PEER_HISTORY_FILE).get("funds", {}).get(code)
    if isinstance(previous, dict) and previous.get("records"):
        return previous
    legacy = _read_json(CACHE_DIR / "_supplement_histories.json").get(code)
    if isinstance(legacy, dict) and legacy.get("records"):
        return legacy
    return {}


def _fallback_portfolio_periods(code: str) -> list[dict]:
    previous = _read_json(PORTFOLIO_POINT_IN_TIME_FILE).get("funds", {}).get(code)
    if isinstance(previous, list) and previous:
        return previous
    # The first import of the public holdings endpoint predates the envelope
    # used by the audited cache and is stored as a bare list.
    try:
        legacy = json.loads(
            (CACHE_DIR / f"_{code}_holdings.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        legacy = []
    return legacy if isinstance(legacy, list) else []


def _available_date(disclosure_date: str) -> str:
    disclosed = datetime.strptime(disclosure_date, "%Y-%m-%d").date()
    return (disclosed + timedelta(days=CONSERVATIVE_DISCLOSURE_LAG_DAYS)).isoformat()


def _fetch_holding_year(code: str, year: int) -> list[dict]:
    params = {
        "type": "jjcc",
        "code": code,
        "topline": 10,
        "year": year,
        "month": 12,
        "rt": "0.123456",
    }
    url = FUND_HOLDINGS_URL + "?" + urllib.parse.urlencode(params)
    try:
        payload = _http_get(
            url,
            timeout=15,
            attempts=1,
            referer=f"https://fundf10.eastmoney.com/ccmx_{code}.html",
        )
    except Exception:
        payload = _powershell_http_get(
            url,
            timeout=25,
            referer=f"https://fundf10.eastmoney.com/ccmx_{code}.html",
        )
    records = _parse_holding_year_payload(payload)
    for record in records:
        disclosure_date = str(record.get("disclosure_date", ""))
        record["available_date"] = _available_date(disclosure_date)
        record["available_date_method"] = (
            f"report_period_end_plus_{CONSERVATIVE_DISCLOSURE_LAG_DAYS}_calendar_days"
        )
        record["source_url"] = url
    return records


def _normalize_periods(code: str, periods: list[dict]) -> list[dict]:
    unique: dict[str, dict] = {}
    for item in periods:
        disclosure_date = str(item.get("disclosure_date", ""))
        holdings = item.get("holdings")
        if not disclosure_date or not isinstance(holdings, list):
            continue
        normalized_holdings = []
        for holding in holdings[:10]:
            try:
                weight = float(holding.get("weight", 0))
            except (AttributeError, TypeError, ValueError):
                continue
            if holding.get("name") and 0 < weight <= 1:
                normalized_holdings.append(
                    {
                        "code": str(holding.get("code", "")),
                        "name": str(holding["name"]),
                        "weight": round(weight, 6),
                    }
                )
        if not normalized_holdings:
            continue
        available = str(item.get("available_date") or _available_date(disclosure_date))
        unique[disclosure_date] = {
            "disclosure_date": disclosure_date,
            "effective_date": str(
                item.get("effective_date")
                or (datetime.strptime(disclosure_date, "%Y-%m-%d").date() + timedelta(days=30)).isoformat()
            ),
            "available_date": available,
            "available_date_method": str(
                item.get("available_date_method")
                or f"report_period_end_plus_{CONSERVATIVE_DISCLOSURE_LAG_DAYS}_calendar_days"
            ),
            "holdings": normalized_holdings,
            "source": str(item.get("source") or "eastmoney-holdings-history"),
            "source_url": str(
                item.get("source_url")
                or f"https://fundf10.eastmoney.com/ccmx_{code}.html"
            ),
        }
    return sorted(unique.values(), key=lambda item: item["disclosure_date"])


def refresh() -> tuple[dict, dict]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    peer_funds = {}
    for code, metadata in PEER_FUNDS.items():
        try:
            history = _fetch_fund_history_backup(code, HISTORY_DAYS)
        except Exception:
            history = _fallback_peer_history(code)
        records = list(history.get("records") or [])
        if len(records) < 600:
            raise RuntimeError(f"{code} 同类样本历史净值不足600条")
        peer_funds[code] = {
            **metadata,
            "records": records,
            "latest_date": str(history.get("latest_date") or records[0]["date"]),
            "source": str(history.get("source") or "eastmoney-pingzhongdata"),
            "source_url": f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
            "history_days_requested": HISTORY_DAYS,
            "record_count": len(records),
        }
    peer_payload = {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "purpose": "holding_alert_calibration_peer_navs",
        "funds": peer_funds,
    }
    # Keep the expanded research cache self-contained for tools that consume
    # backtest_research_history.json directly. These codes remain calibration
    # only and are still absent from the formal recommendation pool.
    existing_research = _read_json(RESEARCH_HISTORY_CACHE) or {}
    merged_research_funds = {
        **(existing_research.get("funds", {}) or {}),
        **peer_funds,
    }
    atomic_write_json(
        RESEARCH_HISTORY_CACHE,
        {
            "ts": int(datetime.now().timestamp()),
            "history_days": int(existing_research.get("history_days") or RESEARCH_HISTORY_DAYS),
            "funds": merged_research_funds,
        },
    )

    periods_by_fund = {}
    for code in PORTFOLIO_HOLDINGS:
        periods = []
        for year in range(2023, date.today().year + 1):
            try:
                periods.extend(_fetch_holding_year(code, year))
            except Exception:
                continue
        if not periods:
            periods = _fallback_portfolio_periods(code)
        normalized = _normalize_periods(code, periods)
        if not normalized:
            raise RuntimeError(f"{code} 未取得有效的历史持仓快照")
        periods_by_fund[code] = normalized
    portfolio_payload = {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "purpose": "portfolio_risk_point_in_time_holdings",
        "portfolio_codes": list(PORTFOLIO_HOLDINGS),
        "source": "eastmoney-holdings-history",
        "available_date_policy": (
            "Each quarterly report uses a conservative 45-calendar-day publication lag; "
            "only records available on or before a signal date are eligible."
        ),
        "funds": periods_by_fund,
    }
    atomic_write_json(PEER_HISTORY_FILE, peer_payload)
    atomic_write_json(PORTFOLIO_POINT_IN_TIME_FILE, portfolio_payload)
    return peer_payload, portfolio_payload


if __name__ == "__main__":
    peers, portfolio = refresh()
    print(
        json.dumps(
            {
                "peer_histories": {
                    code: item["record_count"] for code, item in peers["funds"].items()
                },
                "portfolio_periods": {
                    code: len(items) for code, items in portfolio["funds"].items()
                },
            },
            ensure_ascii=False,
        )
    )
