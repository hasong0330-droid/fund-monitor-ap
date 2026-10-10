import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
REPORTS_DIR = BASE_DIR / "reports"
LOG_DIR = BASE_DIR / "logs"
HOLDING_ALERT_CALIBRATION_FILE = CACHE_DIR / "holding_alert_calibration.json"

WINDOW_TITLE = "基金买点观察"
WINDOW_SIZE = "1180x860"
MIN_RECOMMENDATION_DAILY_LIMIT = 500

TARGET_FUNDS = []
TARGET_FUND_NAMES = {}

# Personal defaults live in the ignored local file portfolio_defaults.py.
# A clean checkout remains usable and starts with no preloaded holdings.
try:
    from portfolio_defaults import PORTFOLIO_HOLDINGS
except ImportError:
    PORTFOLIO_HOLDINGS = {}

FUND_PROFILES = {
    "016702": {
        "theme": "全球数字经济",
        "style": "量化成长",
        "index_focus": "美股数字经济与科技龙头",
        "one_day_limit": 50000,
        "is_global_or_us_focus": True,
    },
    "378006": {
        "theme": "全球新兴市场",
        "style": "新兴市场成长",
        "index_focus": "亚洲及全球新兴市场龙头",
        "one_day_limit": 50000,
        "is_global_or_us_focus": True,
    },
    "018229": {
        "theme": "全球优质成长",
        "style": "均衡成长",
        "index_focus": "全球消费升级与龙头科技",
        "one_day_limit": 50000,
        "is_global_or_us_focus": True,
    },
    "005534": {
        "theme": "A股成长",
        "style": "灵活配置",
        "index_focus": "先进制造、成长白马",
        "one_day_limit": 100000,
        "is_global_or_us_focus": False,
    },
    "161815": {
        "theme": "全球通胀对冲",
        "style": "商品与资源周期",
        "index_focus": "全球能源、金属与通胀资产",
        "one_day_limit": 50000,
        "is_global_or_us_focus": True,
    },
    "378546": {
        "theme": "全球天然资源",
        "style": "商品与资源周期",
        "index_focus": "全球能源、矿业与原材料龙头",
        "one_day_limit": 50000,
        "is_global_or_us_focus": True,
    },
    "270023": {
        "theme": "全球精选",
        "style": "全球均衡成长",
        "index_focus": "全球龙头、科技与消费",
        "one_day_limit": 50000,
        "is_global_or_us_focus": True,
    },
}

RECOMMENDATION_POOL = {
    "017653": {
        "name": "汇添富全球芯片产业股票发起式(QDII)A",
        "theme": "全球半导体产业",
        "style": "主动行业主题",
        "investment_scope": "全球",
        "is_global_or_us_focus": True,
        "one_day_limit": 100000,
        "manager_type": "主动管理",
        "rank_percentile": 46.39,
        "rank_desc": "近36个月月均同类百分位46.39%",
        "reason": "资格信息仅供参考；行业集中度较高，仍需结合防御模型和数据可信度后才可形成买入信号。",
    },
}

INDEX_SYMBOLS = {
    "IXIC": "纳斯达克综合指数",
    "DJI": "道琼斯工业指数",
    "^HSI": "恒生指数",
    "000300.SS": "沪深300",
}

EASTMONEY_INDEX_SECIDS = {
    "IXIC": "100.NDX",
    "DJI": "100.DJIA",
    "^HSI": "100.HSI",
    "000300.SS": "1.000300",
}

TENCENT_INDEX_SYMBOLS = {
    "IXIC": "us.IXIC",
    "DJI": "us.DJI",
    "^HSI": "hkHSI",
    "000300.SS": "sh000300",
}

# Weighted benchmark baskets. The first item is the primary proxy.
FUND_BENCHMARKS = {
    "016702": (("IXIC", 0.75), ("DJI", 0.25)),
    "378006": (("^HSI", 0.55), ("IXIC", 0.25), ("DJI", 0.20)),
    "270023": (("IXIC", 0.55), ("DJI", 0.45)),
    "161815": (("DJI", 0.55), ("IXIC", 0.45)),
    "378546": (("DJI", 0.70), ("^HSI", 0.30)),
    "018229": (("IXIC", 0.55), ("DJI", 0.45)),
    "005534": (("000300.SS", 1.00),),
}

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://fund.eastmoney.com/",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

