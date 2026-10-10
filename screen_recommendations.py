from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from fetcher import _fetch_recommendation_qualification, _short_fetch_error


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = BASE_DIR / "reports" / "recommendation_screening.json"

# Research universe only. Passing funds must still be copied into the production
# recommendation pool before they can appear in the dashboard.
CANDIDATES = {
    "017730": ("嘉实全球产业升级股票发起式(QDII)A", "全球"),
    "100055": ("富国全球科技互联网股票(QDII)A", "全球"),
    "000043": ("嘉实美国成长股票人民币", "美股"),
    "000988": ("嘉实全球互联网股票人民币", "全球"),
    "001668": ("汇添富全球移动互联混合(QDII)人民币A", "全球"),
    "004877": ("汇添富全球医疗混合(QDII)人民币", "全球"),
    "012535": ("博时全球成长一年持有期混合(QDII)A", "全球"),
    "320017": ("诺安全球收益不动产(QDII)A", "全球"),
    "206011": ("鹏华美国房地产(QDII)", "美股"),
    "006308": ("汇添富全球消费混合(QDII)人民币A", "全球"),
    "006373": ("国富全球科技互联混合(QDII)人民币A", "全球"),
    "006555": ("浦银安盛全球智能科技(QDII)A", "全球"),
    "070031": ("嘉实全球房地产(QDII)", "全球"),
    "501226": ("鹏华全球新能源股票发起式(QDII)A", "全球"),
    "378006": ("摩根全球新兴市场混合(QDII)", "全球"),
    "486002": ("工银全球精选股票(QDII)", "全球"),
    "270023": ("广发全球精选股票(QDII)人民币A", "全球"),
    "000041": ("华夏全球股票(QDII)(人民币)", "全球"),
    "010644": ("富国全球健康生活主题混合(QDII)人民币A", "全球"),
    "015204": ("嘉实全球新能源汽车股票(QDII)A", "全球"),
    "007729": ("浦银安盛全球消费(QDII)人民币A", "全球"),
    "017653": ("汇添富全球芯片产业股票发起式(QDII)A", "全球"),
    "017436": ("华宝纳斯达克精选股票发起式(QDII)A", "美股"),
    "016664": ("嘉实全球高端装备股票(QDII)A", "全球"),
    "378546": ("摩根全球天然资源混合(QDII)A", "全球"),
    "008253": ("华宝致远混合(QDII)A", "全球"),
    "008284": ("易方达全球医药行业混合发起式(QDII)A", "全球"),
    "009108": ("富国红利精选混合(QDII)人民币", "全球"),
    "012060": ("富国全球消费精选混合(QDII)人民币A", "全球"),
    "016199": ("汇添富全球汽车产业升级混合(QDII)人民币A", "全球"),
    "012920": ("易方达全球成长精选混合(QDII)人民币A", "全球"),
    "013328": ("嘉实全球价值股票(QDII)人民币", "全球"),
    "017429": ("嘉实全球创新龙头股票(QDII)人民币A", "全球"),
    "018155": ("创金合信全球医药生物股票发起(QDII)A", "全球"),
}


def _screen_one(code: str, name: str, scope: str) -> dict:
    meta = {
        "name": name,
        "investment_scope": scope,
        "is_global_or_us_focus": True,
    }
    try:
        return _fetch_recommendation_qualification(code, meta)
    except Exception as error:
        return {
            "code": code,
            "name": name,
            "source": "missing",
            "eligible": False,
            "reasons": ["候选资格抓取失败"],
            "error": _short_fetch_error(error),
        }


def main() -> None:
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    results = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_screen_one, code, name, scope): code
            for code, (name, scope) in CANDIDATES.items()
        }
        for future in as_completed(futures):
            result = future.result()
            results[result["code"]] = result

    ordered = [results[code] for code in CANDIDATES]
    eligible = [item for item in ordered if item.get("eligible")]
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "started_at": started_at,
        "criteria": {
            "one_day_limit": "> 1000",
            "rank_percentile": "近36个月月末同类百分位均值 <= 50%",
            "manager_type": "主动管理",
            "investment_scope": "全球或美股",
        },
        "eligible_count": len(eligible),
        "eligible_codes": [item["code"] for item in eligible],
        "funds": ordered,
    }
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
