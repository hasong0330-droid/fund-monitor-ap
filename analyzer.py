import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Dict, List

from config import FUND_BENCHMARKS, RECOMMENDATION_POOL, TARGET_FUND_NAMES
from fetcher import FundQuote, IndexSnapshot
from data_quality import CONSERVATIVE_FALLBACK, READY
from decision_gate import build_decision_gate
from factor_model import evaluate_fund_factors
from source_router import live_source_names


@dataclass
class FundDecision:
    code: str
    name: str
    nav: float
    est_nav: float | None
    nav_date: str
    data_source: str
    status: str
    status_level: int
    target_range: str
    buy_levels: List[str]
    trigger: str
    dont_buy: str
    method: str
    summary: str
    valuation_hint: str
    rule_date: str
    rule_basis: str
    analysis_mode: str = "STANDARD"
    factor_model: dict | None = None
    decision_gate: dict | None = None


@dataclass
class RecommendationDecision:
    code: str
    name: str
    nav: float
    one_day_limit: int
    manager_type: str
    investment_scope: str
    rank_desc: str
    reason: str
    fit_for: str


TARGET_CONTEXT = {
    "016702": {
        "method": "数字经济回撤分批",
        "summary": "全球数字经济量化成长基金，适合作为科技卫星仓并等待估值回撤后分批。",
        "risk": "科技盈利预期下修、量化风格失效或美债利率快速上行",
    },
    "378006": {
        "method": "新兴市场回撤分批",
        "summary": "覆盖全球新兴市场龙头，重点观察亚洲增长、美元与资金流向。",
        "risk": "美元快速走强、新兴市场资金外流或核心经济体盈利下修",
    },
    "270023": {
        "method": "全球精选回撤分批",
        "summary": "全球龙头均衡配置，买点同时参考科技、消费与美元利率环境。",
        "risk": "全球龙头盈利下修、成长估值收缩或美元利率快速上行",
    },
    "161815": {
        "method": "通胀对冲分批",
        "summary": "全球商品与资源周期工具，重点观察实际利率、美元和能源金属价格的共振。",
        "risk": "实际利率快速上行、大宗商品价格转弱或美元持续走强",
    },
    "378546": {
        "method": "天然资源回撤分批",
        "summary": "聚焦全球能源、矿业与原材料公司，用于观察资源周期与通胀交易机会。",
        "risk": "大宗商品价格转弱、美元持续走强或资源企业盈利明显下修",
    },
    "018229": {
        "method": "定投+回撤分批",
        "summary": "全球均衡成长底仓，买点同时参考全球龙头科技与消费趋势。",
        "risk": "全球消费龙头盈利连续下修或美元利率快速上行",
    },
    "005534": {
        "method": "A股成长轻仓",
        "summary": "主要依赖主动选股超额，适合作为A股成长卫星仓位。",
        "risk": "先进制造订单转弱、基金风格漂移或规模持续明显缩水",
    },
}

FUND_INDEX_MAP = {
    "016702": ("IXIC", "纳斯达克"),
    "378006": ("^HSI", "恒生指数（新兴市场代理）"),
    "270023": ("IXIC", "纳斯达克"),
    "161815": ("DJI", "道琼斯工业指数（全球周期代理）"),
    "378546": ("DJI", "道琼斯工业指数（资源周期代理）"),
    "018229": ("IXIC", "纳斯达克"),
    "005534": ("000300.SS", "沪深300"),
}


