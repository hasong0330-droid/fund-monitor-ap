from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from config import (
    CACHE_DIR,
    HOLDING_ALERT_CALIBRATION_FILE,
    PORTFOLIO_HOLDINGS,
    REPORTS_DIR,
    TARGET_FUNDS,
    TARGET_FUND_NAMES,
)
from factor_model import FUNDAMENTAL_PROFILE_BY_FUND
from holding_alert import (
    MA120_ALERT_BUFFER,
    MA120_IMMEDIATE_BUFFER,
    MA60_ALERT_BUFFER,
    PORTFOLIO_RISK_WEIGHT,
    _history_metrics,
    _history_correlation,
    _is_semiconductor_holding,
    _tail_risk,
    _trend_risk,
)


BASE_DIR = Path(__file__).resolve().parent
SAMPLE_FILE = BASE_DIR / "backtest_samples" / "fund_nav.csv"
CURRENT_HISTORY_FILE = CACHE_DIR / "fund_history.json"
RESULT_FILE = REPORTS_DIR / "holding_alert_calibration.json"
REPORT_FILE = REPORTS_DIR / "holding_alert_calibration.txt"
PORTFOLIO_POINT_IN_TIME_FILE = CACHE_DIR / "portfolio_risk_point_in_time.json"
PEER_HISTORY_FILE = CACHE_DIR / "holding_alert_peer_histories.json"
FEATURES = (
    "tail_risk",
    "trend_risk",
    "drawdown_63",
    "downside_volatility_63",
    "cvar_95_63",
    "ma60_below",
    "ma120_below",
)
HORIZONS = {"20": (20, -0.08), "63": (63, -0.12)}
MODEL_VERSION = "Fund Guard v2.0 Fundamental-Led Five-Factor"
REQUIRED_PROFILES = ("technology", "resource", "balanced", "fof")
PROFILE_LABELS = {
    "technology": "科技成长",
    "resource": "资源周期",
    "balanced": "均衡配置",
    "fof": "FOF资产配置",
}
MIN_PROFILE_FUNDS = 2
MIN_PROFILE_TEST_EVENTS = 5
PORTFOLIO_DISCLOSURE_LAG_DAYS = 45
# The event base rate is about 10%. A high-precision observation alert with
# lower recall is useful; forcing 50% recall was what previously selected an
# always-on threshold. Production approval still requires the stricter gates.
MIN_ALERT_RECALL = 0.10
MIN_ALERT_PRECISION = 0.25
MAX_ALERT_FALSE_POSITIVE_RATE = 0.30
NO_USABLE_THRESHOLD = 0.99


def main() -> None:
    frozen_histories = _load_histories(SAMPLE_FILE)
    current_histories = _load_cached_histories(CURRENT_HISTORY_FILE)
    peer_histories = _load_cached_histories(PEER_HISTORY_FILE)
    histories = _merge_histories(
        _merge_histories(frozen_histories, current_histories), peer_histories
    )
    result = {
        "model_version": MODEL_VERSION,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sample_file": str(SAMPLE_FILE),
        "sample_sources": [
            {
                "path": str(SAMPLE_FILE),
                "type": "frozen_csv",
                "fund_count": len(frozen_histories),
            },
            {
                "path": str(CURRENT_HISTORY_FILE),
                "type": "official_history_cache",
                "fund_count": len(current_histories),
            },
            {
                "path": str(PEER_HISTORY_FILE),
                "type": "audited_peer_history_cache",
                "fund_count": len(peer_histories),
            },
        ],
        "fund_count": len(histories),
        "scope_coverage": {
            "target_funds": _history_scope_coverage(histories, TARGET_FUNDS),
            "portfolio_holdings": _history_scope_coverage(
                histories, list(PORTFOLIO_HOLDINGS)
            ),
        },
        "features": list(FEATURES),
        "horizons": {},
    }
    for key, (horizon, threshold) in HORIZONS.items():
        rows = _build_samples(histories, horizon, threshold)
        result["horizons"][key] = _calibrate_horizon(rows, horizon, threshold)

    profile_validation = _profile_validation(result["horizons"])
    trigger_validation = _trigger_validation(result["horizons"])
    portfolio_validation = _load_portfolio_point_in_time(
        PORTFOLIO_POINT_IN_TIME_FILE,
        signal_date=date.today(),
        histories=histories,
    )
    portfolio_validation["weight"] = PORTFOLIO_RISK_WEIGHT
    acceptance = _production_acceptance(
        result["horizons"],
        profile_validation,
        trigger_validation,
        portfolio_validation,
    )
    result["profile_validation"] = profile_validation
    result["trigger_buffer_validation"] = trigger_validation
    result["portfolio_overlay_validation"] = portfolio_validation
    result["acceptance"] = acceptance
    result["production_approved"] = acceptance["passed"]
    result["status"] = (
        "通过第三阶段生产验收"
        if acceptance["passed"]
        else "未通过第三阶段生产验收，仅供观察"
    )
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    RESULT_FILE.write_text(text, encoding="utf-8")
    HOLDING_ALERT_CALIBRATION_FILE.write_text(text, encoding="utf-8")
    REPORT_FILE.write_text(_format_report(result), encoding="utf-8")
    print(_format_report(result))


