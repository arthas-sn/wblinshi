# -*- coding: utf-8 -*-
"""后台补拉东财5日分钟缓存: 限速退避, 已有缓存跳过"""
import json, time, os, urllib.request
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
CODES = "000563,002223,600415,603580,600630,600000,300750,002594,601899,002170,600558,601138,600519,000858,601318,000333,600900,002230,601166,600036".split(",")

def norm(code):
    return ("1." if code[0] in "659" else "0.") + code

done = fail = skip = 0
for code in CODES:
    fn = ("cache_sh" if code[0] in "659" else "cache_sz") + code + ".json"
    if os.path.exists(fn) and len(json.load(open(fn, encoding="utf-8"))["days"]) >= 4:
        skip += 1
        continue
    ok = False
    for a in range(8):
        try:
            url = (f"https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid={norm(code)}"
                   "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays=5")
            req = urllib.request.Request(url, headers=UA)
            d = json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
            days = {}
            for r in d["data"]["trends"]:
                p = r.split(",")
                dt, tm = p[0].split()
                days.setdefault(dt, []).append([tm, float(p[2]), float(p[5]), float(p[6])])
            json.dump({"name": d["data"].get("name"), "days": days}, open(fn, "w", encoding="utf-8"), ensure_ascii=False)
            ok = True
            break
        except Exception:
            time.sleep(6 * (a + 1))
    if ok: done += 1
    else: fail += 1
    time.sleep(3)
print(f"补拉完成: 新{done} 已有{skip} 失败{fail}")
