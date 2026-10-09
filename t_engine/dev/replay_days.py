# -*- coding: utf-8 -*-
"""多日回放: 东财trends2 ndays=5 拉近5交易日分钟数据, 逐日跑 gen_signals, 统计正T/反T质量
用法: python replay_days.py <引擎py> [codes逗号] [GAP分钟=30]
指标: 信号数 / 窗口封闭数 / 达标率(30分钟内触及回归目标) / MAE(封闭窗口内最大浮亏,分) / MFE(最大浮盈,分)
"""
import sys, math, json, time, datetime
import importlib.util

ENGINE = sys.argv[1] if len(sys.argv) > 1 else "t_engine_orig.py"
CODES = (sys.argv[2] if len(sys.argv) > 2 else
         "000563,002223,600415,603580,600630,600000,300750,002594,601899,002170,600558,601138").split(",")
GAP = int(sys.argv[3]) if len(sys.argv) > 3 else 30

spec = importlib.util.spec_from_file_location("te", ENGINE)
te = importlib.util.module_from_spec(spec)
spec.loader.exec_module(te)
te.OPENER = te._get_opener(None)

def fetch_days(c, ndays=5):
    """东财 trends2 多日(带本地缓存, 保证A/B对比同一数据): 返回 {date: [(time,close,vol手,amt元)]}"""
    import urllib.request, os
    cache_fn = f"cache_{c}.json"
    if os.path.exists(cache_fn):
        j = json.load(open(cache_fn, encoding="utf-8"))
        return j["name"], {k: [tuple(x) for x in v] for k, v in j["days"].items()}
    secid = ("1." if c.startswith("sh") else "0.") + c[2:]
    url = (f"https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid={secid}"
           f"&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays={ndays}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
    for attempt in range(4):
        try:
            d = json.loads(te.OPENER.open(req, timeout=25).read().decode())
            break
        except Exception:
            if attempt == 3: raise
            time.sleep(1.5 * (attempt + 1))
    days = {}
    for r in d["data"]["trends"]:
        p = r.split(",")
        dt, tm = p[0].split()
        days.setdefault(dt, []).append((tm, float(p[2]), float(p[5]), float(p[6])))
    name = d["data"].get("name") or c
    json.dump({"name": name, "days": days}, open(cache_fn, "w", encoding="utf-8"))
    time.sleep(0.6)
    return name, days

def gen_day(name, dt, rows):
    """单日分钟数据 -> gen_signals -> 质量统计"""
    # 腾讯口径: 分钟量(手)累计、分钟额(元)累计
    times, price, cc, ca = [], [], 0.0, 0.0
    cvol, camt = [], []
    for tm, cl, v, a in rows:
        if tm < "09:30": continue
        times.append(tm); price.append(cl)
        cc += v; ca += a
        cvol.append(cc); camt.append(ca)
    n = len(price)
    if n < 45: return None
    avg = [camt[i] / (cvol[i] * 100) if cvol[i] > 0 else price[i] for i in range(n)]
    dif, dea, macd_bar = te.calc_macd(price, te.CONFIG["MACD_FAST"], te.CONFIG["MACD_SLOW"], te.CONFIG["MACD_SIG"])
    _, _, j_list = te.calc_kdj(price, price, price, te.CONFIG["KDJ_N"])
    _, bup, bdown = te.calc_boll(price, te.CONFIG["BOLL_N"], te.CONFIG["BOLL_K"])
    vol_lst = [max(1, cvol[i] - (cvol[i-1] if i else 0)) for i in range(n)]
    tick, pre_close = 0.01, price[0]
    cur = price[-1]
    shares = max(100, int(20000 / cur / 100) * 100)
    amt = shares * cur
    comm = max(5.0, amt * 0.0000341)
    cost_total = comm * 2 + amt * 0.0005 + amt * 0.00001 * 2 + shares * tick
    cost_per_share = cost_total / shares
    target = math.ceil(cost_per_share / tick - 1e-9) * tick
    r = te.gen_signals(times, price, avg, price, price, vol_lst, macd_bar, j_list,
                       bup, bdown, target, cost_per_share, tick, shares)
    sigs, n_filt = r[0], r[1]
    out = {"BUY_T": {"n": 0, "closed": 0, "ok": 0, "mae": [], "mfe": []},
           "SELL_T": {"n": 0, "closed": 0, "ok": 0, "mae": [], "mfe": []}}
    tmap = {t: i for i, t in enumerate(times)}
    for s in sigs:
        i = tmap.get(s["time"])
        if i is None: continue
        st = out[s["type"]]
        st["n"] += 1
        end = min(n, i + 1 + GAP)
        seg = price[i+1:end]
        if not seg: continue
        mfe = (max(seg) - price[i]) * 100
        mae = (price[i] - min(seg)) * 100
        closed = i + 1 + GAP <= n
        if s["type"] == "BUY_T":
            ok = mfe >= target * 100
            st["mae"].append(mae); st["mfe"].append(mfe)
        else:
            ok = mae >= target * 100
            st["mae"].append(mfe); st["mfe"].append(mae)  # 对卖点: mae=踏空幅度, mfe=回避的涨幅
        if closed:
            st["closed"] += 1
            if ok: st["ok"] += 1
    return out, n_filt

agg = {"BUY_T": {"n": 0, "closed": 0, "ok": 0, "mae": [], "mfe": []},
       "SELL_T": {"n": 0, "closed": 0, "ok": 0, "mae": [], "mfe": []}}
days_total = 0
verbose = "-v" in sys.argv
for code in CODES:
    c, _ = te.normalize_code(code)
    try:
        name, days = fetch_days(c)
    except Exception as e:
        print(f"{code}: 拉取失败 {e}"); continue
    for dt in sorted(days.keys()):
        r = gen_day(name, dt, days[dt])
        if not r: continue
        out, n_filt = r
        days_total += 1
        for k in agg:
            agg[k]["n"] += out[k]["n"]; agg[k]["closed"] += out[k]["closed"]; agg[k]["ok"] += out[k]["ok"]
            agg[k]["mae"] += out[k]["mae"]; agg[k]["mfe"] += out[k]["mfe"]
        if verbose:
            b, s_ = out["BUY_T"], out["SELL_T"]
            print(f"{name} {dt}: 正T {b['n']}发(封闭达标 {b['ok']}/{b['closed']}) | 反T {s_['n']}发({s_['ok']}/{s_['closed']})")

print(f"\n===== {ENGINE} | {len(CODES)}只 x {days_total}日 | 窗口{GAP}分钟 =====")
for k, label in (("BUY_T", "正T"), ("SELL_T", "反T")):
    a = agg[k]
    wr = a["ok"] / a["closed"] * 100 if a["closed"] else 0
    mae = sum(a["mae"]) / len(a["mae"]) if a["mae"] else 0
    mfe = sum(a["mfe"]) / len(a["mfe"]) if a["mfe"] else 0
    print(f"{label}: 信号{a['n']} 封闭{a['closed']} 达标率{wr:.1f}%  平均浮盈机会{mfe:.1f}分  平均不利偏移{mae:.1f}分")
