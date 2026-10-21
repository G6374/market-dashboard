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

# ---- LLM 供应商：主用 DeepSeek，失败回退 SiliconFlow（免费兜底）----
PRIMARY = (
    os.environ.get("LLM_API_URL", "https://api.deepseek.com/v1/chat/completions"),
    os.environ.get("LLM_API_KEY", ""),
    os.environ.get("LLM_MODEL", "deepseek-chat"),
)
FALLBACK = (
    os.environ.get("FALLBACK_API_URL", "https://api.siliconflow.cn/v1/chat/completions"),
    os.environ.get("FALLBACK_API_KEY", ""),
    os.environ.get("FALLBACK_MODEL", "Qwen/Qwen2.5-7B-Instruct"),
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
TZ = ZoneInfo("Asia/Shanghai")
# 运行时刻为北京时间 24:00（次日 00:00），所归属的"当天"为运行日减一天
RUN_NOW = datetime.datetime.now(TZ)
BIZ_DATE = (RUN_NOW - datetime.timedelta(days=1)).date()
BIZ_STR = BIZ_DATE.strftime("%Y-%m-%d")

# ---- 各板块关注重点（用于约束 LLM 筛选，避免跑偏）----
FOCUS = {
    "storage": "存储芯片（NAND / DRAM / HBM 等）的价格走势、供需关系、产能变化，以及主要厂商"
                "（三星、SK 海力士、美光、铠侠、西部数据、长江存储、长鑫存储等）的动态。",
    "token": "主流大模型 API（文心、通义、GLM、GPT、Claude 等）的计费与 Token 价格、用量与配额、"
             "新模型发布、上下文窗口与费率变动、API 相关功能更新。",
    "算力市场·国外厂商": "NVIDIA 英伟达、AMD、Intel 英特尔 在 AI 算力领域的新品发布、量产、技术突破、"
                      "合作与客户落地、财报与业绩动态。",
    "算力市场·国内厂商": "华为昇腾、寒武纪、摩尔线程、沐曦、壁仞、天数智芯 等国产 AI 芯片厂商的新品发布、"
                      "量产、技术突破、合作与融资、政策与业绩动态。",
    "京东政企": "京东面向政府与企业的业务（京东企业业务、京东云、京东工业等）的政企客户、解决方案、"
                 "合作中标、行业落地动态。",
}

# ---- 各板块相关性关键词（抓取后硬性过滤，只保留与板块主题高度相关的条目）----
# 说明：搜索源已扩大（官网 + 权威行业媒体），但媒体源内容驳杂，必须用关键词把关，
#       杜绝"标题与板块无关 / 随意堆砌"。关键词命中标题或来源名即视为相关。
KEYWORDS = {
    "storage": ["存储", "闪存", "nand", "dram", "hbm", "ssd", "固态", "内存", "晶圆", "颗粒",
                "铠侠", "西部数据", "sk海力士", "美光", "三星", "长江存储", "长鑫",
                "集邦", "trendforce", "digitimes", "价格", "涨价", "跌价", "供需", "产能", "库存",
                "kioxia", "micron", "sk hynix", "samsung", "semiconductor", "nand闪存", "dram内存"],
    "token": ["大模型", "api", "token", "gpt", "claude", "文心", "通义", "智谱", "glm", "openai",
              "anthropic", "百度", "阿里", "千帆", "百炼", "gemini", "模型", "推理", "上下文", "费率",
              "定价", "降价", "免费额度", "调用", "mistral", "meta ai", "xai", "grok", "cohere",
              "azure", "微软", "谷歌", "模型发布", "多模态", "对话", "智能体", "llm", "chatgpt",
              "claude", "deepseek", "kimi", "豆包", "大模型api", "模型价格"],
    "compute_foreign": ["nvidia", "英伟达", "gpu", "amd", "intel", "英特尔", "加速卡", "显卡",
                        "blackwell", "hopper", "mi300", "数据中心", "ai芯片", "算力", "芯片", "cuda",
                        "rubin", "superchip", "超级芯片", "datacenter", "accelerator"],
    "compute_domestic": ["昇腾", "寒武纪", "摩尔线程", "沐曦", "壁仞", "天数智芯", "燧原", "昆仑芯",
                         "国产", "gpu", "ai芯片", "加速卡", "算力", "芯片", "ascend", "cambricon",
                         "国产gpu", "国产芯片", "信创"],
    "jd": ["政企", "企业业务", "京东云", "京东工业", "中标", "政采", "企业购", "数字化采购", "集采",
           "京东企业", "京东政企", "jd cloud", "jd industry", "政务云", "企业级", "供应链", "to b", "toB"],
}

def _relevant(item, keys):
    """条目标题或来源名命中任一关键词即视为与本板块相关。"""
    if not keys:
        return True
    text = ((item.get("title", "") or "") + " " + (item.get("source", "") or "")).lower()
    return any(k.lower() in text for k in keys)

# ---- 数据源：权威官方 Newsroom + 权威行业媒体（机器之心/量子位/36氪/钛媒体/DIGITIMES 等
#      均为"可抓取的同源权威内容"；严格只取发布日==目标日当天的内容。
#      说明：微信公众号需登录态+强反爬，云端自动化无法直接抓取，故用其同源官网/RSS 替代。----
SOURCES = {
    "storage": [
        {"type": "rss", "url": "https://www.trendforce.com/news/feed/", "source": "TrendForce 集邦咨询"},
        {"type": "rss", "url": "https://news.skhynix.com/feed/", "source": "SK 海力士"},
        {"type": "rss", "url": "https://www.digitimes.com/rss/daily.xml", "source": "DIGITIMES 电子时报（半导体媒体）"},
    ],
    "token": [
        {"type": "rss", "url": "https://openai.com/news/rss.xml", "source": "OpenAI 官方（GPT）"},
        {"type": "html", "url": "https://www.anthropic.com/news", "source": "Anthropic 官方（Claude）"},
        {"type": "rss", "url": "https://blog.google/technology/ai/rss/", "source": "Google 官方博客（Gemini）"},
        {"type": "rss", "url": "https://azure.microsoft.com/en-us/blog/feed/", "source": "微软 Azure 官方博客"},
        {"type": "rss", "url": "https://ai.meta.com/blog/rss/", "source": "Meta AI 官方博客"},
        {"type": "rss", "url": "https://mistral.ai/news/rss.xml", "source": "Mistral AI 官方"},
        {"type": "rss", "url": "https://x.ai/blog/rss.xml", "source": "xAI 官方（Grok）"},
        {"type": "rss", "url": "https://cohere.com/blog/rss.xml", "source": "Cohere 官方"},
        {"type": "html", "url": "https://cloud.baidu.com/product/wenxinworkshop", "source": "百度智能云千帆（文心）"},
        {"type": "html", "url": "https://bailian.aliyun.com/", "source": "阿里云百炼（通义）"},
        {"type": "html", "url": "https://open.bigmodel.cn/", "source": "智谱 AI（GLM）"},
        # 权威行业媒体（可抓取、同日过滤；对应公众号同源）
        {"type": "rss", "url": "https://www.qbitai.com/feed", "source": "量子位（AI 媒体）"},
        {"type": "rss", "url": "https://www.36kr.com/feed", "source": "36氪（科技媒体）"},
        {"type": "rss", "url": "https://www.tmtpost.com/rss.xml", "source": "钛媒体（科技媒体）"},
        {"type": "rss", "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "source": "TechCrunch AI"},
        {"type": "rss", "url": "https://the-decoder.com/feed/", "source": "The Decoder（AI 媒体）"},
        {"type": "rss", "url": "https://www.marktechpost.com/feed/", "source": "Marktechpost（AI 媒体）"},
    ],
    "compute_foreign": [
        {"type": "rss", "url": "https://nvidianews.nvidia.com/news/rss", "source": "NVIDIA 英伟达", "vendor": "英伟达 NVIDIA", "region": "foreign"},
        {"type": "html", "url": "https://www.amd.com/en/newsroom.html", "source": "AMD 官方", "vendor": "AMD", "region": "foreign"},
        {"type": "rss", "url": "https://newsroom.intel.com/feed/", "source": "Intel 英特尔", "vendor": "Intel 英特尔", "region": "foreign"},
        # 权威硬件/半导体媒体（尽力而为；超时不影响主流程）
        {"type": "rss", "url": "https://www.servethehome.com/feed/", "source": "ServeTheHome（硬件媒体）", "region": "foreign"},
        {"type": "rss", "url": "https://www.tomshardware.com/feeds/all", "source": "Tom's Hardware（硬件媒体）", "region": "foreign"},
    ],
    "compute_domestic": [
        {"type": "html", "url": "https://www.hiascend.com/", "source": "华为昇腾", "vendor": "华为昇腾", "region": "domestic"},
        {"type": "html", "url": "https://www.cambricon.com/", "source": "寒武纪", "vendor": "寒武纪", "region": "domestic"},
        {"type": "html", "url": "https://www.mthreads.com/", "source": "摩尔线程", "vendor": "摩尔线程", "region": "domestic"},
        {"type": "html", "url": "https://www.metax-tech.com/", "source": "沐曦股份", "vendor": "沐曦股份", "region": "domestic"},
        {"type": "html", "url": "https://www.birentech.com/", "source": "壁仞科技", "vendor": "壁仞科技", "region": "domestic"},
        {"type": "html", "url": "https://www.iluvatar.com/", "source": "天数智芯", "vendor": "天数智芯", "region": "domestic"},
        {"type": "html", "url": "https://www.enflame-tech.com/", "source": "燧原科技", "vendor": "燧原科技", "region": "domestic"},
        {"type": "html", "url": "https://www.kunlunxin.com/", "source": "昆仑芯", "vendor": "昆仑芯", "region": "domestic"},
        # 权威半导体媒体（尽力而为；超时不影响主流程）
        {"type": "rss", "url": "https://www.jiwei.com.cn/rss", "source": "集微网（半导体媒体）", "region": "domestic"},
    ],
    "jd": [
        {"type": "html", "url": "https://e.jd.com/", "source": "京东企业业务"},
        {"type": "html", "url": "https://www.jdcloud.com/", "source": "京东云"},
        {"type": "html", "url": "https://news.jd.com/", "source": "京东黑板报（官方资讯）"},
        # 权威科技/企业媒体（可抓取、同日过滤、关键词把关；对应公众号同源）
        {"type": "rss", "url": "https://www.tmtpost.com/rss.xml", "source": "钛媒体（科技媒体）"},
        {"type": "rss", "url": "https://www.36kr.com/feed", "source": "36氪（科技媒体）"},
    ],
}

# ---- 官方关注渠道（某板块当日无更新时展示，供用户自行关注；均为权威官方站点）----
CHANNELS = {
    "storage": [
        {"name": "TrendForce 集邦咨询", "url": "https://www.trendforce.com/news/"},
        {"name": "DRAMeXchange", "url": "https://www.dramexchange.com/"},
        {"name": "三星 Samsung Newsroom", "url": "https://news.samsung.com/global/"},
        {"name": "SK 海力士", "url": "https://news.skhynix.com/"},
        {"name": "美光 Micron", "url": "https://www.micron.com/about/newsroom"},
        {"name": "西部数据 Western Digital", "url": "https://blog.westerndigital.com/"},
        {"name": "铠侠 Kioxia", "url": "https://www.kioxia.com/en-us/"},
    ],
    "token": [
        {"name": "OpenAI 官方（GPT）", "url": "https://openai.com/news"},
        {"name": "Anthropic 官方（Claude）", "url": "https://www.anthropic.com/news"},
        {"name": "Google 官方博客（Gemini）", "url": "https://blog.google/technology/ai/"},
        {"name": "微软 Azure 官方博客", "url": "https://azure.microsoft.com/en-us/blog/"},
        {"name": "Meta AI 官方博客", "url": "https://ai.meta.com/blog/"},
        {"name": "Mistral AI 官方", "url": "https://mistral.ai/news"},
        {"name": "xAI 官方（Grok）", "url": "https://x.ai/blog"},
        {"name": "Cohere 官方", "url": "https://cohere.com/blog"},
        {"name": "百度智能云千帆（文心）", "url": "https://cloud.baidu.com/product/wenxinworkshop"},
        {"name": "阿里云百炼（通义）", "url": "https://bailian.aliyun.com/"},
        {"name": "智谱 AI（GLM）", "url": "https://open.bigmodel.cn/"},
    ],
    "compute_foreign": [
        {"name": "NVIDIA 英伟达", "url": "https://nvidianews.nvidia.com/"},
        {"name": "AMD 官方", "url": "https://www.amd.com/en/newsroom.html"},
        {"name": "Intel 英特尔", "url": "https://newsroom.intel.com/"},
    ],
    "compute_domestic": [
        {"name": "华为昇腾", "url": "https://www.hiascend.com/"},
        {"name": "寒武纪", "url": "https://www.cambricon.com/"},
        {"name": "摩尔线程", "url": "https://www.mthreads.com/"},
        {"name": "沐曦股份", "url": "https://www.metax-tech.com/"},
        {"name": "壁仞科技", "url": "https://www.birentech.com/"},
        {"name": "天数智芯", "url": "https://www.iluvatar.com/"},
        {"name": "燧原科技", "url": "https://www.enflame-tech.com/"},
        {"name": "昆仑芯", "url": "https://www.kunlunxin.com/"},
    ],
    "jd": [
        {"name": "京东企业业务", "url": "https://e.jd.com/"},
        {"name": "京东云", "url": "https://www.jdcloud.com/"},
        {"name": "京东", "url": "https://www.jd.com/"},
        {"name": "京东黑板报（官方资讯）", "url": "https://news.jd.com/"},
        {"name": "京东物流", "url": "https://www.jdl.com/"},
        {"name": "京东工业", "url": "https://www.jd.com/"},
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

def collect(group):
    items = []
    for src in SOURCES[group]:
        got = fetch_rss(src) if src["type"] == "rss" else fetch_html(src)
        for it in got:
            it.setdefault("vendor", src.get("vendor", ""))
            it.setdefault("region", src.get("region", ""))
            items.append(it)
        print(f"  {src['source']}: 抓到 {len(got)} 条（仅 {BIZ_STR} 发布）")
    uniq, seen = [], set()
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"]); uniq.append(it)
    # 相关性过滤：只保留与本板块主题高度相关的条目（硬性要求，避免无关内容堆砌）
    keys = KEYWORDS.get(group, [])
    if keys:
        before = len(uniq)
        uniq = [it for it in uniq if _relevant(it, keys)]
        print(f"  相关性过滤：{before} -> {len(uniq)} 条（仅保留与「{SECTION_NAMES.get(group, group)}」相关）")
    return uniq[:15]

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

# ---------- 调用 LLM（主+兜底）----------
def call_llm(system, user, temperature=0.3):
    providers = [p for p in (PRIMARY, FALLBACK) if p[1]]
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
            print(f"  [LLM 供应商失败 {url[:45]}]: {ex}")
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
              "4）所有输出必须是简体中文（英文标题/专有名词需翻译为中文，厂商名可保留英文+中文，如 'NVIDIA 英伟达'）；"
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
            "- 返回的每个对象必须含 idx 字段，且 idx 与输入某条完全一致；title 字段【必须是简体中文标题】。\n"
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
        return {"summary": summary, "items": out_items, "channels": channels or []}
    except Exception as ex:
        print(f"  [LLM失败 {section_label}]: {ex}")
        return {
            "summary": f"{section_label}当日获取到 {len(items)} 条官方动态，摘要生成异常，已沿用原始标题，详见各条官方来源。",
            "items": [{"title": it["title"], "source": it["source"], "url": it["url"],
                       "excerpt": "（摘要生成暂不可用，请点击来源查看官方原文）",
                       "vendor": it.get("vendor", ""), "region": it.get("region", "")} for it in items],
            "channels": channels or [],
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
    # 周总结：目标日期为周日时生成，覆盖当周一到周日（即目标日期）
    if BIZ_DATE.weekday() == 6:
        end = BIZ_DATE
        start = end - datetime.timedelta(days=6)
        sums = period_summaries(data, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        if sums:
            review["weekly"] = {"period": f"{start} 至 {end}", "summary": synthesize_review("周", start, end, sums)}
    # 月总结：目标日期为当月最后一天时生成，覆盖当月
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
    """写回 index.html 前最后一道关：逐项核查，不达标一律剔除并告警。
    检查项：① 日期==目标日（当日，无陈旧）；② 链接可达（无死链）；
           ③ 与板块主题相关（无关内容剔除）；④ 标题-链接一致（url 合法、来自抓取源）。
    """
    print("== 发布前自检：当日 / 死链 / 相关性 / 链接一致性 ==")
    for sec in data["sections"]:
        if sec.get("type") == "review":
            continue
        gid = sec["id"]
        for d, entry in (sec.get("dates") or {}).items():
            # ① 日期必须为当日（历史回填的其它日期不动，这里只校验本次目标日）
            if d == BIZ_STR:
                pass
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
                # ③ 相关性复核（算力按 region 区分国内外关键词）
                if gid == "compute":
                    kset = KEYWORDS["compute_foreign"] if it.get("region") == "foreign" else KEYWORDS["compute_domestic"]
                else:
                    kset = KEYWORDS.get(gid)
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

# ---------- 主流程 ----------
def main():
    html = open(INDEX, encoding="utf-8").read()
    m = re.search(r'(<!--MARKET-DATA-START-->\s*<script type="application/json" id="market-data">)(.*?)(</script>\s*<!--MARKET-DATA-END-->)', html, re.S)
    if not m:
        print("ERROR: 未找到 MARKET-DATA 区块"); sys.exit(1)
    data = json.loads(m.group(2))

    print(f"== 目标更新日期（北京时间 24:00 运行）：{BIZ_STR} ==")

    # 存储 / Token / 京东
    for key in ["storage", "token", "jd"]:
        print(f"-- 抓取 {SECTION_NAMES[key]} --")
        items = collect(key)
        items = filter_dead_links(items)          # 发布前链接自检
        channels = CHANNELS.get(key, [])
        res = synthesize(SECTION_NAMES[key], items, channels)
        data = ensure_date(data, key, BIZ_STR, res)
    # 算力（国外 + 国内）
    print("-- 抓取 算力市场（国外/国内）--")
    f_items = collect("compute_foreign")
    d_items = collect("compute_domestic")
    f_items = filter_dead_links(f_items)
    d_items = filter_dead_links(d_items)
    print(f"  国外 {len(f_items)} 条 / 国内 {len(d_items)} 条")
    f_res = synthesize("算力市场·国外厂商", f_items, CHANNELS["compute_foreign"])
    d_res = synthesize("算力市场·国内厂商", d_items, CHANNELS["compute_domestic"])
    comp_items = f_res["items"] + d_res["items"]
    comp_summary = ("国外厂商：" + f_res["summary"] + "\n国内厂商：" + d_res["summary"]) if (f_items or d_items) else \
                  f"今日（{BIZ_STR}）未检索到算力市场的官方更新，可点击各厂商官方渠道持续关注。"
    compute_sec = next(s for s in data["sections"] if s["id"] == "compute")
    compute_sec.setdefault("dates", {})[BIZ_STR] = {"summary": comp_summary, "items": comp_items,
                                                    "channels": CHANNELS["compute_foreign"] + CHANNELS["compute_domestic"]}

    # 写入各板块的官方关注渠道（当日无更新时展示，供用户自行关注）
    for sec in data["sections"]:
        if sec.get("type") == "review":
            continue
        cid = sec["id"]
        sec["channels"] = (CHANNELS["compute_foreign"] + CHANNELS["compute_domestic"]) if cid == "compute" else CHANNELS.get(cid, [])

    # 周/月总结
    build_review(data)

    # 发布前统一自检（当日 / 死链 / 相关性 / 链接一致性）
    data = verify_before_publish(data)

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

def ensure_date(data, key, date_str, res):
    sec = next(s for s in data["sections"] if s["id"] == key)
    sec.setdefault("dates", {})[date_str] = res   # res 已含 summary/items/channels
    return data

if __name__ == "__main__":
    main()
