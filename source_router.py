"""Shared source-policy helpers for fetch fallbacks and audit evidence."""

from __future__ import annotations

from typing import Callable, Iterable, Mapping, TypeVar

from config import DATA_SOURCE_POLICY


T = TypeVar("T")


# Different endpoints from one vendor are useful fallbacks but not independent
# corroboration. Keep this mapping in one place for every decision module.
SOURCE_ALIASES = {
    "eastmoney-fund-quote": "eastmoney",
    "eastmoney-js": "eastmoney-pingzhongdata",
    "eastmoney-index": "eastmoney-index",
    "eastmoney-stock": "eastmoney-stock",
    "eastmoney-news": "eastmoney-news",
    "ifind-fund-nav": "ifind-fund-nav",
    "gdelt-news": "gdelt-news",
    "sina-news": "sina-news",
    "investoday-news": "investoday-news",
    "sina-forex": "sina-forex",
    "sina-stock": "sina-stock",
    "tencent-index": "tencent-index",
    "tencent-stock": "tencent-stock",
    "sec-companyfacts": "sec-companyfacts",
    "ny-fed": "ny-fed",
}

SOURCE_GROUPS = {
    "fund-manager": "fund_manager",
    "fund-manager-history": "fund_manager",
    "fund-manager-report": "fund_manager",
    "browser-fund-page": "browser",
    "browser-fund-history": "browser",
    "eastmoney": "eastmoney",
    "eastmoney-history": "eastmoney",
    "eastmoney-pingzhongdata": "eastmoney",
    "eastmoney-holdings": "eastmoney",
    "eastmoney-asset-allocation": "eastmoney",
    "eastmoney-index": "eastmoney",
    "eastmoney-stock": "eastmoney",
    "eastmoney-financials": "eastmoney",
    "eastmoney-qualification": "eastmoney",
    "eastmoney-news": "eastmoney",
    "gdelt-news": "gdelt",
    "sina-holdings": "sina",
    "sina-profile": "sina",
    "sina-stock": "sina",
    "sina-credit-etf": "sina",
    "sina-forex": "sina",
    "sina-news": "sina",
    "ths-holdings": "ths",
    "tencent-index": "tencent",
    "tencent-stock": "tencent",
    "yahoo": "yahoo",
    "yahoo-fundamentals": "yahoo",
    "sec-companyfacts": "sec",
    "bea": "bea",
    "bls": "bls",
    "fred": "fred",
    "ny-fed": "ny_fed",
    "us-treasury": "us_treasury",
    "cboe": "cboe",
    "tradingeconomics": "tradingeconomics",
    "naver-financials": "naver",
    "hkex-filings": "hkex",
    "cninfo": "cninfo",
    "dart": "dart",
    "ifind-edb": "ifind",
    "ifind-fund-nav": "ifind",
    "ifind-financials": "ifind",
    "ifind-news": "ifind",
    "investoday-news": "investoday",
    "official-index": "official_index",
    "licensed-index": "licensed_index",
    "licensed-news": "licensed_news",
    "cache": "cache",
    "fallback": "fallback",
    "baseline": "baseline",
}

RESEARCH_REFERENCE_SOURCES = frozenset(
    {"yahoo", "yahoo-stock", "yahoo-fundamentals"}
)
NON_TRADING_SOURCES = frozenset(
    {"cache", "fallback", "baseline", *RESEARCH_REFERENCE_SOURCES}
)

