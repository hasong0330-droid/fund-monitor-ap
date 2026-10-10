"""Persistent user-managed portfolio inputs.

The generated dashboard is served by the local app, so portfolio edits must be
written to the project cache rather than browser-only storage.  The file keeps
only active user inputs; derived valuation and alert state are regenerated.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from config import (
    PORTFOLIO_HOLDINGS,
    HOLDING_ALERT_STATE_FILE,
    PORTFOLIO_LEDGER_FILE,
    PORTFOLIO_POSITIONS_FILE,
    PORTFOLIO_SNAPSHOTS_FILE,
)
from storage import atomic_write_json


PORTFOLIO_CONFIG_VERSION = 1


def _empty_payload() -> dict[str, Any]:
    return {"version": PORTFOLIO_CONFIG_VERSION, "positions": {}, "disabled_codes": []}


def load_portfolio_config(path: Path | None = None) -> dict[str, Any]:
    path = path or PORTFOLIO_POSITIONS_FILE
    if not path.exists():
        return _empty_payload()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_payload()
    if not isinstance(payload, dict):
        return _empty_payload()
    positions = payload.get("positions") if isinstance(payload.get("positions"), dict) else {}
    disabled = payload.get("disabled_codes") if isinstance(payload.get("disabled_codes"), list) else []
    return {
        "version": PORTFOLIO_CONFIG_VERSION,
        "positions": positions,
        "disabled_codes": sorted({str(code) for code in disabled if re.fullmatch(r"\d{6}", str(code))}),
    }


def _validate_position(code: str, market_value: object, pnl: object, purchase_date: object) -> dict[str, Any]:
    code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("基金代码必须是6位数字")
    try:
        market_value = round(float(market_value), 2)
        pnl = round(float(pnl), 2)
    except (TypeError, ValueError) as error:
        raise ValueError("当前市值和当前盈亏必须是数字") from error
    if market_value <= 0:
        raise ValueError("当前市值必须大于0")
    cost_basis = round(market_value - pnl, 2)
    if cost_basis <= 0:
        raise ValueError("当前市值减去当前盈亏后必须得到正的累计成本")
    purchase_date = str(purchase_date or "").strip()
    try:
        date.fromisoformat(purchase_date)
    except ValueError as error:
        raise ValueError("买入日期必须是 YYYY-MM-DD") from error
    return {
        "market_value": market_value,
        "pnl": pnl,
        "purchase_date": purchase_date,
        "cost_basis": cost_basis,
    }


def upsert_portfolio_position(
    code: str,
    market_value: object,
    pnl: object,
    purchase_date: object,
    path: Path | None = None,
) -> dict[str, Any]:
    path = path or PORTFOLIO_POSITIONS_FILE
    payload = load_portfolio_config(path)
    position = _validate_position(code, market_value, pnl, purchase_date)
    code = str(code).strip()
    payload["positions"][code] = position
    payload["disabled_codes"] = [item for item in payload["disabled_codes"] if item != code]
    atomic_write_json(path, payload)
    return position


def _purge_code_from_json(path: Path, code: str, key: str = "") -> None:
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    changed = False
    if key == "snapshots" and isinstance(payload, dict):
        snapshots = payload.get("snapshots")
        if isinstance(snapshots, list):
            for snapshot in snapshots:
                if isinstance(snapshot, dict) and isinstance(snapshot.get("positions"), dict):
                    if code in snapshot["positions"]:
                        snapshot["positions"].pop(code, None)
                        changed = True
    elif isinstance(payload, dict) and isinstance(payload.get("transactions"), list):
        original = len(payload["transactions"])
        payload["transactions"] = [item for item in payload["transactions"] if str(item.get("code")) != code]
        changed = len(payload["transactions"]) != original
    elif isinstance(payload, dict) and code in payload:
        payload.pop(code, None)
        changed = True
    if changed:
        atomic_write_json(path, payload)


def delete_portfolio_position(code: str, path: Path | None = None) -> None:
    path = path or PORTFOLIO_POSITIONS_FILE
    code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("基金代码必须是6位数字")
    payload = load_portfolio_config(path)
    payload["positions"].pop(code, None)
    payload["disabled_codes"] = sorted(set(payload["disabled_codes"]) | {code})
    atomic_write_json(path, payload)
    _purge_code_from_json(PORTFOLIO_LEDGER_FILE, code)
    _purge_code_from_json(PORTFOLIO_SNAPSHOTS_FILE, code, "snapshots")
    _purge_code_from_json(HOLDING_ALERT_STATE_FILE, code)


def load_active_portfolio() -> dict[str, dict[str, Any]]:
    """Merge user inputs over legacy defaults without mutating config constants."""
    payload = load_portfolio_config()
    active = copy.deepcopy(PORTFOLIO_HOLDINGS)
    for code in payload["disabled_codes"]:
        active.pop(code, None)
    for raw_code, raw_item in payload["positions"].items():
        code = str(raw_code)
        if not re.fullmatch(r"\d{6}", code) or not isinstance(raw_item, dict):
            continue
        try:
            item = _validate_position(code, raw_item.get("market_value"), raw_item.get("pnl"), raw_item.get("purchase_date"))
        except ValueError:
            continue
        merged = copy.deepcopy(active.get(code, {}))
        merged.update(item)
        merged["market_value_input"] = item["market_value"]
        merged["pnl_input"] = item["pnl"]
        merged["valuation_mode"] = "nav"
        # Shares are filled after the latest quote is available.
        merged.setdefault("shares", 0.0)
        active[code] = merged
    return active


def apply_quote_based_shares(portfolio: dict[str, dict[str, Any]], quotes: dict[str, object]) -> None:
    for code, item in portfolio.items():
        market_value = item.get("market_value_input")
        if market_value is None:
            continue
        quote = quotes.get(code)
        nav = getattr(quote, "nav", None)
        try:
            if float(nav) > 0:
                item["shares"] = round(float(market_value) / float(nav), 6)
        except (TypeError, ValueError):
            continue
