"""临时诊断：在 GitHub Actions 云端（境外 IP）实测京东各价格端点可达性。
结果写入 probe_result.json，由 workflow 提交后从仓库读回。"""
import json, urllib.request, urllib.error, time

SKU = "100039823341"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
ENDPOINTS = [
    ("p.3.cn mget", f"https://p.3.cn/prices/mget?skuIds=J_{SKU}&type=1"),
    ("p.3.cn mgets", f"https://p.3.cn/prices/mgets?skuIds=J_{SKU}"),
    ("c0.3.cn mget", f"https://c0.3.cn/prices/mget?skuIds=J_{SKU}&type=1"),
    ("api.m.jd.com wareBusiness", f"https://api.m.jd.com/client.action?functionId=wareBusiness&appid=item-v3&client=pc&clientVersion=1.0.0&body=%7B%22skuId%22%3A%22{SKU}%22%7D"),
    ("api.m.jd.com pc_detail", f"https://api.m.jd.com/?appid=item-v3&functionId=pc_detailpage_wareBusiness&client=pc&clientVersion=1.0.0&body=%7B%22skuId%22%3A%22{SKU}%22%7D"),
    ("item.m.jd.com", f"https://item.m.jd.com/product/{SKU}.html"),
    ("item.jd.com", f"https://item.jd.com/{SKU}.html"),
]

out = {"note": "GitHub Actions ubuntu-latest 云端实测", "results": []}
for name, url in ENDPOINTS:
    rec = {"name": name, "url": url}
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Referer": "https://item.jd.com/",
            "Accept": "application/json, text/html, */*",
        })
        with urllib.request.urlopen(req, timeout=12) as r:
            body = r.read()
            rec["http"] = r.status
            rec["bytes"] = len(body)
            txt = body.decode("utf-8", "replace")
            rec["sample"] = txt[:220].replace("\n", " ")
            rec["has_jdprice"] = '"jdPrice"' in txt
            rec["has_p_field"] = '"p":' in txt
            rec["masked_price"] = "??" in txt
            # 尝试抽取 p.3.cn 风格的价格
            import re
            pm = re.search(r'"p"\s*:\s*"?([\d.]+)"?', txt)
            if pm:
                rec["parsed_p"] = pm.group(1)
    except Exception as e:
        rec["http"] = 0
        rec["error"] = f"{type(e).__name__}: {e}"
    rec["secs"] = round(time.time() - t0, 1)
    out["results"].append(rec)
    print(rec["name"], "->", rec.get("http"), rec.get("bytes"), rec.get("error", ""))

open("probe_result.json", "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=2))
print("probe_result.json written")