FUND_QUOTE_URL = "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFInfo"
EASTMONEY_JS_URL = "https://fundgz.1234567.com.cn/js/{code}.js"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
NASDAQ_INDEX_HISTORY_URL = "https://api.nasdaq.com/api/quote/{symbol}/historical"
YAHOO_FUNDAMENTALS_URL = "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{symbol}"
EASTMONEY_INDEX_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT_INDEX_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_US_KLINE_URL = "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20{symbol}=/US_MinKService.getDailyK"
SINA_FOREX_KLINE_URL = "https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/var%20_{symbol}=/NewForexService.getDayKLine"
CBOE_VIX_HISTORY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
US_TREASURY_YIELD_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
NY_FED_EFFR_URL = "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/180.json"
BEA_NIPA_ANNUAL_URL = "https://apps.bea.gov/national/Release/TXT/NipaDataA.txt"
EASTMONEY_FINANCIALS_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
TRADING_ECONOMICS_INDICATOR_URLS = {
    "US_CPI": "https://tradingeconomics.com/united-states/inflation-cpi",
    "US_UNEMPLOYMENT": "https://tradingeconomics.com/united-states/unemployment-rate",
    "US_M2": "https://tradingeconomics.com/united-states/money-supply-m2",
}
NAVER_FINANCIAL_URL = "https://finance.naver.com/item/main.naver?code={code}"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_MACRO_SERIES = {
    "US2Y": ("DGS2", "美国2年期国债收益率"),
    "US10Y": ("DGS10", "美国10年期国债收益率"),
    "US10Y_REAL": ("DFII10", "美国10年期实际利率"),
    "VIX": ("VIXCLS", "VIX恐慌指数"),
    "DXY": ("DTWEXBGS", "美元贸易加权指数"),
    "USDCNY": ("DEXCHUS", "美元兑人民币"),
    "US_GDP": ("A191RL1Q225SBEA", "美国实际GDP环比折年率"),
    "US_CPI": ("CPIAUCSL", "美国CPI指数"),
    "US_UNEMPLOYMENT": ("UNRATE", "美国失业率"),
    "US_M2": ("M2SL", "美国M2货币供应量"),
    "US_FED_FUNDS": ("FEDFUNDS", "美国联邦基金有效利率"),
    "US_HY_SPREAD": ("BAMLH0A0HYM2", "美国高收益债信用利差"),
    "US_CREDIT_STRESS_PROXY": ("", "美国信用压力代理（HYG/LQD）"),
}
# 经济数据不是日频行情，必须按各自频率计算变化，避免把20个月误当成20个交易日。
FRED_MACRO_META = {
    "US_GDP": {"frequency": "quarterly", "value_type": "rate", "lookback": 1},
    "US_CPI": {"frequency": "monthly", "value_type": "level", "lookback": 12},
    "US_UNEMPLOYMENT": {"frequency": "monthly", "value_type": "rate", "lookback": 3},
    "US_M2": {"frequency": "monthly", "value_type": "level", "lookback": 12},
    "US_FED_FUNDS": {"frequency": "monthly", "value_type": "rate", "lookback": 3},
    "US_HY_SPREAD": {"frequency": "daily", "value_type": "spread_pct", "lookback": 20},
    "US_CREDIT_STRESS_PROXY": {
        "frequency": "daily",
        "value_type": "stress_proxy",
        "lookback": 60,
    },
}
# 指数优先使用中文财经源，失败后切换国际备用源。
INDEX_SOURCE_ORDER = ("tencent", "eastmoney")
RESEARCH_REFERENCE_SOURCE_ORDER = (
    "yahoo",
    "yahoo-stock",
    "yahoo-fundamentals",
)
# 基金净值先取东方财富批量/逐只接口，再依次切换 iFinD 和公开历史净值。
FUND_NAV_SOURCE_ORDER = (
    "eastmoney",
    "ifind-fund-nav",
    "eastmoney-pingzhongdata",
    "cache",
)
FUND_HOLDING_SOURCE_ORDER = (
    "eastmoney-holdings",
    "sina-holdings",
    "ths-holdings",
    "cache",
)

# 每类数据的实时来源顺序。cache 只用于展示历史结果，不属于分析来源。
SOURCE_PRIORITY = {
    "fund_nav": (
        "fund-manager",
        "eastmoney",
        "ifind-fund-nav",
        "eastmoney-pingzhongdata",
        "cache",
        "baseline",
    ),
    "fund_history": ("fund-manager-history", "eastmoney-history", "eastmoney-pingzhongdata", "cache", "baseline"),
    "fund_holdings": ("eastmoney-holdings", "sina-holdings", "ths-holdings", "cache", "baseline"),
    "indexes": ("tencent-index", "eastmoney-index", "cache", "baseline"),
    "macro": ("bea", "ny-fed", "tradingeconomics", "sina-credit-etf", "fred", "bls", "world-bank", "eastmoney-macro", "cache", "baseline"),
    "fundamentals": ("sec-companyfacts", "ifind-financials", "eastmoney-financials", "naver-financials", "cache", "baseline"),
    "news": ("finance-news", "investoday-news", "cache", "baseline"),
    "recommendation_qualification": (
        "eastmoney-qualification",
        "multi-source-qualification",
        "cache",
        "baseline",
    ),
}

