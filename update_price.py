#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内存价格变化 —— 每周四 10:00（北京时间）云端自动更新脚本

设计要点：
  1. 只用 Python 标准库（zipfile / xml / urllib），GitHub Actions ubuntu 无需额外依赖。
  2. 数据以「仓库内的 xlsx 表格快照」为准：表格里每周新增一列，脚本解析后写回
     index.html 的 MARKET-DATA 区块顶层键 memoryPrice，并同步 price_data.json。
  3. 表格中的链接是京东商品详情页，实测返回 JS 空壳、公开价格接口不可达，
     因此在线抓价只作为「未来接口可用时自动生效」的可选增强：抓取失败时
     静默保留表格快照，绝不写入猜测价格，也不破坏已有历史列。
  4. 只改 memoryPrice，其余数据（meta / glossary / sections / dates / 周月总结）一字不动。

用法：
  python update_price.py                 # 常规：读仓库 xlsx → 写回
  python update_price.py --no-fetch      # 跳过在线抓价探测
  python update_price.py --xlsx 路径      # 指定表格路径
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

INDEX = "index.html"
PRICE_JSON = "price_data.json"
XLSX_CANDIDATES = [
    "price_source.xlsx", "内存价格跟进.xlsx",
    os.path.join("data", "price_source.xlsx"), os.path.join("data", "内存价格跟进.xlsx"),
]
BLOCK = re.compile(
    r'(<!--MARKET-DATA-START-->\s*<script type="application/json" id="market-data">)(.*?)(</script>\s*<!--MARKET-DATA-END-->)',
    re.S)
NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
EXCEL_EPOCH = dt.date(1899, 12, 30)


# ─────────────────────────── xlsx 解析（纯标准库） ───────────────────────────
def col_to_idx(ref):
    """'E2' -> (5, 2)"""
    m = re.match(r"([A-Z]+)(\d+)", ref)
    if not m:
        return None
    letters, row = m.group(1), int(m.group(2))
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n, row


