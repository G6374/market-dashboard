#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场动态看板 · 数据生成脚本

更新时间：北京时间每日 24:00（即次日 00:00，GitHub Actions cron = 0 16 * * *）
目标日期：本次运行所归属的"当天"。例如 10月19日24:00（=10月20日00:00）运行，
          目标日期记为 2026-10-19，只抓取【2026-10-19 发布】的资讯。

硬性规则（用户明确、不可违背）：
1. 只保留"发布日期 == 目标日期"的资讯；抓取不到发布日期、或日期不符的，一律丢弃。
   绝不追溯、绝不杜撰、绝不混入陈旧（如几个月前）的旧闻。
2. 每条动态在写回前必须做可达性自检：无法访问（连接失败/404/410/5xx 等）的链接
   一律剔除，保证页面上不出现"打不开"的来源。
3. 外文（英文等）标题与要点统一翻译为书面化中文；先筛选本板块关注的要点，
   再提炼每条 2-3 句中文重点，源链接仅作参考、不得改动。
4. 相关性：无论官方厂商还是行业媒体，标题必须命中本板块/本区域关键词才保留；
   无关内容（客户案例、活动招募、招聘、非目标厂商等）一律剔除。
5. 优先级：官网/厂商官方消息优先；若当日官方无新资讯，再从其他权威渠道（行业媒体）抓取。
6. 国内外分列：存储、Token、算力均分"国外 / 国内"两组，分别展示、分别把关。