# The policy is broader than the currently installed public-source adapters.
# Optional licensed and official-provider entries are reserved for future
# credentials/adapters; they are skipped rather than masquerading as live data.
DATA_SOURCE_POLICY = {
    "fund_nav": {
        "order": (
            "fund-manager",
            "eastmoney",
            "ifind-fund-nav",
            "eastmoney-pingzhongdata",
            "ths-nav",
            "cache",
            "baseline",
        ),
    },
    "fund_history": {
        "order": (
            "fund-manager-history",
            "eastmoney-history",
            "eastmoney-pingzhongdata",
            "ths-history",
            "cache",
            "baseline",
        ),
    },
    "fund_holdings": {
        "order": (
            "fund-manager-report",
            "eastmoney-holdings",
            "sina-holdings",
            "ths-holdings",
            "cache",
            "baseline",
        ),
    },
    "indexes": {
        "order": (
            "licensed-index",
            "official-index",
            "tencent-index",
            "eastmoney-index",
            "cache",
            "baseline",
        ),
    },
    "fx": {
        "order": (
            "cfets",
            "licensed-fx",
            "sina-forex",
            "tencent-fx",
            "eastmoney-macro",
            "fred",
            "cache",
            "baseline",
        ),
    },
    "macro": {
        "order": (
            "bea",
            "bls",
            "fed-board",
            "ny-fed",
            "us-treasury",
            "cboe",
            "fred",
            "ifind-edb",
            "tradingeconomics",
            "sina-credit-etf",
            "world-bank",
            "eastmoney-macro",
            "cache",
            "baseline",
        ),
    },
    "fundamentals": {
        "order": (
            "sec-companyfacts",
            "hkex-filings",
            "cninfo",
            "dart",
            "ifind-financials",
            "eastmoney-financials",
            "naver-financials",
            "cache",
            "baseline",
        ),
    },
    "holding_stock_trends": {
        "order": (
            "licensed-stock",
            "official-stock",
            "tencent-stock",
            "sina-stock",
            "eastmoney-stock",
            "cache",
            "baseline",
        ),
    },
    "news": {
        "order": (
            "issuer-ir",
            "regulator-news",
            "licensed-news",
            "eastmoney-news",
            "sina-news",
            "investoday-news",
            "ifind-news",
            "gdelt-news",
            "cache",
            "baseline",
        ),
    },
    "recommendation_qualification": {
        "order": (
            "fund-manager-qualification",
            "galaxy-rank",
            "morningstar-rank",
            "eastmoney-qualification",
            "sina-profile",
            "cache",
            "baseline",
        ),
    },
}

INVESTODAY_MCP_URL = os.environ.get(
    "FUND_MONITOR_INVESTODAY_MCP_URL",
    "https://data-api.investoday.net/data/mcp/preset",
).strip()
INVESTODAY_MCP_API_KEY_ENV = "FUND_MONITOR_INVESTODAY_MCP_API_KEY"

