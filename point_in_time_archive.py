"""Auditable point-in-time archive for fundamentals and fund-relevant news.

The archive stores source events with their original publication timestamps.
The backtest panel is derived from those events and never overwrites an older
observation with a later filing or article.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from config import (
    EASTMONEY_FUND_NEWS_QUERIES,
    POINT_IN_TIME_NEWS_FUND_GROUPS,
    POINT_IN_TIME_ARCHIVE_FILE,
    POINT_IN_TIME_AUDIT_FILE,
    POINT_IN_TIME_PANEL_FILE,
)
from factor_model import _company_fundamental_score
from source_router import is_live_source
from storage import atomic_write_json, atomic_write_text


ARCHIVE_SCHEMA_VERSION = 1
MIN_FUNDAMENTAL_COVERAGE = 0.60
NEWS_LOOKBACK_DAYS = 45
# Formal acceptance needs enough fund-level observations to test the model, not
# merely a large number of issuer filings concentrated in a few portfolios.
# The expanded research sample is not formally accepted until at least twenty
# distinct funds have usable 60%-weighted point-in-time fundamentals.
FORMAL_MIN_COVERED_FUNDS = 20
FORMAL_MIN_USABLE_FUNDAMENTAL_SNAPSHOTS = 180
FORMAL_CORE_FUND_CODES = ("018229", "005698", "017437")
FORMAL_MIN_CORE_SNAPSHOTS = 2
SEC_TICKER_OVERRIDES = {
    "09988": "BABA",
    "00883": "CEO",
    "01179": "HTHT",
    "2330": "TSM",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _date(value: object) -> str:
    text = str(value or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _load_archive() -> dict:
    try:
        payload = json.loads(POINT_IN_TIME_ARCHIVE_FILE.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "created_at": payload.get("created_at") or _now(),
        "updated_at": payload.get("updated_at") or "",
        "fundamentals": payload.get("fundamentals") or {},
        "news": payload.get("news") or [],
        "errors": payload.get("errors") or [],
        "backfill_runs": payload.get("backfill_runs") or [],
    }


def _normalise_metrics(metrics: object) -> dict[str, float]:
    if not isinstance(metrics, dict):
        return {}
    return {
        str(key): round(float(value), 10)
        for key, value in metrics.items()
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    }


def _fundamental_event(code: str, item: dict) -> dict | None:
    source = str(item.get("source") or "").strip()
    fiscal_end = _date(item.get("fiscal_end"))
    filed_at = _date(item.get("filed_at"))
    metrics = _normalise_metrics(item.get("metrics"))
    score, metric_coverage = _company_fundamental_score(metrics)
    if (
        not fiscal_end
        or not filed_at
        or filed_at < fiscal_end
        or metric_coverage < 0.50
        or not is_live_source(source, "fundamentals")
    ):
        return None
    core = {
        "security_code": code,
        "ticker": str(item.get("ticker") or code),
        "company": str(item.get("company") or code),
        "as_of_date": fiscal_end,
        "available_date": filed_at,
        "filed_at": filed_at,
        "metrics": metrics,
        "score": round(score, 4),
        "metric_coverage": round(metric_coverage, 4),
        "source": source,
        "provider": str(item.get("provider") or source),
        "cik": str(item.get("cik") or ""),
    }
    return {
        **core,
        "event_id": _fingerprint({"type": "fundamental", **core}),
        "observed_at": _now(),
    }


def _news_source_key(source: object) -> str:
    label = str(source or "").lower()
    if "ifind" in label:
        return "ifind-news"
    if "gdelt" in label:
        return "gdelt-news"
    if "eastmoney" in label or "东方" in str(source or ""):
        return "eastmoney-news"
    if "sina" in label or "新浪" in str(source or ""):
        return "sina-news"
    return "finance-news"


def _fund_codes(value: object) -> list[str]:
    return sorted(
        {
            code
            for code in re.split(r"[、,，;；\s]+", str(value or ""))
            if re.fullmatch(r"\d{6}", code)
        }
    )


def _news_score(direction: object) -> float:
    text = str(direction or "")
    if "利好" in text:
        return 65.0
    if "利空" in text:
        return 35.0
    return 50.0


def _news_event(item: object) -> dict | None:
    title = str(getattr(item, "title", "") or "").strip()
    summary = str(getattr(item, "summary", "") or "").strip()
    published_at = str(getattr(item, "published_at", "") or "").strip()
    publication_date = _date(published_at)
    # Providers do not consistently provide a timezone and market-session
    # cutoff. Make dated news available from the next calendar day so a
    # same-day after-close article cannot leak into a daily fund signal.
    available_date = (
        (datetime.strptime(publication_date, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
        if publication_date
        else ""
    )
    source_label = str(getattr(item, "source", "") or "").strip()
    source = _news_source_key(source_label)
    if not title or not available_date or not is_live_source(source, "news"):
        return None
    core = {
        "title": title,
        "summary": summary,
        "available_date": available_date,
        "published_at": published_at,
        "source": source,
        "source_label": source_label,
        "link": str(getattr(item, "link", "") or ""),
        "direction": str(getattr(item, "direction", "") or ""),
        "score": _news_score(getattr(item, "direction", "")),
        "affected_funds": _fund_codes(getattr(item, "affected_funds", "")),
        "tag": str(getattr(item, "tag", "") or ""),
    }
    return {
        **core,
        "event_id": _fingerprint({"type": "news", **core}),
        "observed_at": _now(),
    }


def _news_available_date(event: dict) -> str:
    """Derive a conservative usable date from the immutable publication time."""
    published_date = _date(event.get("published_at"))
    if not published_date:
        return ""
    return (
        datetime.strptime(published_date, "%Y-%m-%d").date() + timedelta(days=1)
    ).isoformat()


def _news_affected_funds(event: dict) -> list[str]:
    """Return source labels plus the auditable topic-to-research mapping."""
    source_codes = event.get("affected_funds") or []
    if isinstance(source_codes, str):
        source_codes = _fund_codes(source_codes)
    codes = {str(code).strip() for code in source_codes if re.fullmatch(r"\d{6}", str(code).strip())}
    topic = str(event.get("tag") or "").strip()
    codes.update(POINT_IN_TIME_NEWS_FUND_GROUPS.get(topic, ()))
    return sorted(codes)


def _merge_events(existing: list[dict], events: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    by_id = {str(item.get("event_id")): item for item in existing if item.get("event_id")}
    inserted = []
    for event in events:
        event_id = str(event.get("event_id") or "")
        if event_id and event_id not in by_id:
            by_id[event_id] = event
            inserted.append(event)
    merged = sorted(
        by_id.values(),
        key=lambda item: (str(item.get("available_date", "")), str(item.get("event_id", ""))),
    )
    return merged, inserted


def _write_audit(archive: dict, new_events: list[dict]) -> None:
    previous = []
    if POINT_IN_TIME_AUDIT_FILE.exists():
        try:
            previous = [
                json.loads(line)
                for line in POINT_IN_TIME_AUDIT_FILE.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except Exception:
            previous = []
    known = {str(item.get("event_id")) for item in previous}
    for event in new_events:
        if event["event_id"] in known:
            continue
        previous.append({
            "event_id": event["event_id"],
            "available_date": event["available_date"],
            "source": event["source"],
            "record": event,
            "archived_at": _now(),
        })
    lines = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in previous]
    atomic_write_text(POINT_IN_TIME_AUDIT_FILE, "\n".join(lines) + ("\n" if lines else ""))


def archive_observations(stock_data: dict[str, dict], news: Iterable[object]) -> dict:
    """Append verified current observations without rewriting their original dates."""
    archive = _load_archive()
    inserted: list[dict] = []
    # fetch_holding_stock_trends returns a transport envelope (ts/stocks/errors),
    # while backfill callers may pass the stocks mapping directly.
    stock_records = (stock_data or {}).get("stocks", stock_data or {})
    if not isinstance(stock_records, dict):
        stock_records = {}
    for code, stock in stock_records.items():
        if not isinstance(stock, dict):
            continue
        event = _fundamental_event(str(code), (stock or {}).get("fundamentals") or {})
        if not event:
            continue
        existing = list(archive["fundamentals"].get(str(code), []))
        merged, fresh = _merge_events(existing, [event])
        archive["fundamentals"][str(code)] = merged
        inserted.extend(fresh)

    news_events = [event for item in news for event in [_news_event(item)] if event]
    archive["news"], fresh_news = _merge_events(list(archive["news"]), news_events)
    inserted.extend(fresh_news)
    archive["updated_at"] = _now()
    atomic_write_json(POINT_IN_TIME_ARCHIVE_FILE, archive)
    _write_audit(archive, inserted)
    return {
        "fundamental_events": sum(len(items) for items in archive["fundamentals"].values()),
        "news_events": len(archive["news"]),
        "new_events": len(inserted),
        "updated_at": archive["updated_at"],
    }


def archive_sec_history(security_codes: Iterable[str]) -> dict:
    """Backfill US/ADR annual filing events from SEC with actual filing dates."""
    from fetcher import fetch_sec_company_fundamentals_history

    archive = _load_archive()
    inserted: list[dict] = []
    errors = []
    codes = sorted({str(code).strip() for code in security_codes if str(code).strip()})
    for code in codes:
        ticker = SEC_TICKER_OVERRIDES.get(code, code if re.fullmatch(r"[A-Z][A-Z0-9.-]*", code) else "")
        if not ticker:
            continue
        try:
            events = [
                event
                for item in fetch_sec_company_fundamentals_history(code, ticker)
                for event in [_fundamental_event(code, item)]
                if event
            ]
        except Exception as error:
            errors.append({"security_code": code, "source": "sec-companyfacts", "error": str(error)[:240]})
            continue
        existing = list(archive["fundamentals"].get(code, []))
        merged, fresh = _merge_events(existing, events)
        archive["fundamentals"][code] = merged
        inserted.extend(fresh)
    archive["errors"] = [*(archive.get("errors") or []), *errors][-200:]
    archive["backfill_runs"] = [
        *(archive.get("backfill_runs") or []),
        {
            "type": "sec-fundamentals",
            "source": "sec-companyfacts",
            "attempted_securities": len(codes),
            "new_events": len(inserted),
            "errors": errors,
            "recorded_at": _now(),
        },
    ][-100:]
    archive["updated_at"] = _now()
    atomic_write_json(POINT_IN_TIME_ARCHIVE_FILE, archive)
    _write_audit(archive, inserted)
    return {
        "attempted_securities": len(codes),
        "new_events": len(inserted),
        "errors": errors,
        "updated_at": archive["updated_at"],
    }


def archive_historical_ifind_news(
    start_date: str, end_date: str, chunk_days: int = 31
) -> dict:
    """Backfill dated iFinD news in bounded windows when a token is configured."""
    from fetcher import NewsItem, _classify_news_direction, _match_news_rule
    from ifind_client import get_ifind_client, ifind_is_configured

    if not ifind_is_configured():
        return {
            "available": False,
            "new_events": 0,
            "reason": "FUND_MONITOR_IFIND_AUTH_TOKEN is not configured",
        }
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError as error:
        raise ValueError("historical news dates must use YYYY-MM-DD") from error
    if end < start:
        raise ValueError("historical news end date precedes start date")

    queried_items = 0
    new_events = 0
    errors = []
    windows = []
    client = get_ifind_client()
    selected_queries = tuple(
        dict.fromkeys(
            str(query).strip()
            for query in EASTMONEY_FUND_NEWS_QUERIES
            if str(query).strip()
        )
    ) or ("美联储利率、AI、半导体、人民币汇率",)
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=max(1, chunk_days) - 1), end)
        window_events = []
        published_dates = []
        query_errors = []
        for topic_query in selected_queries:
            try:
                rows = client.historical_relevant_news(
                    cursor.isoformat(),
                    window_end.isoformat(),
                    size=100,
                    query=topic_query,
                )
            except Exception as error:
                rows = []
                query_errors.append({
                    "query": topic_query,
                    "error": str(error)[:240],
                })
                continue
            for row in rows:
                title = str(row.get("title") or "").strip()
                summary = str(row.get("summary") or "").strip()
                rule = _match_news_rule(f"{title} {summary}")
                if not title or not rule:
                    continue
                published_at = str(row.get("published_at") or "")
                window_events.append(
                    NewsItem(
                        title=title,
                        source="同花顺iFinD",
                        summary=summary,
                        link=str(row.get("link") or ""),
                        tag=rule["tag"],
                        impact=rule["impact"],
                        affected_funds="、".join(rule["funds"]),
                        direction=_classify_news_direction(f"{title} {summary}"),
                        watch=rule["watch"],
                        priority=rule["priority"],
                        published_at=published_at,
                    )
                )
                if _date(published_at):
                    published_dates.append(_date(published_at))
        window_status = archive_observations({}, window_events) if window_events else {
            "new_events": 0
        }
        queried_items += len(window_events)
        new_events += int(window_status.get("new_events", 0) or 0)
        window_audit = {
            "requested_start": cursor.isoformat(),
            "requested_end": window_end.isoformat(),
            "matched_articles": len(window_events),
            "new_events": int(window_status.get("new_events", 0) or 0),
            "earliest_published_at": min(published_dates) if published_dates else "",
            "latest_published_at": max(published_dates) if published_dates else "",
        }
        if query_errors:
            window_audit["query_errors"] = query_errors
            errors.extend(
                {
                    "source": "ifind-news",
                    "start": cursor.isoformat(),
                    "end": window_end.isoformat(),
                    **item,
                }
                for item in query_errors
            )
        window_audit["queries"] = list(selected_queries)
        windows.append(window_audit)
        archive = _load_archive()
        if query_errors:
            window_errors = [
                {
                    "source": "ifind-news",
                    "start": cursor.isoformat(),
                    "end": window_end.isoformat(),
                    **item,
                }
                for item in query_errors
            ]
            archive["errors"] = [*(archive.get("errors") or []), *window_errors][-200:]
        archive["backfill_runs"] = [
            *(archive.get("backfill_runs") or []),
            {
                "type": "ifind-news-window",
                "source": "ifind-news",
                **window_audit,
                "recorded_at": _now(),
            },
        ][-100:]
        archive["updated_at"] = _now()
        atomic_write_json(POINT_IN_TIME_ARCHIVE_FILE, archive)
        cursor = window_end + timedelta(days=1)
    archive = _load_archive()
    archive["backfill_runs"] = [
        *(archive.get("backfill_runs") or []),
        {
            "type": "ifind-news",
            "source": "ifind-news",
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "queried_items": queried_items,
            "new_events": new_events,
            "errors": errors,
            "windows": windows,
            "recorded_at": _now(),
        },
    ][-100:]
    archive["updated_at"] = _now()
    atomic_write_json(POINT_IN_TIME_ARCHIVE_FILE, archive)
    return {
        "available": bool(queried_items) or not errors,
        "queried_items": queried_items,
        "new_events": new_events,
        "windows": windows,
        "errors": errors,
        "fundamental_events": sum(len(items) for items in archive["fundamentals"].values()),
        "news_events": len(archive["news"]),
        "updated_at": archive["updated_at"],
    }


def archive_historical_public_news(
    start_date: str,
    end_date: str,
    max_pages_per_query: int = 12,
    start_page: int = 1,
) -> dict:
    """Backfill dated Eastmoney fund-impact news when iFinD is unavailable.

    The source endpoint is ordered by time. Every returned article is filtered
    against the requested historical range before entering the immutable audit
    archive, and only titles matching the app's fund-impact rules are kept.
    """
    from fetcher import _fetch_eastmoney_search_news

    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError as error:
        raise ValueError("historical news dates must use YYYY-MM-DD") from error
    if end < start:
        raise ValueError("historical news end date precedes start date")

    first_page = max(1, int(start_page))
    page_count = max(1, int(max_pages_per_query))
    items = []
    errors = []
    reached_start = set()
    query_audit = {}
    for query in EASTMONEY_FUND_NEWS_QUERIES:
        audit = {
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "page_start": first_page,
            "page_end_requested": first_page + page_count - 1,
            "pages_attempted": 0,
            "pages_with_items": 0,
            "articles_returned": 0,
            "articles_in_requested_range": 0,
            "earliest_returned_published_at": "",
            "latest_returned_published_at": "",
            "earliest_archived_published_at": "",
            "latest_archived_published_at": "",
            "stop_reason": "max_pages_reached",
        }
        for page_index in range(first_page, first_page + page_count):
            audit["pages_attempted"] += 1
            try:
                page_items = _fetch_eastmoney_search_news(
                    query, page_index=page_index, page_size=100
                )
            except Exception as error:
                errors.append({
                    "source": "eastmoney-news",
                    "query": query,
                    "page": page_index,
                    "error": str(error)[:240],
                })
                audit["stop_reason"] = "request_error"
                break
            if not page_items:
                audit["stop_reason"] = "empty_page"
                break
            audit["pages_with_items"] += 1
            audit["articles_returned"] += len(page_items)
            dated_items = []
            for item in page_items:
                published = _date(getattr(item, "published_at", ""))
                if not published:
                    continue
                published_date = datetime.strptime(published, "%Y-%m-%d").date()
                audit["earliest_returned_published_at"] = min(
                    audit["earliest_returned_published_at"] or published, published
                )
                audit["latest_returned_published_at"] = max(
                    audit["latest_returned_published_at"] or published, published
                )
                if start <= published_date <= end:
                    dated_items.append(item)
            audit["articles_in_requested_range"] += len(dated_items)
            if dated_items:
                dated_values = [_date(getattr(item, "published_at", "")) for item in dated_items]
                dated_values = [item for item in dated_values if item]
                audit["earliest_archived_published_at"] = min(
                    [audit["earliest_archived_published_at"], *dated_values]
                    if audit["earliest_archived_published_at"] else dated_values
                )
                audit["latest_archived_published_at"] = max(
                    [audit["latest_archived_published_at"], *dated_values]
                    if audit["latest_archived_published_at"] else dated_values
                )
            items.extend(dated_items)
            page_dates = [
                _date(getattr(item, "published_at", ""))
                for item in page_items
                if _date(getattr(item, "published_at", ""))
            ]
            if page_dates and min(page_dates) < start.isoformat():
                reached_start.add(query)
                audit["stop_reason"] = "reached_requested_start"
                break
        query_audit[query] = audit

    status = archive_observations({}, items)
    archive = _load_archive()
    archive["errors"] = [*(archive.get("errors") or []), *errors][-200:]
    archive["backfill_runs"] = [
        *(archive.get("backfill_runs") or []),
        {
            "type": "public-news",
            "source": "eastmoney-news",
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "page_start": first_page,
            "max_pages_per_query": page_count,
            "queries": query_audit,
            "queries_reaching_start": sorted(reached_start),
            "errors": errors,
            "recorded_at": _now(),
        },
    ][-100:]
    archive["updated_at"] = _now()
    atomic_write_json(POINT_IN_TIME_ARCHIVE_FILE, archive)
    return {
        "available": bool(items) or not errors,
        "source": "eastmoney-news",
        "queried_items": len(items),
        "page_start": first_page,
        "max_pages_per_query": page_count,
        "queries_reaching_start": sorted(reached_start),
        "query_audit": query_audit,
        "errors": errors,
        **status,
    }


def archive_historical_gdelt_news(
    start_date: str,
    end_date: str,
    chunk_days: int = 31,
    max_records: int = 250,
    queries: Iterable[str] | None = None,
) -> dict:
    """Backfill date-windowed public news when vendor search windows are exhausted."""
    from fetcher import _fetch_gdelt_historical_news

    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError as error:
        raise ValueError("historical news dates must use YYYY-MM-DD") from error
    if end < start:
        raise ValueError("historical news end date precedes start date")

    selected_queries = tuple(
        dict.fromkeys(
            str(query).strip()
            for query in (queries or EASTMONEY_FUND_NEWS_QUERIES)
            if str(query).strip()
        )
    )
    queried_items = 0
    new_events = 0
    errors = []
    query_audit = {}
    window_size = max(1, int(chunk_days))
    record_limit = max(1, min(int(max_records), 250))
    for query in selected_queries:
        cursor = start
        windows = []
        while cursor <= end:
            window_end = min(cursor + timedelta(days=window_size - 1), end)
            window_error = None
            try:
                window_items = _fetch_gdelt_historical_news(
                    query,
                    cursor.isoformat(),
                    window_end.isoformat(),
                    max_records=record_limit,
                )
            except Exception as error:
                window_error = {
                    "source": "gdelt-news",
                    "query": query,
                    "start": cursor.isoformat(),
                    "end": window_end.isoformat(),
                    "error": str(error)[:240],
                }
                errors.append(window_error)
                window_audit = {
                    "requested_start": cursor.isoformat(),
                    "requested_end": window_end.isoformat(),
                    "error": str(error)[:240],
                }
            else:
                published_dates = sorted(
                    {
                        _date(getattr(item, "published_at", ""))
                        for item in window_items
                        if _date(getattr(item, "published_at", ""))
                    }
                )
                window_status = archive_observations({}, window_items) if window_items else {
                    "new_events": 0
                }
                queried_items += len(window_items)
                new_events += int(window_status.get("new_events", 0) or 0)
                window_audit = {
                    "requested_start": cursor.isoformat(),
                    "requested_end": window_end.isoformat(),
                    "returned_items": len(window_items),
                    "new_events": int(window_status.get("new_events", 0) or 0),
                    "earliest_published_at": published_dates[0] if published_dates else "",
                    "latest_published_at": published_dates[-1] if published_dates else "",
                }
            windows.append(window_audit)
            archive = _load_archive()
            if window_error:
                archive["errors"] = [*(archive.get("errors") or []), window_error][-200:]
            archive["backfill_runs"] = [
                *(archive.get("backfill_runs") or []),
                {
                    "type": "gdelt-news-window",
                    "source": "gdelt-news",
                    "query": query,
                    **window_audit,
                    "recorded_at": _now(),
                },
            ][-100:]
            archive["updated_at"] = _now()
            atomic_write_json(POINT_IN_TIME_ARCHIVE_FILE, archive)
            cursor = window_end + timedelta(days=1)
        query_audit[query] = windows

    archive = _load_archive()
    archive["backfill_runs"] = [
        *(archive.get("backfill_runs") or []),
        {
            "type": "gdelt-news",
            "source": "gdelt-news",
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "chunk_days": window_size,
            "max_records": record_limit,
            "selected_queries": list(selected_queries),
            "queried_items": queried_items,
            "new_events": new_events,
            "queries": query_audit,
            "errors": errors,
            "recorded_at": _now(),
        },
    ][-100:]
    archive["updated_at"] = _now()
    atomic_write_json(POINT_IN_TIME_ARCHIVE_FILE, archive)
    return {
        "available": bool(queried_items) or not errors,
        "source": "gdelt-news",
        "queried_items": queried_items,
        "new_events": new_events,
        "query_audit": query_audit,
        "errors": errors,
        "fundamental_events": sum(
            len(items) for items in archive["fundamentals"].values()
        ),
        "news_events": len(archive["news"]),
        "updated_at": archive["updated_at"],
    }


def _fundamental_coverage_acceptance(fund_coverage: dict | None) -> dict:
    """Return the fund-level coverage gate for a formal point-in-time test."""
    by_fund = fund_coverage if isinstance(fund_coverage, dict) else {}
    covered_funds = sorted(
        code
        for code, item in by_fund.items()
        if isinstance(item, dict) and int(item.get("snapshots_at_or_above_60pct", 0) or 0) > 0
    )
    usable_snapshots = sum(
        int(item.get("snapshots_at_or_above_60pct", 0) or 0)
        for item in by_fund.values()
        if isinstance(item, dict)
    )
    core_status = {
        code: int((by_fund.get(code) or {}).get("snapshots_at_or_above_60pct", 0) or 0)
        for code in FORMAL_CORE_FUND_CODES
    }
    reasons = []
    if len(covered_funds) < FORMAL_MIN_COVERED_FUNDS:
        reasons.append(
            f"基金级点时财报覆盖不足：仅 {len(covered_funds)} 只基金至少有一次60%覆盖，"
            f"正式验收要求至少 {FORMAL_MIN_COVERED_FUNDS} 只"
        )
    if usable_snapshots < FORMAL_MIN_USABLE_FUNDAMENTAL_SNAPSHOTS:
        reasons.append(
            f"基金级有效财报快照不足：{usable_snapshots} 条，正式验收要求至少 "
            f"{FORMAL_MIN_USABLE_FUNDAMENTAL_SNAPSHOTS} 条"
        )
    missing_core = [
        code for code, count in core_status.items() if count < FORMAL_MIN_CORE_SNAPSHOTS
    ]
    if missing_core:
        reasons.append(
            "核心研究基金缺少至少2个60%覆盖的点时财报快照：" + ", ".join(missing_core)
        )
    return {
        "fundamental_coverage_ready": not reasons,
        "funds_with_60pct_fundamental_coverage": len(covered_funds),
        "required_funds_with_60pct_fundamental_coverage": FORMAL_MIN_COVERED_FUNDS,
        "usable_fundamental_snapshots": usable_snapshots,
        "required_usable_fundamental_snapshots": FORMAL_MIN_USABLE_FUNDAMENTAL_SNAPSHOTS,
        "core_fund_snapshots_at_or_above_60pct": core_status,
        "coverage_blocking_reasons": reasons,
    }


def _cached_fund_coverage(archive_updated_at: str) -> dict | None:
    """Use a previously built panel only when it matches the raw archive revision."""
    try:
        panel = json.loads(POINT_IN_TIME_PANEL_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(panel, dict) or panel.get("archive_updated_at") != archive_updated_at:
        return None
    coverage = (panel.get("coverage") or {}).get("by_fund")
    return coverage if isinstance(coverage, dict) else None


def archive_status(fund_coverage: dict | None = None) -> dict:
    archive = _load_archive()
    fundamental_events = sum(len(items) for items in archive["fundamentals"].values())
    fundamental_dates = sorted(
        {
            str(item.get("available_date", ""))
            for items in archive["fundamentals"].values()
            for item in items
            if item.get("available_date")
        }
    )
    mapped_news = [item for item in archive["news"] if item.get("affected_funds")]
    news_dates = sorted(
        {str(item.get("available_date", "")) for item in mapped_news if item.get("available_date")}
    )

    def span_days(values: list[str]) -> int:
        if len(values) < 2:
            return 0
        return (
            datetime.strptime(values[-1], "%Y-%m-%d").date()
            - datetime.strptime(values[0], "%Y-%m-%d").date()
        ).days

    if fund_coverage is None:
        fund_coverage = _cached_fund_coverage(str(archive.get("updated_at") or ""))
    coverage_status = _fundamental_coverage_acceptance(fund_coverage)
    reasons = []
    if fundamental_events < 100 or span_days(fundamental_dates) < 365:
        reasons.append("点时财报事件不足 100 条或覆盖期不足 12 个月")
    if len(mapped_news) < 60 or span_days(news_dates) < 365:
        reasons.append("带发布时间的点时新闻不足 60 条或覆盖期不足 12 个月")
    if fund_coverage is None:
        reasons.append("基金级点时财报覆盖面板未生成或未与原始归档同步")
    reasons.extend(coverage_status["coverage_blocking_reasons"])
    return {
        "fundamental_events": fundamental_events,
        "news_events": len(archive["news"]),
        "mapped_news_events": len(mapped_news),
        "fundamental_span_days": span_days(fundamental_dates),
        "news_span_days": span_days(news_dates),
        **coverage_status,
        "acceptance_ready": not reasons,
        "blocking_reasons": reasons,
        "updated_at": archive.get("updated_at", ""),
    }


def _latest_events_as_of(events: list[dict], as_of: str) -> dict | None:
    available = [event for event in events if str(event.get("available_date", "")) <= as_of]
    return max(available, key=lambda item: str(item.get("available_date", "")), default=None)


def _fund_snapshot(fund_code: str, holding: dict, archive: dict, as_of: str) -> dict | None:
    items = holding.get("holdings", [])[:10]
    total_weight = sum(float(item.get("weight", 0)) for item in items)
    weighted_score = 0.0
    covered_weight = 0.0
    as_of_dates = []
    event_ids = []
    for item in items:
        weight = float(item.get("weight", 0))
        event = _latest_events_as_of(archive["fundamentals"].get(str(item.get("code", "")), []), as_of)
        if not event or float(event.get("metric_coverage", 0)) < 0.50:
            continue
        covered_weight += weight
        weighted_score += weight * float(event["score"])
        as_of_dates.append(str(event["as_of_date"]))
        event_ids.append(str(event["event_id"]))
    if not covered_weight:
        return None
    coverage = covered_weight / total_weight if total_weight else 0.0
    return {
        "available_date": as_of,
        "as_of_date": max(as_of_dates) if as_of_dates else "",
        "score": round(weighted_score / covered_weight, 4),
        "coverage": round(coverage, 4),
        "covered_weight": round(covered_weight, 4),
        "top10_weight": round(total_weight, 4),
        "source": "derived-point-in-time-fundamentals",
        "input_event_ids": sorted(event_ids),
        "fund_code": fund_code,
    }


def build_backtest_panel(holdings_history: dict[str, list[dict]]) -> dict:
    """Derive the fund-level panel only from archived source events."""
    archive = _load_archive()
    fundamentals: dict[str, list[dict]] = {}
    for fund_code, periods in (holdings_history or {}).items():
        ordered = sorted(periods, key=lambda item: str(item.get("effective_date", "")))
        snapshots = []
        for index, period in enumerate(ordered):
            start = _date(period.get("effective_date"))
            end = _date(ordered[index + 1].get("effective_date")) if index + 1 < len(ordered) else "9999-12-31"
            if not start:
                continue
            relevant_dates = {start}
            for item in period.get("holdings", [])[:10]:
                for event in archive["fundamentals"].get(str(item.get("code", "")), []):
                    available = str(event.get("available_date", ""))
                    if start <= available < end:
                        relevant_dates.add(available)
            for snapshot_date in sorted(relevant_dates):
                snapshot = _fund_snapshot(str(fund_code), period, archive, snapshot_date)
                if snapshot:
                    snapshots.append(snapshot)
        fundamentals[str(fund_code)] = snapshots

    news: dict[str, list[dict]] = {}
    for event in archive["news"]:
        available_date = _news_available_date(event)
        if not available_date:
            continue
        for fund_code in _news_affected_funds(event):
            news.setdefault(str(fund_code), []).append({
                "available_date": available_date,
                "published_at": event.get("published_at", ""),
                "score": event["score"],
                "source": event["source"],
                "event_id": event["event_id"],
                "title": event["title"],
            })
    for events in news.values():
        events.sort(key=lambda item: (item["available_date"], item["event_id"]))

    fund_coverage = {
        fund_code: {
            "snapshots": len(snapshots),
            "snapshots_at_or_above_60pct": sum(
                1 for item in snapshots
                if float(item.get("coverage", 0)) >= MIN_FUNDAMENTAL_COVERAGE
            ),
            "max_coverage": round(
                max((float(item.get("coverage", 0)) for item in snapshots), default=0.0), 4
            ),
        }
        for fund_code, snapshots in fundamentals.items()
    }
    funds_below_60pct = sorted(
        fund_code
        for fund_code, item in fund_coverage.items()
        if item["snapshots_at_or_above_60pct"] == 0
    )
    news_funds_without_events = sorted(
        fund_code for fund_code in fundamentals if not news.get(fund_code)
    )

    fundamental_count = sum(len(items) for items in fundamentals.values())
    usable_fundamental_count = sum(
        1 for items in fundamentals.values() for item in items
        if float(item.get("coverage", 0)) >= MIN_FUNDAMENTAL_COVERAGE
    )
    panel = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "generated_at": _now(),
        "archive_updated_at": archive.get("updated_at", ""),
        "fundamentals": fundamentals,
        "news": news,
        "coverage": {
            "fundamental_records": fundamental_count,
            "fundamental_records_at_or_above_60pct": usable_fundamental_count,
            "news_records": sum(len(items) for items in news.values()),
            "news_lookback_days": NEWS_LOOKBACK_DAYS,
            "funds_with_60pct_fundamental_coverage": sum(
                1 for item in fund_coverage.values()
                if item["snapshots_at_or_above_60pct"] > 0
            ),
            "funds_below_60pct_fundamental_coverage": funds_below_60pct,
            "funds_without_mapped_news": news_funds_without_events,
            "by_fund": fund_coverage,
        },
        "acceptance": archive_status(fund_coverage),
        "audit_rule": "Only source events with available_date <= signal_date may be selected.",
        "news_mapping_rule": (
            "新闻面板使用来源原始affected_funds与POINT_IN_TIME_NEWS_FUND_GROUPS的并集；"
            "不改变不可变新闻事件及其原始发布时间。"
        ),
    }
    atomic_write_json(POINT_IN_TIME_PANEL_FILE, panel)
    return panel


def archived_security_codes() -> list[str]:
    archive = _load_archive()
    return sorted(archive["fundamentals"].keys())


def load_historical_holdings() -> dict[str, list[dict]]:
    """Load the historical disclosure cache only when its full JSON is valid."""
    holdings_file = POINT_IN_TIME_ARCHIVE_FILE.parent / "backtest_holdings_history.json"
    try:
        payload = json.loads(holdings_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"历史持仓缓存不可用：{error}") from error
    if not isinstance(payload, dict):
        raise RuntimeError("历史持仓缓存结构不是对象")
    return {
        str(code): [item for item in periods if isinstance(item, dict)]
        for code, periods in payload.items()
        if isinstance(periods, list)
    }


def historical_holding_security_codes(holdings_history: dict[str, list[dict]]) -> list[str]:
    """Return unique top-ten securities from valid historical fund disclosures."""
    return sorted(
        {
            str(item.get("code") or "").strip()
            for periods in holdings_history.values()
            for period in periods
            if isinstance(period, dict)
            for item in (period.get("holdings") or [])[:10]
            if isinstance(item, dict) and str(item.get("code") or "").strip()
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="点时财报与新闻审计归档")
    parser.add_argument("--backfill-sec", action="store_true", help="回填历史重仓股的 SEC 披露事件")
    parser.add_argument("--sec-limit", type=int, default=0, help="限制 SEC 回填证券数；0 表示全部")
    parser.add_argument("--backfill-ifind", action="store_true", help="回填 iFinD 历史新闻（需独立程序令牌）")
    parser.add_argument("--backfill-public-news", action="store_true", help="回填东方财富历史新闻作为 iFinD 的公开备用源")
    parser.add_argument(
        "--backfill-gdelt-news",
        action="store_true",
        help="按日期窗口回填带发布时间的公开历史新闻",
    )
    parser.add_argument("--news-start", default="2024-01-01", help="iFinD 新闻回填起日 YYYY-MM-DD")
    parser.add_argument("--news-end", default=date.today().isoformat(), help="iFinD 新闻回填止日 YYYY-MM-DD")
    parser.add_argument("--news-pages", type=int, default=12, help="每个公开新闻关键词最多回溯页数")
    parser.add_argument(
        "--news-page-start",
        type=int,
        default=1,
        help="公开新闻回填起始页，用于按批续传历史页面",
    )
    parser.add_argument("--news-chunk-days", type=int, default=31, help="按日期回填新闻的单个窗口天数")
    parser.add_argument("--news-max-records", type=int, default=250, help="每个关键词窗口最多归档新闻数")
    parser.add_argument(
        "--news-query",
        action="append",
        default=[],
        help="仅回填指定新闻主题；可重复传入，空值表示全部主题",
    )
    parser.add_argument("--build-panel", action="store_true", help="从原始归档重建基金级点时面板")
    parser.add_argument("--status", action="store_true", help="仅显示正式验收就绪状态")
    args = parser.parse_args()

    result: dict[str, object] = {}
    holdings_history: dict[str, list[dict]] | None = None
    if args.backfill_sec or args.build_panel:
        holdings_history = load_historical_holdings()
    if args.backfill_sec:
        codes = historical_holding_security_codes(holdings_history or {})
        if args.sec_limit > 0:
            codes = codes[: args.sec_limit]
        result["sec_backfill"] = archive_sec_history(codes)
    if args.backfill_ifind:
        result["ifind_news_backfill"] = archive_historical_ifind_news(
            args.news_start, args.news_end
        )
    if args.backfill_public_news:
        result["public_news_backfill"] = archive_historical_public_news(
            args.news_start,
            args.news_end,
            args.news_pages,
            args.news_page_start,
        )
    if args.backfill_gdelt_news:
        result["gdelt_news_backfill"] = archive_historical_gdelt_news(
            args.news_start,
            args.news_end,
            args.news_chunk_days,
            args.news_max_records,
            args.news_query or None,
        )
    if (
        args.build_panel
        or args.backfill_sec
        or args.backfill_ifind
        or args.backfill_public_news
        or args.backfill_gdelt_news
    ):
        panel = build_backtest_panel(holdings_history or load_historical_holdings())
        result["panel"] = panel.get("coverage", {})
    result["status"] = archive_status()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