def read_xlsx(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()

        # sharedStrings
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(NS_MAIN + "si"):
                shared.append("".join(t.text or "" for t in si.iter(NS_MAIN + "t")))

        # 工作表：取第一个 sheet 对应的 xml
        sheet_xml = "xl/worksheets/sheet1.xml"
        if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            rid = None
            for sh in wb.iter(NS_MAIN + "sheet"):
                rid = sh.get(NS_REL + "id")
                break
            for rel in rels:
                if rel.get("Id") == rid:
                    target = rel.get("Target", "")
                    if not target.startswith("/"):
                        target = "xl/" + target.lstrip("/")
                    else:
                        target = target.lstrip("/")
                    if target in names:
                        sheet_xml = target
                    break

        # 超链接目标（D 列显示 1~24，真实 URL 在 rels 中）
        link_map = {}
        rel_path = sheet_xml.rsplit("/", 1)[0] + "/_rels/" + sheet_xml.rsplit("/", 1)[1] + ".rels"
        if rel_path in names:
            rroot = ET.fromstring(z.read(rel_path))
            rid2url = {r.get("Id"): r.get("Target", "") for r in rroot}
            sroot = ET.fromstring(z.read(sheet_xml))
            hl = sroot.find(NS_MAIN + "hyperlinks")
            if hl is not None:
                for h in hl.findall(NS_MAIN + "hyperlink"):
                    url = rid2url.get(h.get(NS_REL + "id"), "")
                    if url:
                        link_map[h.get("ref")] = url.split("?")[0]

        # 单元格
        cells = {}
        sroot = ET.fromstring(z.read(sheet_xml))
        for c in sroot.iter(NS_MAIN + "c"):
            ref = c.get("r")
            if not ref:
                continue
            pos = col_to_idx(ref)
            if not pos:
                continue
            t = c.get("t")
            v = c.find(NS_MAIN + "v")
            is_el = c.find(NS_MAIN + "is")
            if t == "s" and v is not None:
                val = shared[int(v.text)] if v.text and int(v.text) < len(shared) else ""
            elif t == "inlineStr" and is_el is not None:
                val = "".join(x.text or "" for x in is_el.iter(NS_MAIN + "t"))
            elif v is not None:
                val = v.text
            else:
                val = ""
            cells[pos] = val

    if not cells:
        raise RuntimeError("xlsx 未解析出任何单元格")

    # 表头行：包含「品牌」「型号」的那一行
    header_row = None
    header = {}
    for (col, row), val in cells.items():
        if isinstance(val, str) and val.strip() in ("品牌", "型号", "链接"):
            header_row = row
    if header_row is None:
        raise RuntimeError("xlsx 未找到表头行（品牌/型号/链接）")
    for (col, row), val in cells.items():
        if row == header_row and isinstance(val, str) and val.strip():
            header[col] = val.strip()

    col_brand = next((c for c, v in header.items() if v == "品牌"), None)
    col_model = next((c for c, v in header.items() if v == "型号"), None)
    col_link = next((c for c, v in header.items() if v == "链接"), None)
    if col_model is None:
        raise RuntimeError("xlsx 表头缺少「型号」列")

    # 日期列：表头行中位于型号列之后、值为 Excel 日期序列号的单元格
    date_cols = []
    for (col, row), val in cells.items():
        if row != header_row or col <= col_model:
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if 30000 < num < 80000:   # 合理的 Excel 日期序列号范围
            d = EXCEL_EPOCH + dt.timedelta(days=int(num))
            date_cols.append((col, d.isoformat()))
    date_cols.sort()
    if not date_cols:
        raise RuntimeError("xlsx 表头行未识别出日期列")

    weeks = [d for _, d in date_cols]
    max_row = max(r for _, r in cells)
    items, brand_fill = [], ""
    for row in range(header_row + 1, max_row + 1):
        brand = str(cells.get((col_brand, row), "") or "").strip() if col_brand else ""
        if brand:
            brand_fill = brand
        model = str(cells.get((col_model, row), "") or "").strip()
        if not model:
            continue
        link = ""
        if col_link:
            link = link_map.get(f"{chr(64 + col_link)}{row}", "")
            if not link:
                raw = str(cells.get((col_link, row), "") or "").strip()
                if raw.startswith("http"):
                    link = raw
        prices = []
        for col, _ in date_cols:
            raw = cells.get((col, row), "")
            try:
                prices.append(float(raw))
            except (TypeError, ValueError):
                prices.append(0.0)
        items.append({"brand": brand_fill, "model": model, "link": link, "prices": prices})

    if not items:
        raise RuntimeError("xlsx 未解析出任何型号行")
    return {
        "title": "内存价格变化",
        "unit": "元/条（京东）",
        "source": "京东商品页 · 每周四人工录入快照",
        "weeks": weeks,
        "items": items,
    }


# ─────────────────────────── 在线抓价（可选增强） ───────────────────────────
def sku_of(link):
    m = re.search(r"item\.jd\.com/(\d+)\.html", link or "")
    return m.group(1) if m else None


def fetch_jd_prices(skus):
    """京东公开价格接口；当前实测不可达（HTTP 000 / 空响应）。失败时抛异常由主流程兜底。"""
    url = "https://p.3.cn/prices/mget?skuIds=" + ",".join("J_" + s for s in skus) + "&type=1"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Referer": "https://item.jd.com/",
        "Accept": "application/json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=8) as r:
        body = r.read().decode("utf-8", "replace").strip()
    if not body:
        raise RuntimeError("空响应")
    arr = json.loads(body)
    out = {}
    for x in arr:
        sid = str(x.get("id", "")).replace("J_", "")
        try:
            p = float(x.get("p") or x.get("op") or 0)
        except (TypeError, ValueError):
            continue
        if p > 0:
            out[sid] = p
    return out


def try_online_snapshot(mp):
    """接口可用时追加/更新本周价格列；不可用则原样返回，不写入任何猜测值。"""
    skus = [sku_of(it.get("link")) for it in mp["items"]]
    skus = [s for s in skus if s]
    if not skus:
        print("  [在线抓价] 表格中无可识别的京东 SKU，跳过")
        return mp, False
    try:
        got = fetch_jd_prices(skus[:24])
    except Exception as e:
        print(f"  [在线抓价] 京东价格接口不可达（{type(e).__name__}: {e}），保留表格快照")
        return mp, False
    if len(got) < max(1, len(skus) // 2):
        print(f"  [在线抓价] 仅拿到 {len(got)}/{len(skus)} 个价格，样本不足，保留表格快照")
        return mp, False

    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
    weeks = list(mp["weeks"])
    if today in weeks:
        idx = weeks.index(today)
    else:
        weeks.append(today)
        idx = len(weeks) - 1
        for it in mp["items"]:
            it["prices"] = list(it["prices"]) + [0.0] * (len(weeks) - len(it["prices"]))
    filled = 0
    for it in mp["items"]:
        s = sku_of(it.get("link"))
        if s and s in got:
            while len(it["prices"]) <= idx:
                it["prices"].append(0.0)
            it["prices"][idx] = got[s]
            filled += 1
    mp["weeks"] = weeks
    mp["source"] = "京东价格接口 · 每周四自动抓取 + 表格快照"
    print(f"  [在线抓价] 成功写入 {filled} 个价格（{today}）")
    return mp, True


# ───────────────────── 本机抓取快照合并（每周四自动） ─────────────────────
SNAPSHOT = "price_snapshot.json"


def merge_snapshot(mp, path=SNAPSHOT):
    """把本机抓取产出的 price_snapshot.json 合并为最新一期价格列。

    - 只按 SKU 精确匹配写入，不按位置猜测；
    - 没抓到的型号该期留 None（前端显示「—」），绝不拿旧价冒充新价；
    - 同一日期重复合并幂等，不会重复追加列；
    - 快照文件里只有价格数字，不含任何京东登录态。
    """
    if not os.path.isfile(path):
        return mp, False
    try:
        snap = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"  [抓取快照] 读取失败（{e}），忽略")
        return mp, False
    date = str(snap.get("date") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        print("  [抓取快照] date 非法，忽略")
        return mp, False
    weeks0 = list(mp.get("weeks") or [])
    if weeks0 and date < weeks0[-1] and date not in weeks0:
        # 防回灌：仓库里残留的旧快照绝不能把已经回滚/不存在的历史期重新合并进来
        print(f"  [抓取快照] 快照日期 {date} 早于最新一期 {weeks0[-1]}，判定为过期快照，忽略")
        return mp, False
    got = {}
    for k, v in (snap.get("prices") or {}).items():
        try:
            p = float(v)
        except (TypeError, ValueError):
            continue
        if p >= 20:                     # 与抓取脚本一致：低价视为解码异常
            got[str(k)] = p
    if not got:
        print("  [抓取快照] 无有效价格，忽略")
        return mp, False

    weeks = list(mp["weeks"])
    if date in weeks:
        idx = weeks.index(date)
    else:
        weeks.append(date)
        idx = len(weeks) - 1
        for it in mp["items"]:
            it["prices"] = list(it["prices"]) + [None] * (len(weeks) - len(it["prices"]))
    filled = 0
    for it in mp["items"]:
        while len(it["prices"]) <= idx:
            it["prices"].append(None)
        sku = sku_of(it.get("link"))
        if sku and sku in got:
            it["prices"][idx] = got[sku]
            filled += 1
    mp["weeks"] = weeks
    mp["source"] = ("京东登录态自动抓取 · 每周四 10:00 同步"
                    "（本期 %d/%d 款有价，未抓到显示「—」）" % (filled, len(mp["items"])))
    print(f"  [抓取快照] 合并 {date}：{filled}/{len(mp['items'])} 款有价，其余显示「—」")
    return mp, True


# ─────────────────────────── 数据块读写 ───────────────────────────
def load_block():
    html = open(INDEX, encoding="utf-8").read()
    m = BLOCK.search(html)
    if not m:
        raise RuntimeError("index.html 未找到 MARKET-DATA 区块")
    return html, m, json.loads(m.group(2))


def save_block(html, m, data):
    new_block = json.dumps(data, ensure_ascii=False, indent=2)
    out = html[:m.start(2)] + new_block + html[m.end(2):]
    open(INDEX, "w", encoding="utf-8", newline="").write(out)


def validate(mp):
    if not isinstance(mp.get("weeks"), list) or len(mp["weeks"]) < 1:
        raise RuntimeError("weeks 非法")
    if not isinstance(mp.get("items"), list) or not mp["items"]:
        raise RuntimeError("items 为空")
    n = len(mp["weeks"])
    for i, it in enumerate(mp["items"]):
        if not it.get("model"):
            raise RuntimeError(f"items[{i}] 缺型号")
        if len(it.get("prices") or []) != n:
            raise RuntimeError(f"items[{i}] 价格列数 {len(it.get('prices') or [])} != 周数 {n}")
        for p in it["prices"]:
            if p is None:
                # 本期该型号未被抓到（京东风控/登录态），前端显示「—」；
                # 绝不拿上一期价格冒充本期价格。
                continue
            if not isinstance(p, (int, float)) or p < 0:
                raise RuntimeError(f"items[{i}] 存在非法价格 {p}")
        lk = it.get("link") or ""
        if lk and not lk.startswith("https://"):
            raise RuntimeError(f"items[{i}] 链接非法: {lk}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default="")
    ap.add_argument("--no-fetch", action="store_true")
    args = ap.parse_args()

    html, m, data = load_block()
    before = json.dumps(data.get("memoryPrice"), ensure_ascii=False, sort_keys=True) if data.get("memoryPrice") else None
    print("== 内存价格变化 · 每周四自动更新 ==")
    print(f"  现有数据：{len((data.get('memoryPrice') or {}).get('weeks') or [])} 期 / "
          f"{len((data.get('memoryPrice') or {}).get('items') or [])} 款")

    xlsx = args.xlsx
    if not xlsx:
        xlsx = next((p for p in XLSX_CANDIDATES if os.path.isfile(p)), "")
    mp = None
    if args.xlsx and os.path.isfile(args.xlsx):
        # 显式指定表格时以表格为准（人工修复入口）
        print(f"  读取指定表格：{args.xlsx}")
        mp = read_xlsx(args.xlsx)
        print(f"  解析结果：{len(mp['weeks'])} 期 / {len(mp['items'])} 款，最新 {mp['weeks'][-1]}")
    elif data.get("memoryPrice"):
        # 关键：以 index.html 现有数据为基准累积，保留全部历史期。
        # 若每次都从 xlsx 重建，会丢掉本机同步上来的新一期价格。
        print(f"  基准：index.html 现有 memoryPrice（{len(data['memoryPrice']['weeks'])} 期，保留全部历史）")
        mp = json.loads(json.dumps(data["memoryPrice"]))
    elif xlsx:
        print(f"  首次初始化，读取表格：{xlsx}")
        mp = read_xlsx(xlsx)
        print(f"  解析结果：{len(mp['weeks'])} 期 / {len(mp['items'])} 款，最新 {mp['weeks'][-1]}")
    else:
        print("  既无 xlsx 表格也无现有数据，本次不改动 index.html")
        return

    if not args.no_fetch:
        # 优先合并本机抓取快照；没有快照时才尝试公开价格接口探测
        mp, snap_ok = merge_snapshot(mp)
        if not snap_ok:
            mp, _ = try_online_snapshot(mp)

    validate(mp)
    data["memoryPrice"] = mp
    after = json.dumps(mp, ensure_ascii=False, sort_keys=True)
    if after == before:
        print("  数据与线上一致，无需改动 index.html")
    else:
        save_block(html, m, data)
        print("  已写回 index.html 的 memoryPrice（其余数据保持不变）")
    json.dump(mp, open(PRICE_JSON, "w", encoding="utf-8", newline=""),
              ensure_ascii=False, indent=2)
    print(f"  已同步 {PRICE_JSON}")
    print("== 完成 ==")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