FUND_CACHE_FILE = CACHE_DIR / "fund_quotes.json"
FUND_BROWSER_CACHE_FILE = CACHE_DIR / "browser_fund_quotes.json"
AI_CYCLE_NEWS_CACHE_FILE = CACHE_DIR / "ai_cycle_news.json"
FUND_HISTORY_CACHE_FILE = CACHE_DIR / "fund_history.json"
FUND_HOLDINGS_CACHE_FILE = CACHE_DIR / "fund_holdings.json"
MARKET_CACHE_FILE = CACHE_DIR / "market_snapshot.json"
HOLDING_STOCK_TRENDS_CACHE_FILE = CACHE_DIR / "holding_stock_trends.json"
SEC_TICKERS_CACHE_FILE = CACHE_DIR / "sec_company_tickers.json"
POINT_IN_TIME_ARCHIVE_FILE = CACHE_DIR / "point_in_time_archive.json"
POINT_IN_TIME_AUDIT_FILE = CACHE_DIR / "point_in_time_audit.jsonl"
POINT_IN_TIME_PANEL_FILE = CACHE_DIR / "backtest_point_in_time.json"
REPORT_CACHE_FILE = CACHE_DIR / "latest_report.json"
NEWS_CACHE_FILE = CACHE_DIR / "finance_news.json"
RECOMMENDATION_QUALIFICATION_CACHE_FILE = CACHE_DIR / "recommendation_qualifications.json"
QUANT_SIGNAL_FILE = CACHE_DIR / "quant_signal.json"
PORTFOLIO_LEDGER_FILE = CACHE_DIR / "portfolio_ledger.json"
PORTFOLIO_SNAPSHOTS_FILE = CACHE_DIR / "portfolio_snapshots.json"
PORTFOLIO_POSITIONS_FILE = CACHE_DIR / "portfolio_positions.json"
HOLDING_ALERT_STATE_FILE = CACHE_DIR / "holding_alert_state.json"
FUND_HISTORY_URL = "https://api.fund.eastmoney.com/f10/lsjz"
FUND_HISTORY_BACKUP_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
# Optional official fund-manager adapters. Leave unset until an approved
# provider endpoint is configured; normal public fallbacks remain active.
FUND_MANAGER_NAV_URL_TEMPLATE = os.environ.get(
    "FUND_MONITOR_FUND_MANAGER_NAV_URL_TEMPLATE", ""
).strip()
FUND_MANAGER_HISTORY_URL_TEMPLATE = os.environ.get(
    "FUND_MONITOR_FUND_MANAGER_HISTORY_URL_TEMPLATE", ""
).strip()
FUND_FEE_PAGE_URL = "https://fundf10.eastmoney.com/jjfl_{code}.html"
FUND_HOLDINGS_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
SINA_FUND_HOLDINGS_URL = "https://stock.finance.sina.com.cn/fundInfo/api/openapi.php/StockFinanceService.getFundStockHoldings"
SINA_FUND_PROFILE_URL = "https://stock.finance.sina.com.cn/fundInfo/api/openapi.php/FundPageInfoService.tabjjgk"
THS_FUND_HOLDINGS_URL = "https://fund.10jqka.com.cn/{code}/"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
EASTMONEY_NEWS_SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"
GDELT_DOC_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
EASTMONEY_FUND_NEWS_QUERIES = [
    "美联储",
    "英伟达",
    "港股互联网",
    "高端制造",
    "人民币汇率",
    "出口管制",
]

AI_CYCLE_NEWS_QUERIES = [
    "云厂商 资本开支 下调",
    "GPU HBM 交期 缩短",
    "DRAM NAND 存储价格 下跌",
    "AI收入 折旧 融资成本",
    "芯片 龙头 盈利指引 下调",
]

# Historical news records carry a topic tag, while their source-side fund
# labels only cover the currently monitored funds. This derived mapping keeps
# the wider research universe from being silently excluded from point-in-time
# news evidence. It is applied only to the panel; the immutable source event
# and its original affected-fund labels remain unchanged.
POINT_IN_TIME_NEWS_FUND_GROUPS = {
    "美元利率": (
        "000041", "000043", "000934", "000988", "001668", "002230", "002379",
        "003243", "004877", "005534", "005698", "005699", "006282", "006308",
        "006373", "006555", "007280", "007455", "008253", "008284", "009108",
        "012060", "012920", "013328", "017429", "017437", "018155", "018229",
        "270023", "486002",
    ),
    "AI与科技龙头": (
        "000041", "000043", "000934", "000988", "001668", "005534", "005698",
        "005699", "006373", "006555", "007455", "008253", "012920", "013328",
        "017429", "017437", "018229", "270023", "486002",
    ),
    "港股互联网": (
        "000934", "000988", "001668", "002230", "002379", "003243", "005699",
        "006308", "007455", "012060", "012920", "013328", "017429", "018229",
        "270023", "486002",
    ),
    "先进制造": (
        "000934", "001668", "002230", "002379", "003243", "005534", "006282",
        "006308", "006373", "006555", "007455", "012060", "012920", "013328",
        "017429", "018229", "270023", "486002",
    ),
    "汇率变化": (
        "000041", "000043", "000934", "000988", "001668", "002230", "002379",
        "003243", "004877", "005534", "005698", "005699", "006282", "006308",
        "006373", "006555", "007280", "007455", "008253", "008284", "009108",
        "012060", "012920", "013328", "017429", "017437", "018155", "018229",
        "270023", "486002",
    ),
    "政策与地缘风险": (
        "000041", "000043", "000934", "000988", "001668", "002230", "002379",
        "003243", "004877", "005534", "005698", "005699", "006282", "006308",
        "006373", "006555", "007280", "007455", "008253", "008284", "009108",
        "012060", "012920", "013328", "017429", "017437", "018155", "018229",
        "270023", "486002",
    ),
}

