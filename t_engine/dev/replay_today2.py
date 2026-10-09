# -*- coding: utf-8 -*-
"""当日快照多股回放 v2: 逐只拉今日分时 -> gen_signals -> 封闭窗口统计(宽/严格)
用法: python replay_today2.py <引擎py> <变体名> [池上限100]
"""
import sys, math, json, time, urllib.request, os
import importlib.util
from concurrent.futures import ThreadPoolExecutor

ENGINE = sys.argv[1] if len(sys.argv) > 1 else "t_engine_orig.py"
VARIANT = sys.argv[2] if len(sys.argv) > 2 else "base"
POOL_N = int(sys.argv[3]) if len(sys.argv) > 3 else 100
GAP = 30
CACHE = "today_cache.json"

spec = importlib.util.spec_from_file_location("te", ENGINE)
te = importlib.util.module_from_spec(spec)
spec.loader.exec_module(te)
te.OPENER = te._get_opener(None)

VAR = {
    "base": {}, "res3": {"MIN_RES_BUY": 3},
    "space15": {"BUY_SPACE": 1.5}, "space12": {"BUY_SPACE": 1.2},
    "slope_half": {"SLOPE_BUY_MAX": 0.075},
    "mom_on": {"BUY_MOM_ON": 1}, "mom_on_space12": {"BUY_MOM_ON": 1, "BUY_SPACE": 1.2},
    "stab": {"BUY_STAB_ON": 1},
    "up3": {"BUY_UP_N": 3}, "up5": {"BUY_UP_N": 5},
    "up3_space12": {"BUY_UP_N": 3, "BUY_SPACE": 1.2},
    "stab_up3": {"BUY_STAB_ON": 1, "BUY_UP_N": 3},
    "early12": {"BUY_EARLY12": 1}, "early15": {"BUY_EARLY15": 1},
    "early15_space12": {"BUY_EARLY15": 1, "BUY_SPACE": 1.2},
}[VARIANT]
for k, v in VAR.items():
    if k == "BUY_MOM_ON": te.CONFIG["BUY_MOM"] = True
    elif k == "BUY_STAB_ON": te.CONFIG["BUY_STAB"] = True
    elif k == "BUY_EARLY12": te.CONFIG["BUY_SPACE_EARLY"] = 1.2
    elif k == "BUY_EARLY15": te.CONFIG["BUY_SPACE_EARLY"] = 1.5
    elif k == "BUY_SPACE": te.CONFIG["BUY_SPACE"] = v
    elif k == "SLOPE_BUY_MAX": te.CONFIG["SLOPE_BUY_MAX"] = v
    else: te.CONFIG[k] = v

try:
    d = json.load(open(r"C:/Users/yuwell/WorkBuddy/2026-10-09-09-17-59/steady_v3.json", encoding="utf-8"))
    pool = [s["code"] for s in d["hits"]]
except Exception:
    pool = []
pool = list(dict.fromkeys(pool))[:POOL_N]

# ---- 今日分时缓存(跨变体复用) ----
days = {}
if os.path.exists(CACHE):
    days = json.load(open(CACHE, encoding="utf-8"))
missing = [c for c in pool if c not in days]

def fetch(c):
    try:
        t, p, v, a, src = te.fetch_minutes(c)
        return c, [t, p, v, a]
    except Exception:
        return c, None

with ThreadPoolExecutor(2) as ex:
    for c, r in ex.map(fetch, missing):
        if r: days[c] = r
json.dump(days, open(CACHE, "w", encoding="utf-8"))
print(f"池{len(pool)} 缓存{len(days)}")

