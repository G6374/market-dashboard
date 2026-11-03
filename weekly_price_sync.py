#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本机「京东抓价结果 → 看板」同步脚本（每周四由 WorkBuddy 自动化调用）

职责边界（重要）：
  * 只读取本机抓取脚本产出的 prices_<date>.json，生成并上传一个
    纯价格文件 price_snapshot.json 到 GitHub 仓库。
  * 绝不读取、复制、上传任何京东登录态：jd_state.json / cookies / 账号密码
    均留在本机，仅由 jd_scrape.js 在本机浏览器内使用。
  * 不直接改 index.html，避免与每日 24:00 的资讯更新互相覆盖。
    index.html 的合并与部署由云端 .github/workflows/price-update.yml 完成。

数据完整性：
  * 抓到的价格不足 --min-ok 条时，拒绝上传，看板保留上一期数据（绝不编数字）。
  * 单款没抓到时该格为空（前端显示「—」），不拿旧价冒充新价。

用法：
  python weekly_price_sync.py                      # 自动找当天抓取结果并提交
  python weekly_price_sync.py --date 2026-11-03    # 指定日期
  python weekly_price_sync.py --prices <路径>      # 指定抓取结果 JSON
  python weekly_price_sync.py --dry-run            # 只生成不提交
  python weekly_price_sync.py --min-ok 18          # 提高入库门槛
"""
import argparse
import base64
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

REPO = "G6374/market-dashboard"
SNAPSHOT = "price_snapshot.json"
TOOLS_TMP = r"D:\文件集\WorkBuddy\2026-10-28-10-38-22\.workbuddy\tmp"
BJ = dt.timezone(dt.timedelta(hours=8))


def today_bj():
    return dt.datetime.now(BJ).date().isoformat()


def pat_from_git():
    """PAT 从 git remote 解析（https://<PAT>@github.com/...），不硬编码。"""
    url = subprocess.check_output(
        ["git", "config", "--get", "remote.origin.url"], text=True
    ).strip()
    m = re.match(r"https://([^@]+)@github\.com/", url)
    if not m:
        raise RuntimeError("git remote 中未找到 PAT，无法调用 GitHub API")
    return m.group(1)


def gh(pat, path, method="GET", payload=None):
    url = "https://api.github.com/repos/" + REPO + "/" + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + pat,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "weekly-price-sync",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def put_snapshot(pat, content_bytes, message):
    """PUT contents，base sha 取自 git/trees（大文件 GET 常被截断，tree 响应稳定）。"""
    last = None
    for attempt in range(3):
        tree = gh(pat, "git/trees/main?recursive=1")
        sha = next((x["sha"] for x in tree["tree"] if x["path"] == SNAPSHOT), None)
        payload = {
            "message": message,
            "content": base64.b64encode(content_bytes).decode("ascii"),
            "branch": "main",
        }
        if sha:
            payload["sha"] = sha
        try:
            return gh(pat, "contents/" + SNAPSHOT, "PUT", payload)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 409 and attempt < 2:   # 期间有其它提交，重新取 sha 再试
                continue
            raise
    raise last


def find_prices(date, explicit):
    if explicit:
        if not os.path.isfile(explicit):
            raise RuntimeError("指定的抓取结果文件不存在: " + explicit)
        return explicit
    p = os.path.join(TOOLS_TMP, "prices_%s.json" % date)
    if os.path.isfile(p):
        return p
    # 兜底：取最近一份抓取结果
    cands = []
    for fn in os.listdir(TOOLS_TMP):
        m = re.match(r"prices_(\d{4}-\d{2}-\d{2})\.json$", fn)
        if m:
            cands.append((m.group(1), os.path.join(TOOLS_TMP, fn)))
    if not cands:
        raise RuntimeError("未找到任何 prices_<date>.json 抓取结果")
    cands.sort()
    return cands[-1][1]


def build_snapshot(path, min_ok):
    d = json.load(open(path, encoding="utf-8"))
    date = str(d.get("date") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise RuntimeError("抓取结果缺少合法 date 字段")
    prices = {}
    for it in d.get("items", []):
        sku = str(it.get("sku") or "").strip()
        try:
            p = float(it.get("price") or 0)
        except (TypeError, ValueError):
            continue
        # 与 jd_scrape.js 一致：低于 20 元视为解码/打码异常，丢弃
        if sku and p >= 20:
            prices[sku] = round(p, 2)
    return {
        "date": date,
        "captured_at": dt.datetime.now(BJ).isoformat(timespec="seconds"),
        "ok_count": len(prices),
        "total": 24,
        "source": "京东登录态抓取（本机 Playwright + 字体解码）",
        "prices": prices,   # sku -> 价格；未抓到的型号不出现在此字典
    }, date


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=today_bj())
    ap.add_argument("--prices", default="")
    ap.add_argument("--min-ok", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = find_prices(args.date, args.prices)
    snap, date = build_snapshot(src, args.min_ok)
    print("== 内存价格同步（本机抓取 → 看板）==")
    print("  抓取结果：%s" % src)
    print("  本期日期：%s，有效价格 %s/%s" % (date, snap["ok_count"], snap["total"]))

    if snap["ok_count"] < args.min_ok:
        print("  ABORT 有效价格不足 %s 条，本次不提交；看板保留上一期数据（不编数字）" % args.min_ok)
        return 2

    body = json.dumps(snap, ensure_ascii=False, indent=2).encode("utf-8")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), SNAPSHOT)
    open(out, "wb").write(body)
    print("  已生成 %s（%s 字节，仅含价格数字，不含任何登录态）" % (SNAPSHOT, len(body)))

    if args.dry_run:
        print("  DRY-RUN 未提交")
        return 0

    pat = pat_from_git()
    res = put_snapshot(pat, body, "内存价格自动抓取 %s (%s/%s)" % (date, snap["ok_count"], snap["total"]))
    print("  已提交到仓库，commit: %s" % (res.get("commit", {}).get("sha", "?")[:10]))
    print("  云端 price-update 工作流会自动把本期价格合并进看板并部署")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("ERROR: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        sys.exit(1)
