"""Auditable portfolio ledger and NAV-based daily valuation snapshots."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from config import (
    PORTFOLIO_HOLDINGS,
    PORTFOLIO_LEDGER_FILE,
    PORTFOLIO_SNAPSHOTS_FILE,
)
from storage import atomic_write_json


LEDGER_VERSION = 1
VALID_TRANSACTION_TYPES = {"BUY", "SELL", "DIVIDEND"}


class PortfolioLedgerError(ValueError):
    """Raised when a supplied transaction cannot be safely recorded."""


def refresh_portfolio_ledger(
    quotes: dict[str, object],
    portfolio: dict[str, dict] | None = None,
    ledger_path: Path | None = None,
    snapshots_path: Path | None = None,
    snapshot_date: date | None = None,
) -> dict[str, Any]:
    """Value the ledger with the latest official NAVs and persist one daily snapshot."""
    portfolio = portfolio or PORTFOLIO_HOLDINGS
    ledger_path = ledger_path or PORTFOLIO_LEDGER_FILE
    snapshots_path = snapshots_path or PORTFOLIO_SNAPSHOTS_FILE
    ledger, ledger_status = load_or_initialize_ledger(portfolio, ledger_path)
    snapshots, snapshot_status = _load_snapshots(snapshots_path)
    valuation_day = snapshot_date or date.today()
    previous = _latest_snapshot_before(snapshots, valuation_day.isoformat())
    positions = calculate_positions(ledger, quotes, previous)
    snapshot = {
        "snapshot_date": valuation_day.isoformat(),
        "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "positions": positions,
        "total_market_value": round(
            sum(
                float(item.get("market_value", 0) or 0)
                for item in positions.values()
            ),
            2,
        ),
    }
    if snapshot_status == "OK":
        snapshots = [
            item for item in snapshots if item.get("snapshot_date") != snapshot["snapshot_date"]
        ]
        snapshots.append(snapshot)
        snapshots.sort(key=lambda item: str(item.get("snapshot_date", "")))
        atomic_write_json(snapshots_path, {"version": 1, "snapshots": snapshots})
    return {
        "ledger": ledger,
        "ledger_status": ledger_status,
        "positions": positions,
        "snapshot": snapshot,
        "snapshot_status": snapshot_status,
        "snapshot_count": len(snapshots),
    }


def load_or_initialize_ledger(
    portfolio: dict[str, dict] | None = None,
    ledger_path: Path | None = None,
) -> tuple[dict[str, Any], str]:
    """Load transactions or create a non-destructive opening balance once."""
    portfolio = portfolio or PORTFOLIO_HOLDINGS
    ledger_path = ledger_path or PORTFOLIO_LEDGER_FILE
    if ledger_path.exists():
        try:
            payload = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger = _validate_ledger(payload)
        except (OSError, json.JSONDecodeError, PortfolioLedgerError) as error:
            return _opening_ledger(portfolio), f"READ_ERROR: {error}"
        additions = _opening_transactions_for_missing_codes(ledger, portfolio)
        if additions:
            ledger["transactions"].extend(additions)
            atomic_write_json(ledger_path, ledger)
        return ledger, "OK"

    ledger = _opening_ledger(portfolio)
    atomic_write_json(ledger_path, ledger)
    return ledger, "INITIALIZED"


def append_transaction(
    transaction: dict[str, Any],
    ledger_path: Path | None = None,
) -> dict[str, Any]:
    """Append one validated transaction without editing historical entries."""
    ledger_path = ledger_path or PORTFOLIO_LEDGER_FILE
    ledger, status = load_or_initialize_ledger(ledger_path=ledger_path)
    if status.startswith("READ_ERROR"):
        raise PortfolioLedgerError("ledger is unreadable; historical data was not changed")
    normalized = _normalize_transaction(transaction, len(ledger["transactions"]))
    ledger["transactions"].append(normalized)
    atomic_write_json(ledger_path, ledger)
    return normalized


def calculate_positions(
    ledger: dict[str, Any],
    quotes: dict[str, object],
    previous_snapshot: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Calculate moving-cost positions, realized P/L, and NAV-based valuation."""
    states: dict[str, dict[str, float]] = {}
    for raw in ledger.get("transactions", []):
        item = _normalize_transaction(raw, 0)
        code = item["code"]
        state = states.setdefault(
            code,
            {
                "shares": 0.0,
                "cost_basis": 0.0,
                "gross_buy_amount": 0.0,
                "gross_sold_amount": 0.0,
                "dividend_cash": 0.0,
                "realized_pnl": 0.0,
            },
        )
        if item["type"] == "BUY":
            state["shares"] += item["shares"]
            state["cost_basis"] += item["amount"]
            state["gross_buy_amount"] += item["amount"]
        elif item["type"] == "SELL":
            if item["shares"] > state["shares"] + 1e-8:
                raise PortfolioLedgerError(f"{code} sell shares exceed available shares")
            average_cost = state["cost_basis"] / state["shares"] if state["shares"] else 0.0
            cost_removed = average_cost * item["shares"]
            state["shares"] -= item["shares"]
            state["cost_basis"] = max(0.0, state["cost_basis"] - cost_removed)
            state["gross_sold_amount"] += item["amount"]
            state["realized_pnl"] += item["amount"] - cost_removed
        else:
            state["dividend_cash"] += item["amount"]

    previous_positions = (previous_snapshot or {}).get("positions", {})
    results: dict[str, dict[str, Any]] = {}
    for code, state in states.items():
        quote = quotes.get(code)
        nav = _quote_value(quote, "nav")
        nav_date = str(_quote_value(quote, "date") or "--")
        nav_source = str(_quote_value(quote, "source") or "--")
        shares = round(max(state["shares"], 0.0), 6)
        cost_basis = round(max(state["cost_basis"], 0.0), 2)
        cost_nav = cost_basis / shares if shares else None
        market_value = nav * shares if nav is not None and shares else None
        unrealized_pnl = market_value - cost_basis if market_value is not None else None
        unrealized_pnl_pct = unrealized_pnl / cost_basis if unrealized_pnl is not None and cost_basis else None
        total_pnl = (
            market_value + state["gross_sold_amount"] + state["dividend_cash"] - state["gross_buy_amount"]
            if market_value is not None
            else None
        )
        previous = previous_positions.get(code, {})
        previous_value = _number(previous.get("market_value"))
        previous_shares = _number(previous.get("shares"))
        daily_pnl = (
            market_value - previous_value
            if market_value is not None
            and previous_value is not None
            and previous_shares is not None
            and abs(previous_shares - shares) < 1e-6
            else None
        )
        results[code] = {
            "code": code,
            "shares": shares,
            "gross_buy_amount": round(state["gross_buy_amount"], 2),
            "gross_sold_amount": round(state["gross_sold_amount"], 2),
            "dividend_cash": round(state["dividend_cash"], 2),
            "cost_basis": cost_basis,
            "cost_nav": round(cost_nav, 6) if cost_nav is not None else None,
            "current_nav": round(nav, 6) if nav is not None else None,
            "nav_date": nav_date,
            "nav_source": nav_source,
            "market_value": round(market_value, 2) if market_value is not None else None,
            "unrealized_pnl": round(unrealized_pnl, 2) if unrealized_pnl is not None else None,
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 6) if unrealized_pnl_pct is not None else None,
            "realized_pnl": round(state["realized_pnl"], 2),
            "total_pnl": round(total_pnl, 2) if total_pnl is not None else None,
            "daily_pnl": round(daily_pnl, 2) if daily_pnl is not None else None,
        }
    return results