def _load_histories(path: Path) -> dict[str, list[dict]]:
    histories: dict[str, list[dict]] = defaultdict(list)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            histories[row["code"]].append(
                {
                    "date": row["date"],
                    "nav": float(row["nav"]),
                    "name": row.get("name", ""),
                }
            )
    return {code: sorted(rows, key=lambda item: item["date"]) for code, rows in histories.items()}


def _load_cached_histories(path: Path) -> dict[str, list[dict]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    histories = {}
    for code, item in (payload.get("funds") or {}).items():
        if not isinstance(item, dict) or item.get("source") not in {
            "eastmoney-history",
            "eastmoney-pingzhongdata",
        }:
            continue
        records = []
        for record in item.get("records") or []:
            try:
                nav = float(record["nav"])
                record_date = str(record["date"])
            except (KeyError, TypeError, ValueError):
                continue
            if nav <= 0 or not record_date:
                continue
            records.append(
                {
                    "date": record_date,
                    "nav": nav,
                    "name": str(
                        item.get("name")
                        or TARGET_FUND_NAMES.get(str(code), "")
                    ),
                }
            )
        if records:
            histories[str(code)] = sorted(records, key=lambda record: record["date"])
    return histories


def _portfolio_point_in_time_validation(
    path: Path,
    signal_date: date | str | None = None,
    histories: dict[str, list[dict]] | None = None,
) -> dict:
    """Validate auditable portfolio holdings without treating gaps as neutral.

    ``available_date`` is the earliest date on which a quarterly disclosure may
    influence a signal. Records after ``signal_date`` are excluded and counted
    as look-ahead violations rather than silently used.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if isinstance(signal_date, str):
        try:
            signal = date.fromisoformat(signal_date)
        except ValueError:
            signal = date.today()
    else:
        signal = signal_date or date.today()
    raw_funds = payload.get("funds", {}) if isinstance(payload, dict) else {}
    periods_by_fund: dict[str, list[dict]] = {}
    duplicate_periods: dict[str, int] = {}
    lookahead_violations: list[dict] = []
    blocking_reasons: list[str] = []
    for code in PORTFOLIO_HOLDINGS:
        raw_periods = raw_funds.get(code, [])
        if not isinstance(raw_periods, list):
            raw_periods = []
        seen: set[str] = set()
        duplicates = 0
        eligible: list[dict] = []
        for item in raw_periods:
            if not isinstance(item, dict):
                continue
            disclosure = str(item.get("disclosure_date", ""))
            holdings = item.get("holdings")
            if not disclosure or not isinstance(holdings, list) or not holdings:
                continue
            available_text = str(item.get("available_date", ""))
            if not available_text:
                try:
                    available_text = (
                        date.fromisoformat(disclosure)
                        + timedelta(days=PORTFOLIO_DISCLOSURE_LAG_DAYS)
                    ).isoformat()
                except ValueError:
                    available_text = ""
            try:
                available = date.fromisoformat(available_text)
            except ValueError:
                blocking_reasons.append(f"{code} 存在无法解析的可用日期。")
                continue
            if disclosure in seen:
                duplicates += 1
                continue
            seen.add(disclosure)
            if available > signal:
                lookahead_violations.append(
                    {
                        "code": code,
                        "disclosure_date": disclosure,
                        "available_date": available.isoformat(),
                        "signal_date": signal.isoformat(),
                    }
                )
                continue
            eligible.append(item)
        periods_by_fund[code] = sorted(
            eligible, key=lambda item: str(item.get("disclosure_date", ""))
        )
        duplicate_periods[code] = duplicates
        if len(eligible) < 4:
            blocking_reasons.append(
                f"{code} 仅有 {len(eligible)} 个有效季度点时快照，至少需要 4 个。"
            )
    if lookahead_violations:
        blocking_reasons.append("点时快照包含晚于信号日的记录，已排除未来信息。")

    overlap_samples: list[dict] = []
    portfolio_codes = list(PORTFOLIO_HOLDINGS)
    if len(portfolio_codes) >= 2:
        left_code, right_code = portfolio_codes[:2]
        left_by_date = {
            item["disclosure_date"]: item for item in periods_by_fund.get(left_code, [])
        }
        right_by_date = {
            item["disclosure_date"]: item for item in periods_by_fund.get(right_code, [])
        }
        for disclosure in sorted(set(left_by_date) & set(right_by_date)):
            left_weights = {
                str(item.get("code") or item.get("name")): float(item.get("weight", 0))
                for item in left_by_date[disclosure].get("holdings", [])[:10]
            }
            right_weights = {
                str(item.get("code") or item.get("name")): float(item.get("weight", 0))
                for item in right_by_date[disclosure].get("holdings", [])[:10]
            }
            overlap = sum(
                min(weight, right_weights.get(stock, 0))
                for stock, weight in left_weights.items()
            )
            overlap_samples.append(
                {
                    "disclosure_date": disclosure,
                    "overlap": round(overlap, 6),
                }
            )
    if not overlap_samples:
        blocking_reasons.append("没有可用于前十大持仓重合度计算的共同季度快照。")

    if histories is None:
        histories = _load_cached_histories(CURRENT_HISTORY_FILE)
    correlation = None
    if len(portfolio_codes) >= 2:
        left = histories.get(portfolio_codes[0], [])
        right = histories.get(portfolio_codes[1], [])
        if left and right:
            correlation = _history_correlation(
                {"records": left}, {"records": right}
            )
    correlation_coverage = {
        "pairs": 1 if correlation is not None else 0,
        "required_pairs": 1 if len(portfolio_codes) >= 2 else 0,
        "ready": correlation is not None,
        "mean_nav_correlation": round(correlation, 6) if correlation is not None else None,
    }
    if not correlation_coverage["ready"]:
        blocking_reasons.append("缺少两只持仓的重叠净值历史，无法计算相关性。")

    exposure_samples = []
    for code, periods in periods_by_fund.items():
        for item in periods:
            exposure = sum(
                float(holding.get("weight", 0))
                for holding in item.get("holdings", [])[:10]
                if _is_semiconductor_holding(holding)
            )
            exposure_samples.append(
                {
                    "code": code,
                    "disclosure_date": item.get("disclosure_date", ""),
                    "semiconductor_exposure": round(exposure, 6),
                }
            )
    if not exposure_samples:
        blocking_reasons.append("缺少可用于行业/半导体暴露计算的持仓快照。")

    valid_period_count = sum(len(periods) for periods in periods_by_fund.values())
    ready = bool(
        len(portfolio_codes) >= 2
        and valid_period_count >= 8
        and all(len(periods_by_fund.get(code, [])) >= 4 for code in portfolio_codes)
        and not blocking_reasons
        and bool(overlap_samples)
        and bool(exposure_samples)
    )
    return {
        "ready": ready,
        "sample_count": valid_period_count,
        "fund_count": len(portfolio_codes),
        "periods_by_fund": {
            code: len(periods_by_fund.get(code, [])) for code in portfolio_codes
        },
        "valid_period_count": valid_period_count,
        "lookahead_violations": lookahead_violations,
        "duplicate_periods": duplicate_periods,
        "top10_overlap_samples": overlap_samples,
        "top10_overlap_sample_count": len(overlap_samples),
        "top10_overlap_mean": round(
            sum(item["overlap"] for item in overlap_samples) / len(overlap_samples), 6
        )
        if overlap_samples
        else None,
        "correlation_coverage": correlation_coverage,
        "sector_or_semiconductor_exposure_samples": exposure_samples,
        "sector_or_semiconductor_exposure_sample_count": len(exposure_samples),
        "signal_date": signal.isoformat(),
        "available_date_policy": str(
            payload.get("available_date_policy", "") if isinstance(payload, dict) else ""
        ),
        "blocking_reasons": blocking_reasons,
        "reason": (
            "组合点时快照、净值相关性、前十大重合与行业暴露样本均满足门槛。"
            if ready
            else "；".join(blocking_reasons) or "组合风险点时样本不足。"
        ),
    }


def _load_portfolio_point_in_time(
    path: Path = PORTFOLIO_POINT_IN_TIME_FILE,
    signal_date: date | str | None = None,
    histories: dict[str, list[dict]] | None = None,
) -> dict:
    """Publicly named loader used by tests and downstream calibration tools."""
    return _portfolio_point_in_time_validation(path, signal_date, histories)


def _merge_histories(
    frozen: dict[str, list[dict]], current: dict[str, list[dict]]
) -> dict[str, list[dict]]:
    merged = {}
    for code in sorted(set(frozen) | set(current)):
        by_date = {
            str(record["date"]): dict(record)
            for record in frozen.get(code, [])
        }
        for record in current.get(code, []):
            record_date = str(record["date"])
            existing_name = str((by_date.get(record_date) or {}).get("name", ""))
            by_date[record_date] = {
                **record,
                "name": str(record.get("name") or existing_name),
            }
        merged[code] = sorted(by_date.values(), key=lambda record: record["date"])
    return merged


def _history_scope_coverage(
    histories: dict[str, list[dict]], codes: list[str], minimum_records: int = 252
) -> dict:
    funds = {}
    for code in codes:
        records = histories.get(code, [])
        funds[code] = {
            "record_count": len(records),
            "ready": len(records) >= minimum_records,
            "start_date": records[0]["date"] if records else "",
            "end_date": records[-1]["date"] if records else "",
        }
    ready_count = sum(item["ready"] for item in funds.values())
    return {
        "required_count": len(codes),
        "ready_count": ready_count,
        "minimum_records": minimum_records,
        "ready": ready_count == len(codes),
        "funds": funds,
    }


def _build_samples(histories: dict[str, list[dict]], horizon: int, threshold: float) -> list[dict]:
    samples = []
    for code, records in histories.items():
        # Non-overlapping outcomes prevent one market fall from being counted many times.
        for index in range(126, len(records) - horizon, horizon):
            prior = records[: index + 1]
            metrics = _history_metrics(prior)
            future = [float(item["nav"]) for item in records[index : index + horizon + 1]]
            forward_drawdown = min(value / future[0] - 1 for value in future)
            samples.append(
                {
                    "code": code,
                    "profile": _profile_for_fund(
                        code, str(records[0].get("name", ""))
                    ),
                    "date": records[index]["date"],
                    "outcome_end": records[index + horizon]["date"],
                    "x": _feature_vector(metrics),
                    "label": int(forward_drawdown <= threshold),
                    "forward_drawdown": forward_drawdown,
                    "ma60_gap": metrics["ma60_gap"],
                    "ma120_gap": metrics["ma120_gap"],
                }
            )
    return sorted(samples, key=lambda item: (item["date"], item["code"]))


def _feature_vector(metrics: dict) -> list[float]:
    return [
        _tail_risk(metrics) / 100,
        _trend_risk(metrics) / 100,
        min(max(-metrics["max_drawdown_63"], 0), 0.50) / 0.50,
        min(metrics["downside_volatility_63"], 0.60) / 0.60,
        min(max(-metrics["cvar_95_63"], 0), 0.08) / 0.08,
        min(max(-metrics["ma60_gap"], 0), 0.25) / 0.25,
        min(max(-metrics["ma120_gap"], 0), 0.35) / 0.35,
    ]


def _calibrate_horizon(rows: list[dict], horizon: int, drawdown_threshold: float) -> dict:
    train, validation, test, train_end, validation_end = _split_samples(rows)
    coefficients = _fit_logistic(train)
    threshold_selection = _select_alert_threshold_details(validation, coefficients)
    threshold = threshold_selection["threshold"]
    split_metrics = {
        "train": _evaluate(train, coefficients, threshold),
        "validation": _evaluate(validation, coefficients, threshold),
        "test": _evaluate(test, coefficients, threshold),
    }
    test_metrics = split_metrics["test"]
    walk_forward = _walk_forward(rows)
    profile_metrics = {
        profile: _profile_metrics(
            [row for row in test if row.get("profile") == profile],
            coefficients,
            threshold,
        )
        for profile in REQUIRED_PROFILES
    }
    rule_audit = _rule_buffer_audit(test)
    production_approved = bool(
        test_metrics["events"] >= 20
        and test_metrics["recall"] >= 0.70
        and test_metrics["precision"] >= 0.60
        and test_metrics["false_positive_rate"] <= 0.30
        and test_metrics["brier"] < test_metrics["baseline_brier"]
        and test_metrics["high_low_drawdown_gap"] <= -0.03
        and walk_forward["passing_window_ratio"] >= 0.60
        and threshold_selection["usable"]
    )
    return {
        "horizon": horizon,
        "drawdown_threshold": drawdown_threshold,
        "sample_count": len(rows),
        "event_count": sum(row["label"] for row in rows),
        "date_range": [rows[0]["date"], rows[-1]["date"]],
        "boundaries": {"train_end": train_end, "validation_end": validation_end},
        "coefficients": {
            "intercept": coefficients[0],
            **{name: coefficients[index + 1] for index, name in enumerate(FEATURES)},
        },
        "alert_probability_threshold": threshold,
        "threshold_selection": threshold_selection,
        "probability_training": {
            "class_weighting": "none",
            "reason": "保留真实事件率，避免类别加权抬高风险概率并造成全量报警。",
        },
        "splits": split_metrics,
        "walk_forward": walk_forward,
        "profile_metrics": profile_metrics,
        "trigger_rule_audit": rule_audit,
        "production_approved": production_approved,
        "acceptance": {
            "minimum_test_events": 20,
            "recall": 0.70,
            "precision": 0.60,
            "maximum_false_positive_rate": 0.30,
            "brier_better_than_baseline": True,
            "high_low_drawdown_gap": -0.03,
        },
    }


def _split_samples(
    rows: list[dict],
) -> tuple[list[dict], list[dict], list[dict], str, str]:
    dates = sorted({row["date"] for row in rows})
    train_end = dates[int(len(dates) * 0.60)]
    validation_end = dates[int(len(dates) * 0.80)]
    train = [row for row in rows if row["outcome_end"] < train_end]
    validation = [
        row
        for row in rows
        if train_end <= row["date"] and row["outcome_end"] < validation_end
    ]
    test = [row for row in rows if row["date"] >= validation_end]
    return train, validation, test, train_end, validation_end


def _profile_for_fund(code: str, name: str = "") -> str:
    if code == "161815" or "FOF" in name.upper():
        return "fof"
    explicit = FUNDAMENTAL_PROFILE_BY_FUND.get(code)
    if explicit:
        return explicit
    text = name.lower()
    if any(term in text for term in ("资源", "商品", "能源", "矿业", "抗通胀")):
        return "resource"
    if any(
        term in text
        for term in ("科技", "互联网", "数字经济", "芯片", "智能", "创新", "纳斯达克")
    ):
        return "technology"
    return "balanced"


def _profile_metrics(
    rows: list[dict], coefficients: list[float], threshold: float
) -> dict:
    metrics = _evaluate(rows, coefficients, threshold)
    fund_count = len({row["code"] for row in rows})
    statistics_ready = bool(
        metrics
        and fund_count >= MIN_PROFILE_FUNDS
        and metrics["events"] >= MIN_PROFILE_TEST_EVENTS
        and metrics["samples"] >= 30
    )
    passed = bool(
        statistics_ready
        and metrics["recall"] >= 0.60
        and metrics["precision"] >= 0.40
        and metrics["false_positive_rate"] <= 0.40
        and metrics["brier"] < metrics["baseline_brier"]
        and metrics["high_low_drawdown_gap"] < 0
    )
    return {
        "fund_count": fund_count,
        "statistics_ready": statistics_ready,
        "passed": passed,
        **metrics,
    }


def _evaluate_rule(rows: list[dict], predicate) -> dict:
    if not rows:
        return {
            "samples": 0,
            "events": 0,
            "alerts": 0,
            "recall": 0.0,
            "precision": 0.0,
            "false_positive_rate": 0.0,
            "confusion": {"tp": 0, "fp": 0, "tn": 0, "fn": 0},
        }
    labels = [int(row["label"]) for row in rows]
    predictions = [int(bool(predicate(row))) for row in rows]
    tp = sum(p == 1 and y == 1 for p, y in zip(predictions, labels))
    fp = sum(p == 1 and y == 0 for p, y in zip(predictions, labels))
    tn = sum(p == 0 and y == 0 for p, y in zip(predictions, labels))
    fn = sum(p == 0 and y == 1 for p, y in zip(predictions, labels))
    return {
        "samples": len(rows),
        "events": sum(labels),
        "alerts": sum(predictions),
        "recall": round(tp / (tp + fn), 6) if tp + fn else 0.0,
        "precision": round(tp / (tp + fp), 6) if tp + fp else 0.0,
        "false_positive_rate": round(fp / (fp + tn), 6) if fp + tn else 0.0,
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
    }


def _rule_buffer_audit(rows: list[dict]) -> dict:
    legacy = _evaluate_rule(
        rows,
        lambda row: row["ma60_gap"] < 0 or row["ma120_gap"] < 0,
    )
    buffered = _evaluate_rule(
        rows,
        lambda row: (
            row["ma60_gap"] <= -MA60_ALERT_BUFFER
            or row["ma120_gap"] <= -MA120_ALERT_BUFFER
        ),
    )
    immediate = _evaluate_rule(
        rows, lambda row: row["ma120_gap"] <= -MA120_IMMEDIATE_BUFFER
    )
    return {
        "thresholds": {
            "ma60_alert": -MA60_ALERT_BUFFER,
            "ma120_alert": -MA120_ALERT_BUFFER,
            "ma120_immediate": -MA120_IMMEDIATE_BUFFER,
        },
        "legacy_zero_buffer": legacy,
        "buffered_rule": buffered,
        "immediate_rule": immediate,
        "false_positive_rate_reduction": round(
            legacy["false_positive_rate"] - buffered["false_positive_rate"], 6
        ),
        "precision_change": round(buffered["precision"] - legacy["precision"], 6),
    }


def _profile_validation(horizons: dict[str, dict]) -> dict:
    profiles = {}
    blocking_reasons = []
    for profile in REQUIRED_PROFILES:
        horizon_metrics = {
            key: item.get("profile_metrics", {}).get(profile, {})
            for key, item in horizons.items()
        }
        fund_count = max(
            (int(item.get("fund_count", 0)) for item in horizon_metrics.values()),
            default=0,
        )
        coverage_ready = fund_count >= MIN_PROFILE_FUNDS and all(
            item.get("statistics_ready") for item in horizon_metrics.values()
        )
        passed = coverage_ready and all(
            item.get("passed") for item in horizon_metrics.values()
        )
        profiles[profile] = {
            "label": PROFILE_LABELS[profile],
            "fund_count": fund_count,
            "coverage_ready": coverage_ready,
            "passed": passed,
            "horizons": horizon_metrics,
        }
        if fund_count < MIN_PROFILE_FUNDS:
            blocking_reasons.append(
                f"{PROFILE_LABELS[profile]}样本仅覆盖{fund_count}只基金，至少需要{MIN_PROFILE_FUNDS}只。"
            )
        elif not coverage_ready:
            blocking_reasons.append(
                f"{PROFILE_LABELS[profile]}样本外事件或样本数量不足。"
            )
        elif not passed:
            blocking_reasons.append(
                f"{PROFILE_LABELS[profile]}样本外精确率、召回率或误报率未达标。"
            )
    return {
        "required_profiles": list(REQUIRED_PROFILES),
        "minimum_funds_per_profile": MIN_PROFILE_FUNDS,
        "minimum_test_events_per_horizon": MIN_PROFILE_TEST_EVENTS,
        "passed": not blocking_reasons,
        "profiles": profiles,
        "blocking_reasons": blocking_reasons,
    }


def _trigger_validation(horizons: dict[str, dict]) -> dict:
    results = {}
    blocking_reasons = []
    for key, item in horizons.items():
        audit = item.get("trigger_rule_audit", {})
        buffered = audit.get("buffered_rule", {})
        legacy = audit.get("legacy_zero_buffer", {})
        passed = bool(
            buffered.get("events", 0) >= 20
            and buffered.get("recall", 0) >= 0.60
            and buffered.get("false_positive_rate", 1)
            <= legacy.get("false_positive_rate", 1)
        )
        results[key] = {**audit, "passed": passed}
        if not passed:
            blocking_reasons.append(
                f"未来{key}日窗口的均线缓冲规则尚未同时满足事件数、召回率和误报率门槛。"
            )
    return {
        "passed": not blocking_reasons,
        "horizons": results,
        "blocking_reasons": blocking_reasons,
    }


def _production_acceptance(
    horizons: dict[str, dict],
    profile_validation: dict,
    trigger_validation: dict,
    portfolio_validation: dict,
) -> dict:
    probability_ready = all(
        item.get("production_approved") for item in horizons.values()
    )
    blocking_reasons = []
    if not probability_ready:
        blocking_reasons.append("20日或63日概率模型未通过滚动样本外硬门槛。")
    blocking_reasons.extend(profile_validation.get("blocking_reasons", []))
    blocking_reasons.extend(trigger_validation.get("blocking_reasons", []))
    if not portfolio_validation.get("ready"):
        blocking_reasons.append(str(portfolio_validation.get("reason", "组合风险样本不足。")))
    return {
        "passed": not blocking_reasons,
        "probability_model_passed": probability_ready,
        "profile_validation_passed": bool(profile_validation.get("passed")),
        "trigger_buffer_validation_passed": bool(trigger_validation.get("passed")),
        "portfolio_overlay_validation_passed": bool(portfolio_validation.get("ready")),
        "blocking_reasons": blocking_reasons,
    }


def _walk_forward(rows: list[dict]) -> dict:
    dates = sorted({row["date"] for row in rows})
    windows = []
    for start_ratio, end_ratio in ((0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 1.0)):
        start = dates[int(len(dates) * start_ratio)]
        end_index = min(int(len(dates) * end_ratio), len(dates) - 1)
        end = dates[end_index]
        available = [row for row in rows if row["outcome_end"] < start]
        if len(available) < 80:
            continue
        available_dates = sorted({row["date"] for row in available})
        validation_start = available_dates[int(len(available_dates) * 0.80)]
        train = [row for row in available if row["outcome_end"] < validation_start]
        validation = [row for row in available if row["date"] >= validation_start]
        test = [row for row in rows if start <= row["date"] <= end]
        if not train or not validation or not test:
            continue
        coefficients = _fit_logistic(train)
        threshold = _select_alert_threshold(validation, coefficients)
        metrics = _evaluate(test, coefficients, threshold)
        passed = bool(
            metrics["events"] >= 5
            and metrics["recall"] >= 0.60
            and metrics["precision"] >= 0.40
            and metrics["false_positive_rate"] <= 0.40
            and metrics["brier"] < metrics["baseline_brier"]
            and metrics["high_low_drawdown_gap"] < 0
        )
        windows.append(
            {
                "start": start,
                "end": end,
                "threshold": threshold,
                "passed": passed,
                **metrics,
            }
        )
    passing = sum(item["passed"] for item in windows)
    return {
        "window_count": len(windows),
        "passing_windows": passing,
        "passing_window_ratio": round(passing / len(windows), 6) if windows else 0.0,
        "windows": windows,
    }


def _fit_logistic(rows: list[dict]) -> list[float]:
    coefficients = [0.0] * (len(FEATURES) + 1)
    rate = sum(row["label"] for row in rows) / max(len(rows), 1)
    coefficients[0] = math.log(max(rate, 1e-4) / max(1 - rate, 1e-4))
    learning_rate = 0.12
    l2 = 0.025
    for iteration in range(3000):
        gradient = [0.0] * len(coefficients)
        for row in rows:
            vector = [1.0, *row["x"]]
            probability = _sigmoid(sum(coefficients[i] * vector[i] for i in range(len(vector))))
            error = probability - row["label"]
            for i, value in enumerate(vector):
                gradient[i] += error * value
        scale = max(len(rows), 1)
        for i in range(len(coefficients)):
            penalty = 0 if i == 0 else l2 * coefficients[i]
            coefficients[i] -= learning_rate * (gradient[i] / scale + penalty)
        if iteration and iteration % 1000 == 0:
            learning_rate *= 0.7
    return [round(value, 8) for value in coefficients]


def _select_alert_threshold(rows: list[dict], coefficients: list[float]) -> float:
    return _select_alert_threshold_details(rows, coefficients)["threshold"]


def _select_alert_threshold_details(
    rows: list[dict], coefficients: list[float]
) -> dict:
    """Choose a threshold only when it has useful precision and FPR."""
    candidates = []
    for threshold_percent in range(5, 96):
        threshold = threshold_percent / 100
        metrics = _evaluate(rows, coefficients, threshold)
        precision = metrics.get("precision", 0.0)
        recall = metrics.get("recall", 0.0)
        false_positive_rate = metrics.get("false_positive_rate", 1.0)
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        if (
            recall >= MIN_ALERT_RECALL
            and precision >= MIN_ALERT_PRECISION
            and false_positive_rate <= MAX_ALERT_FALSE_POSITIVE_RATE
        ):
            candidates.append(
                (f1, precision, recall, -false_positive_rate, -threshold, metrics)
            )
    if candidates:
        selected = max(candidates)
        return {
            "threshold": -selected[4],
            "usable": True,
            "candidate_count": len(candidates),
            "minimum_recall": MIN_ALERT_RECALL,
            "minimum_precision": MIN_ALERT_PRECISION,
            "maximum_false_positive_rate": MAX_ALERT_FALSE_POSITIVE_RATE,
            "selected_metrics": selected[5],
            "reason": "阈值满足研究阶段的召回率、精确率和误报率约束。",
        }
    return {
        "threshold": NO_USABLE_THRESHOLD,
        "usable": False,
        "candidate_count": 0,
        "minimum_recall": MIN_ALERT_RECALL,
        "minimum_precision": MIN_ALERT_PRECISION,
        "maximum_false_positive_rate": MAX_ALERT_FALSE_POSITIVE_RATE,
        "selected_metrics": {},
        "reason": "没有候选阈值同时满足精确率和误报率约束，当前周期不输出概率预警。",
    }


def _evaluate(rows: list[dict], coefficients: list[float], threshold: float) -> dict:
    if not rows:
        return {}
    probabilities = [_predict(row["x"], coefficients) for row in rows]
    labels = [row["label"] for row in rows]
    predictions = [int(value >= threshold) for value in probabilities]
    tp = sum(p == 1 and y == 1 for p, y in zip(predictions, labels))
    fp = sum(p == 1 and y == 0 for p, y in zip(predictions, labels))
    tn = sum(p == 0 and y == 0 for p, y in zip(predictions, labels))
    fn = sum(p == 0 and y == 1 for p, y in zip(predictions, labels))
    event_rate = sum(labels) / len(labels)
    high = [row["forward_drawdown"] for row, p in zip(rows, probabilities) if p >= threshold]
    low = [row["forward_drawdown"] for row, p in zip(rows, probabilities) if p < threshold]
    return {
        "samples": len(rows),
        "events": sum(labels),
        "event_rate": round(event_rate, 6),
        "recall": round(tp / (tp + fn), 6) if tp + fn else 0.0,
        "precision": round(tp / (tp + fp), 6) if tp + fp else 0.0,
        "false_positive_rate": round(fp / (fp + tn), 6) if fp + tn else 0.0,
        "brier": round(sum((p - y) ** 2 for p, y in zip(probabilities, labels)) / len(rows), 6),
        "baseline_brier": round(sum((event_rate - y) ** 2 for y in labels) / len(rows), 6),
        "high_risk_mean_drawdown": round(sum(high) / len(high), 6) if high else None,
        "low_risk_mean_drawdown": round(sum(low) / len(low), 6) if low else None,
        "high_low_drawdown_gap": round(sum(high) / len(high) - sum(low) / len(low), 6) if high and low else 0.0,
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
    }


def _predict(features: list[float], coefficients: list[float]) -> float:
    return _sigmoid(coefficients[0] + sum(coefficients[i + 1] * value for i, value in enumerate(features)))


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1 / (1 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1 + exponential)


def _format_report(result: dict) -> str:
    lines = [
        f"模型：{result['model_version']}",
        f"生成时间：{result['generated_at']}",
        f"基金数：{result['fund_count']}",
        f"生产验收：{result['status']}",
    ]
    scope = result.get("scope_coverage", {})
    target_scope = scope.get("target_funds", {})
    portfolio_scope = scope.get("portfolio_holdings", {})
    lines.extend(
        [
            "指定基金历史覆盖："
            f"{target_scope.get('ready_count', 0)}/{target_scope.get('required_count', 0)}只达标",
            "实际持仓历史覆盖："
            f"{portfolio_scope.get('ready_count', 0)}/{portfolio_scope.get('required_count', 0)}只达标",
        ]
    )
    for key, item in result["horizons"].items():
        threshold_selection = item.get("threshold_selection", {})
        threshold_text = (
            f"{item['alert_probability_threshold']:.0%}"
            if threshold_selection.get("usable", True)
            else "不可用"
        )
        lines.extend(
            [
                "",
                f"未来{key}日回撤阈值：{item['drawdown_threshold']:.0%}",
                f"非重叠样本：{item['sample_count']}，事件：{item['event_count']}，概率阈值：{threshold_text}",
                f"阈值说明：{threshold_selection.get('reason', '未提供')}",
                f"生产验收：{'通过' if item['production_approved'] else '不通过'}",
            ]
        )
        for split, metrics in item["splits"].items():
            lines.append(
                f"{split}：样本{metrics['samples']}，事件{metrics['events']}，召回率{metrics['recall']:.1%}，"
                f"精确率{metrics['precision']:.1%}，误报率{metrics['false_positive_rate']:.1%}，"
                f"Brier {metrics['brier']:.4f}/{metrics['baseline_brier']:.4f}，"
                f"高低风险组未来回撤差{metrics['high_low_drawdown_gap']:.2%}"
            )
        walk = item["walk_forward"]
        lines.append(
            f"滚动样本外：{walk['passing_windows']}/{walk['window_count']}个窗口通过，"
            f"通过率{walk['passing_window_ratio']:.1%}"
        )
        audit = item.get("trigger_rule_audit", {})
        legacy = audit.get("legacy_zero_buffer", {})
        buffered = audit.get("buffered_rule", {})
        lines.append(
            "均线规则：零缓冲误报率"
            f"{legacy.get('false_positive_rate', 0):.1%}，当前缓冲误报率"
            f"{buffered.get('false_positive_rate', 0):.1%}，当前缓冲召回率"
            f"{buffered.get('recall', 0):.1%}"
        )

    lines.extend(["", "分基金画像样本外验收："])
    for profile in REQUIRED_PROFILES:
        item = result.get("profile_validation", {}).get("profiles", {}).get(profile, {})
        lines.append(
            f"{PROFILE_LABELS[profile]}：基金{item.get('fund_count', 0)}只，"
            f"覆盖{'达标' if item.get('coverage_ready') else '不足'}，"
            f"结果{'通过' if item.get('passed') else '未通过'}"
        )

    portfolio = result.get("portfolio_overlay_validation", {})
    lines.extend(
        [
            "",
            "组合风险叠加验收："
            + ("通过" if portfolio.get("ready") else "未通过")
            + f"（权重 {float(portfolio.get('weight', 0)):.0%}）",
            str(portfolio.get("reason", "")),
            "",
            "生产验收阻断原因：",
        ]
    )
    reasons = result.get("acceptance", {}).get("blocking_reasons", [])
    lines.extend(f"- {reason}" for reason in reasons)
    if not reasons:
        lines.append("- 无")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