def analyze_target_funds(
    quotes: Dict[str, FundQuote],
    histories: Dict[str, dict] | None = None,
    market_snapshot: Dict[str, object] | None = None,
    data_quality: dict | None = None,
    holdings: Dict[str, dict] | None = None,
    news: list[object] | None = None,
    stock_data: Dict[str, dict] | None = None,
) -> List[FundDecision]:
    histories = histories or {}
    market_snapshot = market_snapshot or {}
    data_quality = data_quality or {}
    holdings = holdings or {}
    news = news or []
    factors_enabled = stock_data is not None
    stock_data = stock_data or {}
    index_map = {
        item.get("symbol"): item for item in market_snapshot.get("indexes", [])
    }
    decisions: List[FundDecision] = []

    for code, name in TARGET_FUND_NAMES.items():
        quote = quotes.get(code)
        history = histories.get(code) or {}
        records = sorted(history.get("records", []), key=lambda item: item["date"])
        context = TARGET_CONTEXT[code]
        quality_item = data_quality.get("funds", {}).get(code, {})
        decision_gate = build_decision_gate(quality_item, data_quality)
        analysis_mode = decision_gate["mode"]
        if len(records) < 60 and analysis_mode == CONSERVATIVE_FALLBACK:
            nav = float(quote.nav) if quote and quote.nav else 0.0
            nav_date = (
                str(history.get("latest_date") or getattr(quote, "date", "--"))
            )
            decisions.append(
                FundDecision(
                    code=code,
                    name=name,
                    nav=nav,
                    est_nav=quote.est_nav if quote else None,
                    nav_date=nav_date,
                    data_source=_history_source_text(history) if history else _quote_source_text(quote),
                    status="保守基线观察",
                    status_level=1,
                    target_range=f"参考净值 {nav:.4f}" if nav > 0 else "静态参考基线",
                    buy_levels=[],
                    trigger="实时源和缓存历史均不可用，已展示静态参考基线；本次不生成精确买入节点。",
                    dont_buy="静态基线期间不新增买入；待实时源或历史缓存恢复后重新计算买入区间。",
                    method=context["method"],
                    summary=context["summary"],
                    valuation_hint="该数值是明确标记的静态参考，不是最新净值，也不构成交易依据。",
                    rule_date=nav_date,
                    rule_basis="数据回退顺序：实时主源 -> 实时备用源 -> 缓存 -> 静态参考基线。",
                    analysis_mode=CONSERVATIVE_FALLBACK,
                    factor_model=None,
                    decision_gate=decision_gate,
                )
            )
            continue
        if len(records) < 60:
            nav = float(quote.nav) if quote else 0.0
            decisions.append(
                FundDecision(
                    code=code,
                    name=name,
                    nav=nav,
                    est_nav=quote.est_nav if quote else None,
                    nav_date=quote.date if quote else "--",
                    data_source=_quote_source_text(quote),
                    status="数据待更新",
                    status_level=1,
                    target_range="--",
                    buy_levels=[],
                    trigger="缺少至少60个有效净值日，暂不生成每日买入节点。",
                    dont_buy="历史净值数据未完成更新前不执行新增买入。",
                    method=context["method"],
                    summary=context["summary"],
                    valuation_hint="当前仅展示净值，不提供统计买点判断。",
                    rule_date="--",
                    rule_basis="数据不足，未生成动态规则",
                    decision_gate=decision_gate,
                )
            )
            continue

        quality_gate = decision_gate["gate"]
        conservative_delay = decision_gate["mode"] == "CONSERVATIVE_DELAYED"
        conservative_fallback = analysis_mode == CONSERVATIVE_FALLBACK
        conservative_mode = conservative_delay or conservative_fallback
        if not decision_gate["can_analyze"] and not conservative_mode:
            latest = records[-1]
            reasons = quality_item.get("reasons", [])
            reason_text = "；".join(dict.fromkeys(reasons)) or "关键数据未通过来源与时效校验。"
            decisions.append(
                FundDecision(
                    code=code,
                    name=name,
                    nav=float(latest["nav"]),
                    est_nav=quote.est_nav if quote else None,
                    nav_date=str(latest["date"]),
                    data_source=_history_source_text(history),
                    status="等待实时数据",
                    status_level=1,
                    target_range="--",
                    buy_levels=[],
                    trigger=f"{reason_text} 分析未执行，不生成买入节点。",
                    dont_buy="关键实时数据未按来源顺序完整获取前，不执行新增买入。",
                    method=context["method"],
                    summary=context["summary"],
                    valuation_hint="当前净值仅供查看，不构成交易依据。",
                    rule_date=str(latest["date"]),
                    rule_basis=f"数据闸门：{quality_item.get('gate_label', '仅供观察')}",
                    factor_model=None,
                    decision_gate=decision_gate,
                )
            )
            continue

        factor_evaluation = evaluate_fund_factors(
            code,
            history,
            holdings.get(code),
            market_snapshot,
            news,
            stock_data,
        )
        fundamental_coverage = factor_evaluation["coverage"]["fundamental"]
        fundamental_applicable = factor_evaluation["details"]["fundamental"].get(
            "applicable", True
        )

        navs = [float(item["nav"]) for item in records if float(item["nav"]) > 0]
        latest = records[-1]
        nav = float(latest["nav"])
        window_count = min(120, len(navs))
        window = navs[-window_count:]
        index_symbol, index_name = FUND_INDEX_MAP[code]
        benchmark_specs = FUND_BENCHMARKS.get(code, ((index_symbol, 1.0),))
        benchmark_snapshots = [
            {**index_map.get(symbol, {}), "weight": weight}
            for symbol, weight in benchmark_specs
            if index_map.get(symbol)
        ]
        constraint = _buy_range_constraint(
            holdings.get(code), benchmark_snapshots, str(latest["date"])
        )
        factor_block_reason = ""
        if factors_enabled and fundamental_applicable and fundamental_coverage < 0.60:
            factor_block_reason = (
                f"企业财报覆盖 {fundamental_coverage:.1%}，低于执行门槛60%。"
            )
        elif factors_enabled and factor_evaluation["hard_veto"]:
            factor_block_reason = factor_evaluation["hard_veto_reason"]
        elif factors_enabled and factor_evaluation["total_score"] < 50:
            factor_block_reason = (
                f"五因子综合评分 {factor_evaluation['total_score']:.1f}，低于执行门槛50。"
            )
        if factor_block_reason:
            constraint = {
                **constraint,
                "observe_only": True,
                "reason": factor_block_reason,
                "dont_buy": f"{factor_block_reason} 不执行新增买入；",
                "basis": "五因子基本面/风险闸门触发仅观察",
            }
        if conservative_mode:
            constraint = _conservative_range_constraint(constraint)
        low = _percentile(window, constraint["low_percentile"])
        high = _percentile(window, constraint["high_percentile"])
        near = _percentile(window, constraint["near_percentile"])
        chase_line = _percentile(window, constraint["chase_percentile"])
        if high <= low:
            high = low * 1.02
        if near <= high:
            near = high * 1.02
        if chase_line <= near:
            chase_line = near * 1.02

        returns = [
            navs[index] / navs[index - 1] - 1
            for index in range(max(1, len(navs) - 120), len(navs))
        ]
        annual_volatility = (
            statistics.pstdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
        )
        momentum_20 = nav / navs[-21] - 1
        momentum_long = nav / navs[-window_count] - 1
        drawdown_60 = nav / max(navs[-60:]) - 1
        status, level = _status_by_nav(nav, low, high, near)
        if constraint["observe_only"]:
            status, level = (
                ("基本面待补全", 1)
                if factors_enabled
                and fundamental_applicable
                and fundamental_coverage < 0.60
                else ("仅观察", 1)
            )
        if conservative_mode and status != "仅观察":
            status = f"{status}（保守）"
        benchmark_name = " / ".join(
            str(item.get("name") or item.get("symbol"))
            for item in benchmark_snapshots
        ) or index_name
        index_trigger, index_risk = _index_rule(benchmark_name, benchmark_snapshots)

        if momentum_20 > 0.08:
            momentum_trigger = (
                f"基金近20日上涨 {momentum_20:.1%}，等待动量降至8%以内后再判断。"
            )
        elif momentum_20 < -0.08:
            momentum_trigger = (
                f"基金近20日下跌 {abs(momentum_20):.1%}，进入区间后还需连续两个净值日不创新低。"
            )
        else:
            momentum_trigger = (
                f"基金近20日动量 {momentum_20:+.1%}，进入区间后可按三档执行。"
            )

        trigger = f"单位净值进入 {low:.4f}-{high:.4f}；{index_trigger} {momentum_trigger}"
        if constraint["observe_only"]:
            trigger = (
                f"当前仅观察，不执行买入；参考区间 {low:.4f}-{high:.4f}。"
                f"{constraint['reason']}"
            )
        elif conservative_mode:
            fallback_note = (
                "实时源已回退至缓存或静态参考，"
                if conservative_fallback
                else "正常交易日/QDII净值披露延迟，"
            )
            trigger = (
                f"{fallback_note}采用保守区间 {low:.4f}-{high:.4f}；"
                f"{index_trigger} {momentum_trigger}"
            )
        dont_buy = (
            f"净值高于动态追涨线 {chase_line:.4f} 不买；{index_risk}；"
            f"{constraint['dont_buy']}若出现{context['risk']}，暂停新增仓位。"
        )
        midpoint = (low + high) / 2
        source_text = _history_source_text(history)
        decisions.append(
            FundDecision(
                code=code,
                name=name,
                nav=nav,
                est_nav=quote.est_nav if quote else None,
                nav_date=str(latest["date"]),
                data_source=source_text,
                status=status,
                status_level=level,
                target_range=f"{low:.4f} - {high:.4f}",
                buy_levels=(
                    []
                    if constraint["observe_only"]
                    else [
                        f"{high:.4f}{'保守试仓' if conservative_mode else '试仓'}",
                        f"{midpoint:.4f}{'保守加仓' if conservative_mode else '加仓'}",
                        f"{low:.4f}{'保守补仓' if conservative_mode else '补仓'}",
                    ]
                ),
                trigger=trigger,
                dont_buy=dont_buy,
                method=context["method"],
                summary=context["summary"],
                valuation_hint=(
                    "该区间是净值价格位置与持仓风险形成的安全边际代理，不是企业基本面估值。"
                    + (
                        "当前为缓存/基线回退保守模式。"
                        if conservative_fallback
                        else ("当前为正常QDII延迟保守模式。" if conservative_delay else "")
                    )
                    + f"前十大持仓集中度 {constraint['concentration']:.1%}，"
                    f"披露距规则日 {constraint['disclosure_age']} 天；"
                    f"近20日动量 {momentum_20:+.1%}，近{window_count - 1}日动量 {momentum_long:+.1%}，"
                    f"距60日高点 {drawdown_60:.1%}，年化波动率 {annual_volatility:.1%}。"
                ),
                rule_date=str(latest["date"]),
                rule_basis=(
                    f"{source_text}；近{window_count}个净值日"
                    f"{constraint['low_percentile']:.0%}-{constraint['high_percentile']:.0%}分位动态区间；"
                    f"{constraint['basis']}；分析模式={quality_item.get('analysis_mode', 'STANDARD')}"
                ),
                analysis_mode=analysis_mode,
                factor_model=factor_evaluation,
                decision_gate=decision_gate,
            )
        )
    return decisions


