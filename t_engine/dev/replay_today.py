# -*- coding: utf-8 -*-
"""当日快照多股回放: 腾讯批量分时(1次50只) + 本地算指标跑 gen_signals
样本策略: 股票池放大 -> 今天 09:30-11:00 的信号窗口都已封闭(30min)
用法: python replay_today.py <引擎py> [变体]
"""
import sys, math, json, time, urllib.request
import importlib.util
from concurrent.futures import ThreadPoolExecutor

ENGINE = sys.argv[1] if len(sys.argv) > 1 else "t_engine_orig.py"
VARIANT = sys.argv[2] if len(sys.argv) > 2 else "base"
GAP = 30

spec = importlib.util.spec_from_file_location("te", ENGINE)
te = importlib.util.module_from_spec(spec)
spec.loader.exec_module(te)

# ---- 变体开关 ----
V = dict(
    base      = {},
    res3      = {"MIN_RES_BUY": 3},
    space15   = {"BUY_SPACE": 1.5},
    space12   = {"BUY_SPACE": 1.2},
    slope_t   = {"SLOPE_BUY_FACTOR": 0.5},
    gates_on  = {"BUY_MOM_ON": True},
    gates_on_space = {"BUY_MOM_ON": True, "BUY_SPACE": 1.2},
)[VARIANT]
for k, v in V.items():
    if k == "BUY_MOM_ON":
        te.CONFIG["BUY_MOM"] = v
    elif k == "MIN_RES_BUY":
        te.CONFIG["MIN_RES_BUY"] = v
    elif k == "BUY_SPACE":
        te.CONFIG["BUY_SPACE"] = v
    elif k == "SLOPE_BUY_FACTOR":
        te.CONFIG["SLOPE_BUY_FACTOR"] = v

# ---- 股票池: 稳健138只(全价段) + 大盘高波动补充 ----
try:
    d = json.load(open(r"C:/Users/yuwell/WorkBuddy/2026-10-09-09-17-59/steady_v3.json", encoding="utf-8"))
    POOL = [s["code"] for s in d["hits"]]
except Exception:
    POOL = []
EXTRA = ["sh600519","sz000858","sh601318","sz000333","sh600900","sz002230","sh601166","sh600036",
         "sh601899","sz002594","sz300750","sh601138","sh600558","sh601838","sh603444","sz000423",
         "sh600285","sz002746","sz002555","sz300181","sh600750","sz002895","sz002318","sh600809",
         "sh601077","sz002249","sh600050","sh601326","sh601658","sh600908","sh601339","sz002170",
         "sh601728","sh600066","sh601717","sz301215"]
codes = list(dict.fromkeys(POOL + EXTRA))[:120]
print(f"股票池: {len(codes)} 只")

# ---- 腾讯批量分时 ----
def fetch_batch(cs):
    url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=" + ",".join(cs)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
    for a in range(3):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore"))
        except Exception:
            time.sleep(2 + a * 2)
    return {}

datas = {}
with ThreadPoolExecutor(2) as ex:
    chunks = [codes[i:i+40] for i in range(0, len(codes), 40)]
    for j, d in enumerate(ex.map(fetch_batch, chunks)):
        datas.update(d.get("data", {}) or {})
print(f"拉到分时: {len(datas)} 只")

def norm_day(raw_obj, code):
    """腾讯分钟结构 -> times, price, cvol, camt"""
    try:
        node = raw_obj["data"][code]["data"]["data"]
        if isinstance(node, list) and node and isinstance(node[0], str):
            rows = node
        else:
            rows = node.get("data") or []
    except Exception:
        return None
    times, price, cvol, camt = [], [], [], []
    cc = ca = 0.0
    for r in rows:
        p = r.split()
        if len(p) < 4: continue
        t = p[0]
        tm = t[:2] + ":" + t[2:]
        if tm < "09:30": continue
        times.append(tm); price.append(float(p[1]))
        cc += float(p[2]); ca += float(p[3])
        cvol.append(cc); camt.append(ca)
    return times, price, cvol, camt

def day_metrics(name, times, price, cvol, camt):
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
    return times, price, avg, vol_lst, mb, jj, bup, bdown, target, eps, shares, n

agg = {"BUY_T": {"n":0,"closed":0,"wide":0,"strict":0,"stop":0,"to":0,"mfe":[],"mae":[]},
       "SELL_T":{"n":0,"closed":0,"wide":0,"strict":0,"stop":0,"to":0,"mfe":[],"mae":[]}}
sig_cnt = 0
for code, obj in datas.items():
    # 腾讯结构: datas[code].qt... 分时在 raw_obj[code] 里, 统一用全量 dict 再解析
    parsed = norm_day({"data": {code: obj}}, code) if isinstance(obj, dict) else None
    if not parsed: continue
    times, price, cvol, camt = parsed
    m = day_metrics(code, times, price, cvol, camt)
    if not m: continue
    (t2, p2, avg, vol_lst, mb, jj, bup, bdown, target, eps, shares, n) = m
    r = te.gen_signals(t2, p2, avg, p2, p2, vol_lst, mb, jj, bup, bdown, target, eps, 0.01, shares)
    sigs = r[0]
    tmap = {t: i for i, t in enumerate(t2)}
    stop_dist = max(3 * 0.01, 1.5 * eps)
    for s in sigs:
        i = tmap.get(s["time"])
        if i is None: continue
        st = agg[s["type"]]; st["n"] += 1
        end = min(n, i + 1 + GAP)
        closed = i + 1 + GAP <= n
        seg = p2[i+1:end]
        if not seg: continue
        if s["type"] == "BUY_T":
            mfe = (max(seg) - p2[i]) * 100; mae = (p2[i] - min(seg)) * 100
            wide = mfe >= target * 100
            tp = next((k for k, v in enumerate(seg, 1) if v >= p2[i] + target), None)
            sl = next((k for k, v in enumerate(seg, 1) if v <= p2[i] - stop_dist), None)
        else:
            mfe = (p2[i] - min(seg)) * 100; mae = (max(seg) - p2[i]) * 100
            wide = mfe >= target * 100
            tp = next((k for k, v in enumerate(seg, 1) if v <= p2[i] - target), None)
            sl = next((k for k, v in enumerate(seg, 1) if v >= p2[i] + stop_dist), None)
        st["mfe"].append(mfe); st["mae"].append(mae)
        if closed:
            st["closed"] += 1
            if wide: st["wide"] += 1
            if tp is not None and (sl is None or tp < sl): st["strict"] += 1
            elif sl is not None: st["stop"] += 1
            else: st["to"] += 1
    sig_cnt += len(sigs)

print(f"总信号: {sig_cnt}")
print(f"===== {ENGINE} [{VARIANT}] 今日上午 30min窗口 =====")
for k, label in (("BUY_T", "正T"), ("SELL_T", "反T")):
    a = agg[k]; c = a["closed"] or 1
    amfe = sum(a["mfe"])/len(a["mfe"]) if a["mfe"] else 0
    amae = sum(a["mae"])/len(a["mae"]) if a["mae"] else 0
    print(f"{label}: 信号{a['n']} 封闭{a['closed']} | 宽 {a['wide']/c*100:.1f}% 严格 {a['strict']/c*100:.1f}% "
          f"止损 {a['stop']/c*100:.1f}% 超时 {a['to']/c*100:.1f}% | 均MFE {amfe:.1f}分 均MAE {amae:.1f}分")
json.dump(agg, open(f"agg_{VARIANT}.json", "w", encoding="utf-8"), ensure_ascii=False)