# One source registry is shared by every decision module. The source policy in
# config.py controls order; this registry controls whether returned data may be
# treated as live and which data categories it can support.
LIVE_SOURCE_CATEGORIES = {
    "fund_nav": {
        "fund-manager", "browser-fund-page", "eastmoney", "eastmoney-js", "eastmoney-pingzhongdata", "eastmoney-history",
        "ifind-fund-nav",
    },
    "fund_history": {
        "fund-manager-history", "browser-fund-history", "eastmoney-history", "eastmoney-pingzhongdata",
    },
    "indexes": {"eastmoney-index", "tencent-index"},
    "macro": {
        "fred", "bls", "world-bank", "bea", "ny-fed", "tradingeconomics",
        "sina-credit-etf", "eastmoney-macro", "us-treasury", "cboe",
        "sina-forex", "ifind-edb",
    },
    "fundamentals": {
        "sec-companyfacts", "eastmoney-financials", "naver-financials",
        "ifind-financials",
    },
    "holdings": {"eastmoney-holdings", "eastmoney-asset-allocation", "sina-holdings", "ths-holdings"},
    "stock_trends": {
        "eastmoney-stock", "sina-stock", "tencent-stock",
    },
    "news": {"finance-news", "eastmoney-news", "sina-news", "ifind-news", "investoday-news", "gdelt-news"},
    "recommendation_qualification": {
        "eastmoney-qualification", "multi-source-qualification",
    },
}


def live_source_names(category: str | None = None) -> frozenset[str]:
    """Return canonical live sources, optionally limited to one data category."""
    if category is not None:
        return frozenset(LIVE_SOURCE_CATEGORIES.get(category, set()))
    return frozenset(
        source
        for sources in LIVE_SOURCE_CATEGORIES.values()
        for source in sources
    )


def is_live_source(source: str, category: str | None = None) -> bool:
    canonical = canonical_source(source)
    return canonical in live_source_names(category)


def source_registry_snapshot() -> dict[str, list[str]]:
    return {
        category: sorted(sources)
        for category, sources in LIVE_SOURCE_CATEGORIES.items()
    }


def canonical_source(source: str) -> str:
    """Return the policy key for a source label emitted by a fetcher."""
    value = str(source or "").strip().lower()
    return SOURCE_ALIASES.get(value, value)


def source_group(source: str) -> str:
    """Return a vendor group used for independent-source confirmation."""
    canonical = canonical_source(source)
    return SOURCE_GROUPS.get(canonical, canonical or "unknown")


def sources_are_independent(sources: Iterable[str]) -> bool:
    groups = {
        source_group(source)
        for source in sources
        if canonical_source(source) not in NON_TRADING_SOURCES
    }
    return len(groups) >= 2


def is_trading_eligible_source(source: str) -> bool:
    return canonical_source(source) not in NON_TRADING_SOURCES


def is_research_reference_source(source: str) -> bool:
    return canonical_source(source) in RESEARCH_REFERENCE_SOURCES


def source_policy_snapshot() -> dict[str, list[str]]:
    return {
        category: list(policy["order"])
        for category, policy in DATA_SOURCE_POLICY.items()
    }


def ordered_source_handlers(
    category: str,
    handlers: Mapping[str, Callable[[], T]],
) -> list[tuple[str, Callable[[], T]]]:
    """Order available handlers by the registered category policy.

    Policy entries without an installed handler are intentionally skipped. This
    lets optional licensed or fund-manager integrations be configured later
    without changing the fallback behavior of public sources today.
    """
    policy = DATA_SOURCE_POLICY.get(category, {})
    preferred = tuple(policy.get("order", ()))
    remaining = dict(handlers)
    ordered: list[tuple[str, Callable[[], T]]] = []

    for expected in preferred:
        for source, handler in tuple(remaining.items()):
            if canonical_source(source) == expected:
                ordered.append((source, handler))
                remaining.pop(source)
                break

    # Preserve explicitly supplied fallbacks that have not yet been registered.
    ordered.extend(remaining.items())
    return ordered


def source_provenance(category: str, source: str) -> dict[str, object]:
    canonical = canonical_source(source)
    policy = DATA_SOURCE_POLICY.get(category, {})
    order = tuple(policy.get("order", ()))
    try:
        priority = order.index(canonical) + 1
    except ValueError:
        priority = None
    return {
        "category": category,
        "canonical_source": canonical,
        "provider_group": source_group(canonical),
        "priority": priority,
        "trading_eligible": is_trading_eligible_source(canonical),
        "research_reference": is_research_reference_source(canonical),
    }