def _buy_range_constraint(
    holding: dict | None, benchmark_snapshots: list[dict], as_of: str
) -> dict:
    items = (holding or {}).get("holdings", [])[:10]
    concentration = sum(float(item.get("weight", 0)) for item in items)
    disclosure_age = _disclosure_age((holding or {}).get("disclosure_date", ""), as_of)
    gaps = [float(item.get("ma60_gap_pct", 0)) for item in benchmark_snapshots]
    benchmark_divergent = len(gaps) > 1 and max(gaps) > 2 and min(gaps) < -2
    total_weight = sum(float(item.get("weight", 1.0)) for item in benchmark_snapshots) or 1.0
    benchmark_ma60 = sum(
        float(item.get("ma60_gap_pct", 0)) * float(item.get("weight", 1.0))
        for item in benchmark_snapshots
    ) / total_weight

    result = {
        "low_percentile": 0.20,
        "high_percentile": 0.40,
        "near_percentile": 0.60,
        "chase_percentile": 0.80,
        "observe_only": False,
        "concentration": concentration,
        "disclosure_age": disclosure_age,
        "reason": "",
        "dont_buy": "",
        "basis": "持仓与基准风险未触发额外折价",
    }
    if (holding or {}).get("allocation_only"):
        result.update(
            observe_only=True,
            low_percentile=0.10,
            high_percentile=0.25,
            near_percentile=0.40,
            chase_percentile=0.60,
            reason="该基金披露的是资产配置而非个股前十大持仓，保留资产层面的保守观察区间。",
            dont_buy="资产配置披露不足以验证企业暴露时不追买；",
            basis="资产配置披露，仅保留价格观察区间",
        )
    elif not items or disclosure_age >= 999:
        result.update(
            observe_only=True,
            low_percentile=0.10,
            high_percentile=0.25,
            near_percentile=0.40,
            chase_percentile=0.60,
            reason="持仓披露缺失，无法验证集中度与企业暴露。",
            dont_buy="持仓披露缺失时不买；",
            basis="持仓披露缺失，仅保留价格观察区间",
        )
    elif concentration >= 0.70 or disclosure_age > 120:
        reason = (
            f"前十大持仓集中度 {concentration:.1%} 过高。"
            if concentration >= 0.70
            else f"持仓披露已过去 {disclosure_age} 天。"
        )
        result.update(
            observe_only=True,
            low_percentile=0.10,
            high_percentile=0.25,
            near_percentile=0.40,
            chase_percentile=0.60,
            reason=reason,
            dont_buy="持仓高度集中或披露超过120天时不买；",
            basis="高集中度/旧披露触发仅观察与更深折价",
        )
    elif concentration >= 0.55 or disclosure_age > 90 or benchmark_divergent or benchmark_ma60 < -3:
        result.update(
            low_percentile=0.15,
            high_percentile=0.30,
            near_percentile=0.50,
            chase_percentile=0.65,
            dont_buy="持仓集中、披露偏旧或基准偏弱时仅接受更深折价；",
            basis="持仓/基准风险触发收窄区间与追涨线下调",
        )
    return result


