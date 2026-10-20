#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场动态看板 · 数据生成脚本
- 抓取各板块"权威官方来源"的最新动态（RSS / 官网新闻页）
- 调用 DeepSeek 写成书面化、小白可读的小结与条目（URL 均来自真实抓取的官方页）
- 写回 index.html 中 <!--MARKET-DATA-START--> ~ <!--MARKET-DATA-END--> 之间的 JSON
- 保留近 30 天历史；每周一生成周总结、每月 1 日生成月总结
仅改写数据区块，绝不改动页面其它代码。
"""
import os, re, json, sys, datetime
import requests
from bs4 import BeautifulSoup
import feedparser
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")
API_URL = os.environ.get("LLM_API_URL", "https://api.siliconflow.cn/v1/chat/completions")
API_KEY = os.environ.get("LLM_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "Qwen/Qwen2.5-7B-Instruct")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
TZ = ZoneInfo("Asia/Shanghai")
TODAY = datetime.datetime.now(TZ).date()
TODAY_STR = TODAY.strftime("%Y-%m-%d")

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

NEWS_HREF = re.compile(r"/(news|article|press|blog|detail|info|release|announce|posts?|view|show|content|story|c/|n/|a/|html)", re.I)
SECTION_NAMES = {
    "storage": "存储市场", "token": "Token 市场",
    "compute": "算力市场", "jd": "京东政企市场",
}

# ---------- 抓取 ----------
def fetch_rss(src, limit=8):
    try:
        d = feedparser.parse(src["url"])
        items = []
        for e in (d.entries or [])[:limit]:
            title = (e.title or "").strip()
            link = (e.link or "").strip()
            if title and link:
                items.append({"title": title, "url": link, "source": src["source"]})
        return items
    except Exception as ex:
        print(f"  [RSS失败] {src['source']}: {ex}")
        return []

def fetch_html(src, limit=8):
    try:
        r = requests.get(src["url"], headers={"User-Agent": UA}, timeout=15)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        seen, items = set(), []
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            href = a["href"]
            if not text or len(text) < 8 or len(text) > 60:
                continue
            if not NEWS_HREF.search(href):
                continue
            if not href.startswith("http"):
                href = requests.compat.urljoin(src["url"], href)
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
        print(f"  {src['source']}: 抓到 {len(got)} 条")
    # 去重（按 url）
    uniq, seen = [], set()
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"]); uniq.append(it)
    return uniq[:12]

# ---------- 调用 DeepSeek ----------
def call_llm(system, user, temperature=0.3):
    if not API_KEY:
        raise RuntimeError("缺少 LLM_API_KEY 环境变量")
    resp = requests.post(API_URL,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json={"model": MODEL, "temperature": temperature,
              "response_format": {"type": "json_object"},
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}]},
        timeout=120)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]

def synthesize(section_label, items, is_compute=False):
    if not items:
        return {
            "summary": f"今日未检索到{section_label}的重大官方更新，可点击下方官方渠道查看最新动态。",
            "items": [],
        }
    payload = [{"title": i["title"], "source": i["source"], "url": i["url"]} for i in items]
    system = ("你是中国市场动态看板的分析师，面向不懂技术的小白用户。"
              "要求：1）书面化但不口语化；2）无门槛可读，对 GPU、HBM、算力、昇腾、信创、上下文窗口等术语可自然使用（前端会自动加注释）；"
              "3）严格基于提供的官方条目，不得编造；4）每条的 url/source/title 必须原样保留，不得改动或新增链接。")
    user = (f"以下是「{section_label}」今日检索到的官方动态条目（已附真实官方链接）：\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            "请输出 JSON：\n"
            "{\n"
            '  "summary": "2-4 句书面化每日小结，概括今日该板块整体动向",\n'
            '  "items": [ {"title":原样,"source":原样,"url":原样,"excerpt":"基于该条官方内容写的一句书面化摘要(30-60字)"} ]\n'
            "}\n"
            "items 顺序与输入一致、数量一致，url 与 source 必须与输入完全相同。")
    try:
        txt = call_llm(system, user)
        data = json.loads(txt)
        summary = data.get("summary", "")
        out_items = []
        for orig, gen in zip(items, data.get("items", [])):
            out_items.append({
                "title": gen.get("title", orig["title"]),
                "source": gen.get("source", orig["source"]),
                "url": gen.get("url", orig["url"]),
                "excerpt": gen.get("excerpt", ""),
                "vendor": orig.get("vendor", ""),
                "region": orig.get("region", ""),
            })
        return {"summary": summary, "items": out_items}
    except Exception as ex:
        print(f"  [LLM失败 {section_label}]: {ex}")
        # 兜底：保留原始条目，摘要说明
        return {
            "summary": f"{section_label}今日获取到 {len(items)} 条官方动态，摘要生成异常，已沿用原始标题，详见各条官方来源。",
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

def build_review(data):
    review = next((s for s in data["sections"] if s.get("type") == "review"), None)
    if not review:
        return
    # 周总结：周一生成，覆盖上周一至上周日
    if TODAY.weekday() == 0:
        end = TODAY - datetime.timedelta(days=1)
        start = end - datetime.timedelta(days=6)
        sums = period_summaries(data, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        if sums:
            review["weekly"] = {"period": f"{start} 至 {end}",
                                "summary": synthesize_review("周", start, end, sums)}
    # 月总结：每月 1 日生成，覆盖上月
    if TODAY.day == 1:
        first_this = TODAY.replace(day=1)
        last_prev = first_this - datetime.timedelta(days=1)
        start = last_prev.replace(day=1)
        sums = period_summaries(data, start.strftime("%Y-%m-%d"), last_prev.strftime("%Y-%m-%d"))
        if sums:
            review["monthly"] = {"period": f"{start} 至 {last_prev}",
                                 "summary": synthesize_review("月", start, last_prev, sums)}

def synthesize_review(kind, start, end, sums):
    system = ("你是中国市场动态看板的分析师，面向不懂技术的小白用户。"
              "要求：书面化但不口语化、无门槛可读；严格基于提供的各板块每日小结，不得编造；"
              "可做横向对比与趋势概括，对专业术语自然使用（前端会自动注释）。")
    user = (f"以下是 {start} 至 {end} 各板块的每日小结，请综合写成一段「{kind}总结」（4-6 句，概括四大市场整体动向与趋势）：\n"
            f"{json.dumps(sums, ensure_ascii=False, indent=2)}")
    try:
        return call_llm(system, user, temperature=0.4)
    except Exception as ex:
        print(f"  [REVIEW LLM失败]: {ex}")
        return f"{kind}总结生成失败，请稍后查看各板块每日小结。"

# ---------- 主流程 ----------
def main():
    if not API_KEY:
        print("ERROR: 未设置 LLM_API_KEY，退出。"); sys.exit(1)
    html = open(INDEX, encoding="utf-8").read()
    m = re.search(r'(<!--MARKET-DATA-START-->\s*<script type="application/json" id="market-data">)(.*?)(</script>\s*<!--MARKET-DATA-END-->)', html, re.S)
    if not m:
        print("ERROR: 未找到 MARKET-DATA 区块"); sys.exit(1)
    data = json.loads(m.group(2))

    print(f"== 生成日期：{TODAY_STR} ==")
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
                  "今日未检索到算力市场的重大官方更新，可点击各厂商官方渠道查看。"
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
