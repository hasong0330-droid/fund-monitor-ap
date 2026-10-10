"""Shared safety gate for analysis and portfolio actions.

The data-quality layer describes what is available. This module translates that
description into explicit permissions so each consumer applies the same rules.
"""

from __future__ import annotations

from typing import Any

from data_quality import (
    BLOCKED,
    CONSERVATIVE_DELAYED,
    CONSERVATIVE_FALLBACK,
    LIMITED,
    READY,
)


_GATE_RANK = {READY: 0, LIMITED: 1, BLOCKED: 2}


def build_decision_gate(
    quality_item: dict[str, Any] | None = None,
    overall_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a normalized permission set for one fund or the whole report."""
    quality_item = quality_item or {}
    overall_quality = overall_quality or {}
    item_gate = str(quality_item.get("gate") or READY)
    overall_gate = str(overall_quality.get("overall_gate") or READY)
    gate = max((item_gate, overall_gate), key=lambda value: _GATE_RANK.get(value, 2))
    mode = str(quality_item.get("analysis_mode") or "STANDARD")
    reasons = _unique_reasons(
        [
            *quality_item.get("reasons", []),
            *(
                [str(overall_quality.get("summary"))]
                if overall_quality.get("summary")
                else []
            ),
        ]
    )

    if gate == BLOCKED:
        return _result(
            gate,
            "BLOCKED",
            False,
            False,
            False,
            False,
            reasons or ["关键数据缺失、过期或未通过来源校验。"],
        )

    if gate == LIMITED:
        if mode == CONSERVATIVE_DELAYED:
            return _result(
                gate,
                CONSERVATIVE_DELAYED,
                True,
                False,
                False,
                False,
                reasons or ["存在正常披露延迟，仅允许保守分析。"],
            )
        if mode == CONSERVATIVE_FALLBACK:
            return _result(
                gate,
                CONSERVATIVE_FALLBACK,
                True,
                False,
                False,
                False,
                reasons or ["当前使用缓存或静态基线，仅允许观察。"],
            )
        return _result(
            gate,
            "LIMITED_OBSERVE",
            False,
            False,
            False,
            False,
            reasons or ["数据质量受限，仅允许观察。"],
        )

    return _result(
        READY,
        "STANDARD",
        True,
        True,
        True,
        True,
        reasons or ["关键数据完整且在有效期内。"],
    )


def _result(
    gate: str,
    mode: str,
    can_analyze: bool,
    can_buy: bool,
    can_strong_buy: bool,
    can_exact_reduce: bool,
    reasons: list[str],
) -> dict[str, Any]:
    labels = {
        READY: "标准分析与交易评估",
        LIMITED: "受限数据",
        BLOCKED: "禁止判断",
    }
    return {
        "gate": gate,
        "gate_label": labels.get(gate, "禁止判断"),
        "mode": mode,
        "can_analyze": can_analyze,
        "can_buy": can_buy,
        "can_strong_buy": can_strong_buy,
        "can_exact_reduce": can_exact_reduce,
        "reasons": _unique_reasons(reasons),
    }


def _unique_reasons(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value).strip()))

