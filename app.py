from __future__ import annotations

import argparse
import html
import json
import logging
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from analyzer import (
    analyze_market,
    analyze_recommendations,
    analyze_target_funds,
    build_report_payload,
    format_brief_report,
)
from ai_cycle_alert import apply_ai_cycle_gates, build_ai_cycle_alert
from config import (
    CACHE_DIR,
    LOG_DIR,
    MIN_RECOMMENDATION_DAILY_LIMIT,
    PORTFOLIO_HOLDINGS,
    RECOMMENDATION_POOL,
    REPORT_CACHE_FILE,
    REPORTS_DIR,
    TARGET_FUND_NAMES,
    TARGET_FUNDS,
    WINDOW_TITLE,
)
from storage import (
    AlreadyRunningError,
    atomic_write_json,
    atomic_write_text,
    single_instance_lock,
)
from fetcher import (
    fetch_finance_news,
    fetch_ai_cycle_news,
    fetch_fund_histories,
    fetch_fund_holdings,
    fetch_holding_stock_trends,
    fetch_fund_quotes,
    fetch_market_snapshot,
    fetch_recommendation_qualifications,
    fetch_audit_snapshot,
    network_preflight_snapshot,
    reset_source_health,
    run_network_preflight,
    source_priority_snapshot,
    source_health_snapshot,
)
from quant_model import build_scoring_candidates, evaluate_strong_buy
from data_quality import (
    build_data_quality_report,
    quality_status_text,
    resolve_fund_navs,
)
from holding_alert import evaluate_portfolio_alerts
from portfolio_ledger import refresh_portfolio_ledger
from portfolio_config import (
    apply_quote_based_shares,
    delete_portfolio_position,
    load_active_portfolio,
    load_portfolio_config,
    upsert_portfolio_position,
)
from point_in_time_archive import archive_observations, archive_status


CHROME_PATHS = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)
LOGGER = logging.getLogger("fund_monitor")


def _merge_portfolio_fund_quality(data_quality: dict, portfolio_quality: dict) -> None:
    """Expose holding fund gates without changing target-universe confidence."""
    data_quality.setdefault("funds", {}).update(portfolio_quality.get("funds", {}))


def _nav_return_metrics(history: dict | None) -> dict[str, float | None]:
    """Calculate one-day and one-month changes from published NAV records."""
    records = sorted(
        [item for item in (history or {}).get("records", []) if item.get("date")],
        key=lambda item: str(item.get("date", "")),
    )
    values: list[float] = []
    for item in records:
        try:
            nav = float(item.get("nav"))
        except (TypeError, ValueError):
            continue
        if nav > 0:
            values.append(nav)
    if not values:
        return {"daily_return_pct": None, "monthly_return_pct": None}

    latest = values[-1]

    def change(previous: float | None) -> float | None:
        if previous is None or previous <= 0:
            return None
        return round((latest / previous - 1.0) * 100.0, 2)

    return {
        "daily_return_pct": change(values[-2] if len(values) >= 2 else None),
        # A month is represented by approximately 21 published NAV sessions.
        "monthly_return_pct": change(values[-22] if len(values) >= 22 else None),
    }


def _return_text(value: object) -> tuple[str, str]:
    if not isinstance(value, (int, float)):
        return "数据待更新", "return-missing"
    value = float(value)
    prefix = "+" if value > 0 else ""
    arrow = "↑" if value > 0 else "↓" if value < 0 else "→"
    color_class = "return-positive" if value > 0 else "return-negative" if value < 0 else "return-flat"
    return f"{arrow} {prefix}{value:.2f}%", color_class


def _return_strip(item: dict) -> str:
    daily, daily_class = _return_text(item.get("daily_return_pct"))
    monthly, monthly_class = _return_text(item.get("monthly_return_pct"))
    return (
        '<div class="return-strip" aria-label="日涨幅和月涨幅">'
        f'<div class="return-item"><span>日涨幅</span><b class="{daily_class}">{daily}</b></div>'
        f'<div class="return-item"><span>月涨幅</span><b class="{monthly_class}">{monthly}</b></div>'
        "</div>"
    )


def _attach_nav_returns(
    report: dict,
    histories: dict[str, dict],
    portfolio_positions: dict[str, dict],
    quotes: dict | None = None,
) -> None:
    quotes = quotes or {}

    def metrics(code: str) -> dict[str, float | None]:
        history_metrics = _nav_return_metrics(histories.get(code))
        quote = quotes.get(code)
        # Browser-observed pages may have explicit daily/monthly figures even
        # when a long history is unavailable. Preserve those published values.
        if quote and getattr(quote, "source", "") == "browser-fund-page":
            if getattr(quote, "daily_return_pct", None) is not None:
                history_metrics["daily_return_pct"] = quote.daily_return_pct
            if getattr(quote, "monthly_return_pct", None) is not None:
                history_metrics["monthly_return_pct"] = quote.monthly_return_pct
        return history_metrics

    for collection in ("targets", "recommendations"):
        for item in report.get(collection, []):
            item.update(metrics(str(item.get("code", ""))))
    strong_buy = report.get("strong_buy") or {}
    if strong_buy.get("code"):
        strong_buy.update(metrics(str(strong_buy["code"])))
    for code, position in portfolio_positions.items():
        position.update(metrics(str(code)))


def _holding_alert_html(alert: dict) -> str:
    if not alert:
        return """<div class="portfolio-warning"><div class="warning-title"><span class="warning-dot"></span><strong>持仓预警：数据待更新</strong></div></div>"""
    level = str(alert.get("level", "GRAY")).lower()
    label = html.escape(str(alert.get("level_label", "灰色")))
    score = alert.get("risk_score")
    score_text = f"{float(score):.1f}/100" if isinstance(score, (int, float)) else "--"
    if isinstance(score, (int, float)):
        base_level = (
            "红色" if float(score) >= 70 else
            "橙色" if float(score) >= 55 else
            "黄色" if float(score) >= 35 else "绿色"
        )
    else:
        base_level = "--"
    trigger_type = str(alert.get("trigger_type") or "NONE")
    trigger_note = (
        f" · 规则覆盖：{html.escape(trigger_type)}"
        if trigger_type != "NONE" else ""
    )
    reasons = alert.get("reasons", [])
    primary_reason = html.escape(str(reasons[0])) if reasons else "暂无明显异常触发项。"
    metrics = alert.get("metrics", {})
    component_items = (
        ("企业基本面", alert.get("components", {}).get("fundamental_risk")),
        ("宏观风险", alert.get("components", {}).get("macro_risk")),
        ("技术资金", alert.get("components", {}).get("technical_risk")),
        ("行业景气", alert.get("components", {}).get("industry_risk")),
        ("事件风险", alert.get("components", {}).get("other_risk")),
    )
    cards = []
    for title, value in component_items:
        value_text = f"{float(value):.1f}" if isinstance(value, (int, float)) else "--"
        cards.append(
            f'<div class="warning-item {level}"><span>{title}</span><b>{value_text}</b><p>风险分，越高越需要防守。</p></div>'
        )
    factor_status = alert.get("factor_status", {})
    missing_factors = [
        key for key, item in factor_status.items() if not item.get("available")
    ]
    missing_text = "、".join(missing_factors) if missing_factors else "无"
    holding_evidence = alert.get("holding_evidence", {})
    portfolio_evidence = alert.get("portfolio_evidence", {})
    macro_evidence = alert.get("macro_evidence", {})
    vix_value = macro_evidence.get("VIX", {}).get("value")
    us10y_value = macro_evidence.get("US10Y", {}).get("value")
    real10y_value = macro_evidence.get("US10Y_REAL", {}).get("value")
    macro_text = (
        f"VIX {float(vix_value):.2f} · 美债10Y {float(us10y_value):.2f}% · 实际利率 {float(real10y_value):.2f}%"
        if all(isinstance(value, (int, float)) for value in (vix_value, us10y_value, real10y_value))
        else "扩展宏观数据不完整"
    )
    decision_reason = html.escape(str(alert.get("decision_reason", "--")))
    reduction = alert.get("reduction_strategy") or {}
    reduction_status = html.escape(
        str(reduction.get("percentage_label", "暂不输出减持比例"))
    )
    reduction_basis = html.escape(
        str(reduction.get("basis", "减持仅在完整证据链满足时输出。"))
    )
    reduction_reasons = reduction.get("blocked_reasons") or []
    reduction_reason_html = "".join(
        f"<li>{html.escape(str(reason))}</li>" for reason in reduction_reasons
    )
    evidence_rows = []
    for item in reduction.get("evidence", [])[:6]:
        evidence_rows.append(
            "<li><b>{category}</b>：{fact}；{value}；阈值：{threshold}；"
            "日期：{as_of}；来源：{source}</li>".format(
                category=html.escape(str(item.get("category", ""))),
                fact=html.escape(str(item.get("fact", ""))),
                value=html.escape(str(item.get("value", ""))),
                threshold=html.escape(str(item.get("threshold", ""))),
                as_of=html.escape(str(item.get("as_of", "--"))),
                source=html.escape(str(item.get("source", "--"))),
            )
        )
    reduction_detail = (
        f'<div class="reduction-evidence"><b>减持策略：{reduction_status}</b>'
        f'<p>{reduction_basis}</p>'
        + (f"<ul>{''.join(evidence_rows)}</ul>" if evidence_rows else "")
        + (f"<ul class=\"reduction-blocked\">{reduction_reason_html}</ul>" if reduction_reason_html else "")
        + f'<p>复核条件：{html.escape(str(reduction.get("review_condition", "等待下一次数据更新后复核。")))}</p></div>'
    )
    holding_plan = html.escape(str(alert.get("holding_plan", "计划持有期待补充")))
    buy_signal_status = html.escape(str(alert.get("buy_signal_status", "--")))
    five_factor = alert.get("factor_model", {})
    fundamental_coverage = float(
        five_factor.get("coverage", {}).get("fundamental", 0) or 0
    )
    return f"""
      <div class="portfolio-warning alert-{level}">
        <div class="warning-title">
          <span class="warning-dot"></span>
          <strong>持仓预警：{label}</strong>
          <span>{html.escape(str(alert.get('action', '数据待更新')))}</span>
        </div>
        <div class="warning-summary"><b>基础风险等级 {base_level} · 风险分 {score_text}</b><span>最终预警：{label}{trigger_note} · {html.escape(str(alert.get('summary', '--')))}</span></div>
        <div class="warning-grid">{''.join(cards)}</div>
        <div class="warning-trigger"><b>主要依据：</b>{primary_reason}</div>
        {reduction_detail}
        <div class="warning-evidence">
          <span>重仓行情覆盖 <b>{float(holding_evidence.get('stock_trend_coverage', 0)):.0%}</b></span>
          <span>覆盖部分跌破MA60 <b>{float(holding_evidence.get('covered_below_ma60_ratio', 0)):.0%}</b></span>
          <span>持仓相关性 <b>{float(portfolio_evidence.get('mean_nav_correlation', 0)):.2f}</b></span>
          <span>前十大重合 <b>{float(portfolio_evidence.get('max_top10_overlap', 0)):.1%}</b></span>
          <span>组合半导体暴露 <b>{float(portfolio_evidence.get('portfolio_semiconductor_exposure', 0)):.1%}</b></span>
        </div>
        <div class="warning-foot">宏观因子：{html.escape(macro_text)} · 状态机：{html.escape(str(alert.get('state_status', '--')))}</div>
        <div class="warning-foot">统一操作决策：{decision_reason} · 原始买点：{buy_signal_status}</div>
        <div class="warning-foot">持有期：{holding_plan}</div>
        <div class="warning-foot">企业财报覆盖：{fundamental_coverage:.1%} · 五因子数据置信度 {float(five_factor.get('confidence', 0)):.1f}% · 权重：基本面40% / 宏观20% / 技术资金20% / 行业10% / 事件10%</div>
        <div class="warning-foot">未来20/63日回撤原始概率：{float(metrics.get('p20_drawdown_over_8', 0)):.1%}/{float(metrics.get('p63_drawdown_over_12', 0)):.1%} · 因子置信度 {float(alert.get('confidence', 0)):.1f}% · 缺失因子：{html.escape(missing_text)}</div>
        <div class="warning-foot">校准状态：{html.escape(str(alert.get('calibration_status', '--')))} · {html.escape(str(alert.get('confirmation_rule', '')))}</div>
      </div>
    """


def configure_run_logging() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "latest_run.log"
    LOGGER.handlers.clear()
    LOGGER.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    )
    LOGGER.addHandler(handler)
    return log_path


