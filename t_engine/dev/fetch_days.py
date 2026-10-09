# -*- coding: utf-8 -*-
"""多日分时缓存: 每天一次请求(ndays=1), 为每只股票积累最近5个交易日分钟数据
用法: python fetch_days.py [代码列表逗号] [--backfill N]   backfill=往前多拉N天
"""
import sys, os, json, time, urllib.request

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
OUT = "day_cache"
BASE = "000563,002223,600415,603580,600630,600000,300750,002594,601899,002170," \
       "600558,601138,600519,000858,601318,000333,600900,002230,601166,600036,002249,601077"

args = [a for a in sys.argv[1:] if not a.startswith("--")]
codes = args[0].split(",") if args else BASE.split(",")
os.makedirs(OUT, exist_ok=True)

def norm(code):
    return ("1." if code[0] in "659" else "0.") + code

def fetch(secid, ndays, date=None):
    url = (f"https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid={secid}"
           f"&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays={ndays}"
           + (f"&date={date}" if date else ""))
    req = urllib.request.Request(url, headers=UA)
    for a in range(3):
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
            if d.get("data"):
                return d["data"]["name"], d["data"].get("trends") or []
            return None, []
        except Exception:
            time.sleep(0.8 + a)
    return None, []

# 先拿一只的5天窗口确定交易日历
name0, tr0 = fetch(norm(codes[0]), 5)
dates = sorted({r.split(",")[0].split()[0] for r in tr0})
print("交易日历:", dates)

for c in codes:
    fn = f"{OUT}/{c}.json"
    cached = {"name": None, "days": {}}
    if os.path.exists(fn):
        cached = json.load(open(fn, encoding="utf-8"))
    have = set(cached["days"].keys())
    for dt in dates:
        if dt in have: continue
        nm, tr = fetch(norm(c), 1, dt)
        if not nm: continue
        cached["name"] = nm
        rows = []
        for r in tr:
            p = r.split(",")
            if len(p) < 8: continue
            if not p[0].startswith(dt): continue
            rows.append([p[0].split()[1], float(p[2]), float(p[5]), float(p[6])])
        if rows:
            cached["days"][dt] = rows
        time.sleep(0.3)
    json.dump(cached, open(fn, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"{c} {cached['name']}: {sorted(cached['days'].keys())}")
print("完成")