仅改写 index.html 中 <!--MARKET-DATA-START--> ~ <!--MARKET-DATA-END--> 之间的 JSON，
绝不改动页面其它代码。
"""
import os, re, json, sys, datetime, calendar
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
import feedparser
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")

# ---- LLM 供应商：主用 DeepSeek（需有余额），失败回退 SiliconFlow 免费模型（按能力从强到弱）----
PRIMARY = (
    os.environ.get("LLM_API_URL", "https://api.deepseek.com/v1/chat/completions"),
    os.environ.get("LLM_API_KEY", ""),
    os.environ.get("LLM_MODEL", "deepseek-chat"),
)
_SF_URL = os.environ.get("FALLBACK_API_URL", "https://api.siliconflow.cn/v1/chat/completions")
_SF_KEY = os.environ.get("FALLBACK_API_KEY", "")
FALLBACKS = [
    (_SF_URL, _SF_KEY, os.environ.get("FALLBACK_MODEL", "Qwen/Qwen2.5-72B-Instruct")),
    (_SF_URL, _SF_KEY, "Qwen/Qwen2.5-32B-Instruct"),
    (_SF_URL, _SF_KEY, "Qwen/Qwen2.5-7B-Instruct"),
]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
TZ = ZoneInfo("Asia/Shanghai")
# 运行时刻为北京时间 24:00（次日 00:00），所归属的"当天"为运行日减一天
RUN_NOW = datetime.datetime.now(TZ)
BIZ_DATE = (RUN_NOW - datetime.timedelta(days=1)).date()
BIZ_STR = BIZ_DATE.strftime("%Y-%m-%d")

# ---- 各板块关注重点（用于约束 LLM 筛选，避免跑偏）----
FOCUS = {
    "存储市场·国外": "国际存储芯片（NAND / DRAM / HBM 等）的价格走势、供需关系、产能变化，以及主要国际厂商"
                "（三星、SK 海力士、美光、铠侠、西部数据等）的动态。",
    "存储市场·国内": "国产存储芯片（长江存储、长鑫存储、兆易创新、澜起科技等）的产品、产能、技术与市场动态。",
    "Token 市场·国外": "国际主流大模型 API（GPT、Claude、Gemini、Llama、Mistral、Grok、Cohere 等）的计费与"
                   "Token 价格、用量与配额、新模型发布、上下文窗口与费率变动、API 相关功能更新。",
    "Token 市场·国内": "国产大模型 API（文心、通义、智谱 GLM、Kimi、DeepSeek、豆包、讯飞星火等）的计费与"
                   "Token 价格、新模型发布、API 功能更新与费率变动。",
    "算力市场·国外厂商": "NVIDIA 英伟达、AMD、Intel 英特尔 在 AI 算力领域的新品发布、量产、技术突破、"
                      "合作与客户落地、财报与业绩动态。",
    "算力市场·国内厂商": "华为昇腾、寒武纪、摩尔线程、沐曦、壁仞、天数智芯 等国产 AI 芯片厂商的新品发布、"
                      "量产、技术突破、合作与融资、政策与业绩动态。",
    "京东政企": "京东面向政府与企业的业务（京东企业业务、京东云、京东工业等）的政企客户、解决方案、"
                 "合作中标、行业落地动态。",
}

# ---- 各板块/区域相关性关键词（抓取后硬性过滤 + 发布前复核）----
# 规则：对【所有来源】（含厂商官方）一律按标题关键词判定；命中才保留。
KEYWORDS = {
    "storage_foreign": ["samsung", "三星", "sk海力士", "sk hynix", "micron", "美光", "kioxia", "铠侠",
                        "western digital", "西部数据", "trendforce", "digitimes", "集邦", "nand", "dram",
                        "hbm", "ssd", "闪存", "内存", "晶圆", "存储", "wafer", "价格", "涨价", "跌价",
                        "供需", "产能", "库存", "ddr", "3d nand", "semiconductor", "nand闪存", "dram内存"],
    "storage_domestic": ["长江存储", "长鑫", "兆易创新", "澜起", "武汉新芯", "普冉", "北京君正", "国产存储",
                         "长江", "长鑫存储", "闪存", "存储芯片", "nand", "dram", "ssd", "存储", "3d nand",
                         "价格", "涨价", "跌价", "供需", "产能", "库存", "ddr"],
    "token_foreign": ["openai", "gpt", "chatgpt", "anthropic", "claude", "google", "gemini", "azure", "微软",
                      "meta ai", "llama", "mistral", "xai", "grok", "cohere", "api", "token", "大模型", "模型",
                      "推理", "上下文", "费率", "定价", "降价", "免费额度", "调用", "模型发布", "多模态",
                      "对话", "智能体", "llm", "model", "chatbot", "artificial intelligence"],
    "token_domestic": ["文心", "通义", "智谱", "glm", "kimi", "deepseek", "豆包", "讯飞", "星火", "百度", "阿里",
                       "千帆", "百炼", "国产大模型", "大模型", "api", "token", "模型", "推理", "上下文", "费率",
                       "定价", "降价", "免费额度", "调用", "模型发布", "多模态", "对话", "智能体", "llm"],
    "compute_foreign": ["nvidia", "英伟达", "amd", "intel", "英特尔", "blackwell", "hopper", "mi300", "mi325",
                        "rubin", "instinct", "gaudi", "cuda", "datacenter", "superchip", "accelerator",
                        "h100", "b200", "h200", "a100", "rtx"],
    "compute_domestic": ["昇腾", "ascend", "寒武纪", "cambricon", "摩尔线程", "mthreads", "沐曦", "metax",
                         "壁仞", "biren", "天数智芯", "iluvatar", "国产gpu", "国产ai芯片", "信创"],
    "jd": ["政企", "企业业务", "京东云", "京东工业", "中标", "政采", "企业购", "数字化采购", "集采",
           "京东企业", "京东政企", "jd cloud", "jd industry", "政务云", "企业级", "供应链", "to b", "tob", "京东"],
}

def _kname(gid, region):
    if region in ("foreign", "domestic"):
        return f"{gid}_{region}"
    return gid

def _relevant(item, keys):
    """相关性判定：标题（小写）命中任一关键词即相关；空关键词视为相关。
    注意：所有来源（含厂商官方）都走此判定，不再有 core 豁免。"""
    if not keys:
        return True
    title = (item.get("title", "") or "").lower()
    return any(k.lower() in title for k in keys)

# ---- 数据源：权威官方 Newsroom（official）+ 权威行业媒体（media）
#  结构：SOURCES[板块][区域] = [源...]；区域为 foreign / domestic / all(京东)
#  tier=official 表示厂商官网/官方新闻室，tier=media 表示权威行业媒体（公众号同源官网/RSS）。
#  严格只取发布日==目标日当天的内容。微信公众号需登录态+强反爬，云端无法直接抓取，用同源官网/RSS 替代。
SOURCES = {
    "storage": {
        "foreign": [
            {"type": "rss", "url": "https://www.trendforce.com/news/feed/", "source": "TrendForce 集邦咨询", "tier": "official"},
            {"type": "rss", "url": "https://news.skhynix.com/feed/", "source": "SK 海力士", "tier": "official"},
            {"type": "rss", "url": "https://www.digitimes.com/rss/daily.xml", "source": "DIGITIMES 电子时报", "tier": "official"},
            # 权威存储/半导体媒体（官网优先无则取此）
            {"type": "rss", "url": "https://www.tomshardware.com/feeds/all", "source": "Tom's Hardware（存储/半导体媒体）", "tier": "media"},
        ],
        "domestic": [
            # 国内存储厂商官方站（尽力而为；官网无 RSS 时抓新闻页）
            {"type": "html", "url": "https://www.ymtc.com/", "source": "长江存储 YMTC", "tier": "official", "vendor": "长江存储"},
            {"type": "html", "url": "https://www.cxmt.com/", "source": "长鑫存储 CXMT", "tier": "official", "vendor": "长鑫存储"},
            {"type": "html", "url": "https://www.gigadevice.com/", "source": "兆易创新", "tier": "official", "vendor": "兆易创新"},
            # 国内半导体/存储媒体
            {"type": "rss", "url": "https://www.jiwei.com.cn/rss", "source": "集微网（半导体媒体）", "tier": "media"},
            {"type": "rss", "url": "https://www.36kr.com/feed", "source": "36氪（科技媒体）", "tier": "media"},
        ],
    },
    "token": {
        "foreign": [
            {"type": "rss", "url": "https://openai.com/news/rss.xml", "source": "OpenAI 官方（GPT）", "tier": "official"},
            {"type": "rss", "url": "https://blog.google/technology/ai/rss/", "source": "Google 官方博客（Gemini）", "tier": "official"},
            {"type": "rss", "url": "https://azure.microsoft.com/en-us/blog/feed/", "source": "微软 Azure 官方博客", "tier": "official"},
            {"type": "rss", "url": "https://ai.meta.com/blog/rss/", "source": "Meta AI 官方博客", "tier": "official"},
            {"type": "rss", "url": "https://mistral.ai/news/rss.xml", "source": "Mistral AI 官方", "tier": "official"},
            {"type": "rss", "url": "https://x.ai/blog/rss.xml", "source": "xAI 官方（Grok）", "tier": "official"},
            {"type": "rss", "url": "https://cohere.com/blog/rss.xml", "source": "Cohere 官方", "tier": "official"},
            {"type": "html", "url": "https://www.anthropic.com/news", "source": "Anthropic 官方（Claude）", "tier": "official"},
            # 权威 AI 媒体（官网优先无则取此）
            {"type": "rss", "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "source": "TechCrunch AI", "tier": "media"},
            {"type": "rss", "url": "https://the-decoder.com/feed/", "source": "The Decoder（AI 媒体）", "tier": "media"},
            {"type": "rss", "url": "https://www.marktechpost.com/feed/", "source": "Marktechpost（AI 媒体）", "tier": "media"},
        ],
        "domestic": [
            # 国内大模型官方站（尽力而为）
            {"type": "html", "url": "https://cloud.baidu.com/product/wenxinworkshop", "source": "百度智能云千帆（文心）", "tier": "official", "vendor": "百度文心"},
            {"type": "html", "url": "https://bailian.aliyun.com/", "source": "阿里云百炼（通义）", "tier": "official", "vendor": "阿里通义"},
            {"type": "html", "url": "https://open.bigmodel.cn/", "source": "智谱 AI（GLM）", "tier": "official", "vendor": "智谱 GLM"},
            {"type": "html", "url": "https://kimi.moonshot.cn/", "source": "月之暗面 Kimi", "tier": "official", "vendor": "Kimi"},
            {"type": "html", "url": "https://www.deepseek.com/", "source": "DeepSeek 深度求索", "tier": "official", "vendor": "DeepSeek"},
            # 权威 AI 媒体（覆盖国产大模型动态）
            {"type": "rss", "url": "https://www.qbitai.com/feed", "source": "量子位（AI 媒体）", "tier": "media"},
            {"type": "rss", "url": "https://www.36kr.com/feed", "source": "36氪（科技媒体）", "tier": "media"},
            {"type": "rss", "url": "https://www.tmtpost.com/rss.xml", "source": "钛媒体（科技媒体）", "tier": "media"},
        ],
    },
    "compute": {
        "foreign": [
            {"type": "rss", "url": "https://nvidianews.nvidia.com/news/rss", "source": "NVIDIA 英伟达", "tier": "official", "vendor": "英伟达 NVIDIA"},
            {"type": "html", "url": "https://www.amd.com/en/newsroom.html", "source": "AMD 官方", "tier": "official", "vendor": "AMD"},
            {"type": "rss", "url": "https://newsroom.intel.com/feed/", "source": "Intel 英特尔", "tier": "official", "vendor": "Intel 英特尔"},
            # 权威硬件媒体（官网优先无则取此；仅保留提及 NVIDIA/AMD/Intel 的条目）
            {"type": "rss", "url": "https://www.servethehome.com/feed/", "source": "ServeTheHome（硬件媒体）", "tier": "media"},
            {"type": "rss", "url": "https://www.tomshardware.com/feeds/all", "source": "Tom's Hardware（硬件媒体）", "tier": "media"},
        ],
        "domestic": [
            {"type": "html", "url": "https://www.hiascend.com/", "source": "华为昇腾", "tier": "official", "vendor": "华为昇腾"},
            {"type": "html", "url": "https://www.cambricon.com/", "source": "寒武纪", "tier": "official", "vendor": "寒武纪"},
            {"type": "html", "url": "https://www.mthreads.com/", "source": "摩尔线程", "tier": "official", "vendor": "摩尔线程"},
            {"type": "html", "url": "https://www.metax-tech.com/", "source": "沐曦股份", "tier": "official", "vendor": "沐曦股份"},
            {"type": "html", "url": "https://www.birentech.com/", "source": "壁仞科技", "tier": "official", "vendor": "壁仞科技"},
            {"type": "html", "url": "https://www.iluvatar.com/", "source": "天数智芯", "tier": "official", "vendor": "天数智芯"},
            # 国内半导体媒体（仅保留上述 6 家国产 AI 芯片厂商相关条目）
            {"type": "rss", "url": "https://www.jiwei.com.cn/rss", "source": "集微网（半导体媒体）", "tier": "media"},
        ],
    },
    "jd": {
        "all": [
            {"type": "html", "url": "https://e.jd.com/", "source": "京东企业业务", "tier": "official"},
            {"type": "html", "url": "https://www.jdcloud.com/", "source": "京东云", "tier": "official"},
            {"type": "html", "url": "https://news.jd.com/", "source": "京东黑板报（官方资讯）", "tier": "official"},
            # 权威科技/企业媒体（官网优先无则取此）
            {"type": "rss", "url": "https://www.tmtpost.com/rss.xml", "source": "钛媒体（科技媒体）", "tier": "media"},
            {"type": "rss", "url": "https://www.36kr.com/feed", "source": "36氪（科技媒体）", "tier": "media"},
        ],
    },
}

# ---- 官方关注渠道（某板块/区域当日无更新时展示，供用户自行关注；均为权威官方站点）----
CHANNELS = {
    "storage": {
        "foreign": [
            {"name": "三星 Samsung Newsroom", "url": "https://news.samsung.com/global/", "region": "foreign"},
            {"name": "SK 海力士", "url": "https://news.skhynix.com/", "region": "foreign"},
            {"name": "美光 Micron", "url": "https://www.micron.com/about/newsroom", "region": "foreign"},
            {"name": "西部数据 Western Digital", "url": "https://blog.westerndigital.com/", "region": "foreign"},
            {"name": "铠侠 Kioxia", "url": "https://www.kioxia.com/en-us/", "region": "foreign"},
            {"name": "TrendForce 集邦咨询", "url": "https://www.trendforce.com/news/", "region": "foreign"},
        ],
        "domestic": [
            {"name": "长江存储 YMTC", "url": "https://www.ymtc.com/", "region": "domestic"},
            {"name": "长鑫存储 CXMT", "url": "https://www.cxmt.com/", "region": "domestic"},
            {"name": "兆易创新", "url": "https://www.gigadevice.com/", "region": "domestic"},
            {"name": "澜起科技", "url": "https://www.montage-tech.com/", "region": "domestic"},
        ],
    },
    "token": {
        "foreign": [
            {"name": "OpenAI 官方（GPT）", "url": "https://openai.com/news", "region": "foreign"},
            {"name": "Anthropic 官方（Claude）", "url": "https://www.anthropic.com/news", "region": "foreign"},
            {"name": "Google 官方博客（Gemini）", "url": "https://blog.google/technology/ai/", "region": "foreign"},
            {"name": "微软 Azure 官方博客", "url": "https://azure.microsoft.com/en-us/blog/", "region": "foreign"},
            {"name": "Meta AI 官方博客", "url": "https://ai.meta.com/blog/", "region": "foreign"},
            {"name": "Mistral AI 官方", "url": "https://mistral.ai/news", "region": "foreign"},
            {"name": "xAI 官方（Grok）", "url": "https://x.ai/blog", "region": "foreign"},
            {"name": "Cohere 官方", "url": "https://cohere.com/blog", "region": "foreign"},
        ],
        "domestic": [
            {"name": "百度智能云千帆（文心）", "url": "https://cloud.baidu.com/product/wenxinworkshop", "region": "domestic"},
            {"name": "阿里云百炼（通义）", "url": "https://bailian.aliyun.com/", "region": "domestic"},
            {"name": "智谱 AI（GLM）", "url": "https://open.bigmodel.cn/", "region": "domestic"},
            {"name": "月之暗面 Kimi", "url": "https://kimi.moonshot.cn/", "region": "domestic"},
            {"name": "DeepSeek 深度求索", "url": "https://www.deepseek.com/", "region": "domestic"},
            {"name": "讯飞星火", "url": "https://xinghuo.xfyun.cn/", "region": "domestic"},
        ],
    },
    "compute": {
        "foreign": [
            {"name": "NVIDIA 英伟达", "url": "https://nvidianews.nvidia.com/", "region": "foreign"},
            {"name": "AMD 官方", "url": "https://www.amd.com/en/newsroom.html", "region": "foreign"},
            {"name": "Intel 英特尔", "url": "https://newsroom.intel.com/", "region": "foreign"},
        ],
        "domestic": [
            {"name": "华为昇腾", "url": "https://www.hiascend.com/", "region": "domestic"},
            {"name": "寒武纪", "url": "https://www.cambricon.com/", "region": "domestic"},
            {"name": "摩尔线程", "url": "https://www.mthreads.com/", "region": "domestic"},
            {"name": "沐曦股份", "url": "https://www.metax-tech.com/", "region": "domestic"},
            {"name": "壁仞科技", "url": "https://www.birentech.com/", "region": "domestic"},
            {"name": "天数智芯", "url": "https://www.iluvatar.com/", "region": "domestic"},
        ],
    },
    "jd": [
        {"name": "京东企业业务", "url": "https://e.jd.com/", "region": ""},
        {"name": "京东云", "url": "https://www.jdcloud.com/", "region": ""},
        {"name": "京东", "url": "https://www.jd.com/", "region": ""},
        {"name": "京东黑板报（官方资讯）", "url": "https://news.jd.com/", "region": ""},
        {"name": "京东物流", "url": "https://www.jdl.com/", "region": ""},
        {"name": "京东工业", "url": "https://www.jd.com/", "region": ""},
    ],
}

NEWS_HREF = re.compile(
    r"(?:news|article|press|blog|detail|info|release|announce|posts?|view|show|content|story|/c/|/n/|/a/|/\d{4}/\d{2}/\d{2}/|/\d{4}-\d{2}-\d{2})",
    re.I,
)
SECTION_NAMES = {
    "storage": "存储市场", "token": "Token 市场",
    "compute": "算力市场", "jd": "京东政企",
}
REGION_LABELS = [("foreign", "国外"), ("domestic", "国内")]
DATE_RES = [
    re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})"),
    re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
]

def extract_date(text):
    if not text:
        return None
    for rx in DATE_RES:
        m = rx.search(text)
        if m:
            try:
                d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if 2000 <= d.year <= 2100:
                    return d
            except Exception:
                pass
    return None

# ---------- 抓取（严格只取"目标日期"当天发布）----------
def fetch_rss(src, limit=12):
    # 先经 requests 带超时下载，再交给 feedparser 解析，避免无响应源拖垮整个任务
    raw = None
    for attempt in range(2):
        try:
            r = requests.get(src["url"], headers={"User-Agent": UA}, timeout=12)
            if r.status_code == 200 and r.content:
                raw = r.content
                break
        except Exception as ex:
            print(f"  [RSS下载失败] {src['source']}: {ex}")
    if not raw:
        return []
    try:
        d = feedparser.parse(raw)
    except Exception as ex:
        print(f"  [RSS解析失败] {src['source']}: {ex}")
        return []
    cands = []
    for e in (d.entries or []):
        title = (e.title or "").strip()
        link = (e.link or "").strip()
        if not title or not link:
            continue
        pub = e.get("published_parsed") or e.get("updated_parsed")
        pdate = None
        if pub:
            try:
                pdate = datetime.datetime(*pub[:6], tzinfo=datetime.timezone.utc).astimezone(TZ).date()
            except Exception:
                pdate = None
        # 部分源 pubDate 损坏（如统一返回旧日期），改用 URL 路径中的日期兜底
        if pdate is None or pdate != BIZ_DATE:
            url_date = extract_date(link)
            if url_date == BIZ_DATE:
                pdate = url_date
        # 硬性要求：发布日期必须 == 目标日期，否则丢弃（不追溯、不保留无日期项）
        if pdate != BIZ_DATE:
            continue
        cands.append({"title": title, "url": link, "source": src["source"], "date": pdate})
        if len(cands) >= limit:
            break
    return cands

def fetch_html(src, limit=12):
    try:
        r = requests.get(src["url"], headers={"User-Agent": UA}, timeout=15)
        if r.status_code != 200:
            return []
        r.encoding = r.apparent_encoding or "utf-8"   # 修复中文乱码
        soup = BeautifulSoup(r.text, "lxml")
        seen, items = set(), []
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            href = (a["href"] or "").strip()
            if not text or len(text) < 8 or len(text) > 60:
                continue
            if not NEWS_HREF.search(href):
                continue
            # 链接规范化
            if href.startswith("//"):
                href = "https:" + href
            if not href.startswith("http"):
                href = requests.compat.urljoin(src["url"], href)
            low = href.lower()
            if not low.startswith("http"):
                continue
            if low.startswith(("javascript:", "mailto:", "tel:", "data:")) or "#" in href:
                continue
            if href.rstrip("/") == src["url"].rstrip("/"):
                continue
            segs = [s for s in urlparse(href).path.split("/") if s]
            if not segs or segs[-1].lower() == "index.html":
                continue
            # 硬性日期要求：上下文或 URL 路径中能解析出 == 目标日期 的发布日，否则丢弃
            parent = a.find_parent(["li", "tr"]) or a.parent
            ctx = (parent.get_text(" ", strip=True) if parent else "")[:220]
            pdate = extract_date(ctx) or extract_date(href)
            if pdate is None or pdate != BIZ_DATE:
                continue
            if href in seen:
                continue
            seen.add(href)
            items.append({"title": text, "url": href, "source": src["source"]})
            if len(items) >= limit:
                break
        return items
    except Exception as ex:
        print(f"  [HTML失败] {src['source']}: {ex}")
        return []

def _relevance_filter(items, keys):
    if not keys:
        return items
    before = len(items)
    kept = [it for it in items if _relevant(it, keys)]
    print(f"    相关性过滤：{before} -> {len(kept)} 条")
    return kept

def _dedupe(items):
    uniq, seen = [], set()
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"]); uniq.append(it)
    return uniq

def collect_region(section, region):
    """抓取某板块某区域：官网(official)优先；若官方当日无相关条目，再从媒体(media)取。
    所有来源均按本区域关键词把关（无关内容剔除）。"""
    srcs = SOURCES[section][region]
    keys = KEYWORDS[_kname(section, region)]
    off, med = [], []
    for src in srcs:
        got = fetch_rss(src) if src["type"] == "rss" else fetch_html(src)
        for it in got:
            it.setdefault("vendor", src.get("vendor", ""))
            it["region"] = "" if region == "all" else region
            it["_tier"] = src.get("tier", "media")
            (off if src.get("tier") == "official" else med).append(it)
        print(f"  [{section}/{region}] {src['source']}: 抓到 {len(got)} 条（仅 {BIZ_STR} 发布）")
    off = _dedupe(_relevance_filter(off, keys))
    med = _dedupe(_relevance_filter(med, keys))
    # 官网优先；官方无则媒体兜底
    chosen = off if off else med
    print(f"  [{section}/{region}] 采用：{'官方(官网优先)' if off else ('媒体兜底' if med else '无')} 共 {len(chosen)} 条")
    return chosen[:12]

def collect_section(section):
    if section == "jd":
        return {"all": collect_region("jd", "all")}
    return {r: collect_region(section, r) for r, _ in REGION_LABELS}

# ---------- 发布前链接可达性自检 ----------
_url_cache = {}
def check_url(url):
    if url in _url_cache:
        return _url_cache[url]
    result = False
    try:
        r = requests.head(url, headers={"User-Agent": UA}, timeout=8, allow_redirects=True)
        code = r.status_code
        # 反爬常返回 401/403/429，视为"可访问"（只是需要验证），保留
        if code in (200, 201, 202, 203, 204, 206, 301, 302, 303, 307, 308, 401, 403, 429):
            result = True
        elif code >= 400:
            try:
                r2 = requests.get(url, headers={"User-Agent": UA}, timeout=8, allow_redirects=True, stream=True)
                c2 = r2.status_code
                r2.close()
                result = c2 in (200, 206, 301, 302, 303, 307, 308, 401, 403, 429)
            except Exception:
                result = False
    except Exception:
        result = False
    _url_cache[url] = result
    return result

def filter_dead_links(items):
    kept, dropped = [], []
    for it in items:
        if check_url(it.get("url", "")):
            kept.append(it)
        else:
            dropped.append(it.get("url", ""))
    if dropped:
        print(f"  [链接自检] 剔除 {len(dropped)} 条不可访问链接: {dropped[:3]}")
    return kept

# ---------- 中文翻译安全网（对付免费模型英文残留）----------
def _has_cjk(s):
    return any('\u4e00' <= ch <= '\u9fff' for ch in (s or ""))

def _need_cn(s):
    s = (s or "").strip()
    if not s:
        return False
    return not _has_cjk(s)

def _translate_batch(pairs):
    """pairs: [(idx, title, excerpt), ...]；返回 {idx: (title, excerpt)} 中文版。"""
    if not pairs:
        return {}
    payload = [{"idx": i, "title": t, "excerpt": e} for i, t, e in pairs]
    system = ("你是将英文科技资讯翻译为书面化简体中文的翻译专家。专有名词保留英文并附中文"
              "（如 'NVIDIA 英伟达'、'GPT-5'）。只输出 JSON："
              '{"items":[{"idx":0,"title":"中文标题","excerpt":"中文摘要"}]}，不要任何解释。')
    user = "请将以下条目翻译为简体中文：\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        txt = call_llm(system, user, temperature=0.2)
        d = json.loads(txt)
        out = {}
        for g in (d.get("items") or []):
            try:
                idx = int(g.get("idx"))
            except Exception:
                continue
            out[idx] = (g.get("title", ""), g.get("excerpt", ""))
        return out
    except Exception as ex:
        print(f"  [翻译失败] {ex}")
        return {}

def _translate_text(text):
    if not _need_cn(text):
        return text
    system = "你是将英文科技资讯翻译为书面化简体中文的翻译专家。专有名词保留英文并附中文。只输出翻译结果，不要解释。"
    try:
        return call_llm(system, "翻译：\n" + text, temperature=0.2).strip().strip('"').strip()
    except Exception as ex:
        print(f"  [摘要翻译失败] {ex}")
        return text

# ---------- 调用 LLM（主+兜底，按能力从强到弱）----------
def call_llm(system, user, temperature=0.3):
    providers = []
    if PRIMARY[1]:
        providers.append(PRIMARY)
    for fb in FALLBACKS:
        if fb[1]:
            providers.append(fb)
    last_err = None
    for url, key, model in providers:
        try:
            resp = requests.post(url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "temperature": temperature,
                      "response_format": {"type": "json_object"},
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": user}]},
                timeout=180)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as ex:
            last_err = ex
            print(f"  [LLM 供应商失败 {url[:45]} / {model}]: {ex}")
    raise RuntimeError(f"所有 LLM 供应商均失败: {last_err}")

def synthesize(section_label, items, channels=None):
    focus = FOCUS.get(section_label, "")
    if not items:
        return {
            "summary": f"今日（{BIZ_STR}）未检索到「{section_label}」的官方更新，可点击下方官方渠道持续关注。",
            "items": [],
            "channels": channels or [],
        }
    # 为每条打上稳定序号 idx，后续严格按 idx 回贴真实链接，杜绝"标题错位到无关网站"
    for i, it in enumerate(items):
        it["_idx"] = i
    payload = [{"idx": it["_idx"], "title": it["title"], "source": it["source"], "url": it["url"]} for it in items]
    system = ("你是中国市场动态看板的资深分析师，面向不懂技术的小白用户。"
              "要求：1）书面化但不口语化；2）无门槛可读；3）严格基于提供的官方条目，不得编造；"
              "4）【所有输出必须是简体中文】——英文标题/要点必须翻译为中文，厂商名保留英文并附中文"
              "（如 'NVIDIA 英伟达'、'GPT-5'）；绝不可保留英文原标题；"
              "5）url 与 source 仅作参考，最终以输入为准，不得改动或新增链接；"
              "6）必须按 idx 一一对应返回，不得增删、不得调换顺序。")
    user = (f"以下是「{section_label}」在 {BIZ_STR} 当天发布的官方动态条目（均为当天发布、附真实官方链接），每条带 idx：\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            f"本板块的关注重点是：{focus}\n\n"
            "请完成两件事：\n"
            "1）筛选：仅保留与上面【关注重点】高度相关的条目（保留 3-5 条，不足则全部保留），剔除无关的（如客户案例、活动招募、招聘等非核心动态）。"
            "务必只保留你决定留下的条目的 idx。\n"
            "2）提炼：对保留的每条，用书面化中文写 2-3 句要点（面向小白、可读）。\n"
            "严格要求：\n"
            "- 返回的每个对象必须含 idx 字段，且 idx 与输入某条完全一致；title 字段【必须是简体中文标题】（不得是英文原标题）。\n"
            "- excerpt 字段为 2-3 句书面化中文重点摘要。\n"
            "输出 JSON：\n"
            "{\n"
            '  "summary": "2-4 句书面化每日小结，概括该板块当日整体动向（简体中文）",\n'
            '  "items": [ {"idx": 0, "title": 中文标题, "excerpt": "2-3 句中文重点摘要"} ]\n'
            "}\n"
            "items 仅包含你筛选后保留的条目，但每个 idx 必须与输入一致、可对应回原文真实链接。")
    try:
        txt = call_llm(system, user)
        data = json.loads(txt)
        summary = (data.get("summary") or "").strip()
        # 以 idx 为键回贴，绝不按位置配对（修复"标题与链接错位"）
        gens = {}
        for g in (data.get("items") or []):
            try:
                idx = int(g.get("idx"))
            except Exception:
                continue
            gens[idx] = g
        out_items = []
        for it in items:
            g = gens.get(it["_idx"])
            if not g:
                continue   # 被 LLM 筛掉的条目不展示
            out_items.append({
                "title": (g.get("title") or it["title"]).strip(),
                "source": it["source"],
                "url": it["url"],                       # 始终使用真实抓取的官方链接
                "excerpt": (g.get("excerpt") or "").strip(),
                "vendor": it.get("vendor", ""),
                "region": it.get("region", ""),
            })
        if not out_items:
            # LLM 全筛掉或解析异常：兜底保留原始条目（标题/链接天然一一对应）
            out_items = [{"title": it["title"], "source": it["source"], "url": it["url"],
                          "excerpt": "", "vendor": it.get("vendor", ""), "region": it.get("region", "")} for it in items]
            summary = summary or f"{section_label}当日获取到 {len(items)} 条官方动态，详见各条官方来源。"
        # ---- 中文翻译安全网：标题/摘要无中文则翻译 ----
        to_trans = [(i, it.get("title", ""), it.get("excerpt", "")) for i, it in enumerate(out_items)
                    if _need_cn(it.get("title", "")) or _need_cn(it.get("excerpt", ""))]
        if to_trans:
            tr = _translate_batch(to_trans)
            for i, _, _ in to_trans:
                if i in tr:
                    t, e = tr[i]
                    if t:
                        out_items[i]["title"] = t
                    if e:
                        out_items[i]["excerpt"] = e
        if _need_cn(summary):
            summary = _translate_text(summary)
        for it in out_items:
            it.pop("_idx", None)
        return {"summary": summary, "items": out_items, "channels": channels or []}
    except Exception as ex:
        print(f"  [LLM失败 {section_label}]: {ex}")
        # 兜底：保留原始条目并尽力翻译标题
        out_items = []
        for it in items:
            it.pop("_idx", None)
            title = it["title"]
            if _need_cn(title):
                title = _translate_text(title) or title
            out_items.append({"title": title, "source": it["source"], "url": it["url"],
                              "excerpt": "（摘要生成暂不可用，请点击来源查看官方原文）",
                              "vendor": it.get("vendor", ""), "region": it.get("region", "")})
        return {
            "summary": f"{section_label}当日获取到 {len(items)} 条官方动态，摘要生成异常，已沿用原始标题，详见各条官方来源。",
            "items": out_items, "channels": channels or [],
        }

# ---------- 周/月总结 ----------
def period_summaries(data, start, end):
    out = []
    for sec in data["sections"]:
        if sec.get("type") == "review":
            continue
        for d, entry in (sec.get("dates") or {}).items():
            if start <= d <= end:
                out.append({"date": d, "section": sec["name"], "summary": entry.get("summary", "")})
    return out

def is_last_day(d):
    return (d + datetime.timedelta(days=1)).month != d.month

def build_review(data):
    review = next((s for s in data["sections"] if s.get("type") == "review"), None)
    if not review:
        return
    if BIZ_DATE.weekday() == 6:
        end = BIZ_DATE
        start = end - datetime.timedelta(days=6)
        sums = period_summaries(data, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        if sums:
            review["weekly"] = {"period": f"{start} 至 {end}", "summary": synthesize_review("周", start, end, sums)}
    if is_last_day(BIZ_DATE):
        start = BIZ_DATE.replace(day=1)
        sums = period_summaries(data, start.strftime("%Y-%m-%d"), BIZ_DATE.strftime("%Y-%m-%d"))
        if sums:
            review["monthly"] = {"period": f"{start} 至 {BIZ_DATE}", "summary": synthesize_review("月", start, BIZ_DATE, sums)}

def synthesize_review(kind, start, end, sums):
    system = ("你是中国市场动态看板的资深分析师，面向不懂技术的小白用户。"
              "要求：书面化但不口语化、无门槛可读；严格基于提供的各板块每日小结，不得编造；"
              "可做横向对比与趋势概括，对专业术语自然使用（前端会自动注释）。输出简体中文。")
    user = (f"以下是 {start} 至 {end} 各板块的每日小结，请综合写成一段「{kind}总结」（4-6 句，概括四大市场整体动向与趋势，简体中文）：\n"
            f"{json.dumps(sums, ensure_ascii=False, indent=2)}")
    try:
        return call_llm(system, user, temperature=0.4)
    except Exception as ex:
        print(f"  [REVIEW LLM失败]: {ex}")
        return f"{kind}总结生成失败，请稍后查看各板块每日小结。"

# ---------- 发布前统一自检（沿用此前所有硬性规则 + 本次相关性/链接一致性）----------
def verify_before_publish(data):
    print("== 发布前自检：当日 / 死链 / 相关性 / 链接一致性 ==")
    for sec in data["sections"]:
        if sec.get("type") == "review":
            continue
        gid = sec["id"]
        for d, entry in (sec.get("dates") or {}).items():
            kept = []
            for it in (entry.get("items") or []):
                url = it.get("url", "")
                title = it.get("title", "")
                # ④ 链接一致性：必须是合法 http(s) 链接
                if not isinstance(url, str) or not url.lower().startswith("http"):
                    print(f"  [自检-链接非法] {sec['name']}/{d} 剔除: {title[:24]}")
                    continue
                # ② 死链自检
                if not check_url(url):
                    print(f"  [自检-死链] {sec['name']}/{d} 剔除: {url}")
                    continue
                # ③ 相关性复核（按区域关键词）
                region = it.get("region", "")
                kset = KEYWORDS.get(_kname(gid, region)) or KEYWORDS.get(gid)
                if kset and not _relevant(it, kset):
                    print(f"  [自检-关联度低] {sec['name']}/{d} 剔除: {title[:24]}")
                    continue
                kept.append(it)
            entry["items"] = kept
        # 若当日被清空，summary 也提示走官方渠道
        if BIZ_STR in (sec.get("dates") or {}) and not (sec["dates"][BIZ_STR].get("items") or []):
            sec["dates"][BIZ_STR]["summary"] = (
                f"今日（{BIZ_STR}）未检索到与「{sec['name']}」高度相关的官方更新，可点击下方官方渠道持续关注。"
            )
    return data

# ---------- 发布前质量门禁：逐项核对用户三天（10-19~10-21）提出的全部约束 ----------
def quality_gate(data, html):
    """写回前逐项核对用户明确提出的硬性约束；任一【关键】项不通过则 sys.exit(1) 阻断发布。"""
    print("== 质量门禁：核对用户三天提出的全部约束 ==")
    critical_fail = []
    checks = []

    # A. 单页结构（不得回归三页导航 / 价格模块独立页）
    three_tab = any(t in html for t in ['data-page="news"', 'data-page="price"', 'data-page="review"',
                                        'id="pageNews"', 'id="pagePrice"', 'id="pageReview"'])
    single_ok = (not three_tab) and all(s in html for s in ['id="grid"', 'id="reviewPanel"', 'id="footer"', 'class="toolbar"'])
    checks.append(("单页结构（四市场动态+总结，无三页导航）", single_ok, True))
    if not single_ok:
        critical_fail.append("单页结构被破坏（出现三页导航或缺少主容器）")

    # B. 价格模块永不回归
    no_price = "priceModule" not in data
    checks.append(("存储价格变动模块已移除且永不回归", no_price, True))
    if not no_price:
        critical_fail.append("priceModule 重新出现")

    # C. 总结区存在（承载周报/月报）
    has_review = any(s.get("type") == "review" for s in data["sections"])
    checks.append(("总结区(review)存在，可承载周报/月报", has_review, True))
    if not has_review:
        critical_fail.append("review 板块缺失，周报/月报无处安放")

    # D. 四市场板块齐全
    ids = {s["id"] for s in data["sections"]}
    four_ok = {"storage", "token", "compute", "jd"}.issubset(ids)
    checks.append(("四市场板块齐全(存储/Token/算力/京东政企)", four_ok, True))
    if not four_ok:
        critical_fail.append("市场板块缺失: " + ",".join({"storage", "token", "compute", "jd"} - ids))

    # E. 国内外分组关键词齐备（算力/Token/存储均分国内外）
    split_ok = all(f"{k}_{r}" in KEYWORDS for k in ("storage", "token", "compute") for r in ("foreign", "domestic"))
    checks.append(("存储/Token/算力均分国外+国内两组", split_ok, False))

    # F. 日期与 updatedAt
    ua_ok = data.get("meta", {}).get("updatedAt") == BIZ_STR
    checks.append((f"updatedAt 等于目标日 {BIZ_STR}", ua_ok, False))

    # G. 所有条目链接合法 http(s)（可达性已在 verify_before_publish 过滤）
    bad = []
    for sec in data["sections"]:
        if sec.get("type") == "review":
            continue
        for d, e in (sec.get("dates") or {}).items():
            for it in (e.get("items") or []):
                u = str(it.get("url", ""))
                if not u.lower().startswith("http"):
                    bad.append(f"{sec['name']}/{d}:{it.get('title', '')[:16]}")
    link_ok = not bad
    checks.append(("全部条目链接为合法 http(s)", link_ok, False))
    if bad:
        print("    非法链接:", bad[:5])

    for name, ok, crit in checks:
        tag = "[关键]" if crit else "[常规]"
        print(f"  [{'通过' if ok else '不通过'}] {tag} {name}")
    print(f"== 门禁结果：{'全部通过' if not critical_fail else '存在关键项不通过，阻断发布'} ==")
    if critical_fail:
        for f in critical_fail:
            print("  [不通过]", f)
        sys.exit(1)
    return data

# ---------- 主流程 ----------
def main():
    html = open(INDEX, encoding="utf-8").read()
    m = re.search(r'(<!--MARKET-DATA-START-->\s*<script type="application/json" id="market-data">)(.*?)(</script>\s*<!--MARKET-DATA-END-->)', html, re.S)
    if not m:
        print("ERROR: 未找到 MARKET-DATA 区块"); sys.exit(1)
    data = json.loads(m.group(2))

    print(f"== 目标更新日期（北京时间 24:00 运行）：{BIZ_STR} ==")

    # 存储 / Token / 算力：分 国外 / 国内 两组
    for key in ["storage", "token", "compute"]:
        name = SECTION_NAMES[key]
        print(f"-- 抓取 {name}（国外/国内）--")
        sec_items, summaries = [], []
        for region, rlabel in REGION_LABELS:
            raw = collect_region(key, region)
            raw = filter_dead_links(raw)
            label_full = f"{name}·{rlabel}"
            channels = CHANNELS[key].get(region, [])
            res = synthesize(label_full, raw, channels)
            for it in res["items"]:
                it["region"] = region
            sec_items += res["items"]
            if res["items"]:
                summaries.append(f"{rlabel}：{res['summary']}")
        combined = "\n".join(summaries) if summaries else \
            f"今日（{BIZ_STR}）未检索到「{name}」的官方更新，可点击下方官方渠道持续关注。"
        sec_obj = next(s for s in data["sections"] if s["id"] == key)
        sec_obj.setdefault("dates", {})[BIZ_STR] = {
            "summary": combined,
            "items": sec_items,
            "channels": CHANNELS[key]["foreign"] + CHANNELS[key]["domestic"],
        }

    # 京东政企（单一区域）
    print("-- 抓取 京东政企 --")
    jd_raw = collect_region("jd", "all")
    jd_raw = filter_dead_links(jd_raw)
    jd_res = synthesize(SECTION_NAMES["jd"], jd_raw, CHANNELS["jd"])
    jd_obj = next(s for s in data["sections"] if s["id"] == "jd")
    jd_obj.setdefault("dates", {})[BIZ_STR] = {
        "summary": jd_res["summary"], "items": jd_res["items"], "channels": CHANNELS["jd"]
    }

    # 写入各板块的官方关注渠道（无更新时展示，供用户自行关注）
    for sec in data["sections"]:
        if sec.get("type") == "review":
            continue
        cid = sec["id"]
        if cid == "jd":
            sec["channels"] = CHANNELS["jd"]
        else:
            sec["channels"] = CHANNELS[cid]["foreign"] + CHANNELS[cid]["domestic"]

    # 周/月总结
    build_review(data)

    # 发布前统一自检（当日 / 死链 / 相关性 / 链接一致性）
    data = verify_before_publish(data)

    # 质量门禁：逐项核对用户三天提出的全部约束，关键项不通过则阻断发布
    data = quality_gate(data, html)

    # 清理 >30 天历史
    cutoff = (BIZ_DATE - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    for sec in data["sections"]:
        if sec.get("type") == "review":
            continue
        if "dates" in sec:
            sec["dates"] = {d: v for d, v in sec["dates"].items() if d >= cutoff}

    # 写回
    data["meta"]["updatedAt"] = BIZ_STR
    data["meta"]["isPrototype"] = False
    new_block = json.dumps(data, ensure_ascii=False, indent=2)
    new_html = html[:m.start(2)] + new_block + html[m.end(2):]
    open(INDEX, "w", encoding="utf-8").write(new_html)
    print("== 已写回 index.html ==")

if __name__ == "__main__":
    main()