def status_class(status_level: int) -> str:
    if status_level >= 3:
        return "green"
    if status_level == 2:
        return "yellow"
    return "blue"


def direction_class(direction: str) -> str:
    if direction == "偏利多":
        return "positive"
    if direction == "偏利空":
        return "negative"
    return "neutral"


def _ai_cycle_fund_badge(ai_cycle: dict, code: str) -> str:
    impact = (ai_cycle.get("fund_impacts") or {}).get(str(code), {})
    if not impact:
        return ""
    if not impact.get("applicable", True):
        return '<div class="ai-fund-badge neutral">AI周期暴露不适用（FOF资产配置）</div>'
    exposure = impact.get("observed_exposure")
    exposure_text = f"{float(exposure):.1%}" if isinstance(exposure, (int, float)) else "--"
    gate = str(impact.get("gate", "NORMAL"))
    css = "blocked" if gate == "BLOCKED" else "caution" if gate == "CAUTION" else "normal"
    gate_text = "暂停加仓" if gate == "BLOCKED" else "谨慎观察" if gate == "CAUTION" else "正常跟踪"
    return (
        f'<div class="ai-fund-badge {css}">AI周期暴露 {exposure_text} · '
        f'{html.escape(str(impact.get("level", "--")))} · {gate_text}</div>'
    )


def _ai_cycle_alert_html(ai_cycle: dict) -> str:
    if not ai_cycle:
        return ""
    level = str(ai_cycle.get("level", "GRAY")).lower()
    score = ai_cycle.get("risk_score")
    score_text = f"{float(score):.1f}/100" if isinstance(score, (int, float)) else "--"
    dimensions = ai_cycle.get("dimensions", {})
    dimension_cards = []
    for key in ("cloud_capex", "delivery_lead_time", "memory_price", "ai_economics", "earnings_guidance"):
        item = dimensions.get(key, {})
        value = item.get("risk_score")
        value_text = f"{float(value):.0f}" if isinstance(value, (int, float)) else "--"
        dimension_cards.append(
            '<div class="ai-dimension">'
            f'<span>{html.escape(str(item.get("label", key)))}</span>'
            f'<b>{html.escape(str(item.get("status", "数据不足")))} · {value_text}</b>'
            f'<small>{int(item.get("negative_source_count", 0) or 0)}个风险来源 · '
            f'{int(item.get("company_count", 0) or 0)}家公司</small></div>'
        )
    impacts = []
    sortable_impacts = [
        (code, item) for code, item in (ai_cycle.get("fund_impacts") or {}).items()
        if item.get("applicable") and isinstance(item.get("observed_exposure"), (int, float))
    ]
    sortable_impacts.sort(key=lambda row: float(row[1]["observed_exposure"]), reverse=True)
    for code, item in sortable_impacts[:8]:
        impacts.append(
            f'<span>{html.escape(code)} <b>{float(item["observed_exposure"]):.1%}</b> '
            f'{html.escape(str(item.get("level", "--")))}</span>'
        )
    evidence_rows = []
    for item in ai_cycle.get("evidence", [])[:8]:
        evidence_rows.append(
            "<li><b>{dimension}</b>：{title}；日期 {date}；来源 {source}</li>".format(
                dimension=html.escape(
                    str(dimensions.get(item.get("dimension"), {}).get("label", "周期信号"))
                ),
                title=html.escape(str(item.get("title", "--"))),
                date=html.escape(str(item.get("published_at", "--"))),
                source=html.escape(str(item.get("source", "--"))),
            )
        )
    evidence_html = "".join(evidence_rows) or "<li>尚无带日期、可归因的有效证据。</li>"
    return f"""
      <section class="ai-cycle-panel ai-{level}">
        <div class="ai-cycle-head">
          <div>
            <span>AI资本开支周期预警</span>
            <strong>{html.escape(str(ai_cycle.get('level_label', '灰色')))} · {score_text}</strong>
            <p>{html.escape(str(ai_cycle.get('summary', '--')))}</p>
          </div>
          <div class="ai-cycle-meta">
            <span>数据可信度 <b>{float(ai_cycle.get('confidence', 0) or 0):.0f}%</b></span>
            <span>证据日 <b>{html.escape(str(ai_cycle.get('as_of', '--')))}</b></span>
          </div>
        </div>
        <div class="ai-dimension-grid">{''.join(dimension_cards)}</div>
        <div class="ai-impact-strip">{''.join(impacts)}</div>
        <details class="ai-details">
          <summary>查看证据与判定口径</summary>
          <ul>{evidence_html}</ul>
          <p>{html.escape(str(ai_cycle.get('rules', '')))}</p>
        </details>
      </section>
    """


