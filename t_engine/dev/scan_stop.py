# -*- coding: utf-8 -*-
"""一次扫描: 止损宽度敏感性(买/卖) + 反T放宽变体
止损距离 stop_dist = max(X分, Y倍保本): 信号集不变, 只改评分口径 -> 扫描 X,Y
"""
import sys, math, json
import importlib.util

spec = importlib.util.spec_from_file_location("te", "t_engine.py")
te = importlib.util.module_from_spec(spec)
spec.loader.exec_module(te)
te.OPENER = te._get_opener(None)

try:
    d = json.load(open(r"C:/Users/yuwell/WorkBuddy/2026-10-09-09-17-59/steady_v3.json", encoding="utf-8"))
    pool = list(dict.fromkeys([s["code"] for s in d["hits"]]))[:100]
except Exception:
    pool = []
days = json.load(open("today_cache.json", encoding="utf-8"))

GAP = 30

def build_signals(c, rec):
    """基础信号集(默认闸门配置) + eps/target, 供评分扫描"""
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
    return [(s["type"], tmap.get(s["time"])) for s in sigs if tmap.get(s["time"]) is not None], price, target, eps, n

DATA = []
for c in pool:
    if c not in days: continue
    r = build_signals(c, days[c])
    if r: DATA.append(r)

def score(stop_fn):
    agg = {"BUY_T": {"closed":0,"strict":0,"stop":0,"to":0}, "SELL_T": {"closed":0,"strict":0,"stop":0,"to":0}}
    for sigs, price, target, eps, n in DATA:
        stop_dist = stop_fn(eps)
        for typ, i in sigs:
            end = i + 1 + GAP
            if end > n: continue
            seg = price[i+1:end]
            st = agg[typ]; st["closed"] += 1
            if typ == "BUY_T":
                tp = next((k for k, v in enumerate(seg, 1) if v >= price[i] + target), None)
                sl = next((k for k, v in enumerate(seg, 1) if v <= price[i] - stop_dist), None)
            else:
                tp = next((k for k, v in enumerate(seg, 1) if v <= price[i] - target), None)
                sl = next((k for k, v in enumerate(seg, 1) if v >= price[i] + stop_dist), None)
            if tp is not None and (sl is None or tp < sl): st["strict"] += 1
            elif sl is not None: st["stop"] += 1
            else: st["to"] += 1
    return agg

print("== 止损宽度敏感性(严格口径, 30min) ==")
print(f"{'止损规则':<24}{'正T严格':>8}{'正T止损':>8}{'反T严格':>8}{'反T止损':>8}")
for name, fn in [("现行 max(3分,1.5eps)", lambda e: max(0.03, 1.5*e)),
                 ("max(5分,2eps)",        lambda e: max(0.05, 2*e)),
                 ("max(7分,2.5eps)",      lambda e: max(0.07, 2.5*e)),
                 ("max(10分,3eps)",       lambda e: max(0.10, 3*e)),
                 ("不设止损(宽口径)",      lambda e: 9e9)]:
    a = score(fn); b, s_ = a["BUY_T"], a["SELL_T"]
    cb, cs = b["closed"] or 1, s_["closed"] or 1
    print(f"{name:<24}{b['strict']/cb*100:>7.1f}%{b['stop']/cb*100:>7.1f}%{s_['strict']/cs*100:>7.1f}%{s_['stop']/cs*100:>7.1f}%")
