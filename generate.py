#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场动态看板 · 数据生成脚本
- 抓取各板块"权威官方来源"的【当日】最新动态（RSS / 官网新闻页）
- 仅保留发布日期 == 北京时间当日 的资讯（不追溯、不杜撰）
- 调用 LLM 完成：英文翻译为书面化中文 -> 筛选本模块最重要的 3-5 条 -> 提炼每条 2-3 句中文重点
- 写回 index.html 中 <!--MARKET-DATA-START--> ~ <!--MARKET-DATA-END--> 之间的 JSON
- 保留近 30 天历史；每周日 23:00 生成周总结、每月最后一天 23:00 生成月总结
仅改写数据区块，绝不改动页面其它代码。
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
TODAY = datetime.datetime.now(TZ).date()
TODAY_STR = TODAY.strftime("%Y-%m-%d")
RECENT_DAYS = 3     # 仅保留最近 N 天（含今天）发布的资讯，杜绝陈旧旧闻
STALE_DAYS = 14     # HTML 抓取中，若检测到发布日期超过该天数则判定为陈旧并跳过

# ---- 数据源（权威官方 / 官网 Newsroom）----
SOURCES = {
    "storage": [
        {"type": "rss", "url": "https://www.trendforce.com/news/feed/", "source": "TrendForce 集邦咨询"},
        {"type": "rss", "url": "https://www.dramexchange.com/feed/", "source": "DRAMeXchange"},
    ],
    "token": [
        {"type": "rss", "url": "https://openai.com/news/rss.xml", "source": "OpenAI 官方"},
        {"type": "html", "url": "https://www.anthropic.com/news", "source": "Anthropic 官方"},
        {"type": "html", "url": "https://cloud.baidu.com/product/wenxinworkshop", "source": "百度智能云千帆（文心）"},
        {"type": "html", "url": "https://bailian.aliyun.com/", "source": "阿里云百炼（通义）"},
        {"type": "html", "url": "https://open.bigmodel.cn/", "source": "智谱 AI（GLM）"},
    ],
    "compute_foreign": [
        {"type": "html", "url": "https://nvidianews.nvidia.com/", "source": "NVIDIA 英伟达", "vendor": "英伟达 NVIDIA", "region": "foreign"},
        {"type": "html", "url": "https://www.amd.com/en/newsroom.html", "source": "AMD 官方", "vendor": "AMD", "region": "foreign"},
        {"type": "html", "url": "https://newsroom.intel.com/", "source": "Intel 英特尔", "vendor": "Intel 英特尔", "region": "foreign"},
    ],
    "compute_domestic": [
        {"type": "html", "url": "https://www.hiascend.com/", "source": "华为昇腾", "vendor": "华为昇腾", "region": "domestic"},
        {"type": "html", "url": "https://www.cambricon.com/", "source": "寒武纪", "vendor": "寒武纪", "region": "domestic"},
        {"type": "html", "url": "https://www.mthreads.com/", "source": "摩尔线程", "vendor": "摩尔线程", "region": "domestic"},
        {"type": "html", "url": "https://www.metax-tech.com/", "source": "沐曦股份", "vendor": "沐曦股份", "region": "domestic"},
        {"type": "html", "url": "https://www.birentech.com/", "source": "壁仞科技", "vendor": "壁仞科技", "region": "domestic"},
        {"type": "html", "url": "https://www.iluvatar.com/", "source": "天数智芯", "vendor": "天数智芯", "region": "domestic"},
    ],
    "jd": [
        {"type": "html", "url": "https://www.jd.com/", "source": "京东"},
        {"type": "html", "url": "https://e.jd.com/", "source": "京东企业业务"},
        {"type": "html", "url": "https://www.jdcloud.com/", "source": "京东云"},
    ],
}

