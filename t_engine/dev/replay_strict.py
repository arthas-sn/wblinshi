# -*- coding: utf-8 -*-
"""严格口径多日回放: 信号后按分钟模拟真实执行(先止损=失败), 支持变体对比
指标:
  宽达标 = 30min内触及过目标(旧口径, 不看顺序)
  严格达标 = 先于止损线触及目标(止损=入场∓max(3分,1.5保本), 同分钟保守先算止损)
  止损率 / 超时率 / 平均止盈耗时(分钟)
用法: python replay_strict.py <引擎py> [codes逗号|ALL] [变体: v1|v1_res3]
"""
import sys, math, json, time, os, glob
import importlib.util

ENGINE = sys.argv[1] if len(sys.argv) > 1 else "t_engine.py"
CODES_ARG = sys.argv[2] if len(sys.argv) > 2 else "ALL"
VARIANT = sys.argv[3] if len(sys.argv) > 3 else "v1"

spec = importlib.util.spec_from_file_location("te", ENGINE)
te = importlib.util.module_from_spec(spec)
spec.loader.exec_module(te)

# 变体参数
if VARIANT == "v1_res3":
    te.CONFIG["MIN_RES_BUY"] = 3
if VARIANT == "v0_nomom":     # 只留贴均+战绩, 关闭动量确认
    te.CONFIG["BUY_MOM"] = False

files = sorted(glob.glob("cache_*.json"))
if CODES_ARG != "ALL":
    want = set()
    for c in CODES_ARG.split(","):
        want.add(("cache_sh" if c[0] in "659" else "cache_sz") + c)
    files = [f for f in files if f[:-5] in want]

def replay_day(times, price, avg, vol_lst, macd_bar, j_list, bup, bdown, target, eps, tick, shares, gap):
    r = te.gen_signals(times, price, avg, price, price, vol_lst, macd_bar, j_list,
                       bup, bdown, target, eps, tick, shares)
    sigs = r[0]
    tmap = {t: i for i, t in enumerate(times)}
    stop_dist = max(3 * tick, 1.5 * eps)
    out = {"BUY_T": {"n":0,"closed":0,"wide":0,"strict":0,"stop":0,"to":0,"mins":[]},
           "SELL_T": {"n":0,"closed":0,"wide":0,"strict":0,"stop":0,"to":0,"mins":[]}}
    for s in sigs:
        i = tmap.get(s["time"])
        if i is None: continue
        st = out[s["type"]]
        st["n"] += 1
        end = min(len(price), i + 1 + gap)
        closed = i + 1 + gap <= len(price)
        seg = price[i+1:end]
        if not seg: continue
        if s["type"] == "BUY_T":
            wide_hit = any(p >= price[i] + target for p in seg)
            stop_hit = any(p <= price[i] - stop_dist for p in seg)
            tp_at = next((k for k, p in enumerate(seg, 1) if p >= price[i] + target), None)
            sl_at = next((k for k, p in enumerate(seg, 1) if p <= price[i] - stop_dist), None)
        else:
            wide_hit = any(p <= price[i] - target for p in seg)
            stop_hit = any(p >= price[i] + stop_dist for p in seg)
            tp_at = next((k for k, p in enumerate(seg, 1) if p <= price[i] - target), None)
            sl_at = next((k for k, p in enumerate(seg, 1) if p >= price[i] + stop_dist), None)
        if closed:
            st["closed"] += 1
            if wide_hit: st["wide"] += 1
            if tp_at is not None and (sl_at is None or tp_at < sl_at):
                st["strict"] += 1; st["mins"].append(tp_at)
            elif sl_at is not None:
                st["stop"] += 1
            else:
                st["to"] += 1
    return out

agg = {}
def acc(dst, src):
    for k in src:
        if k == "mins": dst["mins"].extend(src["mins"]); continue
        dst[k] += src[k]

day_cnt = 0
for fn in files:
    j = json.load(open(fn, encoding="utf-8"))
    for dt, rows in sorted(j["days"].items()):
        times, price, cc, ca = [], [], 0.0, 0.0
        cvol, camt = [], []
        for tm, cl, v, a in rows:
            if tm < "09:30": continue
            times.append(tm); price.append(cl); cc += v; ca += a
            cvol.append(cc); camt.append(ca)
        n = len(price)
        if n < 60: continue   # 半天数据也跑: 封闭窗口自动过滤
        day_cnt += 1
        avg = [camt[i] / (cvol[i] * 100) if cvol[i] > 0 else price[i] for i in range(n)]
        dif, dea, macd_bar = te.calc_macd(price, te.CONFIG["MACD_FAST"], te.CONFIG["MACD_SLOW"], te.CONFIG["MACD_SIG"])
        _, _, j_list = te.calc_kdj(price, price, price, te.CONFIG["KDJ_N"])
        _, bup, bdown = te.calc_boll(price, te.CONFIG["BOLL_N"], te.CONFIG["BOLL_K"])
        vol_lst = [max(1, cvol[i] - (cvol[i-1] if i else 0)) for i in range(n)]
        cur = price[-1]
        shares = max(100, int(20000 / cur / 100) * 100)
        amt = shares * cur
        comm = max(5.0, amt * 0.0000341)
        cost_total = comm * 2 + amt * 0.0005 + amt * 0.00001 * 2 + shares * 0.01
        eps = cost_total / shares
        target = math.ceil(eps / 0.01 - 1e-9) * 0.01
        out = replay_day(times, price, avg, vol_lst, macd_bar, j_list, bup, bdown, target, eps, 0.01, shares, 30)
        if not agg:
            agg = {k: dict(v, mins=[]) for k, v in out.items()}
        else:
            for k in out: acc(agg[k], out[k])

print(f"===== {ENGINE} [{VARIANT}] | {len(files)}只 x {day_cnt}日 | 30min窗口 | 严格=先于止损触及目标 =====")
for k, label in (("BUY_T", "正T"), ("SELL_T", "反T")):
    a = agg[k]
    c = a["closed"] or 1
    avgmin = sum(a["mins"]) / len(a["mins"]) if a["mins"] else 0
    print(f"{label}: 信号{a['n']} 封闭{a['closed']} | 宽达标 {a['wide']}/{c}={a['wide']/c*100:.1f}% "
          f"严格达标 {a['strict']}/{c}={a['strict']/c*100:.1f}% | 止损 {a['stop']/c*100:.1f}% 超时 {a['to']/c*100:.1f}% | 均耗时{avgmin:.0f}min")
