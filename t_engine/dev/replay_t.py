# -*- coding: utf-8 -*-
"""做T信号质量回放: 统计正T/反T 30分钟窗口成功率(与引擎 sell_success 同口径)
用法: python replay_t.py <引擎文件> [代码列表逗号分隔]
"""
import sys, json, importlib.util

ENGINE = sys.argv[1] if len(sys.argv) > 1 else "t_engine_orig.py"
CODES = (sys.argv[2] if len(sys.argv) > 2 else
         "000563,002223,600415,603580,600630,600000,300750,002594,601899,002170,600558,601138").split(",")

spec = importlib.util.spec_from_file_location("te", ENGINE)
te = importlib.util.module_from_spec(spec)
spec.loader.exec_module(te)
te.OPENER = te._get_opener(None)

GAP = 30  # 分钟

def evaluate(an):
    """返回 {BUY_T:{sig,closed,ok}, SELL_T:{...}} 以及明细"""
    series = an.get("series") or {}
    times, price = series.get("times", []), series.get("price", [])
    target = (an.get("cost") or {}).get("suggestFen", 2) / 100.0
    tmap = {t: i for i, t in enumerate(times)}
    n = len(price)
    stats = {"BUY_T": [0, 0, 0], "SELL_T": [0, 0, 0]}  # sig, closed(窗口完整), ok(成功)
    detail = []
    for s in an.get("signals", []):
        i = tmap.get(s["time"])
        if i is None: continue
        typ = s["type"]
        stats[typ][0] += 1
        end = min(n, i + 1 + GAP)
        closed = (i + 1 + GAP <= n)
        if typ == "BUY_T":
            hit = any(price[k] >= price[i] + target for k in range(i + 1, end))
            mfe = (max(price[i+1:end]) - price[i]) * 100 if end > i+1 else 0
        else:
            hit = any(price[k] <= price[i] - target for k in range(i + 1, end))
            mfe = (price[i] - min(price[i+1:end])) * 100 if end > i+1 else 0
        if closed:
            stats[typ][1] += 1
            if hit: stats[typ][2] += 1
        detail.append(f"  {s['time']} {typ} {s['level']} {s['price']} -> {'✓' if hit else '✗'} ({mfe:.1f}分/{'封闭' if closed else '未封闭'})")
    return stats, detail, target

agg = {"BUY_T": [0, 0, 0], "SELL_T": [0, 0, 0]}
for code in CODES:
    an = te.analyze(code)
    if an.get("error"):
        print(f"{code}: ERROR {an['error']}"); continue
    stats, detail, target = evaluate(an)
    b, s_ = stats["BUY_T"], stats["SELL_T"]
    for k in agg:
        for j in range(3): agg[k][j] += stats[k][j]
    def rate(x):
        return f"{x[2]}/{x[1]}" if x[1] else "0/0"
    print(f"{an['name']}({code}) 目标{target*100:.0f}分 | 正T {b[0]}发 成{rate(b)} | 反T {s_[0]}发 成{rate(s_)}")
    for d in detail: print(d)

print("\n===== 汇总 =====")
for k in ("BUY_T", "SELL_T"):
    sig, closed, ok = agg[k]
    wr = ok / closed * 100 if closed else 0
    print(f"{'正T' if k=='BUY_T' else '反T'}: 信号{sig} 窗口封闭{closed} 成功{ok} 成功率{wr:.1f}%")