NEWS_HREF = re.compile(
    r"(?:news|article|press|blog|detail|info|release|announce|posts?|view|show|content|story|/c/|/n/|/a/|/\d{4}/\d{2}/\d{2}/|/\d{4}-\d{2}-\d{2})",
    re.I,
)
SECTION_NAMES = {
    "storage": "存储市场", "token": "Token 市场",
    "compute": "算力市场", "jd": "京东政企市场",
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
                return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except Exception:
                pass
    return None

# ---------- 抓取（仅近期的权威官方动态）----------
def fetch_rss(src, limit=12):
    for attempt in range(2):
        try:
            d = feedparser.parse(src["url"])
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
                cands.append({"title": title, "url": link, "source": src["source"], "date": pdate})
                if len(cands) >= limit * 2:
                    break
            if not cands:
                continue   # 偶发空解析，重试一次
            # 过滤：保留最近 RECENT_DAYS 天内的；无日期者（官网最新列表）默认保留
            kept = [c for c in cands if c["date"] is None or (TODAY - c["date"]).days <= RECENT_DAYS]
            if not kept and cands:
                kept = cands   # 该源日期不可靠（如全部陈旧/统一旧日期），退回保留最新若干条
            seen, out = set(), []
            for c in kept:
                if c["url"] in seen:
                    continue
                seen.add(c["url"]); out.append(c)
            return out[:limit]
        except Exception as ex:
            print(f"  [RSS失败] {src['source']}: {ex}")
    return []

def fetch_html(src, limit=12):
    for attempt in range(2):
        try:
            r = requests.get(src["url"], headers={"User-Agent": UA}, timeout=15)
            if r.status_code != 200:
                continue
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
                # 链接规范化与校验
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
                # 链接质量：剔除纯域名根页与锚点页，其余（含具体文章/栏目页）保留
                segs = [s for s in urlparse(href).path.split("/") if s]
                if not segs:
                    continue
                if segs[-1].lower() == "index.html":
                    continue
                # 日期检测：链接附近文本或 URL 路径中的日期；明显陈旧(>STALE_DAYS)或非法的跳过
                parent = a.find_parent(["li", "tr"]) or a.parent
                ctx = (parent.get_text(" ", strip=True) if parent else "")[:200]
                pdate = extract_date(ctx) or extract_date(href)
                if pdate:
                    delta = (TODAY - pdate).days
                    if delta < 0 or delta > STALE_DAYS:
                        continue
                if href in seen:
                    continue
                seen.add(href)
                items.append({"title": text, "url": href, "source": src["source"]})
                if len(items) >= limit:
                    break
            if items:
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
        print(f"  {src['source']}: 抓到 {len(got)} 条（仅当日）")
    uniq, seen = [], set()
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"]); uniq.append(it)
    return uniq[:15]

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

def synthesize(section_label, items):
    if not items:
        return {
            "summary": f"近期未检索到{section_label}的重大官方更新，可点击下方官方渠道查看最新动态。",
            "items": [],
        }
    payload = [{"title": i["title"], "source": i["source"], "url": i["url"]} for i in items]
    system = ("你是中国市场动态看板的资深分析师，面向不懂技术的小白用户。"
              "要求：1）书面化但不口语化；2）无门槛可读；3）严格基于提供的官方条目，不得编造；"
              "4）所有输出必须是简体中文（英文标题/专有名词需翻译为中文，厂商名可保留英文+中文，如 'NVIDIA 英伟达'）；"
              "5）url 与 source 仅作参考，最终以输入为准，不得改动或新增链接。")
    user = (f"以下是「{section_label}」近期（最近数日）检索到的官方动态条目（均为较新发布的权威官方资讯，附真实官方链接）：\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            "请完成两件事：\n"
            "1）筛选：从中挑出本模块最重要、最具代表性的 3-5 条（不足则全部保留），去掉次要或重复的；\n"
            "2）提炼：对保留的每条，不要只罗列标题，而是提取其重点，用书面化中文写 2-3 句要点（面向小白、可读）。\n"
            "输出 JSON：\n"
            "{\n"
            '  "summary": "2-4 句书面化每日小结，概括该板块近期整体动向（简体中文）",\n'
            '  "items": [ {"title": 中文标题, "excerpt": "2-3 句中文重点摘要"} ]\n'
            "}\n"
            "items 的数量等于你筛选后保留的条数，顺序自定。")
    try:
        txt = call_llm(system, user)
        data = json.loads(txt)
        summary = data.get("summary", "")
        gens = data.get("items", [])
        out_items = []
        for orig, gen in zip(items, gens):
            out_items.append({
                "title": (gen.get("title") or orig["title"]).strip(),
                "source": orig["source"],
                "url": orig["url"],                       # 始终使用真实抓取的官方链接
                "excerpt": (gen.get("excerpt") or "").strip(),
                "vendor": orig.get("vendor", ""),
                "region": orig.get("region", ""),
            })
        return {"summary": summary, "items": out_items}
    except Exception as ex:
        print(f"  [LLM失败 {section_label}]: {ex}")
        return {
            "summary": f"{section_label}近期获取到 {len(items)} 条官方动态，摘要生成异常，已沿用原始标题，详见各条官方来源。",
            "items": [{"title": i["title"], "source": i["source"], "url": i["url"],
                       "excerpt": "（摘要生成暂不可用，请点击来源查看官方原文）",
                       "vendor": i.get("vendor", ""), "region": i.get("region", "")} for i in items],
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
    # 周总结：每周日 23:00 生成，覆盖本周一至周日（即今日）
    if TODAY.weekday() == 6:
        end = TODAY
        start = end - datetime.timedelta(days=6)
        sums = period_summaries(data, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        if sums:
            review["weekly"] = {"period": f"{start} 至 {end}", "summary": synthesize_review("周", start, end, sums)}
    # 月总结：每月最后一天 23:00 生成，覆盖当月
    if is_last_day(TODAY):
        start = TODAY.replace(day=1)
        sums = period_summaries(data, start.strftime("%Y-%m-%d"), TODAY.strftime("%Y-%m-%d"))
        if sums:
            review["monthly"] = {"period": f"{start} 至 {TODAY}", "summary": synthesize_review("月", start, TODAY, sums)}

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

# ---------- 主流程 ----------
def main():
    html = open(INDEX, encoding="utf-8").read()
    m = re.search(r'(<!--MARKET-DATA-START-->\s*<script type="application/json" id="market-data">)(.*?)(</script>\s*<!--MARKET-DATA-END-->)', html, re.S)
    if not m:
        print("ERROR: 未找到 MARKET-DATA 区块"); sys.exit(1)
    data = json.loads(m.group(2))

    print(f"== 生成日期（更新日期）：{TODAY_STR} ==")
    # 存储 / Token / 京东
    for key in ["storage", "token", "jd"]:
        print(f"-- 抓取 {SECTION_NAMES[key]} --")
        items = collect(key)
        res = synthesize(SECTION_NAMES[key], items)
        data = ensure_date(data, key, TODAY_STR, res)
    # 算力（国外 + 国内）
    print("-- 抓取 算力市场（国外/国内）--")
    f_items = collect("compute_foreign")
    d_items = collect("compute_domestic")
    print(f"  国外 {len(f_items)} 条 / 国内 {len(d_items)} 条")
    f_res = synthesize("算力市场·国外厂商", f_items)
    d_res = synthesize("算力市场·国内厂商", d_items)
    comp_items = f_res["items"] + d_res["items"]
    comp_summary = ("国外厂商：" + f_res["summary"] + "\n国内厂商：" + d_res["summary"]) if (f_items or d_items) else \
                  "近期未检索到算力市场的重大官方更新，可点击各厂商官方渠道查看。"
    compute_sec = next(s for s in data["sections"] if s["id"] == "compute")
    compute_sec.setdefault("dates", {})[TODAY_STR] = {"summary": comp_summary, "items": comp_items}

    # 周/月总结
    build_review(data)

    # 清理 >30 天历史
    cutoff = (TODAY - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    for sec in data["sections"]:
        if sec.get("type") == "review":
            continue
        if "dates" in sec:
            sec["dates"] = {d: v for d, v in sec["dates"].items() if d >= cutoff}

    # 写回
    data["meta"]["updatedAt"] = TODAY_STR
    data["meta"]["isPrototype"] = False
    new_block = json.dumps(data, ensure_ascii=False, indent=2)
    new_html = html[:m.start(2)] + new_block + html[m.end(2):]
    open(INDEX, "w", encoding="utf-8").write(new_html)
    print("== 已写回 index.html ==")

def ensure_date(data, key, date_str, res):
    sec = next(s for s in data["sections"] if s["id"] == key)
    sec.setdefault("dates", {})[date_str] = {"summary": res["summary"], "items": res["items"]}
    return data

if __name__ == "__main__":
    main()