def _opening_ledger(portfolio: dict[str, dict]) -> dict[str, Any]:
    return {
        "version": LEDGER_VERSION,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "transactions": _opening_transactions_for_missing_codes({"transactions": []}, portfolio),
    }


def _opening_transactions_for_missing_codes(
    ledger: dict[str, Any], portfolio: dict[str, dict]
) -> list[dict[str, Any]]:
    existing = {str(item.get("code")) for item in ledger.get("transactions", [])}
    additions = []
    for code, item in portfolio.items():
        if code in existing:
            continue
        shares = _number(item.get("shares"))
        amount = _number(item.get("invested_amount"))
        if shares is None or amount is None or shares <= 0 or amount < 0:
            continue
        additions.append(
            {
                "id": f"opening-{code}",
                "date": item.get("purchase_date"),
                "type": "BUY",
                "code": str(code),
                "shares": round(shares, 6),
                "amount": round(amount, 2),
                "note": "Opening balance migrated from PORTFOLIO_HOLDINGS.",
            }
        )
    return additions


def _validate_ledger(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("version") != LEDGER_VERSION:
        raise PortfolioLedgerError("unsupported ledger format")
    transactions = payload.get("transactions")
    if not isinstance(transactions, list):
        raise PortfolioLedgerError("transactions must be a list")
    return {
        "version": LEDGER_VERSION,
        "created_at": str(payload.get("created_at") or "--"),
        "transactions": [
            _normalize_transaction(item, index) for index, item in enumerate(transactions)
        ],
    }


def _normalize_transaction(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise PortfolioLedgerError("transaction must be an object")
    kind = str(item.get("type") or "").upper()
    code = str(item.get("code") or "").strip()
    shares = _number(item.get("shares", 0 if kind == "DIVIDEND" else None))
    amount = _number(item.get("amount"))
    if kind not in VALID_TRANSACTION_TYPES or not code:
        raise PortfolioLedgerError("transaction type or fund code is invalid")
    if shares is None or amount is None or shares < 0 or amount < 0:
        raise PortfolioLedgerError("transaction shares and amount must be non-negative")
    if kind in {"BUY", "SELL"} and shares <= 0:
        raise PortfolioLedgerError("buy and sell transactions require shares")
    return {
        "id": str(item.get("id") or f"transaction-{index + 1}"),
        "date": item.get("date"),
        "type": kind,
        "code": code,
        "shares": round(shares, 6),
        "amount": round(amount, 2),
        "note": str(item.get("note") or ""),
    }


def _load_snapshots(path: Path) -> tuple[list[dict[str, Any]], str]:
    if not path.exists():
        return [], "OK"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        snapshots = payload.get("snapshots", [])
        if not isinstance(snapshots, list):
            raise PortfolioLedgerError("snapshots must be a list")
        return [item for item in snapshots if isinstance(item, dict)], "OK"
    except (OSError, json.JSONDecodeError, PortfolioLedgerError) as error:
        return [], f"READ_ERROR: {error}"


def _latest_snapshot_before(snapshots: list[dict[str, Any]], value: str) -> dict[str, Any] | None:
    previous = [item for item in snapshots if str(item.get("snapshot_date", "")) < value]
    return max(previous, key=lambda item: str(item.get("snapshot_date", ""))) if previous else None


def _quote_value(quote: object | None, field: str) -> Any:
    if quote is None:
        return None
    value = quote.get(field) if isinstance(quote, dict) else getattr(quote, field, None)
    return _number(value) if field == "nav" else value


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None