def _conservative_range_constraint(base: dict) -> dict:
    result = dict(base)
    result.update(
        low_percentile=max(0.10, float(base["low_percentile"]) - 0.05),
        high_percentile=max(0.20, float(base["high_percentile"]) - 0.10),
        near_percentile=max(0.35, float(base["near_percentile"]) - 0.10),
        chase_percentile=max(0.55, float(base["chase_percentile"]) - 0.10),
        basis=f"{base['basis']}；正常QDII延迟触发保守收窄",
        dont_buy=f"正常QDII延迟期间不追涨；{base['dont_buy']}",
    )
    return result


def _disclosure_age(disclosure_date: str, as_of: str) -> int:
    try:
        return max(
            0,
            (datetime.fromisoformat(as_of).date() - datetime.fromisoformat(disclosure_date).date()).days,
        )
    except (TypeError, ValueError):
        return 999


def _percentile(values: List[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _status_by_nav(nav: float, low: float, high: float, near: float) -> tuple[str, int]:
    if low <= nav <= high:
        return ("可分批", 3)
    if nav < low:
        return ("深度回撤区", 4)
    if nav <= near:
        return ("接近买点", 2)
    return ("等待回撤", 1)


def _index_rule(index_name: str, snapshots: list[dict] | dict) -> tuple[str, str]:
    if isinstance(snapshots, dict):
        snapshots = [snapshots] if snapshots else []
    if not snapshots:
        return (
            f"{index_name}数据不足，需人工确认指数没有快速下跌。",
            f"{index_name}数据缺失时不追涨",
        )
    total_weight = sum(float(item.get("weight", 1.0)) for item in snapshots) or 1.0
    ma20_gap = sum(
        float(item.get("ma20_gap_pct", 0)) * float(item.get("weight", 1.0))
        for item in snapshots
    ) / total_weight
    ma60_gap = sum(
        float(item.get("ma60_gap_pct", 0)) * float(item.get("weight", 1.0))
        for item in snapshots
    ) / total_weight
    raw_gaps = [float(item.get("ma60_gap_pct", 0)) for item in snapshots]
    if len(raw_gaps) > 1 and max(raw_gaps) > 2 and min(raw_gaps) < -2:
        return (
            f"{index_name}基准走势出现明显分歧，先等待多个基准方向收敛。",
            "多基准方向不一致时不追涨",
        )
    if ma20_gap > 5:
        return (
            f"{index_name}高于20日均线 {ma20_gap:.1f}%，需先回落到5%以内。",
            f"{index_name}短线过热时不买",
        )
    if ma60_gap < -3:
        return (
            f"{index_name}低于60日均线 {abs(ma60_gap):.1f}%，需先出现止跌修复。",
            f"{index_name}继续远离60日均线时不抄底",
        )
    return (
        f"{index_name}相对20日/60日均线为 {ma20_gap:+.1f}%/{ma60_gap:+.1f}%，趋势未处极端区。",
        f"{index_name}跌破60日趋势且回撤继续扩大时不买",
    )


def _history_source_text(history: dict) -> str:
    if history.get("source") == "eastmoney-history":
        return "东方财富最新历史净值"
    if history.get("source") == "cache":
        return "历史净值缓存"
    if history.get("source") in {"fallback", "baseline"}:
        return "静态参考基线"
    return "未知净值来源"


def _quote_source_text(quote: FundQuote | None) -> str:
    if not quote:
        return "无净值数据"
    if quote.source in {"eastmoney", "eastmoney-history"}:
        return "东方财富最新净值"
    if quote.source == "cache":
        return "净值缓存"
    if quote.source in {"fallback", "baseline"}:
        return "静态参考基线"
    return "离线备用净值"


def analyze_recommendations(
    quotes: Dict[str, FundQuote], qualifications: Dict[str, dict]
) -> List[RecommendationDecision]:
    candidates: List[RecommendationDecision] = []
    for code, meta in RECOMMENDATION_POOL.items():
        quote = quotes.get(code)
        qualification = qualifications.get(code) or {}
        direct_candidate = bool(meta.get("direct_candidate"))
        allowed_sources = {"eastmoney", "eastmoney-js", "eastmoney-pingzhongdata"}
        if direct_candidate:
            allowed_sources |= {"cache", "fallback", "baseline"}
        if not quote or quote.source not in allowed_sources or quote.nav <= 0:
            continue
        candidates.append(
            RecommendationDecision(
                code=code,
                name=qualification.get("name") or meta["name"],
                nav=quote.nav if quote else 0.0,
                one_day_limit=int(qualification.get("one_day_limit", meta.get("one_day_limit", 0)) or 0),
                manager_type=qualification.get("manager_type", meta.get("manager_type", "主动管理")),
                investment_scope=qualification.get("investment_scope", meta.get("investment_scope", "全球")),
                rank_desc=qualification.get("rank_desc", meta.get("rank_desc", "用户指定直入备选池")),
                reason=meta["reason"],
                fit_for=_fit_for_text(meta["theme"], meta["style"]),
            )
        )

    candidates.sort(key=lambda item: (item.code, -item.one_day_limit))
    return candidates


def _fit_for_text(theme: str, style: str) -> str:
    return f"适合补充{theme}暴露，风格偏{style}。"


def analyze_market(index_payload: Dict[str, object]) -> Dict[str, str]:
    raw_indexes = index_payload.get("indexes", [])
    live_index_sources = live_source_names("indexes")
    supported_index_sources = live_index_sources | {"cache", "fallback", "baseline"}
    required_index_fields = ("price", "change_pct", "ma20_gap_pct", "ma60_gap_pct")
    complete_indexes = [
        item
        for item in raw_indexes
        if item.get("source") in supported_index_sources
        and all(isinstance(item.get(field), (int, float)) for field in required_index_fields)
    ]
    indexes = [IndexSnapshot(**item) for item in complete_indexes]
    macro_factors = {
        item.get("key"): item for item in index_payload.get("macro_factors", [])
    }
    live_macro_sources = live_source_names("macro")
    supported_macro_sources = live_macro_sources | {"cache", "fallback", "baseline"}
    required_macro_keys = (
        "VIX", "US2Y", "US10Y", "US10Y_REAL", "DXY", "USDCNY",
        "US_GDP", "US_CPI", "US_UNEMPLOYMENT", "US_M2", "US_FED_FUNDS",
    )
    credit_key = (
        "US_HY_SPREAD"
        if isinstance(macro_factors.get("US_HY_SPREAD", {}).get("value"), (int, float))
        and not macro_factors.get("US_HY_SPREAD", {}).get("proxy")
        else "US_CREDIT_STRESS_PROXY"
    )
    required_macro_keys = (*required_macro_keys, credit_key)

    def macro_fields_ready(key: str) -> bool:
        item = macro_factors.get(key, {})
        required_fields = item.get("required_fields") or (
            ["value", "change_12_pct"] if key == "US_M2" else ["value"]
        )
        return bool(
            all(isinstance(item.get(field), (int, float)) for field in required_fields)
            and item.get("semantic_status", "COMPLETE")
            not in {"INCOMPLETE", "BACKGROUND_ONLY"}
        )

    missing_macro = [
        key
        for key in required_macro_keys
        if not macro_fields_ready(key)
        or macro_factors.get(key, {}).get("source") not in supported_macro_sources
    ]
    missing_indexes = [
        str(item.get("symbol", "未知指数"))
        for item in raw_indexes
        if item not in complete_indexes
    ]
    if missing_macro or missing_indexes:
        return {
            "phase": "保守数据回退观察",
            "summary": "已展示可用的回退数据，但仍有部分关键字段无法获得；不据此生成精确的全球趋势交易判断。",
            "buy_principle": "保守模式下暂停新增仓位，待实时源或可验证缓存恢复后重新计算买入节点。",
            "macro": (
                "待补齐的宏观字段："
                + "、".join(missing_macro or ["无"])
                + "；指数："
                + "、".join(missing_indexes or ["无"])
            ),
        }
    fallback_in_use = any(
        item.get("source") not in live_index_sources for item in complete_indexes
    ) or any(
        macro_factors.get(key, {}).get("source") not in live_macro_sources
        for key in required_macro_keys
    ) or macro_factors.get(credit_key, {}).get("semantic_status") == "PROXY"
    vix = float(macro_factors["VIX"]["value"])
    us2y = float(macro_factors["US2Y"]["value"])
    us10y = float(macro_factors["US10Y"]["value"])
    real10y = float(macro_factors["US10Y_REAL"]["value"])
    dxy_change = float(macro_factors["DXY"].get("change_20_pct") or 0)
    us10y_change = float(macro_factors["US10Y"].get("change_20") or 0)
    gdp = macro_factors.get("US_GDP", {})
    cpi = macro_factors.get("US_CPI", {})
    unemployment = macro_factors.get("US_UNEMPLOYMENT", {})
    m2 = macro_factors.get("US_M2", {})
    credit_factor = macro_factors.get(credit_key, {})
    economic_keys = ("US_GDP", "US_CPI", "US_UNEMPLOYMENT", "US_M2")
    economic_available = sum(
        macro_fields_ready(key)
        and macro_factors.get(key, {}).get("semantic_status", "COMPLETE") == "COMPLETE"
        for key in economic_keys
    )
    credit_available = (
        macro_fields_ready(credit_key)
        and macro_factors.get(credit_key, {}).get("semantic_status", "COMPLETE")
        in {"COMPLETE", "PROXY"}
    )
    # OAS and its ETF proxy are one economic category; never count both.
    economic_available += int(credit_available)

    def macro_text(key: str, field: str = "value", sign: bool = False) -> str:
        value = macro_factors.get(key, {}).get(field)
        if not isinstance(value, (int, float)):
            return "--"
        return f"{float(value):{'+.2f' if sign else '.2f'}}"

    if economic_available < 4:
        phase = "宏观经济数据不完整，处于保守观察阶段"
        buy_principle = "先补齐增长、通胀、就业和流动性数据；数据不完整时不扩大仓位。"
    elif (
        vix >= 30
        or real10y >= 2.2
        or us2y >= 5.0
        or float(credit_factor.get("value") or 0)
        >= (6.0 if credit_key == "US_HY_SPREAD" else 4.0)
        or float(gdp.get("value") or 0) < -1.0
    ):
        phase = "全球宏观处于高利率或风险压力阶段"
        buy_principle = "基本面优质标的也应降低仓位，等待利率或风险指标回落。"
    elif (
        us10y_change <= -0.25
        and vix < 22
        and dxy_change <= 1
        and float(gdp.get("value") or 0) >= 1.0
        and 1.0 <= float(cpi.get("change_12_pct") or 0) <= 3.5
        and float(unemployment.get("value") or 99) <= 5.5
        and float(m2.get("change_12_pct") or -99) > 0
    ):
        phase = "全球流动性边际改善阶段"
        buy_principle = "基本面与行业景气通过后，可在技术回撤节点分批配置。"
    else:
        phase = "全球宏观处于中性观察阶段"
        buy_principle = "优先选择盈利增长和现金流扎实的基金，技术面只用于分批择时。"
    ixic = next((item for item in indexes if item.symbol == "IXIC"), None)
    hsi = next((item for item in indexes if item.symbol == "^HSI"), None)
    hs300 = next((item for item in indexes if item.symbol == "000300.SS"), None)

    us_view = "美股科技维持中期上行结构。"
    if ixic and ixic.ma60_gap_pct > 8:
        us_view = "美股科技偏热，适合等回撤，而不是追高。"
    elif ixic and ixic.ma20_gap_pct < -3:
        us_view = "美股科技正在回撤修复，更适合试仓而非一次性重仓。"

    hk_view = "港股处于震荡修复阶段。"
    if hsi and hsi.ma20_gap_pct > 2:
        hk_view = "港股短线偏强，但不建议大阳线后追入。"
    elif hsi and hsi.ma20_gap_pct < -2:
        hk_view = "港股短线承压，先等止跌信号更稳。"

    a_view = "A股更依赖主动管理的超额收益。"
    if hs300 and hs300.ma60_gap_pct > 4:
        a_view = "A股处于温和修复段，主动基金更容易做出超额。"

    return {
        "phase": f"保守回退：{phase}" if fallback_in_use else phase,
        "summary": (
            "实时源已按优先顺序回退至缓存或静态参考，以下判断仅供保守观察。 "
            if fallback_in_use
            else ""
        ) + " ".join([us_view, hk_view, a_view]),
        "buy_principle": (
            "回退模式下不追高、不触发强烈买入；仅在既有计划内小额观察。 "
            if fallback_in_use
            else ""
        ) + buy_principle,
        "macro": (
            f"VIX {vix:.2f}，美国2年/10年国债收益率 {us2y:.2f}%/{us10y:.2f}%，"
            f"10年实际利率 {real10y:.2f}%，美元20日变化 {dxy_change:+.2f}%；"
            f"实际GDP {macro_text('US_GDP', sign=True)}%、CPI同比 "
            f"{macro_text('US_CPI', 'change_12_pct')}%、失业率 "
            f"{macro_text('US_UNEMPLOYMENT')}%、M2同比 "
            f"{macro_text('US_M2', 'change_12_pct', sign=True)}%、"
            f"{('高收益债 OAS' if credit_key == 'US_HY_SPREAD' else 'HYG/LQD 信用压力代理')} "
            f"{macro_text(credit_key)}%。"
        ),
    }


def build_report_payload(
    target_decisions: List[FundDecision],
    recommendations: List[RecommendationDecision],
    market_analysis: Dict[str, str],
    headlines: List[str],
    strong_buy: Dict[str, object] | None = None,
    quotes: Dict[str, FundQuote] | None = None,
    data_quality: dict | None = None,
) -> Dict[str, object]:
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "targets": [asdict(item) for item in target_decisions],
        "recommendations": [asdict(item) for item in recommendations],
        "market": market_analysis,
        "headlines": headlines,
        "strong_buy": strong_buy or {},
        "data_quality": data_quality or {},
        "recommendation_qualifications": (data_quality or {}).get(
            "recommendation_qualifications", {}
        ),
    }
    return report


def format_brief_report(report: Dict[str, object]) -> str:
    def return_summary(item: dict) -> str:
        def value_text(value: object) -> str:
            return f"{float(value):+.2f}%" if isinstance(value, (int, float)) else "数据待更新"
        return f"日涨幅：{value_text(item.get('daily_return_pct'))} · 月涨幅：{value_text(item.get('monthly_return_pct'))}"

    lines: List[str] = []
    lines.append(f"生成时间：{report.get('generated_at', '--')}")
    lines.append(f"市场阶段：{report.get('market', {}).get('phase', '--')}")
    lines.append(f"市场结论：{report.get('market', {}).get('buy_principle', '--')}")
    quality = report.get("data_quality", {})
    lines.append(
        f"数据可信度：{quality.get('overall_score', 0):.1f}/100 · "
        f"{quality.get('overall_gate_label', '禁止判断')} · {quality.get('summary', '--')}"
    )
    lines.append("")
    targets = report.get("targets", [])
    lines.append(f"{len(targets)}只基金每日动态结论：")
    for item in targets:
        factor = item.get("factor_model") or {}
        scores = factor.get("scores", {})
        fundamental_detail = factor.get("details", {}).get("fundamental", {})
        fundamental_text = (
            "财报覆盖不适用（FOF资产配置）"
            if fundamental_detail.get("applicable") is False
            else f"财报覆盖{float(factor.get('coverage', {}).get('fundamental', 0)):.1%}"
        )
        factor_text = (
            " | 五因子："
            f"基本面{float(scores.get('fundamental', 0)):.1f}/"
            f"宏观{float(scores.get('macro', 0)):.1f}/"
            f"技术资金{float(scores.get('technical', 0)):.1f}/"
            f"行业{float(scores.get('industry', 0)):.1f}/"
            f"事件{float(scores.get('other', 0)):.1f}"
            f" | {fundamental_text}"
            if factor
            else ""
        )
        lines.append(
            f"{item['code']} {item['name']} | 规则日：{item.get('rule_date', '--')} | "
            f"状态：{item['status']} | 动态区间：{item['target_range']}"
            f" | {return_summary(item)}{factor_text} | 不买：{item['dont_buy']}"
        )
    lines.append("")
    alerts = report.get("holding_alerts", {})
    alert_model = next(iter(alerts.values()), {}).get(
        "model_version", "Fund Guard"
    )
    lines.append(f"持仓预警（{alert_model}）：")
    if alerts:
        for code, alert in alerts.items():
            score = alert.get("risk_score")
            score_text = f"{float(score):.1f}" if isinstance(score, (int, float)) else "--"
            lines.append(
                f"{code} {alert.get('name', '--')} | 等级：{alert.get('level_label', '--')} | "
                f"风险分：{score_text} | 动作：{alert.get('action', '--')} | "
                f"依据：{'; '.join(alert.get('reasons', [])[:2])}"
            )
    else:
        lines.append("暂无持仓预警数据。")
    lines.append("")
    lines.append("推荐基金：")
    for item in report.get("recommendations", []):
        lines.append(
            f"{item['code']} {item['name']} | {return_summary(item)} | "
            f"限额：{item['one_day_limit']} | 排名：{item['rank_desc']}"
        )
    strong_buy = report.get("strong_buy", {})
    lines.append("")
    lines.append(
        f"{strong_buy.get('model_version', 'GAF-QM')} 量化强买监测（样本外观察期）："
    )
    if strong_buy.get("is_strong_buy"):
        lines.append(
            f"强烈建议买入：{strong_buy.get('code', '--')} {strong_buy.get('name', '--')} | "
            f"综合评分：{strong_buy.get('total_score', 0):.1f} | "
            f"数据置信度：{strong_buy.get('confidence', 0):.1f}%"
        )
    elif strong_buy.get("is_recommended") and strong_buy.get("code"):
        lines.append(
            f"综合评分首选（当前暂缓执行）：{strong_buy.get('code')} "
            f"{strong_buy.get('name')} | 综合评分：{strong_buy.get('total_score', 0):.1f} | "
            f"数据置信度：{strong_buy.get('confidence', 0):.1f}% | {return_summary(strong_buy)}"
        )
        for reason in strong_buy.get("failed_conditions", [])[:4]:
            lines.append(f"未通过：{reason}")
    else:
        lines.append("当前无强烈建议买入标的。")
        for reason in strong_buy.get("failed_conditions", [])[:4]:
            lines.append(f"未通过：{reason}")
    return "\n".join(lines)