def render_dashboard(report: dict, data_status: str) -> str:
    generated_at = html.escape(report.get("generated_at", "--"))
    market = report.get("market", {})
    phase = html.escape(market.get("phase", "--"))
    summary = html.escape(market.get("summary", "--"))
    buy_principle = html.escape(market.get("buy_principle", "--"))
    macro = html.escape(market.get("macro", "--"))
    brief_report = html.escape(format_brief_report(report))
    ai_cycle = report.get("ai_cycle_alert", {})
    ai_cycle_html = _ai_cycle_alert_html(ai_cycle)
    quality = report.get("data_quality", {})
    macro_detail = quality.get("macro", {})
    confidence_detail = quality.get("confidence_detail", {})
    critical_macro_notice = (
        "当前未配置指定基金；季度 GDP 或真实 OAS 未完整获取，当前仅允许保守分析。"
        if macro_detail.get("critical_missing")
        else ""
    )
    macro_coverage_text = (
        f"宏观经济实时覆盖 {float(macro_detail.get('economic_coverage', 0)):.0%}"
        f"（缓存 {float(macro_detail.get('economic_cached_coverage', 0)):.0%}） · "
        f"市场风险实时覆盖 {float(macro_detail.get('market_coverage', 0)):.0%}"
        f"（缓存 {float(macro_detail.get('market_cached_coverage', 0)):.0%}） · "
        f"状态 {html.escape(str(macro_detail.get('status_label', '--')))}"
    )
    macro_coverage_text += (
        f" · 静态参考 {float(macro_detail.get('economic_static_coverage', 0)):.0%}/"
        f"{float(macro_detail.get('market_static_coverage', 0)):.0%}"
    )
    macro_coverage_text += (
        f" · 字段完整度 {float(macro_detail.get('field_completeness', 0)):.0%}"
        f" · 来源完整性 {float(macro_detail.get('source_integrity', 0)):.0%}"
    )
    confidence_notes = [
        f"字段完整度 {float(confidence_detail.get('field_completeness', 0)):.0%}",
        f"宏观来源完整性 {float(confidence_detail.get('source_integrity', 0)):.0%}",
    ]
    if confidence_detail.get("fundamental_coverage") is not None:
        confidence_notes.append(
            f"基本面覆盖 {float(confidence_detail['fundamental_coverage']):.0%}"
        )
    if confidence_detail.get("freshness_score") is not None:
        confidence_notes.append(
            f"财报新鲜度 {float(confidence_detail['freshness_score']):.0f}/100"
        )
    if float(confidence_detail.get("proxy_penalty", 0)):
        confidence_notes.append(
            f"信用代理扣分 {float(confidence_detail['proxy_penalty']):.0f}"
        )
    confidence_cap_reasons = [
        str(item) for item in confidence_detail.get("cap_reasons", []) if item
    ]
    confidence_notes.extend(confidence_cap_reasons)
    fetch_telemetry = report.get("fetch_telemetry", {})
    module_timings = fetch_telemetry.get("modules", {})
    slowest_module = max(
        module_timings,
        key=lambda key: float(module_timings[key].get("duration_sec", 0)),
        default="--",
    )
    slowest_duration = float(
        module_timings.get(slowest_module, {}).get("duration_sec", 0)
    )
    telemetry_text = (
        f"抓取耗时 {float(fetch_telemetry.get('total_fetch_sec', 0)):.1f}秒"
        f" · 最慢模块 {slowest_module} {slowest_duration:.1f}秒"
    )
    quality_html = f"""
      <section class="quality-panel">
        <div class="quality-heading">
          <div>
            <span>数据可信度</span>
            <strong>{float(quality.get('overall_score', 0)):.1f}<small>/100</small></strong>
          </div>
          <p>{html.escape(str(quality.get('summary', '尚未生成数据质量报告。')))}</p>
          <b>{html.escape(str(quality.get('overall_gate_label', '禁止判断')))}</b>
        </div>
      </section>
    """

    acquisition = report.get("data_acquisition", {})
    preflight = acquisition.get("network_preflight", {})
    preflight_available = bool(preflight.get("available"))
    preflight_status = "网络可用" if preflight_available else "网络不可用"
    preflight_class = "available" if preflight_available else "unavailable"
    audit_html = f"""
      <section class="audit-panel">
        <div class="audit-heading">
          <div>
            <span>数据获取审计</span>
            <strong class="{preflight_class}">{preflight_status}</strong>
          </div>
        </div>
      </section>
    """

    news_cards = []
    for item in report.get("news", []):
        link = item.get("link") or ""
        title = html.escape(item.get("title", "--"))
        source = html.escape(item.get("source", "--"))
        published_at = html.escape(item.get("published_at", ""))
        summary_text = html.escape(item.get("summary", "--"))
        tag = html.escape(item.get("tag", "市场动态"))
        impact = html.escape(item.get("impact", "--"))
        affected_funds = html.escape(item.get("affected_funds", "--"))
        direction = item.get("direction", "中性观察")
        direction_text = html.escape(direction)
        watch = html.escape(item.get("watch", "等待更多数据确认。"))
        title_html = (
            f'<a href="{html.escape(link)}" target="_blank" rel="noreferrer">{title}</a>'
            if link
            else title
        )
        news_cards.append(
            f"""
            <article class="news-card">
              <div class="news-top">
                <div class="news-signals">
                  <span class="chip">{tag}</span>
                  <span class="direction {direction_class(direction)}">{direction_text}</span>
                </div>
                <span class="news-source">{source}{f'<br>{published_at}' if published_at else ''}</span>
              </div>
              <h3>{title_html}</h3>
              <p>{summary_text}</p>
              <div class="affected">影响基金：{affected_funds}</div>
              <div class="impact">走势影响：{impact}</div>
              <div class="watch">后续观察：{watch}</div>
            </article>
            """
        )

    target_cards = []
    for item in report.get("targets", []):
        holding_alert = report.get("holding_alerts", {}).get(item.get("code"), {})
        buy_gate = holding_alert.get("buy_gate")
        ai_impact = (ai_cycle.get("fund_impacts") or {}).get(str(item.get("code")), {})
        gate_note = ""
        if buy_gate:
            gate_note = (
                '<p class="rule-text bad">持仓统一建议：'
                f"{html.escape(str(holding_alert.get('action', '--')))}。"
                f"{html.escape(str(holding_alert.get('buy_gate_label', '--')))}。"
                f"{html.escape(str(holding_alert.get('decision_reason', '')))}</p>"
            )
        elif ai_impact.get("gate") == "BLOCKED":
            gate_note = (
                '<p class="rule-text bad">AI周期风险：当前暂停新增买入，等待资本开支、'
                '交期、存储价格或盈利指引出现连续改善。</p>'
            )
        est_nav = (
            f" | 估算 {item['est_nav']:.4f}" if item.get("est_nav") is not None else ""
        )
        levels = "".join(
            f'<span class="pill">{html.escape(level)}</span>'
            for level in item.get("buy_levels", [])
        )
        factor_model = item.get("factor_model") or {}
        factor_scores = factor_model.get("scores", {})
        factor_coverage = factor_model.get("coverage", {})
        fundamental_detail = factor_model.get("details", {}).get("fundamental", {})
        fundamental_coverage_text = (
            "不适用（FOF资产配置）"
            if fundamental_detail.get("applicable") is False
            else f'{float(factor_coverage.get("fundamental", 0)):.1%}'
        )
        factor_labels = (
            ("基本面40%", "fundamental"),
            ("宏观20%", "macro"),
            ("技术资金20%", "technical"),
            ("行业10%", "industry"),
            ("事件10%", "other"),
        )
        factor_html = "".join(
            f"<span>{label} <b>{float(factor_scores.get(key, 0)):.1f}</b></span>"
            for label, key in factor_labels
        )
        factor_line = (
            f'<div class="warning-evidence">{factor_html}'
            f'<span>财报覆盖 <b>{fundamental_coverage_text}</b></span>'
            f'<span>五因子置信度 <b>{float(factor_model.get("confidence", 0)):.1f}%</b></span></div>'
            if factor_model
            else ""
        )
        target_cards.append(
            f"""
            <section class="fund-card">
              <div class="fund-head">
                <div>
                  <div class="fund-name">{html.escape(item['name'])}（{html.escape(item['code'])}）</div>
                  <div class="fund-meta">净值 {item['nav']:.4f}{est_nav} · 净值日 {html.escape(item.get('nav_date', '--'))} · 动态理想区间 {html.escape(item['target_range'])}</div>
                  {_return_strip(item)}
                  {_ai_cycle_fund_badge(ai_cycle, item['code'])}
                  <div class="delta">{html.escape(item['summary'])}</div>
                  {factor_line}
                </div>
                <div class="status {status_class(item['status_level'])}">{html.escape(item['status'])}</div>
              </div>
              <div class="rules">
                <details class="rule-box">
                  <summary>买入节点</summary>
                  <div class="rule-content">
                    <div class="levels">{levels}</div>
                    <p class="rule-text">触发条件：{html.escape(item['trigger'])}</p>
                    {gate_note}
                  </div>
                </details>
                <details class="rule-box">
                  <summary>不买条件</summary>
                  <div class="rule-content">
                    <p class="rule-text bad">{html.escape(item['dont_buy'])}</p>
                    <p class="rule-text">执行方式：{html.escape(item['method'])}</p>
                    <p class="rule-text hint">位置判断：{html.escape(item['valuation_hint'])}</p>
                  </div>
                </details>
              </div>
            </section>
            """
        )

    recommendation_cards = []
    for item in report.get("recommendations", []):
        recommendation_cards.append(
            f"""
            <article class="pick-card">
              <h3>{html.escape(item['name'])}（{html.escape(item['code'])}）</h3>
              <p>重点市场：{html.escape(item['investment_scope'])} · 净值 {item['nav']:.4f}</p>
              {_return_strip(item)}
              {_ai_cycle_fund_badge(ai_cycle, item['code'])}
              <p>单日限额 {item['one_day_limit']} · {html.escape(item['manager_type'])}</p>
              <p>近三年月均排名：{html.escape(item['rank_desc'])}</p>
              <p>{html.escape(item['reason'])} {html.escape(item['fit_for'])}</p>
            </article>
            """
        )
    if not recommendation_cards:
        qualification_items = report.get("recommendation_qualifications", {})
        excluded = []
        for code, item in qualification_items.items():
            reasons = "；".join(item.get("reasons", []))
            if item.get("error"):
                reasons = f"{reasons}；抓取异常：{item['error']}".strip("；")
            excluded.append(
                f"{code}：{reasons or '未通过全部硬条件'}"
            )
        exclusion_text = " ｜ ".join(excluded) or "资格数据不足，暂不推荐。"
        recommendation_cards.append(
            f"""
            <article class="pick-card">
              <h3>当前暂无可展示的额外基金</h3>
              <p>{html.escape(exclusion_text)}</p>
              <p>未通过的资格条件仅作为参考信息，不再作为推荐前置门槛。</p>
            </article>
            """
        )

    headlines_html = "".join(
        f'<div class="macro-item"><p>{html.escape(line)}</p></div>'
        for line in report.get("headlines", [])
    )
    portfolio_positions = report.get("portfolio_ledger", {}).get("positions", {})
    holding_alerts = report.get("holding_alerts", {})
    portfolio_html = ""
    manual_portfolio_cards = []
    portfolio_config = report.get("portfolio_config") or PORTFOLIO_HOLDINGS
    for code, config_item in portfolio_config.items():
        position = portfolio_positions.get(code, {})
        target = next(
            (item for item in report.get("targets", []) if item.get("code") == code),
            {},
        )
        holding_alert = holding_alerts.get(code, {})
        name = html.escape(
            holding_alert.get("name")
            or target.get("name")
            or TARGET_FUND_NAMES.get(code, code)
        )
        nav = target.get("nav")
        if not isinstance(nav, (int, float)) or nav <= 0:
            nav = position.get("current_nav")
        nav_text = f"{nav:.4f}" if isinstance(nav, (int, float)) and nav else "--"
        nav_date_value = target.get("nav_date") or position.get("nav_date") or "--"
        nav_source_value = target.get("data_source") or position.get("nav_source") or "--"
        nav_date = html.escape(str(nav_date_value))
        nav_source = html.escape(str(nav_source_value))
        fetch_date = generated_at[:10] if len(generated_at) >= 10 else "--"
        if str(nav_source_value).lower() == "cache":
            freshness_note = "缓存估值，仅供展示"
        elif nav_date != fetch_date and nav_date != "--":
            freshness_note = "官方净值存在披露延迟，未使用未经确认的估算值"
        else:
            freshness_note = "官方净值已更新至本次抓取日"
        status = html.escape(target.get("status", "数据待更新"))
        operation_action = html.escape(str(holding_alert.get("action", "数据待更新")))
        operation_gate = html.escape(str(holding_alert.get("buy_gate_label", "风险闸门待更新")))
        summary = html.escape(target.get("summary", "结合市场环境继续跟踪。"))
        invested = float(
            position.get(
                "cost_basis",
                config_item.get("cost_basis", config_item.get("invested_amount", 0)),
            )
            or 0
        )
        shares = float(position.get("shares", config_item.get("shares", 0)) or 0)
        current = position.get("market_value")
        if current is None:
            current = config_item.get("market_value_input")
        if current is None:
            current = nav * shares if isinstance(nav, (int, float)) and nav > 0 else None
        profit = position.get("unrealized_pnl")
        if profit is None:
            profit = config_item.get("pnl_input")
        if profit is None and current is not None:
            profit = current - invested
        profit_pct = position.get("unrealized_pnl_pct")
        if profit_pct is None:
            profit_pct = profit / invested if profit is not None and invested else None
        profit_class = "profit" if profit is not None and profit >= 0 else "loss"
        cost_nav = position.get("cost_nav") or (invested / shares if shares else None)
        current_text = f"¥{current:,.2f}" if current is not None else "--"
        profit_text = f"{profit:+,.2f}" if profit is not None else "--"
        profit_pct_text = f"{profit_pct:+.2%}" if profit_pct is not None else "--"
        manual_portfolio_cards.append(
            f"""
            <section class="portfolio-card" data-portfolio-code="{html.escape(code)}">
              <div class="portfolio-heading">
                <div>
                  <div class="portfolio-kicker">我的持仓</div>
                  <h2>{name}<span>{html.escape(code)}</span></h2>
                  <p>官方单位净值 {nav_text} · 净值日 {nav_date} · 成本净值 {cost_nav:.4f} · {summary}</p>
                  <p class="delta">本次抓取 {fetch_date} · 来源 {nav_source} · {freshness_note}</p>
                </div>
                <div class="portfolio-action">
                  <div class="portfolio-action-label">当前操作建议</div>
                  <strong>{operation_action}</strong>
                  <span>原始买点：{status} · {operation_gate}</span>
                </div>
              </div>
              {_return_strip(position)}
              {_ai_cycle_fund_badge(ai_cycle, code)}
              <div class="portfolio-metrics">
                <div class="metric">
                  <span>累计投入</span>
                  <strong>¥{invested:,.2f}</strong>
                  <small>按用户录入成本</small>
                </div>
                <div class="metric">
                  <span>持有份额</span>
                  <strong>{shares:,.2f} 份</strong>
                  <small>按最新单位净值自动计算</small>
                </div>
                <div class="metric">
                  <span>当前市值</span>
                  <strong>{current_text}</strong>
                  <small>净值日 {nav_date} · {nav_text} × {shares:,.2f}份 · 抓取 {fetch_date}</small>
                </div>
                <div class="metric pnl-metric">
                  <span>当前盈亏</span>
                  <strong class="{profit_class}">{profit_text}</strong>
                  <small>{profit_pct_text} · 相对累计投入</small>
                </div>
              </div>
              {_holding_alert_html(holding_alerts.get(code, {}))}
            </section>
            """
        )
    portfolio_html += "".join(manual_portfolio_cards)

    strong_buy = report.get("strong_buy", {})
    failed_conditions = strong_buy.get("failed_conditions", [])
    failure_html = "".join(
        f"<li>{html.escape(str(reason))}</li>" for reason in failed_conditions[:6]
    ) or "<li>当前数据未形成满足全部门槛的候选。</li>"
    data_errors = strong_buy.get("data_errors", [])
    data_error_html = "".join(
        f"<li>{html.escape(str(reason))}</li>" for reason in data_errors[:3]
    )
    strong_factor = strong_buy.get("factor_model", {})
    strong_scores = strong_factor.get("scores", {})
    strong_coverage = strong_factor.get("coverage", {})
    score_items = [
        ("企业基本面 40%", strong_scores.get("fundamental")),
        ("宏观经济 20%", strong_scores.get("macro")),
        ("技术资金面 20%", strong_scores.get("technical")),
        ("行业景气 10%", strong_scores.get("industry")),
        ("新闻事件 10%", strong_scores.get("other")),
    ]
    score_html = "".join(
        f"""
        <div class="quant-score">
          <span>{label}</span>
          <strong>{float(value):.1f}</strong>
          <div><i style="width:{max(0, min(float(value), 100)):.1f}%"></i></div>
        </div>
        """
        for label, value in score_items
        if value is not None
    )
    macro_reason_html = "".join(
        f"<li>{html.escape(str(reason))}</li>"
        for reason in strong_buy.get("macro_reasons", [])
    ) or "<li>宏观数据不足，未形成可验证的支持理由。</li>"
    holding_reason_html = "".join(
        f"<li>{html.escape(str(reason))}</li>"
        for reason in strong_buy.get("holding_reasons", [])
    ) or "<li>持仓披露不足，未形成可验证的支持理由。</li>"
    risk_condition_html = "".join(
        f"<li>{html.escape(str(reason))}</li>"
        for reason in strong_buy.get("risk_conditions", [])
    ) or "<li>若趋势、盈利预期或申购状态恶化，模型将继续保持空信号。</li>"
    metrics = strong_buy.get("metrics", {})
    candidate_name = html.escape(str(strong_buy.get("name", "暂无候选")))
    candidate_code = html.escape(str(strong_buy.get("code", "--")))
    total_score = float(strong_buy.get("total_score", 0) or 0)
    confidence = float(strong_factor.get("confidence", strong_buy.get("confidence", 0)) or 0)
    risk_penalty = float(strong_buy.get("risk_penalty", 0) or 0)
    market_multiplier = float(strong_buy.get("market_multiplier", 0) or 0)
    model_version = html.escape(
        str(
            strong_buy.get(
                "model_version",
                "GAF-QM v4.0 Fundamental-Led Five-Factor 3-9M",
            )
        )
    )
    regime_state = html.escape(str(strong_buy.get("regime_state", "--")))
    portfolio_risk_budget = strong_buy.get("portfolio_risk_budget", {}) or {}
    risk_budget_reasons = portfolio_risk_budget.get("blocking_reasons", [])
    risk_budget_reason_html = "、".join(
        html.escape(str(reason)) for reason in risk_budget_reasons[:2]
    ) or "当前组合风险预算允许候选基金进入进一步复核"
    risk_budget_html = ""
    if portfolio_risk_budget:
        risk_budget_html = (
            f"<div class='quant-holdings'>组合风险预算："
            f"当前波动 {float(portfolio_risk_budget.get('current_volatility', 0)):.1%} · "
            f"候选相关性 {float(portfolio_risk_budget.get('candidate_correlation', 0)):.2f} · "
            f"最大新增比例 {float(portfolio_risk_budget.get('max_additional_weight', 0)):.1%} · "
            f"建议目标仓位 {float(portfolio_risk_budget.get('recommended_target_weight', 0)):.1%}"
            f"<br>预算结论：{risk_budget_reason_html}</div>"
        )
    is_strong_buy = bool(strong_buy.get("is_strong_buy"))
    is_recommended = bool(strong_buy.get("is_recommended") and strong_buy.get("code"))
    if is_strong_buy and not failed_conditions:
        failure_html = (
            "<li>已通过全部硬性门槛；实盘仍应分批执行，并在失效条件触发时停止加仓。</li>"
        )
    quant_state_class = "approved" if is_strong_buy else ("ranked" if is_recommended else "empty")
    quant_state_text = (
        "强烈建议买入"
        if is_strong_buy
        else ("综合评分首选" if is_recommended else "当前无可评分候选")
    )
    quant_subtitle = (
        "已通过全部硬性门槛及连续两个有效数据周期确认。"
        if is_strong_buy
        else (
            f"在 {int(strong_buy.get('scored_candidate_count', 0) or 0)} 只完成评分的基金中排名第一；"
            "当前不满足的条件只限制立即执行，不再取消推荐展示。"
            if is_recommended
            else "当前没有完成关键数据校验的基金，无法生成综合评分排序。"
        )
    )
    detail_html = ""
    if strong_buy.get("code"):
        detail_html = f"""
          <div class="quant-score-grid">{score_html}</div>
          <div class="quant-evidence-grid">
            <div class="quant-evidence">
              <h3>市场状态依据</h3>
              <ul>{macro_reason_html}</ul>
            </div>
            <div class="quant-evidence">
              <h3>持仓依据</h3>
              <ul>{holding_reason_html}</ul>
            </div>
            <div class="quant-evidence risk">
              <h3>压力与失效条件</h3>
              <ul>{risk_condition_html}</ul>
            </div>
          </div>
          <div class="quant-risk-strip">
            <span>年化收益 <b>{float(metrics.get('annual_return', 0)):.1%}</b></span>
            <span>年化波动 <b>{float(metrics.get('annual_volatility', 0)):.1%}</b></span>
            <span>最大回撤 <b>{float(metrics.get('max_drawdown', 0)):.1%}</b></span>
            <span>近126日回撤 <b>{float(metrics.get('max_drawdown_126', 0)):.1%}</b></span>
            <span>下行波动 <b>{float(metrics.get('downside_volatility_126', 0)):.1%}</b></span>
            <span>126日CVaR <b>{float(metrics.get('cvar_95_126', 0)):.2%}</b></span>
            <span>回撤恢复 <b>{metrics.get('recovery_days_126', 0)}日</b></span>
            <span>日度95% CVaR <b>{float(metrics.get('cvar_95', 0)):.2%}</b></span>
            <span>Sharpe <b>{float(metrics.get('sharpe', 0)):.2f}</b></span>
            <span>Sortino <b>{float(metrics.get('sortino', 0)):.2f}</b></span>
            <span>目标波动仓位系数 <b>{float(metrics.get('volatility_multiplier', 0)):.0%}</b></span>
            <span>市场状态 <b>{regime_state}</b></span>
            <span>市场仓位系数 <b>{market_multiplier:.0%}</b></span>
            <span>风险扣分 <b>-{risk_penalty:.1f}</b></span>
          </div>
          <div class="quant-holdings">持仓披露 {html.escape(str(metrics.get('disclosure_date', '--')))} · 前三大持仓：{html.escape(str(metrics.get('top_holdings', '--')))}</div>
          <div class="quant-holdings">企业财报覆盖 {float(strong_coverage.get('fundamental', 0)):.1%} · 企业财报覆盖门槛为60%，未达门槛时禁止强买；重大负面事件保留一票否决。</div>
          {risk_budget_html}
        """
    strong_buy_html = f"""
      <section class="quant-card {quant_state_class}">
        <div class="quant-head">
          <div>
            <div class="quant-kicker">{model_version} · 混合量化综合评分与强买监测</div>
            <h2>{candidate_name}<span>{candidate_code}</span></h2>
            <p>{quant_subtitle}</p>
          </div>
          <div class="quant-verdict">
            <span>{quant_state_text}</span>
            <strong>{total_score:.1f}</strong>
            <small>综合评分 · 置信度 {confidence:.1f}%</small>
          </div>
        </div>
        {_return_strip(strong_buy)}
        {detail_html}
        <div class="quant-failures">
          <h3>{'执行前复核条件' if is_strong_buy else ('当前暂缓买入的原因' if is_recommended else '无候选原因')}</h3>
          <ul>{failure_html}</ul>
          {f'<div class="quant-data-errors"><strong>其他候选数据提示</strong><ul>{data_error_html}</ul></div>' if data_error_html else ''}
        </div>
        <div class="quant-note">综合评分第一名始终作为首选推荐展示；是否可以立即买入由数据、风险和申购状态闸门单独决定。决策权重：企业基本面40% + 宏观经济20% + 技术与资金面20% + 行业景气10% + 负面新闻及突发事件10%。目标持有63至189个净值周期（约3至9个月）；强买还需总分≥70、企业财报覆盖≥60%、五因子置信度≥70%，并通过重大事件、数据来源和连续周期门槛。</div>
      </section>
    """

    portfolio_config_html = """
      <section class="portfolio-config" aria-labelledby="portfolio-config-title">
        <div class="portfolio-config-head">
          <strong id="portfolio-config-title">配置我的持仓</strong>
          <span>当前市值与盈亏按金额录入；累计成本、收益率和持有天数自动推算。</span>
        </div>
        <form class="portfolio-config-form" id="portfolio-config-form">
          <label>基金代码<input id="portfolio-code" inputmode="numeric" placeholder="例如 000001" required /></label>
          <label>当前市值（元）<input id="portfolio-market-value" type="number" min="0.01" step="0.01" placeholder="例如 1200.00" required /></label>
          <label>当前盈亏（元）<input id="portfolio-pnl" type="number" step="0.01" placeholder="例如 -18.50" required /></label>
          <label>买入日期<input id="portfolio-purchase-date" type="date" required /></label>
          <button type="submit">加入并重新评估</button>
        </form>
        <div class="portfolio-config-message" id="portfolio-config-message" aria-live="polite">
          录入后本页会立即计算成本和收益率；删除会同步清除本浏览器保存的派生曲线与预警状态。
        </div>
        <div id="portfolio-user-list" aria-live="polite"></div>
      </section>
    """

    return f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{WINDOW_TITLE}</title>
  <style>
    :root {{
      --bg: #0c2f76;
      --bg-deep: #0a255f;
      --panel: #173d99;
      --card: #2b2f93;
      --card-2: #214588;
      --line: #2ca4ff;
      --text: #f5f7ff;
      --muted: #b7c9f1;
      --green: #59c94f;
      --yellow: #ffc928;
      --blue: #39a8ff;
      --ink: #071128;
      --shadow: 0 18px 50px rgba(6, 18, 55, 0.28);
    }}

    * {{ box-sizing: border-box; }}

    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(82, 152, 255, 0.22), transparent 28%),
        radial-gradient(circle at top right, rgba(28, 99, 255, 0.18), transparent 24%),
        linear-gradient(180deg, #0b2b72 0%, #0a265f 100%);
      color: var(--text);
      padding: 28px;
    }}

    .frame {{
      width: min(1160px, 100%);
      margin: 0 auto;
      border-radius: 24px;
      overflow: hidden;
      background: linear-gradient(180deg, #154ca3 0%, #0f3f92 100%);
      box-shadow: var(--shadow);
      border: 1px solid rgba(114, 173, 255, 0.18);
    }}

    .header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      padding: 28px 28px 18px;
      background: linear-gradient(180deg, #12489d 0%, #11438f 100%);
      position: relative;
    }}

    .header::after {{
      content: "";
      position: absolute;
      left: 28px;
      right: 28px;
      bottom: 0;
      height: 1px;
      background: rgba(182, 212, 255, 0.22);
    }}

    .brand {{ display: flex; gap: 18px; align-items: center; }}

    .logo {{
      width: 52px;
      height: 52px;
      border-radius: 12px;
      background: linear-gradient(180deg, #0a1329, #07101f);
      display: grid;
      place-items: center;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.1);
      position: relative;
    }}

    .logo::before,
    .logo::after {{
      content: "";
      position: absolute;
      bottom: 12px;
      width: 8px;
      border-radius: 4px 4px 0 0;
      background: linear-gradient(180deg, #0e7af0, #59d8ff);
    }}

    .logo::before {{ left: 14px; height: 20px; }}
    .logo::after {{ right: 14px; height: 30px; }}
    .logo span {{ position: absolute; left: 12px; right: 12px; bottom: 10px; height: 2px; background: rgba(255,255,255,0.14); }}

    .titles h1 {{ margin: 0; font-size: 24px; font-weight: 800; }}
    .titles p {{ margin: 6px 0 0; color: var(--muted); font-size: 14px; }}
    .state-box {{
      min-width: 250px;
      text-align: left;
      color: #dbe7ff;
      font-size: 13px;
      line-height: 1.7;
      padding: 10px 14px;
      border-radius: 10px;
      background: rgba(6, 25, 70, 0.3);
      border: 1px solid rgba(166, 207, 255, 0.16);
    }}
    .update-time {{ color: #ffffff; font-size: 14px; font-weight: 800; }}
    .update-note {{ color: #9fd2ff; }}

    .body {{
      padding: 18px 20px 26px;
      background:
        linear-gradient(180deg, rgba(37, 70, 151, 0.55), rgba(20, 50, 128, 0.15)),
        linear-gradient(180deg, #154395, #133a86 32%, #0f3478 100%);
    }}

    .section-title {{ margin: 6px 6px 12px; color: #d9e8ff; font-size: 14px; font-weight: 700; letter-spacing: 0.2px; }}
    .grid {{ display: grid; gap: 12px; }}

    .portfolio-card {{
      position: relative;
      overflow: hidden;
      padding: 20px;
      border-radius: 16px;
      background: linear-gradient(135deg, #16285d 0%, #293594 54%, #174b91 100%);
      border: 1px solid rgba(146, 202, 255, 0.23);
      box-shadow: 0 14px 34px rgba(5, 18, 56, 0.2);
    }}
    .portfolio-card::after {{ content: ""; position: absolute; width: 340px; height: 340px; right: 0; top: -210px; border: 1px solid rgba(120, 193, 255, 0.18); border-radius: 50%; }}
    .portfolio-heading {{ position: relative; z-index: 1; display: flex; justify-content: space-between; gap: 20px; align-items: flex-start; }}
    .portfolio-heading > *, .fund-head > *, .rule-box, .metric {{ min-width: 0; }}
    .portfolio-kicker {{ color: #8dcfff; font-size: 12px; font-weight: 800; }}
    .portfolio-heading h2 {{ margin: 5px 0 0; font-size: 20px; line-height: 1.35; }}
    .portfolio-heading h2 span {{ display: inline-block; margin-left: 9px; color: #9fc9ff; font-size: 13px; font-weight: 700; }}
    .portfolio-heading p {{ margin: 7px 0 0; color: #c6d7fa; font-size: 13px; line-height: 1.6; }}
    .portfolio-card, .fund-card, .portfolio-card p, .fund-card p, .warning-foot, .warning-trigger, .audit-priority-row span {{ overflow-wrap: anywhere; word-break: break-word; }}
    .portfolio-action {{ min-width: 224px; padding: 12px 14px; border-radius: 10px; background: rgba(9, 25, 71, 0.42); border-left: 4px solid #ffc928; }}
    .portfolio-action-label {{ color: #aed1ff; font-size: 12px; }}
    .portfolio-action strong {{ display: block; margin-top: 4px; color: #ffe68b; font-size: 17px; }}
    .portfolio-action span {{ display: block; margin-top: 5px; color: #d7e4ff; font-size: 12px; }}
    .portfolio-metrics {{ position: relative; z-index: 1; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1px; margin-top: 20px; background: rgba(151, 196, 255, 0.18); border: 1px solid rgba(151, 196, 255, 0.15); }}
    .metric {{ min-height: 100px; padding: 14px 16px; background: rgba(10, 30, 82, 0.38); }}
    .metric span, .metric small {{ display: block; color: #b6cbed; font-size: 12px; }}
    .metric strong {{ display: block; margin: 8px 0 6px; color: #ffffff; font-size: 20px; }}
    .metric .pending {{ color: #ffdf7c; font-size: 17px; }}
    .metric .profit {{ color: #9aef92; }}
    .metric .loss {{ color: #ffafa8; }}
    .pnl-metric {{ background: rgba(68, 58, 143, 0.46); }}
    .portfolio-warning {{ position: relative; z-index: 1; margin-top: 16px; padding: 14px; border-radius: 10px; background: rgba(7, 22, 64, 0.3); border: 1px solid rgba(160, 203, 255, 0.12); }}
    .bottom-data-results {{ margin-top: 22px; padding-top: 18px; border-top: 1px solid rgba(124,190,255,.2); }}
    .bottom-data-title {{ margin: 0 6px 10px; color: #d9e8ff; font-size: 14px; font-weight: 800; }}
    .quality-panel, .audit-panel {{ border: 1px solid rgba(124,190,255,.22); background: #102f70; box-shadow: var(--shadow); }}
    .quality-heading, .audit-heading {{ display: grid; grid-template-columns: minmax(170px,.55fr) minmax(180px,1fr) auto; gap: 18px; align-items: center; padding: 16px 18px; }}
    .quality-heading > div span {{ display: block; color: #9fc9ff; font-size: 12px; }}
    .quality-heading > div strong {{ display: inline-block; margin-top: 3px; font-size: 28px; }}
    .quality-heading > div small {{ color: #9fc9ff; font-size: 13px; }}
    .quality-heading p {{ margin: 0; color: #d8e6ff; font-size: 13px; line-height: 1.6; }}
    .quality-heading > b {{ color: #ffe28a; font-size: 13px; }}
    .audit-panel {{ margin-top: 10px; background: rgba(9,31,76,.42); }}
    .audit-heading span {{ display: block; color: #9fc9ff; font-size: 12px; }}
    .audit-heading strong {{ display: block; margin-top: 4px; font-size: 26px; }}
    .audit-heading strong.available {{ color: #9aef92; }}
    .audit-heading strong.unavailable {{ color: #ffb0a7; }}
    .warning-title {{ display: flex; align-items: center; flex-wrap: wrap; gap: 8px; color: #d8e8ff; font-size: 12px; }}
    .warning-title strong {{ color: #ffe18a; font-size: 14px; }}
    .warning-dot {{ width: 9px; height: 9px; border-radius: 50%; background: #ffc928; box-shadow: 0 0 0 4px rgba(255, 201, 40, 0.12); }}
    .warning-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }}
    .warning-item {{ min-height: 106px; padding: 11px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.08); background: rgba(255,255,255,0.035); }}
    .warning-item span {{ color: #c9d8f2; font-size: 12px; }}
    .warning-item b {{ display: block; margin-top: 5px; font-size: 13px; }}
    .warning-item p {{ margin: 6px 0 0; color: #d6e0f7; font-size: 12px; line-height: 1.55; }}
    .warning-item.yellow b {{ color: #ffe086; }}
    .warning-item.green b {{ color: #9aef92; }}
    .warning-item.red b {{ color: #ffafa8; }}
    .warning-item.orange b {{ color: #ffb144; }}
    .warning-item.gray b {{ color: #c7d2e8; }}
    .alert-green {{ border-color: rgba(100, 220, 128, 0.28); }}
    .alert-yellow {{ border-color: rgba(255, 201, 40, 0.38); }}
    .alert-orange {{ border-color: rgba(255, 177, 68, 0.55); background: rgba(95, 56, 25, 0.28); }}
    .alert-red {{ border-color: rgba(255, 120, 120, 0.58); background: rgba(90, 32, 46, 0.30); }}
    .alert-gray {{ border-color: rgba(190, 205, 230, 0.3); background: rgba(40, 52, 80, 0.3); }}
    .warning-summary {{ display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; margin-top: 10px; color: #d6e4fb; font-size: 12px; line-height: 1.5; }}
    .warning-summary b {{ color: #fff; font-size: 15px; }}
    .warning-trigger {{ margin-top: 10px; color: #e0e9f9; font-size: 12px; line-height: 1.55; }}
    .reduction-evidence {{ margin-top: 10px; padding: 10px 12px; border-left: 3px solid #f0b35b; background: rgba(15, 30, 62, 0.52); color: #e8effc; font-size: 12px; line-height: 1.6; }}
    .reduction-evidence p {{ margin: 4px 0 0; color: #b7c9e8; }}
    .reduction-evidence ul {{ margin: 6px 0 0 18px; padding: 0; }}
    .reduction-evidence li {{ margin: 3px 0; }}
    .reduction-blocked {{ color: #ffd18a; }}
    .warning-evidence {{ display: flex; flex-wrap: wrap; gap: 7px 16px; margin-top: 9px; padding: 9px 10px; background: rgba(4, 17, 46, 0.26); color: #bfd1ec; font-size: 11px; }}
    .warning-evidence b {{ color: #fff; }}
    .warning-foot {{ margin-top: 8px; color: #aebfdd; font-size: 11px; line-height: 1.5; }}
    .ai-cycle-panel {{ margin: 12px 0 18px; padding: 16px 18px; border: 1px solid rgba(151,196,255,.24); background: #10285c; box-shadow: var(--shadow); overflow: hidden; }}
    .ai-cycle-panel.ai-green {{ border-color: rgba(100,220,128,.42); }}
    .ai-cycle-panel.ai-yellow {{ border-color: rgba(255,201,40,.50); }}
    .ai-cycle-panel.ai-orange {{ border-color: rgba(255,177,68,.62); background: #3d2b35; }}
    .ai-cycle-panel.ai-red {{ border-color: rgba(255,110,105,.65); background: #442438; }}
    .ai-cycle-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; }}
    .ai-cycle-head > div {{ min-width: 0; }}
    .ai-cycle-head span {{ color: #9fc9ff; font-size: 12px; }}
    .ai-cycle-head strong {{ display: block; margin-top: 4px; color: #fff; font-size: 20px; }}
    .ai-cycle-head p {{ margin: 6px 0 0; color: #d5e4fa; font-size: 13px; line-height: 1.55; overflow-wrap: anywhere; }}
    .ai-cycle-meta {{ flex: 0 0 auto; display: grid; gap: 5px; text-align: right; }}
    .ai-cycle-meta b {{ color: #fff; }}
    .ai-dimension-grid {{ display: grid; grid-template-columns: repeat(5,minmax(0,1fr)); gap: 8px; margin-top: 13px; }}
    .ai-dimension {{ min-width: 0; padding: 10px; background: rgba(4,17,46,.30); border: 1px solid rgba(255,255,255,.08); }}
    .ai-dimension span, .ai-dimension small {{ display: block; color: #acc3e5; font-size: 11px; overflow-wrap: anywhere; }}
    .ai-dimension b {{ display: block; margin: 5px 0; color: #fff; font-size: 13px; }}
    .ai-impact-strip {{ display: flex; flex-wrap: wrap; gap: 7px 14px; margin-top: 10px; padding: 9px 10px; background: rgba(4,17,46,.25); color: #c6d7ee; font-size: 11px; }}
    .ai-impact-strip b {{ color: #fff; }}
    .ai-details {{ margin-top: 10px; color: #d8e6f8; font-size: 12px; }}
    .ai-details summary {{ cursor: pointer; color: #9fd3ff; font-weight: 800; }}
    .ai-details ul {{ margin: 9px 0 0; padding-left: 18px; line-height: 1.65; }}
    .ai-details p {{ color: #abc0df; line-height: 1.55; }}
    .ai-fund-badge {{ display: inline-flex; max-width: 100%; margin-top: 8px; padding: 5px 8px; border-left: 3px solid #62b7ff; background: rgba(6,22,58,.30); color: #d8e8fb; font-size: 11px; line-height: 1.4; overflow-wrap: anywhere; }}
    .ai-fund-badge.caution {{ border-left-color: #ffd45c; }}
    .ai-fund-badge.blocked {{ border-left-color: #ff8177; color: #ffd0cb; }}
    .ai-fund-badge.neutral {{ border-left-color: #9cabbe; color: #c7d2e2; }}

    .quant-card {{ margin-top: 12px; padding: 18px; border-radius: 12px; background: linear-gradient(135deg, #102b61 0%, #153a72 58%, #174a76 100%); border: 1px solid rgba(124, 196, 255, 0.22); box-shadow: 0 14px 34px rgba(5, 18, 56, 0.18); }}
    .quant-card.approved {{ border-color: rgba(100, 220, 128, 0.55); background: linear-gradient(135deg, #12365c 0%, #145a62 100%); }}
    .quant-card.ranked {{ border-color: rgba(255, 201, 40, 0.5); }}
    .quant-head {{ display: flex; justify-content: space-between; gap: 20px; align-items: flex-start; }}
    .quant-kicker {{ color: #86d4ff; font-size: 12px; font-weight: 800; }}
    .quant-head h2 {{ margin: 6px 0 0; font-size: 20px; }}
    .quant-head h2 span {{ margin-left: 9px; color: #a8cdf8; font-size: 13px; }}
    .quant-head p {{ margin: 7px 0 0; color: #c5d8f4; font-size: 13px; }}
    .quant-verdict {{ min-width: 230px; padding: 12px 14px; border-left: 4px solid #ffc928; background: rgba(5, 20, 53, 0.34); }}
    .quant-card.approved .quant-verdict {{ border-left-color: #67db7d; }}
    .quant-verdict span, .quant-verdict small {{ display: block; color: #d4e2f8; font-size: 12px; }}
    .quant-verdict strong {{ display: block; margin: 3px 0; color: #ffe086; font-size: 26px; line-height: 1.1; }}
    .quant-card.approved .quant-verdict strong {{ color: #9af0a8; }}
    .quant-score-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; margin-top: 16px; }}
    .quant-score {{ padding: 10px; background: rgba(255,255,255,0.045); border: 1px solid rgba(255,255,255,0.07); }}
    .quant-score span {{ color: #b9cdec; font-size: 12px; }}
    .quant-score strong {{ display: block; margin: 4px 0 7px; font-size: 17px; }}
    .quant-score div {{ height: 4px; background: rgba(255,255,255,0.1); overflow: hidden; }}
    .quant-score i {{ display: block; height: 100%; background: #42adff; }}
    .quant-evidence-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }}
    .quant-evidence {{ padding: 12px; background: rgba(5, 20, 53, 0.28); border-top: 3px solid #3caeff; }}
    .quant-evidence.risk {{ border-top-color: #ffb144; }}
    .quant-evidence h3, .quant-failures h3 {{ margin: 0 0 7px; font-size: 13px; }}
    .quant-evidence ul, .quant-failures ul {{ margin: 0; padding-left: 18px; color: #d5e1f4; font-size: 12px; line-height: 1.65; }}
    .quant-risk-strip {{ display: flex; flex-wrap: wrap; gap: 8px 18px; margin-top: 12px; padding: 10px 12px; background: rgba(4, 17, 46, 0.28); color: #bfd1ec; font-size: 12px; }}
    .quant-risk-strip b {{ color: #ffffff; }}
    .quant-holdings {{ margin-top: 9px; color: #bcd2f1; font-size: 12px; line-height: 1.6; }}
    .quant-failures {{ margin-top: 12px; padding: 12px; background: rgba(80, 34, 54, 0.24); border-left: 3px solid #ff8f7f; }}
    .quant-card.approved .quant-failures {{ background: rgba(13, 64, 45, 0.24); border-left-color: #67db7d; }}
    .quant-data-errors {{ margin-top: 9px; padding-top: 9px; border-top: 1px solid rgba(255,255,255,0.08); color: #c8d8ef; font-size: 12px; }}
    .quant-data-errors ul {{ margin-top: 5px; }}
    .quant-note {{ margin-top: 10px; color: #9fb9dd; font-size: 11px; line-height: 1.55; }}

    .fund-card {{
      position: relative;
      background: linear-gradient(180deg, #2a2d92 0%, #2b2c8c 100%);
      border-radius: 16px;
      padding: 16px 18px;
      box-shadow: inset 0 0 0 1px rgba(111, 147, 255, 0.08);
      overflow: hidden;
    }}

    .fund-card::before {{
      content: "";
      position: absolute;
      left: 0;
      top: 0;
      bottom: 0;
      width: 6px;
      background: linear-gradient(180deg, #30a8ff 0%, #1d78ff 100%);
    }}

    .fund-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }}
    .fund-name {{ font-size: 17px; font-weight: 800; line-height: 1.35; }}
    .fund-meta {{ margin-top: 6px; color: var(--muted); font-size: 14px; }}
    .return-strip {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }}
    .return-item {{ min-width: 0; padding: 8px 10px; border: 1px solid rgba(151, 196, 255, 0.16); border-radius: 8px; background: rgba(5, 18, 55, 0.28); }}
    .return-item span {{ display: block; color: #9eb9df; font-size: 11px; }}
    .return-item b {{ display: block; margin-top: 2px; font-size: 17px; }}
    .return-positive {{ color: #ffaaa4; }}
    .return-negative {{ color: #91efa1; }}
    .return-flat, .return-missing {{ color: #c4d4ee; }}
    .delta {{ margin-top: 8px; font-size: 14px; font-weight: 700; color: var(--yellow); }}

    .status {{
      min-width: 108px;
      text-align: center;
      padding: 8px 14px;
      border-radius: 10px;
      font-size: 14px;
      font-weight: 800;
      color: white;
      box-shadow: inset 0 -2px 0 rgba(0,0,0,0.12);
    }}

    .status.green {{ background: #58c948; }}
    .status.yellow {{ background: #f7bf17; color: #694a00; }}
    .status.blue {{ background: #289fff; }}

    .rules {{ margin-top: 14px; display: grid; grid-template-columns: 1.2fr 1fr; gap: 12px 20px; }}
    .rule-box {{ background: rgba(10, 23, 72, 0.17); border-radius: 12px; padding: 12px 14px; border: 1px solid rgba(148, 187, 255, 0.08); }}
    .rule-box summary {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; color: #a6d2ff; font-size: 13px; font-weight: 800; cursor: pointer; list-style: none; }}
    .rule-box summary::-webkit-details-marker {{ display: none; }}
    .rule-box summary::after {{ content: "+"; flex: 0 0 auto; color: #d9e9ff; font-size: 18px; line-height: 1; }}
    .rule-box[open] summary::after {{ content: "−"; }}
    .rule-content {{ min-width: 0; padding-top: 12px; overflow-wrap: anywhere; word-break: break-word; }}
    .levels {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }}
    .pill {{ padding: 6px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; background: rgba(255,255,255,0.08); color: #eff4ff; border: 1px solid rgba(255,255,255,0.08); }}
    .rule-text {{ margin: 6px 0 0; font-size: 13px; line-height: 1.6; color: #edf4ff; }}
    .rule-text.bad {{ color: #ffd3b0; }}
    .rule-text.hint {{ color: #88e4ff; }}

    .compact-grid {{ margin-top: 18px; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }}
    .pick-card {{ background: linear-gradient(180deg, #1f4f98 0%, #194486 100%); border-radius: 16px; padding: 16px; border: 1px solid rgba(134, 199, 255, 0.12); box-shadow: inset 0 0 0 1px rgba(255,255,255,0.03); }}
    .pick-card h3 {{ margin: 0 0 8px; font-size: 16px; font-weight: 800; }}
    .pick-card p, .macro p {{ margin: 0; font-size: 13px; line-height: 1.65; color: #dde8ff; }}

    .news-grid {{ margin-top: 18px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .news-card {{ background: linear-gradient(180deg, #173a7b 0%, #132f66 100%); border-radius: 16px; padding: 16px; border: 1px solid rgba(134, 199, 255, 0.12); }}
    .news-top {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 8px; }}
    .news-signals {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
    .chip {{ display: inline-flex; align-items: center; padding: 4px 10px; border-radius: 999px; background: rgba(57,168,255,0.18); color: #9ed9ff; font-size: 12px; font-weight: 700; }}
    .direction {{ display: inline-flex; align-items: center; padding: 4px 9px; border-radius: 6px; font-size: 12px; font-weight: 800; }}
    .direction.positive {{ background: rgba(89, 201, 79, 0.2); color: #9aef92; }}
    .direction.negative {{ background: rgba(255, 111, 97, 0.2); color: #ffb0a7; }}
    .direction.neutral {{ background: rgba(255, 201, 40, 0.18); color: #ffe28a; }}
    .news-source {{ color: #94baf8; font-size: 12px; }}
    .news-card h3 {{ margin: 0 0 8px; font-size: 15px; line-height: 1.5; }}
    .news-card a {{ color: #ffffff; text-decoration: none; }}
    .news-card a:hover {{ text-decoration: underline; }}
    .news-card p {{ margin: 8px 0 0; font-size: 13px; line-height: 1.65; color: #dbe7ff; }}
    .affected {{ margin-top: 12px; font-size: 12px; font-weight: 800; color: #ffffff; line-height: 1.6; }}
    .impact {{ margin-top: 6px; font-size: 12px; color: #9fd2ff; line-height: 1.6; }}
    .watch {{ margin-top: 6px; padding-top: 8px; border-top: 1px solid rgba(145, 183, 255, 0.12); font-size: 12px; color: #c9d8f4; line-height: 1.6; }}

    .macro {{ margin-top: 18px; background: linear-gradient(180deg, rgba(13, 31, 88, 0.36), rgba(10, 28, 76, 0.24)); border-radius: 18px; padding: 16px 18px; border: 1px solid rgba(151, 191, 255, 0.1); }}
    .macro-list {{ display: grid; gap: 10px; margin-top: 10px; }}
    .macro-item {{ padding-left: 14px; border-left: 3px solid rgba(98, 167, 255, 0.8); }}

    .report {{ margin-top: 18px; background: #0b173f; border-radius: 16px; padding: 18px; border: 1px solid rgba(145, 183, 255, 0.16); color: #edf3ff; font-family: Consolas, "Courier New", monospace; white-space: pre-wrap; line-height: 1.7; font-size: 13px; }}
    .footer {{ margin-top: 16px; font-size: 12px; color: #b6caee; text-align: center; }}

    .dashboard-tabs {{ display: flex; flex-wrap: wrap; gap: 7px; margin: 0 0 16px; padding: 6px; border: 1px solid rgba(166, 207, 255, 0.18); border-radius: 12px; background: rgba(6, 25, 70, 0.32); }}
    .dashboard-tab {{ flex: 1 1 150px; min-height: 40px; padding: 8px 11px; border: 1px solid transparent; border-radius: 8px; background: transparent; color: #c6d7fa; font: inherit; font-size: 12px; font-weight: 800; cursor: pointer; }}
    .dashboard-tab:hover {{ background: rgba(57, 168, 255, 0.16); }}
    .dashboard-tab.active {{ background: #289fff; color: #ffffff; box-shadow: inset 0 -2px 0 rgba(0,0,0,0.12); }}
    .dashboard-tab-panel[hidden] {{ display: none; }}
    .portfolio-config {{ margin: 0 0 14px; padding: 14px; border: 1px solid rgba(151, 196, 255, 0.18); border-radius: 12px; background: rgba(9, 25, 71, 0.34); }}
    .portfolio-config-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: baseline; flex-wrap: wrap; }}
    .portfolio-config-head strong {{ color: #e5f0ff; font-size: 14px; }}
    .portfolio-config-head span {{ color: #aec7e9; font-size: 11px; }}
    .portfolio-config-form {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)) auto; gap: 9px; align-items: end; margin-top: 11px; }}
    .portfolio-config-form label {{ display: block; color: #aec7e9; font-size: 11px; }}
    .portfolio-config-form input {{ width: 100%; min-width: 0; height: 36px; margin-top: 5px; padding: 0 9px; border: 1px solid rgba(151, 196, 255, 0.26); border-radius: 7px; background: rgba(10, 30, 82, 0.52); color: #ffffff; }}
    .portfolio-config-form button {{ height: 36px; padding: 0 13px; border: 1px solid #39a8ff; border-radius: 7px; background: #289fff; color: #ffffff; font: inherit; font-size: 12px; font-weight: 800; cursor: pointer; }}
    .portfolio-config-message {{ min-height: 18px; margin-top: 7px; color: #a9c5eb; font-size: 11px; }}
    .portfolio-user-card {{ display: none; margin: 0 0 12px; padding: 13px; border: 1px dashed rgba(151, 196, 255, 0.28); border-radius: 12px; background: rgba(10, 30, 82, 0.35); }}
    .portfolio-user-card.visible {{ display: block; }}
    .portfolio-user-card .user-card-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }}
    .portfolio-user-card h3 {{ margin: 0; font-size: 16px; }}
    .portfolio-user-card p {{ margin: 5px 0 0; color: #bcd2f1; font-size: 12px; }}
    .portfolio-user-card button {{ border: 0; background: transparent; color: #ffb0a7; font: inherit; font-size: 12px; cursor: pointer; }}
    .portfolio-user-card .user-values {{ display: flex; flex-wrap: wrap; gap: 8px 16px; margin-top: 10px; color: #e7f0ff; font-size: 12px; }}

    @media (max-width: 960px) {{
      body {{ padding: 16px; }}
      .header {{ flex-direction: column; gap: 14px; }}
      .state-box {{ text-align: left; }}
      .portfolio-heading {{ flex-direction: column; }}
      .portfolio-action {{ width: 100%; }}
      .portfolio-metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .quality-heading {{ grid-template-columns: 1fr; }}
      .audit-heading {{ grid-template-columns: 1fr; }}
      .audit-grid {{ grid-template-columns: 1fr; }}
      .audit-block + .audit-block {{ border-left: 0; border-top: 1px solid rgba(124,190,255,.12); }}
      .audit-attempt {{ grid-template-columns: 1fr; gap: 4px; }}
      .warning-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .ai-dimension-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .ai-cycle-head {{ flex-direction: column; }}
      .ai-cycle-meta {{ text-align: left; }}
      .quant-head {{ flex-direction: column; }}
      .quant-verdict {{ width: 100%; }}
      .quant-score-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .quant-evidence-grid {{ grid-template-columns: 1fr; }}
      .rules {{ grid-template-columns: 1fr; }}
      .compact-grid {{ grid-template-columns: 1fr; }}
      .news-grid {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 520px) {{
      body {{ padding: 6px; }}
      .frame {{ border-radius: 12px; }}
      .header {{ padding: 18px 14px 14px; }}
      .header::after {{ left: 14px; right: 14px; }}
      .brand {{ gap: 11px; align-items: flex-start; }}
      .logo {{ width: 42px; height: 42px; flex: 0 0 42px; }}
      .titles h1 {{ font-size: 20px; }}
      .state-box {{ width: 100%; min-width: 0; overflow-wrap: anywhere; }}
      .body {{ padding: 12px 10px 18px; }}
      .portfolio-card, .fund-card, .quant-card {{ padding: 14px 12px; border-radius: 10px; }}
      .portfolio-heading h2 {{ font-size: 17px; overflow-wrap: anywhere; }}
      .portfolio-heading h2 span {{ display: block; margin: 4px 0 0; }}
      .portfolio-action {{ min-width: 0; }}
      .portfolio-metrics {{ grid-template-columns: 1fr; }}
      .metric {{ min-height: 0; }}
      .warning-grid, .quant-score-grid {{ grid-template-columns: 1fr; }}
      .ai-cycle-panel {{ padding: 14px 12px; }}
      .ai-dimension-grid {{ grid-template-columns: 1fr; }}
      .fund-head {{ flex-direction: column; }}
      .status {{ min-width: 0; width: 100%; }}
      .return-strip {{ grid-template-columns: 1fr; }}
      .quality-heading, .quality-confidence-notes {{ padding-left: 12px; padding-right: 12px; }}
      .audit-probe, .audit-priority-row {{ grid-template-columns: minmax(82px, auto) minmax(0, 1fr); }}
      .news-top {{ align-items: flex-start; flex-direction: column; }}
      .report {{ padding: 12px; font-size: 11px; overflow-wrap: anywhere; }}
      .portfolio-config-form {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .portfolio-config-form button {{ width: 100%; }}
    }}
    @media (max-width: 520px) {{
      .dashboard-tabs {{ gap: 5px; padding: 5px; }}
      .dashboard-tab {{ flex-basis: calc(50% - 5px); min-height: 42px; padding: 7px 8px; }}
      .portfolio-config-form {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="frame">
    <div class="header">
      <div class="brand">
        <div class="logo"><span></span></div>
        <div class="titles">
          <h1>{WINDOW_TITLE}</h1>
          <p>{generated_at} · 自动生成基金机会简报 · 本地独立运行</p>
        </div>
      </div>
      <div class="state-box">
        <div class="update-time">本次数据更新时间：{generated_at}</div>
        <div class="update-note">下次更新：重新启动程序时</div>
        <div>{html.escape(telemetry_text)}</div>
        <div>数据状态：{html.escape(data_status)}</div>
        {f'<div class="critical-notice">{html.escape(critical_macro_notice)}</div>' if critical_macro_notice else ''}
        <div>市场阶段：{phase}</div>
      </div>
    </div>

    <div class="body">
      <nav class="dashboard-tabs" role="tablist" aria-label="基金买点观察模块">
        <button class="dashboard-tab active" type="button" role="tab" aria-selected="true" aria-controls="tab-holdings" data-tab-target="tab-holdings">我的持仓</button>
        <button class="dashboard-tab" type="button" role="tab" aria-selected="false" aria-controls="tab-ai" data-tab-target="tab-ai">AI资本开支周期预警</button>
        <button class="dashboard-tab" type="button" role="tab" aria-selected="false" aria-controls="tab-recommend" data-tab-target="tab-recommend">推荐买入 · 综合评分首选与强买监测</button>
        <button class="dashboard-tab" type="button" role="tab" aria-selected="false" aria-controls="tab-target" data-tab-target="tab-target">指定基金分析（未配置）</button>
        <button class="dashboard-tab" type="button" role="tab" aria-selected="false" aria-controls="tab-extra" data-tab-target="tab-extra">额外基金筛选</button>
        <button class="dashboard-tab" type="button" role="tab" aria-selected="false" aria-controls="tab-watch" data-tab-target="tab-watch">关注基金走势影响监测、简明报告</button>
      </nav>

      <section id="tab-holdings" class="dashboard-tab-panel" role="tabpanel">
        <div class="section-title">持仓总览</div>
        {portfolio_config_html}
        {portfolio_html}
      </section>

      <section id="tab-ai" class="dashboard-tab-panel" role="tabpanel" hidden>
        <div class="section-title">AI资本开支周期预警</div>
        {ai_cycle_html}
      </section>

      <section id="tab-recommend" class="dashboard-tab-panel" role="tabpanel" hidden>
        <div class="section-title">推荐买入 · 综合评分首选与强买监测</div>
        {strong_buy_html}
      </section>

      <section id="tab-target" class="dashboard-tab-panel" role="tabpanel" hidden>
        <div class="section-title">指定基金分析（未配置）</div>
        <div class="grid">
          {''.join(target_cards) or '<div class="empty-state">当前未配置指定基金。</div>'}
        </div>
      </section>

      <section id="tab-extra" class="dashboard-tab-panel" role="tabpanel" hidden>
        <div class="section-title">额外基金筛选（基金资格信息仅作辅助参考）</div>
        <div class="compact-grid">
          {''.join(recommendation_cards)}
        </div>
      </section>

      <section id="tab-watch" class="dashboard-tab-panel" role="tabpanel" hidden>
        <div class="section-title">关注基金走势影响监测（来源：东方财富）</div>
        <div class="news-grid">
          {''.join(news_cards)}
        </div>
        <div class="macro">
          <div class="section-title" style="margin:0 0 10px;color:#eef5ff;">市场环境与结论</div>
          <div class="macro-list">
            <div class="macro-item"><p>{summary}</p></div>
            <div class="macro-item"><p>{buy_principle}</p></div>
            <div class="macro-item"><p>{macro}</p></div>
            <div class="macro-item"><p>{macro_coverage_text}</p></div>
            {headlines_html}
          </div>
        </div>
        <div class="section-title" style="margin-top:18px;">简明报告</div>
        <div class="report">{brief_report}</div>
      </section>

      <div class="footer">新闻方向为规则化辅助判断，不构成单独买卖依据。联网受限时会回退到基金相关的离线观察项。</div>

      <div class="bottom-data-results">
        <div class="bottom-data-title">数据状态</div>
        {quality_html}
        {audit_html}
      </div>
    </div>
  </div>
  <script>
    const dashboardTabs = Array.from(document.querySelectorAll(".dashboard-tab"));
    const dashboardPanels = Array.from(document.querySelectorAll(".dashboard-tab-panel"));
    dashboardTabs.forEach((tab) => {{
      tab.addEventListener("click", () => {{
        const target = tab.dataset.tabTarget;
        dashboardTabs.forEach((item) => {{
          const active = item === tab;
          item.classList.toggle("active", active);
          item.setAttribute("aria-selected", String(active));
        }});
        dashboardPanels.forEach((panel) => {{
          panel.hidden = panel.id !== target;
        }});
      }});
    }});

    const portfolioForm = document.getElementById("portfolio-config-form");
    const portfolioUserList = document.getElementById("portfolio-user-list");
    const portfolioMessage = document.getElementById("portfolio-config-message");
    const formatPortfolioMoney = (value) => Number(value).toLocaleString("zh-CN", {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }});
    let portfolioPositions = [];
    const renderUserPositions = (items) => {{
      if (!portfolioUserList) return;
      portfolioUserList.innerHTML = "";
      const renderedCodes = new Set(Array.from(document.querySelectorAll("[data-portfolio-code]")).map((item) => item.dataset.portfolioCode));
      portfolioPositions = (Array.isArray(items) ? items : []).filter((item) => !renderedCodes.has(String(item.code)));
      portfolioPositions.forEach((item) => {{
        const marketValue = Number(item.market_value);
        const pnl = Number(item.pnl);
        const cost = marketValue - pnl;
        const returnPct = cost > 0 ? pnl / cost * 100 : null;
        const days = item.purchase_date ? Math.max(0, Math.floor((Date.now() - new Date(item.purchase_date + "T00:00:00").getTime()) / 86400000)) : null;
        const colorClass = pnl >= 0 ? "profit" : "loss";
        const sign = pnl >= 0 ? "+" : "";
        const card = document.createElement("article");
        card.className = "portfolio-user-card visible";
        card.dataset.code = item.code;
        card.innerHTML = `<div class="user-card-head"><div><h3>${{item.code}} · 用户新增持仓</h3><p>买入日期 ${{item.purchase_date}} · 持有 ${{days === null ? "--" : days + " 天"}}</p></div><button type="button" data-remove-user-position="${{item.code}}">删除</button></div><div class="user-values"><span>当前市值 ¥${{formatPortfolioMoney(marketValue)}}</span><span>推算成本 ¥${{formatPortfolioMoney(cost)}}</span><span class="${{colorClass}}">当前盈亏 ${{sign}}¥${{formatPortfolioMoney(pnl)}}</span><span class="${{colorClass}}">收益率 ${{returnPct === null ? "待计算" : sign + returnPct.toFixed(2) + "%"}}</span><span>状态：等待后端刷新完成（临时状态，尚未形成正式预警）</span></div>`;
        portfolioUserList.appendChild(card);
      }});
    }};
    const loadPortfolioPositions = async () => {{
      try {{
        const response = await fetch("/api/portfolio", {{ cache: "no-store" }});
        if (!response.ok) throw new Error("持仓配置读取失败");
        const payload = await response.json();
        renderUserPositions(Object.entries(payload.positions || {{}}).map(([code, item]) => ({{ code, ...item }})));
        if (payload.error) portfolioMessage.textContent = `重新抓取失败：${{payload.error}}`;
        return payload;
      }} catch (error) {{
        portfolioMessage.textContent = "无法连接本地持仓服务；请使用 run_app.bat 启动程序，不要直接打开 HTML 文件。";
        portfolioMessage.style.color = "#ffb0a7";
        renderUserPositions([]);
        return null;
      }}
    }};
    const pollPortfolioRefresh = () => {{
      let attempts = 0;
      const timer = window.setInterval(async () => {{
        attempts += 1;
        const payload = await loadPortfolioPositions();
        if (!payload || !payload.refreshing || attempts >= 30) {{
          window.clearInterval(timer);
          if (payload && !payload.refreshing && !payload.error) window.location.reload();
        }}
      }}, 2000);
    }};
    if (portfolioForm) {{
      portfolioForm.addEventListener("submit", (event) => {{
        event.preventDefault();
        const code = document.getElementById("portfolio-code").value.trim();
        const marketValue = Number(document.getElementById("portfolio-market-value").value);
        const pnl = Number(document.getElementById("portfolio-pnl").value);
        const purchaseDate = document.getElementById("portfolio-purchase-date").value;
        const cost = marketValue - pnl;
        if (!/^\\d{{6}}$/.test(code) || !Number.isFinite(marketValue) || marketValue <= 0 || !Number.isFinite(pnl) || !/^\\d{{4}}-\\d{{2}}-\\d{{2}}$/.test(purchaseDate) || cost <= 0) {{
          portfolioMessage.textContent = "请填写 6 位基金代码、正数当前市值、有效盈亏金额和买入日期；当前市值必须大于盈亏金额，才能推算正成本。";
          portfolioMessage.style.color = "#ffb0a7";
          return;
        }}
        fetch(`/api/portfolio/${{encodeURIComponent(code)}}`, {{
          method: "PUT",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ market_value: Number(marketValue.toFixed(2)), pnl: Number(pnl.toFixed(2)), purchase_date: purchaseDate }})
        }}).then(async (response) => {{
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.error || "保存失败");
          portfolioMessage.textContent = `${{code}} 已写入项目配置，正在重新抓取并运行正式持仓预警。`;
          portfolioMessage.style.color = "#9aef92";
          portfolioForm.reset();
          renderUserPositions(Object.entries(payload.positions || {{}}).map(([itemCode, item]) => ({{ code: itemCode, ...item }})));
          pollPortfolioRefresh();
        }}).catch((error) => {{
          portfolioMessage.textContent = error.message || "持仓保存失败";
          portfolioMessage.style.color = "#ffb0a7";
        }});
      }});
    }}
    if (portfolioUserList) {{
      portfolioUserList.addEventListener("click", (event) => {{
        const button = event.target.closest("[data-remove-user-position]");
        if (!button) return;
        const code = button.dataset.removeUserPosition;
        fetch(`/api/portfolio/${{encodeURIComponent(code)}}`, {{ method: "DELETE" }}).then(async (response) => {{
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.error || "删除失败");
          portfolioMessage.textContent = `${{code}} 已从项目配置删除，相关账本快照和预警状态正在清理。`;
          portfolioMessage.style.color = "#9aef92";
          renderUserPositions(Object.entries(payload.positions || {{}}).map(([itemCode, item]) => ({{ code: itemCode, ...item }})));
          pollPortfolioRefresh();
        }}).catch((error) => {{
          portfolioMessage.textContent = error.message || "持仓删除失败";
          portfolioMessage.style.color = "#ffb0a7";
        }});
      }});
    }}
    loadPortfolioPositions();

    const params = new URLSearchParams(window.location.search);
    if (params.get("auto-refresh") === "1") {{
      const key = "fund-dashboard-refresh-count";
      const count = Number(sessionStorage.getItem(key) || "0");
      if (count < 6) {{
        sessionStorage.setItem(key, String(count + 1));
        window.setTimeout(() => window.location.reload(), 15000);
      }} else {{
        sessionStorage.removeItem(key);
        window.history.replaceState(null, "", window.location.pathname);
      }}
    }}
  </script>
</body>
</html>
    """


def generate_outputs(output_html: Path | None = None) -> tuple[Path, Path, dict, str]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    reset_source_health()
    fetch_started = time.perf_counter()
    telemetry: dict[str, dict] = {}
    telemetry_lock = threading.Lock()
    portfolio_holdings = load_active_portfolio()
    recommendation_codes = list(RECOMMENDATION_POOL.keys())
    history_codes = list(
        dict.fromkeys(
            [*TARGET_FUNDS, *recommendation_codes, *portfolio_holdings.keys()]
        )
    )
    holding_codes = list(
        dict.fromkeys(
            [*portfolio_holdings.keys(), *TARGET_FUNDS, *recommendation_codes]
        )
    )

    def timed_fetch(name: str, callback):
        started = time.perf_counter()
        try:
            result = callback()
            status = "success"
            return result
        except Exception:
            status = "error"
            raise
        finally:
            with telemetry_lock:
                telemetry[name] = {
                    "status": status,
                    "duration_sec": round(time.perf_counter() - started, 2),
                }

    network_status = timed_fetch("网络预检", run_network_preflight)

    def fetch_fund_core():
        return (
            timed_fetch("基金实时净值", lambda: fetch_fund_quotes(portfolio_holdings.keys())),
            timed_fetch("基金历史净值", lambda: fetch_fund_histories(history_codes)),
        )

    def fetch_market_and_news():
        return (
            timed_fetch("全球指数与宏观", fetch_market_snapshot),
            timed_fetch("基金影响新闻", fetch_finance_news),
            timed_fetch("AI资本开支周期新闻", fetch_ai_cycle_news),
        )

    def fetch_metadata():
        return (
            timed_fetch(
                "基金持仓披露",
                lambda: fetch_fund_holdings(holding_codes, portfolio_holdings.keys()),
            ),
            timed_fetch("推荐资格", fetch_recommendation_qualifications),
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        fund_future = executor.submit(fetch_fund_core)
        market_future = executor.submit(fetch_market_and_news)
        metadata_future = executor.submit(fetch_metadata)
        quotes, histories = fund_future.result()
        market_snapshot, news, ai_cycle_news = market_future.result()
        holdings, recommendation_qualifications = metadata_future.result()

    resolve_fund_navs(quotes, histories)
    apply_quote_based_shares(portfolio_holdings, quotes)
    portfolio_valuation = timed_fetch(
        "持仓账本",
        lambda: refresh_portfolio_ledger(quotes, portfolio_holdings),
    )
    portfolio_for_alerts = {
        code: {
            **config,
            "market_value": (portfolio_valuation.get("positions", {}).get(code) or {}).get(
                "market_value", config.get("market_value_input")
            ) or config.get("market_value_input"),
            "cost_basis": (portfolio_valuation.get("positions", {}).get(code) or {}).get(
                "cost_basis", config.get("cost_basis")
            ) or config.get("cost_basis"),
            "ledger_shares": (portfolio_valuation.get("positions", {}).get(code) or {}).get(
                "shares", config.get("shares")
            ) or config.get("shares"),
        }
        for code, config in portfolio_holdings.items()
    }
    holding_stock_trends = timed_fetch(
        "重仓股行情",
        lambda: fetch_holding_stock_trends(
            {
                code: holdings.get(code, {})
                for code in history_codes
            }
        ),
    )
    # Evidence archival is deliberately non-blocking for the desktop report. A
    # failed archive can never be substituted by cache data for a trading call.
    try:
        point_in_time_increment = timed_fetch(
            "点时证据归档",
            lambda: archive_observations(holding_stock_trends, news),
        )
    except Exception as error:
        LOGGER.exception("点时证据归档失败")
        point_in_time_increment = {
            "status": "error",
            "reason": str(error)[:240],
        }
    point_in_time_state = archive_status()
    fetch_duration = round(time.perf_counter() - fetch_started, 2)
    data_quality = build_data_quality_report(
        TARGET_FUNDS,
        recommendation_codes,
        quotes,
        histories,
        holdings,
        market_snapshot,
        news,
        recommendation_qualifications,
        stock_data=holding_stock_trends,
    )
    portfolio_quality = build_data_quality_report(
        portfolio_holdings.keys(),
        [],
        quotes,
        histories,
        holdings,
        market_snapshot,
        news,
        {},
        stock_data=holding_stock_trends,
    )
    _merge_portfolio_fund_quality(data_quality, portfolio_quality)
    targets = analyze_target_funds(
        quotes,
        histories,
        market_snapshot,
        data_quality,
        holdings,
        news,
        holding_stock_trends,
    )
    recommendations = analyze_recommendations(quotes, recommendation_qualifications)
    market_analysis = analyze_market(market_snapshot)
    scoring_candidates = build_scoring_candidates(
        targets, [asdict(item) for item in recommendations]
    )
    strong_buy = evaluate_strong_buy(
        scoring_candidates,
        quotes,
        histories,
        holdings,
        market_snapshot,
        news,
        data_quality=data_quality,
        stock_trends=holding_stock_trends,
        portfolio_positions=portfolio_for_alerts,
    )
    holding_alerts = evaluate_portfolio_alerts(
        portfolio_for_alerts,
        quotes,
        histories,
        holdings,
        market_snapshot,
        news,
        data_quality=data_quality,
        stock_trends=holding_stock_trends,
        target_signals={item.code: asdict(item) for item in targets},
    )
    ai_cycle_alert = build_ai_cycle_alert(
        [*news, *ai_cycle_news],
        holdings,
    )
    apply_ai_cycle_gates(ai_cycle_alert, holding_alerts, strong_buy)

    report = build_report_payload(
        targets,
        recommendations,
        market_analysis,
        market_snapshot.get("headlines", []),
        strong_buy,
        quotes,
        data_quality,
    )
    report["news"] = [
        {
            "title": item.title,
            "source": item.source,
            "summary": item.summary,
            "link": item.link,
            "tag": item.tag,
            "impact": item.impact,
            "affected_funds": item.affected_funds,
            "direction": item.direction,
            "watch": item.watch,
            "published_at": item.published_at,
        }
        for item in news
    ]
    report["recommendation_qualifications"] = recommendation_qualifications
    report["holding_alerts"] = holding_alerts
    report["ai_cycle_alert"] = ai_cycle_alert
    report["portfolio_ledger"] = portfolio_valuation
    report["portfolio_config"] = portfolio_holdings
    _attach_nav_returns(
        report,
        histories,
        portfolio_valuation.get("positions", {}),
        quotes,
    )
    report["point_in_time_archive"] = {
        "incremental": point_in_time_increment,
        "acceptance": point_in_time_state,
    }
    report["data_acquisition"] = {
        "policy": "严格数据闸门：缓存仅展示，不参与模型计算；关键实时数据未通过时不执行分析。",
        "source_priority": source_priority_snapshot(),
        "network_preflight": network_status,
        "attempts": fetch_audit_snapshot(),
    }
    report["data_acquisition"]["policy"] = (
        "数据按官方/授权源 -> 独立公开源 -> 聚合公开源 -> 带日期缓存依次回退；"
        "基金净值、持仓和账户估值不使用缓存替代实时数据。仅在基金核心数据可信时，"
        "缓存宏观可参与保守预警；任何缓存或静态参考均不触发强烈买入或精确减持。"
    )
    report["fetch_telemetry"] = {
        "total_fetch_sec": fetch_duration,
        "modules": telemetry,
        "source_health": source_health_snapshot(),
        "network_preflight": network_preflight_snapshot(),
    }

    data_status = f"{quality_status_text(data_quality)}；抓取耗时 {fetch_duration:.1f}秒"

    html_path = output_html or REPORTS_DIR / "latest_dashboard.html"
    txt_path = REPORTS_DIR / "latest_report.txt"

    atomic_write_json(REPORT_CACHE_FILE, report)
    atomic_write_text(html_path, render_dashboard(report, data_status))
    atomic_write_text(txt_path, format_brief_report(report))
    return html_path, txt_path, report, data_status


def open_dashboard(html_path: Path, auto_refresh: bool = False) -> None:
    chrome = next((path for path in CHROME_PATHS if path.exists()), None)
    if not chrome:
        return
    profile_dir = REPORTS_DIR / ".chrome_dashboard_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    dashboard_uri = html_path.resolve().as_uri()
    if auto_refresh:
        dashboard_uri += "?auto-refresh=1"
    subprocess.Popen(
        [
            str(chrome),
            f"--user-data-dir={profile_dir.resolve()}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            f"--app={dashboard_uri}",
        ],
        cwd=str(REPORTS_DIR),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def open_dashboard_url(url: str) -> None:
    chrome = next((path for path in CHROME_PATHS if path.exists()), None)
    if not chrome:
        return
    profile_dir = REPORTS_DIR / ".chrome_dashboard_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            str(chrome),
            f"--user-data-dir={profile_dir.resolve()}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            f"--app={url}",
        ],
        cwd=str(REPORTS_DIR),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


_portfolio_refresh_lock = threading.Lock()
_portfolio_refresh_state = {"refreshing": False, "error": None}
_portfolio_refresh_pending = False


def _refresh_after_portfolio_change() -> None:
    global _portfolio_refresh_state, _portfolio_refresh_pending
    with _portfolio_refresh_lock:
        if _portfolio_refresh_state["refreshing"]:
            _portfolio_refresh_pending = True
            return
        _portfolio_refresh_state = {"refreshing": True, "error": None}

    def run() -> None:
        global _portfolio_refresh_state, _portfolio_refresh_pending
        try:
            generate_outputs()
            _portfolio_refresh_state = {"refreshing": False, "error": None}
        except Exception as error:  # pragma: no cover - exercised by live API failures
            LOGGER.exception("持仓变更后的重新抓取失败")
            _portfolio_refresh_state = {"refreshing": False, "error": str(error)[:240]}
        with _portfolio_refresh_lock:
            pending = _portfolio_refresh_pending
            _portfolio_refresh_pending = False
        if pending:
            _refresh_after_portfolio_change()

    threading.Thread(target=run, name="portfolio-refresh", daemon=True).start()


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Small same-origin API used by the local dashboard only."""

    server_version = "FundMonitorLocal/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        LOGGER.info("dashboard api | " + format, *args)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _portfolio_response(self) -> dict:
        config = load_portfolio_config()
        return {
            "positions": config.get("positions", {}),
            "disabled_codes": config.get("disabled_codes", []),
            "refreshing": bool(_portfolio_refresh_state.get("refreshing")),
            "error": _portfolio_refresh_state.get("error"),
        }

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/api/portfolio":
            self._send_json(200, self._portfolio_response())
            return
        if route in {"/", "/latest_dashboard.html"}:
            path = REPORTS_DIR / "latest_dashboard.html"
            try:
                body = path.read_bytes()
            except OSError:
                self._send_json(404, {"error": "看板尚未生成"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json(404, {"error": "not found"})

    def do_PUT(self) -> None:  # noqa: N802
        route = urlparse(self.path).path.rstrip("/")
        prefix = "/api/portfolio/"
        if not route.startswith(prefix):
            self._send_json(404, {"error": "not found"})
            return
        code = unquote(route[len(prefix):])
        try:
            payload = self._read_json()
            upsert_portfolio_position(
                code,
                payload.get("market_value"),
                payload.get("pnl"),
                payload.get("purchase_date"),
            )
            _refresh_after_portfolio_change()
        except (ValueError, json.JSONDecodeError) as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(202, {"ok": True, **self._portfolio_response()})

    def do_DELETE(self) -> None:  # noqa: N802
        route = urlparse(self.path).path.rstrip("/")
        prefix = "/api/portfolio/"
        if not route.startswith(prefix):
            self._send_json(404, {"error": "not found"})
            return
        try:
            delete_portfolio_position(unquote(route[len(prefix):]))
            _refresh_after_portfolio_change()
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(202, {"ok": True, **self._portfolio_response()})


def serve_dashboard(port: int, no_open: bool = False) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardRequestHandler)
    url = f"http://127.0.0.1:{port}/latest_dashboard.html"
    LOGGER.info("本地看板服务启动：%s", url)
    if not no_open:
        open_dashboard_url(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _run_once(args: argparse.Namespace, output_html: Path | None, log_path: Path) -> None:
    existing_html = output_html or REPORTS_DIR / "latest_dashboard.html"
    opened_existing_dashboard = False
    if not args.no_open and existing_html.exists() and not args.serve:
        open_dashboard(existing_html, auto_refresh=True)
        opened_existing_dashboard = True

    try:
        html_path, txt_path, report, data_status = generate_outputs(output_html)
    except Exception:
        LOGGER.exception("本次数据刷新失败")
        raise

    summary = {
        "generated_at": report.get("generated_at"),
        "data_status": data_status,
        "html": str(html_path),
        "text": str(txt_path),
        "phase": report.get("market", {}).get("phase", "--"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    quality = report.get("data_quality", {})
    LOGGER.info(
        "刷新完成 | generated_at=%s | data_status=%s | gate=%s | score=%s",
        summary["generated_at"],
        data_status,
        quality.get("overall_gate", "--"),
        quality.get("overall_score", "--"),
    )
    for item in quality.get("records", []):
        LOGGER.info(
            "数据项 | %s | status=%s | source=%s | date=%s | error=%s",
            item.get("label") or item.get("name") or item.get("key", "--"),
            item.get("status", "--"),
            item.get("source", "--"),
            item.get("as_of", "--"),
            item.get("error", ""),
        )
    LOGGER.info("运行日志：%s", log_path)

    if not args.no_open and not opened_existing_dashboard and not args.serve:
        open_dashboard(html_path)


def main() -> None:
    log_path = configure_run_logging()
    LOGGER.info("基金监测程序启动")
    parser = argparse.ArgumentParser(description="生成基金分析仪表盘")
    parser.add_argument("--no-open", action="store_true", help="只生成文件，不自动打开浏览器")
    parser.add_argument("--html", help="自定义 HTML 输出路径")
    parser.add_argument("--serve", action="store_true", help="启动本地看板服务，启用持仓配置 API")
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="以本地桌面应用方式启动：生成看板并在独立窗口中打开",
    )
    parser.add_argument("--port", type=int, default=8767, help="本地看板服务端口")
    args = parser.parse_args()
    if args.desktop:
        args.serve = True
    output_html = Path(args.html).resolve() if args.html else None

    try:
        with single_instance_lock(CACHE_DIR / "fund_monitor_app.lock"):
            _run_once(args, output_html, log_path)
            if args.serve:
                serve_dashboard(args.port, args.no_open)
    except AlreadyRunningError:
        LOGGER.warning("基金监测程序已经在运行，本次启动已跳过")


if __name__ == "__main__":
    main()