NEWS_SOURCES = [
    {
        "name": "新浪财经",
        "kind": "json",
        "url": "https://feed.mix.sina.com.cn/api/roll/get?pageid=155&lid=1686&num=50&page=1",
    },
    {
        "name": "东方财富",
        "kind": "json",
        "url": "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?client=web&biz=web_news_col&order=1&needInteractData=0&pageSize=50&fields=title,mediaName,summary,code,showTime,url&column=345",
    },
]

NEWS_KEYWORD_RULES = [
    {
        "keywords": [
            "资本开支", "资本支出", "GPU交期", "HBM交期", "交付周期",
            "存储价格", "DRAM", "NAND", "HBM", "AI收入", "折旧压力",
            "融资成本", "下调指引", "指引下修",
        ],
        "tag": "AI资本开支周期",
        "funds": ["016702", "378006", "270023", "018229", "005534", "002891"],
        "impact": "云厂商投入、算力交付、存储价格与AI商业回报共同决定AI产业链盈利周期。",
        "watch": "需跨来源验证资本开支、交期、价格、AI回报和盈利指引，单一信号不触发红色。",
        "priority": 8,
    },
    {
        "keywords": ["美联储", "降息", "加息", "美国通胀", "美国CPI", "美国非农", "美债", "美国国债收益率"],
        "tag": "美元利率",
        "funds": ["016702", "378006", "270023", "018229", "005534", "002891"],
        "impact": "美元利率直接影响全球成长股估值；收益率下行通常利好科技QDII，快速上行则压制净值。",
        "watch": "重点看美国10年期国债收益率、降息预期和纳斯达克走势是否同向。",
        "priority": 5,
    },
    {
        "keywords": ["英伟达", "微软", "苹果", "谷歌", "亚马逊", "Meta", "台积电", "博通", "AI", "人工智能", "算力", "芯片", "半导体", "云计算"],
        "tag": "AI与科技龙头",
        "funds": ["016702", "378006", "270023", "018229", "005534", "002891"],
        "impact": "核心科技公司业绩、资本开支和订单预期会直接改变全球科技基金的盈利与估值空间。",
        "watch": "重点看财报收入指引、AI资本开支、毛利率及半导体订单是否超预期。",
        "priority": 6,
    },
    {
        "keywords": ["港股", "恒生科技", "腾讯", "阿里", "美团", "京东", "小米", "港股互联网"],
        "tag": "港股互联网",
        "funds": [],
        "impact": "港股互联网龙头的盈利修复、回购和监管变化会影响亚洲精选基金的主要弹性。",
        "watch": "重点看恒生科技成交额、南向资金、平台公司盈利和回购强度。",
        "priority": 5,
    },
    {
        "keywords": ["机器人", "高端制造", "新能源", "储能", "光伏", "电动车", "汽车零部件", "工业自动化"],
        "tag": "先进制造",
        "funds": ["005534"],
        "impact": "制造业订单、价格和政策支持会影响A股成长与亚洲制造持仓的盈利预期。",
        "watch": "重点看行业订单、产能利用率、价格竞争和政策落地后的盈利改善。",
        "priority": 4,
    },
    {
        "keywords": ["人民币汇率", "美元指数", "人民币升值", "人民币贬值", "外汇"],
        "tag": "汇率变化",
        "funds": ["016702", "378006", "270023", "161815", "378546", "018229", "005534"],
        "impact": "人民币汇率会影响QDII折算收益；人民币快速升值可能抵消部分海外资产涨幅。",
        "watch": "同时观察美元指数和人民币中间价，区分资产上涨与汇率折算贡献。",
        "priority": 4,
    },
    {
        "keywords": ["出口管制", "芯片禁令", "科技制裁", "贸易战", "关税", "地缘冲突", "中美关系"],
        "tag": "政策与地缘风险",
        "funds": ["016702", "378006", "270023", "161815", "378546", "018229", "005534"],
        "impact": "贸易限制和地缘冲突可能同时冲击科技供应链、亚洲风险偏好和全球权益估值。",
        "watch": "确认政策是否落地、涉及公司范围以及供应链是否出现收入或成本端实质影响。",
        "priority": 6,
    },
]