def evaluate(c, rec):
    times, price, cvol, camt = rec
    n = len(price)
    if n < 75: return None
    avg = [camt[i] / (cvol[i] * 100) if cvol[i] > 0 else price[i] for i in range(n)]
    dif, dea, mb = te.calc_macd(price, te.CONFIG["MACD_FAST"], te.CONFIG["MACD_SLOW"], te.CONFIG["MACD_SIG"])
    _, _, jj = te.calc_kdj(price, price, price, te.CONFIG["KDJ_N"])
    _, bup, bdown = te.calc_boll(price, te.CONFIG["BOLL_N"], te.CONFIG["BOLL_K"])
    vol_lst = [max(1, cvol[i] - (cvol[i-1] if i else 0)) for i in range(n)]
    cur = price[-1]
    shares = max(100, int(20000 / cur / 100) * 100)
    amt = shares * cur
    comm = max(5.0, amt * 0.0000341)
    ct = comm * 2 + amt * 0.0005 + amt * 0.00001 * 2 + shares * 0.01
    eps = ct / shares
    target = math.ceil(eps / 0.01 - 1e-9) * 0.01
    sigs = te.gen_signals(times, price, avg, price, price, vol_lst, mb, jj, bup, bdown,
                          target, eps, 0.01, shares)[0]
    tmap = {t: i for i, t in enumerate(times)}
    stop_dist = max(3 * 0.01, 1.5 * eps)
    out = {"BUY_T": {"n":0,"closed":0,"wide":0,"strict":0,"stop":0,"to":0,"mfe":[],"mae":[]},
           "SELL_T":{"n":0,"closed":0,"wide":0,"strict":0,"stop":0,"to":0,"mfe":[],"mae":[]}}
    for s in sigs:
        i = tmap.get(s["time"])
        if i is None: continue
        st = out[s["type"]]; st["n"] += 1
        end = min(n, i + 1 + GAP)
        seg = price[i+1:end]
        if not seg: continue
        if s["type"] == "BUY_T":
            mfe = (max(seg) - price[i]) * 100; mae = (price[i] - min(seg)) * 100
            wide = mfe >= target * 100
            tp = next((k for k, v in enumerate(seg, 1) if v >= price[i] + target), None)
            sl = next((k for k, v in enumerate(seg, 1) if v <= price[i] - stop_dist), None)
        else:
            mfe = (price[i] - min(seg)) * 100; mae = (max(seg) - price[i]) * 100
            wide = mfe >= target * 100
            tp = next((k for k, v in enumerate(seg, 1) if v <= price[i] - target), None)
            sl = next((k for k, v in enumerate(seg, 1) if v >= price[i] + stop_dist), None)
        st["mfe"].append(mfe); st["mae"].append(mae)
        if i + 1 + GAP <= n:
            st["closed"] += 1
            if wide: st["wide"] += 1
            if tp is not None and (sl is None or tp < sl): st["strict"] += 1
            elif sl is not None: st["stop"] += 1
            else: st["to"] += 1
    return out

def acc(dst, src):
    for k in src:
        if k == "mins": continue
        if isinstance(src[k], list): dst[k].extend(src[k])
        else: dst[k] += src[k]

agg = {"BUY_T": {"n":0,"closed":0,"wide":0,"strict":0,"stop":0,"to":0,"mfe":[],"mae":[]},
       "SELL_T":{"n":0,"closed":0,"wide":0,"strict":0,"stop":0,"to":0,"mfe":[],"mae":[]}}
for c in pool:
    if c not in days: continue
    out = evaluate(c, days[c])
    if not out: continue
    for k in agg: acc(agg[k], out[k])

print(f"===== {os.path.basename(ENGINE)} [{VARIANT}] 今日 30min窗 池{len(pool)} =====")
for k, label in (("BUY_T", "正T"), ("SELL_T", "反T")):
    a = agg[k]; c = a["closed"] or 1
    amfe = sum(a["mfe"])/len(a["mfe"]) if a["mfe"] else 0
    amae = sum(a["mae"])/len(a["mae"]) if a["mae"] else 0
    print(f"{label}: 信号{a['n']} 封闭{a['closed']} | 宽 {a['wide']/c*100:.1f}% 严格 {a['strict']/c*100:.1f}% "
          f"止损 {a['stop']/c*100:.1f}% 超时 {a['to']/c*100:.1f}% | MFE均 {amfe:.1f} MAE均 {amae:.1f}")
