#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
做T引擎 (T-Engine) —— 独立 CLI 形态, 供统一 Node 服务以子进程调用。
纯标准库, 零第三方依赖。

用法:
  python t_engine.py 600000
  python t_engine.py 600000 --no-proxy
  python t_engine.py 600000 --proxy http://127.0.0.1:7890

输出: 一行 JSON 到 stdout (analyze 结果)。错误也以 JSON {"error": "..."} 输出, 退出码 0。
"""
import os
import sys
import json
import math
import time
import random
import urllib.request
import urllib.parse
import datetime

# ---------- 配置 (与已验证版本一致) ----------
CONFIG = {
    "DEV_BUY": 1.8,
    "DEV_SELL": 1.8,
    "KDJ_OVERSOLD": 20,
    "KDJ_OVERBOUGHT": 100,
    "BOLL_N": 20,
    "BOLL_K": 2.0,
    "MACD_FAST": 5,
    "MACD_SLOW": 34,
    "MACD_SIG": 5,
    "KDJ_N": 9,
    "MIN_RESONANCE": 2,
    "AMP_MIN": 2.0,
    "SLOPE_N": 10,      # 均线斜率观察窗口(分钟)
    "SLOPE_MAX": 0.15,  # 均线斜率闸门(%): 超过则视为单边趋势, 禁做逆势T
    "T_GAP": 30,        # 卖出后回补观察窗(分钟): 窗口内未回落目标价=卖飞
    "ABOVE_MAX": 45,    # 卖出防卖飞: 价格连续在均价线上方超过该分钟数=单边强势日, 禁卖
    "OPEN_CALM": 30,    # 首卖加强确认: 开盘冷静期(分钟), 期内不出第一个卖点
    "TOUCH_N": 20,      # 首卖加强确认: 近N分钟内须回踩过均价线(证明震荡市)
    "FIRST_SPACE": 1.5, # 首卖加强确认: 回归空间须达目标的倍数(首卖安全垫)
    "FIRST_UNTIL": 60,  # 首卖加强确认仅限开盘后该分钟数内, 之后切常规闸门(防全天哑火)
    "GAP_N": 30,        # 贴均超时豁免观察窗(分钟): 窗内价差曾缩小到0.5倍目标=有回归迹象
    "GAP_X": 0.5,       # 贴均超时豁免阈值(倍目标)
    "SELL_CUTOFF": "14:45",  # 尾盘禁卖时点: 之后卖出没有时间等回落
}

DEFAULT_PROXY = "http://127.0.0.1:7890"


def _resolve_proxy(no_proxy: bool, proxy_arg: str):
    """返回代理 URL 或 None。优先级: --proxy > --no-proxy(强制直连) > 环境变量 > 默认"""
    if proxy_arg:
        return proxy_arg
    if no_proxy:
        return None
    env = os.environ.get("http_proxy") or os.environ.get("https_proxy") or \
          os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    if env:
        return env
    return DEFAULT_PROXY


def _get_opener(proxy):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


OPENER = None  # 由 main 初始化


def http_get(url: str, timeout: int = 15, referer: str = None, encoding: str = "utf-8") -> str:
    """带重试的 GET: 上游CDN偶发断连/限速掐连接, 重试4次, 间隔递增+随机抖动"""
    last_err = None
    for attempt in range(4):
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": referer or "https://finance.qq.com/",
        })
        try:
            return OPENER.open(req, timeout=timeout).read().decode(encoding, "ignore")
        except Exception as e:
            last_err = e
            if attempt < 3:
                time.sleep(0.8 * (attempt + 1) + random.random() * 0.4)
    raise last_err


# ---------- 指标计算 ----------
def ema(values, n):
    if not values:
        return []
    k = 2.0 / (n + 1)
    out = [values[0]]
    for i in range(1, len(values)):
        out.append(values[i] * k + out[i - 1] * (1 - k))
    return out


def calc_macd(close, fast, slow, sig):
    ef, es = ema(close, fast), ema(close, slow)
    dif = [ef[i] - es[i] for i in range(len(close))]
    dea = ema(dif, sig)
    bar = [2 * (dif[i] - dea[i]) for i in range(len(close))]
    return dif, dea, bar


def calc_kdj(high, low, close, n=9):
    k_list, d_list, j_list = [], [], []
    k, d = 50.0, 50.0
    for i in range(len(close)):
        if i < n - 1:
            k_list.append(50.0); d_list.append(50.0); j_list.append(50.0); continue
        hh = max(high[i - n + 1:i + 1])
        ll = min(low[i - n + 1:i + 1])
        rsv = (close[i] - ll) / (hh - ll) * 100 if hh != ll else 50.0
        k = 2.0 / 3 * k + 1.0 / 3 * rsv
        d = 2.0 / 3 * d + 1.0 / 3 * k
        j = 3 * k - 2 * d
        k_list.append(k); d_list.append(d); j_list.append(j)
    return k_list, d_list, j_list


def calc_boll(close, n=20, k=2.0):
    mid, up, low = [], [], []
    for i in range(len(close)):
        if i < n - 1:
            mid.append(None); up.append(None); low.append(None); continue
        win = close[i - n + 1:i + 1]
        m = sum(win) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in win) / n)
        mid.append(m); up.append(m + k * sd); low.append(m - k * sd)
    return mid, up, low


# ---------- 数据拉取 ----------
def normalize_code(raw: str):
    raw = raw.strip().lower()
    if raw.startswith("sh") or raw.startswith("sz"):
        return raw[:2] + raw[2:].upper(), raw[:2]
    raw = raw.upper()
    if raw[0] == "6" or raw[0] == "5" or raw[0] == "9":
        return "sh" + raw, "sh"
    return "sz" + raw, "sz"


def _extract_rows(d, code):
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, list) and v and isinstance(v[0], str) and len(v[0].split()) >= 4:
                    return v
                r = walk(v)
                if r:
                    return r
        elif isinstance(o, list):
            for it in o:
                if isinstance(it, list) and it and isinstance(it[0], str) and len(it[0].split()) >= 4:
                    return it
                r = walk(it)
                if r:
                    return r
        return None
    try:
        std = d["data"][code]["data"]["data"]
        if isinstance(std, list) and std:
            return std
    except (KeyError, TypeError):
        pass
    return walk(d)


def fetch_minutes(code: str):
    """腾讯分时 -> 失败自动切东财备援。返回 (times, price, cum_vol(手), cum_amt(元), src)"""
    c, _ = normalize_code(code)
    bust = int(time.time() * 1000)
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?_var=min_data_{c}&code={c}&_r={bust}"
        txt = http_get(url, referer="https://gu.qq.com/")
        if "min_data_" in txt:
            txt = txt.split("=", 1)[1]
        d = json.loads(txt)
        rows = _extract_rows(d, c)
        times, price, cvol, camt = [], [], [], []
        for r in rows:
            parts = r.split()
            if len(parts) < 4:
                continue
            t = parts[0]
            times.append(t[:2] + ":" + t[2:])
            price.append(float(parts[1]))
            cvol.append(float(parts[2]))   # 累计成交量(手)
            camt.append(float(parts[3]))   # 累计成交额(元)
        if times:
            return times, price, cvol, camt, "qq"
        raise RuntimeError("腾讯分时返回空")
    except Exception:
        return fetch_minutes_em(c)


def fetch_minutes_em(c: str):
    """东财分时备援(trends2): 分钟OHLC+量(手)+额(元)+均价, 累计后与腾讯口径对齐"""
    secid = ("1." if c.startswith("sh") else "0.") + c[2:]
    bust = int(time.time() * 1000)
    url = (f"https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid={secid}"
           f"&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays=1&_r={bust}")
    txt = http_get(url, referer="https://quote.eastmoney.com/")
    d = json.loads(txt)
    data = d.get("data") or {}
    tr = data.get("trends") or []
    if not tr:
        raise RuntimeError("分时数据拉取失败: 腾讯与东财均无数据")
    times, price, cvol, camt = [], [], [], []
    cc = ca = 0.0
    for r in tr:
        p = r.split(",")
        if len(p) < 8:
            continue
        cc += float(p[5])   # 分钟量(手)
        ca += float(p[6])   # 分钟额(元)
        times.append(p[0].split()[-1])
        price.append(float(p[2]))   # 分钟收盘
        cvol.append(cc)
        camt.append(ca)
    return times, price, cvol, camt, "em"


def fetch_meta(code: str):
    """名称与昨收: 腾讯qt(GBK) -> 失败切新浪(GBK)"""
    code, _ = normalize_code(code)
    try:
        txt = http_get(f"https://qt.gtimg.cn/q={code}", referer="https://gu.qq.com/", encoding="gbk")
        seg = txt.split('"')[1].split("~")
        return seg[1], float(seg[4])
    except Exception:
        txt = http_get(f"https://hq.sinajs.cn/list={code}", referer="https://finance.sina.com.cn/", encoding="gbk")
        seg = txt.split('"')[1].split(",")
        return seg[0], float(seg[2])


def fetch_quote(code: str):
    """实时盘口: 腾讯qt(GBK, 买1~5/卖1~5) -> 失败切新浪(仅一档, 量为股需/100转手)。"""
    c, _ = normalize_code(code)
    try:
        txt = http_get(f"https://qt.gtimg.cn/q={c}", referer="https://gu.qq.com/", encoding="gbk")
        seg = txt.split('"')[1].split("~")

        def f(i):
            try:
                return float(seg[i])
            except (ValueError, IndexError):
                return None
        return {
            "cur": f(3), "preClose": f(4), "open": f(5),
            "bid": [f(9), f(11), f(13), f(15), f(17)],
            "bidVol": [f(10), f(12), f(14), f(16), f(18)],
            "ask": [f(19), f(21), f(23), f(25), f(27)],
            "askVol": [f(20), f(22), f(24), f(26), f(28)],
        }
    except Exception:
        txt = http_get(f"https://hq.sinajs.cn/list={c}", referer="https://finance.sina.com.cn/", encoding="gbk")
        seg = txt.split('"')[1].split(",")

        def f(i):
            try:
                return float(seg[i])
            except (ValueError, IndexError):
                return None
        b1v, a1v = f(10), f(20)
        return {
            "cur": f(3), "preClose": f(2), "open": f(1),
            "bid": [f(6), None, None, None, None],
            "bidVol": [b1v / 100 if b1v else None, None, None, None, None],  # 新浪量为股 -> 手
            "ask": [f(7), None, None, None, None],
            "askVol": [a1v / 100 if a1v else None, None, None, None, None],
        }


# ---------- 信号生成 ----------
def eval_bar(i, price, avg, kdj_j, vol, ma5v, macd_bar, ub, lb, C):
    """对单根K线(i)评估买/卖共振条件, 返回 (buy_cond, sell_cond, reasons_b, reasons_s)"""
    c = price[i]
    a = avg[i]
    dev = (c - a) / a * 100 if a else 0
    j = kdj_j[i]
    v = vol[i]
    mv = ma5v[i]
    buy_cond = sell_cond = 0
    reasons_b, reasons_s = [], []
    if dev <= -C["DEV_BUY"]:
        buy_cond += 1; reasons_b.append(f"低于均价线{-dev:.2f}%")
    if j < C["KDJ_OVERSOLD"]:
        buy_cond += 1; reasons_b.append(f"KDJ超卖(J={j:.0f})")
    if lb[i] is not None and c <= lb[i]:
        buy_cond += 1; reasons_b.append("触及布林下轨")
    if mv > 0 and v < 0.8 * mv:
        buy_cond += 1; reasons_b.append("缩量(杀不动)")
    if macd_bar[i] < 0 and macd_bar[i] > macd_bar[i - 1]:
        buy_cond += 1; reasons_b.append("MACD绿柱缩短(跌势衰减)")
    if dev >= C["DEV_SELL"]:
        sell_cond += 1; reasons_s.append(f"高于均价线{dev:.2f}%")
    if j > C["KDJ_OVERBOUGHT"]:
        sell_cond += 1; reasons_s.append(f"KDJ超买(J={j:.0f})")
    if ub[i] is not None and c >= ub[i]:
        sell_cond += 1; reasons_s.append("触及布林上轨")
    win_hi = max(price[max(0, i - 9):i + 1])
    if c >= win_hi - 1e-9 and mv > 0 and v < mv:
        sell_cond += 1; reasons_s.append("量价背离(涨不动)")
    if macd_bar[i] > 0 and macd_bar[i] < macd_bar[i - 1]:
        sell_cond += 1; reasons_s.append("MACD红柱缩短(涨势衰减)")
    return buy_cond, sell_cond, reasons_b, reasons_s


def gen_signals(times, price, avg, close_high, close_low, vol, macd_bar, kdj_j, ub, lb,
                target, breakeven, tick, shares):
    """target=建议目标(元), breakeven=每股保本(元), tick=最小变动, shares=股数(算止损金额)"""
    C = CONFIG
    n = len(price)
    ma5v = []
    for i in range(n):
        s = max(0, i - 4)
        seg = vol[s:i + 1]
        ma5v.append(sum(seg) / len(seg)) if seg else 0

    # 均线斜率(%): 回归的前提是均价线别自己往下(上)跑, 否则价格追不上一条下坠的均线
    # 窗口不足SLOPE_N时退化为自开盘起算的短窗口斜率, 避免早盘飞刀漏网
    slopes = [0.0] * n
    for i in range(1, n):
        base = avg[i - C["SLOPE_N"]] if i >= C["SLOPE_N"] else avg[0]
        if base:
            slopes[i] = (avg[i] / base - 1) * 100

    stop_dist = max(3 * tick, 1.5 * breakeven)   # 止损距离: max(3档, 1.5倍保本)
    horizon = C["T_GAP"]

    def sell_success(idx):
        """卖点后 horizon 分钟内是否回落到 目标价 之下(成功T出)。窗口未封闭返回 None"""
        end = min(n, idx + 1 + horizon)
        if idx + 1 >= end:
            return None
        return min(price[idx + 1:end]) <= price[idx] - target

    raw = []
    n_filtered = 0
    sell_emitted = []   # 已放行的卖出信号索引(供防卖飞战绩统计, 因果: 只看已封闭窗口)
    for i in range(1, n):
        buy_cond, sell_cond, reasons_b, reasons_s = eval_bar(
            i, price, avg, kdj_j, vol, ma5v, macd_bar, ub, lb, C)
        if buy_cond >= C["MIN_RESONANCE"]:
            space = avg[i] - price[i]          # 正T回归空间(元)
            if space >= target and slopes[i] > -C["SLOPE_MAX"]:
                raw.append((i, "BUY_T", buy_cond, reasons_b))
            else:
                n_filtered += 1
        if sell_cond >= C["MIN_RESONANCE"]:
            # ---- 尾盘禁卖: 收盘前卖出没有时间等回落(回补不了) ----
            if times[i] >= C["SELL_CUTOFF"]:
                n_filtered += 1
                continue
            space = price[i] - avg[i]          # 反T回归空间(元)
            if space < target or slopes[i] >= C["SLOPE_MAX"]:
                n_filtered += 1
                continue
            # ---- 防卖飞·首卖加强确认: 第一个卖点实时无法事后验证(无✗标记), 必须更严 ----
            # 仅限开盘后 FIRST_UNTIL 分钟内; 之后切常规闸门, 防止"首卖被拦->永远当首卖"的全天哑火
            if not sell_emitted and i < C["FIRST_UNTIL"]:
                if i < C["OPEN_CALM"]:
                    n_filtered += 1    # 开盘冷静期: 半小时内方向未明, 不做反T
                    continue
                if not any(price[k] <= avg[k] for k in range(max(0, i - C["TOUCH_N"]), i + 1)):
                    n_filtered += 1    # 近端未回踩均价线: 今日尚未证明是震荡市, 单边拉升不卖
                    continue
                if space < C["FIRST_SPACE"] * target:
                    n_filtered += 1    # 首卖安全垫: 空间须达目标1.5倍
                    continue
            # ---- 防卖飞第4道闸: 贴均时间(带回归迹象豁免) ----
            # 价格连续运行于均价线上方超过 ABOVE_MAX 分钟:
            #   近 GAP_N 分钟内价差曾缩小到 0.5倍目标以内 => 有回归迹象, 放行(围绕均价波动的股)
            #   持续远离均价 => 单边强势日, 卖出必飞, 禁卖
            above_mins = 0
            k = i
            while k >= 0 and price[k] > avg[k]:
                above_mins += 1
                k -= 1
            if above_mins > C["ABOVE_MAX"]:
                min_gap = min(price[k] - avg[k] for k in range(max(0, i - C["GAP_N"]), i + 1))
                if min_gap > C["GAP_X"] * target:
                    n_filtered += 1
                    continue
            # ---- 防卖飞第5道闸: 当日反T战绩(因果: 只看窗口已封闭的历史卖点) ----
            # ①某卖点30分钟内未回落, 且此后价格再创新高于该卖点价、期间未回踩均价 -> 趋势确认, 停卖
            # ②窗口已封闭卖点>=2次且成功率<50% -> 趋势日, 停卖
            fly = False
            closed = []
            for j in sell_emitted:
                ok = sell_success(j)
                if ok is None:
                    continue
                closed.append((j, ok))
                if (not ok and price[i] > price[j]
                        and all(price[k] > avg[k] for k in range(j + 1, i + 1))):
                    fly = True
                    break
            if not fly and len(closed) >= 2:
                ok_rate = sum(1 for _, ok in closed if ok) / len(closed)
                if ok_rate < 0.5:
                    fly = True
            if fly:
                n_filtered += 1
                continue
            raw.append((i, "SELL_T", sell_cond, reasons_s))
            sell_emitted.append(i)

    signals = []
    i = 0
    while i < len(raw):
        j = i
        best = raw[i]
        while j + 1 < len(raw) and raw[j + 1][0] - raw[j][0] <= 4 and raw[j + 1][1] == raw[i][1]:
            j += 1
            if raw[j][1] == "BUY_T" and price[raw[j][0]] < price[best[0]]:
                best = raw[j]
            if raw[j][1] == "SELL_T" and price[raw[j][0]] > price[best[0]]:
                best = raw[j]
        idx, typ, strength, reasons = best
        # 止损线: 买点=入场价下方 / 卖点=入场价上方(防卖飞)
        if typ == "BUY_T":
            stop = round(price[idx] - stop_dist, 3)
            stop_text = f"止损: 跌破{stop:.2f}止损买回(认亏≤{stop_dist * shares:.0f}元)"
        else:
            stop = round(price[idx] + stop_dist, 3)
            buyback = round(price[idx] - target, 3)
            stop_text = (f"止损: 突破{stop:.2f}止损买回,防卖飞(认亏≤{stop_dist * shares:.0f}元)；"
                         f"限{C['T_GAP']}分钟内回落至{buyback:.2f}下方买回,否则市价回补")
        # 事后当日验证: 该信号触发后, 当日剩余时间是否给过 达到目标 的离场机会
        if idx + 1 < n:
            if typ == "BUY_T":
                best_exit = max(price[idx + 1:])
                ok = best_exit >= price[idx] + target
                fen = (best_exit - price[idx]) * 100
            else:
                best_exit = min(price[idx + 1:])
                ok = best_exit <= price[idx] - target
                fen = (price[idx] - best_exit) * 100
            check = f"✓ 当日可T出(最好+{fen:.1f}分)" if ok else f"✗ 当日未能T出(最好{'+' if fen>=0 else ''}{fen:.1f}分)"
        else:
            check = "实时信号·未验证"
        signals.append({
            "time": times[idx], "price": round(price[idx], 3),
            "type": typ, "strength": strength,
            "reasons": reasons[:4],
            # 成交规则: 买入必须挂对手盘的卖1(吃卖单)才能确保成交; 卖出挂买1(吃买单)
            "fill": "挂卖1价买入·确保成交" if typ == "BUY_T" else "挂买1价卖出·确保成交",
            "stop": stop, "stopText": stop_text,
            "check": check,
            "level": "极强" if strength >= 4 else ("高" if strength == 3 else "中"),
        })
        i = j + 1
    return signals, n_filtered, sell_emitted


def analyze(code: str, shares_in=None, cost_in=None, comm_in=None):
    try:
        times, price, cvol, camt, msrc = fetch_minutes(code)
    except Exception as e:
        return {"error": f"分时数据不可达: {e}"}
    name, pre_close = fetch_meta(code)
    if not price:
        return {"error": "无分时数据(可能未开盘或非交易日)"}
    n = len(price)
    avg = [camt[i] / (cvol[i] * 100) if cvol[i] > 0 else price[i] for i in range(n)]
    high = price[:]; low = price[:]
    dif, dea, macd_bar = calc_macd(price, CONFIG["MACD_FAST"], CONFIG["MACD_SLOW"], CONFIG["MACD_SIG"])
    k_list, d_list, j_list = calc_kdj(high, low, price, CONFIG["KDJ_N"])
    bmid, bup, bdown = calc_boll(price, CONFIG["BOLL_N"], CONFIG["BOLL_K"])
    vol_lst = [max(1, cvol[i] - (cvol[i - 1] if i else 0)) for i in range(n)]

    cur = price[-1]; cur_avg = avg[-1]; cur_dev = (cur - cur_avg) / cur_avg * 100 if cur_avg else 0
    day_high, day_low = max(price), min(price)
    amp = (day_high - day_low) / pre_close * 100 if pre_close else 0
    zone = "超买区(谨慎追高,优先反T)" if cur_dev > CONFIG["DEV_SELL"] else (
        "超卖区(可关注正T买点)" if cur_dev < -CONFIG["DEV_BUY"] else "均衡区")
    suit = "波动充足,适合做T" if amp >= CONFIG["AMP_MIN"] else "当日振幅偏小,做T易亏手续费,建议观望"

    # ---- 做T成本核算(优先用户真实持仓, 否则按 2W 元本金口径) ----
    # 费用: 佣金万2.5(最低5元/笔)x2 + 印花税卖出0.05% + 过户费0.001%x2; 价差: 吃对手盘1档
    tick = 0.001 if code[2:4] in ("51", "56", "58", "15", "16", "18") else 0.01  # ETF 0.001 / 股票 0.01
    use_hold = bool(shares_in and shares_in >= 100)
    if use_hold:
        shares = int(shares_in)
        base_price = cost_in if (cost_in and cost_in > 0) else cur
    else:
        shares = max(100, int(20000 / cur / 100) * 100) if cur > 0 else 100
        base_price = cur
    amt = shares * base_price
    # 佣金: 一律保留最低5元/笔(主流券商均不免5); 用户填费率(‱)按其真实费率, 未填默认华泰0.00341%(万0.341)
    if comm_in and comm_in > 0:
        comm = max(5.0, amt * comm_in / 10000.0)
        comm_note = f"佣金万{comm_in:g}最低5元/笔"
    else:
        comm = max(5.0, amt * 0.0000341)
        comm_note = "佣金万0.341最低5元/笔(华泰)"
    stamp = amt * 0.0005            # 印花税(卖出单边)
    transfer = amt * 0.00001 * 2    # 过户费(双边)
    fee_total = comm * 2 + stamp + transfer
    gap_cost = shares * tick
    cost_total = fee_total + gap_cost
    cost_per_share = cost_total / shares            # 每股保本净移动(元)
    # 建议目标 = 保本涨跌向上取整到最小变动单位(如保本1.3分→2分); 随股价/仓位自适应
    suggest_mv = math.ceil(cost_per_share / tick - 1e-9) * tick
    src_txt = f"持仓{shares}股@{base_price:.2f}" if use_hold else f"按2W本金({shares}股)"
    per_fen = shares * 0.01  # 价格每多走1分(0.01元)的净利金额
    cost_text = (f"{src_txt}：一次T成本≈{cost_total:.1f}元"
                 f"（{comm_note}: 买{comm:.1f}+卖{comm:.1f}；印花税仅卖出收{stamp:.1f}，买入免；"
                 f"过户费双边{transfer:.1f}；价差{gap_cost:.1f}），"
                 f"保本需涨跌{cost_per_share*100:.1f}分，超过即有收益（每多走1分净赚{per_fen:.0f}元）；"
                 f"建议目标≥{suggest_mv*100:.1f}分（{suggest_mv/base_price*100:.2f}%，保本向上取整）≈净赚{(suggest_mv - cost_per_share) * shares:.0f}元")
    # 价格移动情景: pnl = (移动分数x0.01 - 每股保本) x 股数
    # 0分=白跑一趟亏滑移+费用; 每朝有利方向多走1分, 净赚 股数x0.01 元
    scenarios = [{"mvFen": mv, "pnl": round((mv * 0.01 - cost_per_share) * shares, 1)} for mv in (0, 1, 2, 3)]
    scenario_text = " ｜ ".join(
        f"{'不动' if s['mvFen'] == 0 else '+' + str(s['mvFen']) + '分'}:{s['pnl']:+.0f}元" for s in scenarios
    ) + f"（每多走1分净赚{per_fen:.0f}元）"
    pnl = (cur - cost_in) * shares if (use_hold and cost_in and cost_in > 0) else None

    # ---- 信号生成(带回归空间/均线斜率过滤 + 防卖飞双闸 + 止损线 + 事后验证) ----
    signals, n_filtered, sell_emitted = gen_signals(times, price, avg, high, low,
                                                    vol_lst, macd_bar, j_list, bup, bdown,
                                                    target=suggest_mv, breakeven=cost_per_share,
                                                    tick=tick, shares=shares)

    q = fetch_quote(code)
    bid = q.get("bid") or [None] * 5
    ask = q.get("ask") or [None] * 5
    bidVol = q.get("bidVol") or [None] * 5
    askVol = q.get("askVol") or [None] * 5
    bid1, ask1 = bid[0], ask[0]
    bid1v, ask1v = bidVol[0], askVol[0]
    spread = round(ask1 - bid1, 3) if (ask1 and bid1) else None
    spread_bp = round((ask1 - bid1) / pre_close * 10000, 1) if (ask1 and bid1 and pre_close) else None
    depth_text = ""
    if bid1v and ask1v:
        if ask1v > bid1v * 2:
            depth_text = "卖盘压单重：反T卖出易成交；正T买入后警惕继续下探"
        elif bid1v > ask1v * 2:
            depth_text = "买盘托单厚：正T买入易成交；反T卖出注意流动性"
        else:
            depth_text = "买卖盘力量均衡"
    if zone.startswith("超买"):
        fill_now = ("反T卖出：挂买1 %.3f 确保成交" % bid1) if bid1 else "反T卖出：挂买1价确保成交"
    elif zone.startswith("超卖"):
        fill_now = ("正T买入：挂卖1 %.3f 确保成交" % ask1) if ask1 else "正T买入：挂卖1价确保成交"
    else:
        fill_now = "均衡区：买挂卖1、卖挂买1，确保即时成交"

    # ---- 实时做T决策: 最后一根K线技术状态 + 当前盘口对手价 ----
    ma5v_live = []
    for i in range(n):
        s = max(0, i - 4)
        segv = vol_lst[s:i + 1]
        ma5v_live.append(sum(segv) / len(segv)) if segv else 0
    live = {"triggered": False, "action": None, "strength": 0, "level": "-",
            "reasons": [], "orderSide": None, "orderPrice": None, "orderVol": None,
            "fillText": None, "advice": ""}
    if n >= 2:
        bi = n - 1
        bc, sc, rb, rs = eval_bar(bi, price, avg, j_list, vol_lst, ma5v_live, macd_bar, bup, bdown, CONFIG)
        best_cond = max(bc, sc)
        live_slope = (avg[bi] / avg[bi - CONFIG["SLOPE_N"]] - 1) * 100 if bi >= CONFIG["SLOPE_N"] and avg[bi - CONFIG["SLOPE_N"]] else 0.0
        live["slope"] = round(live_slope, 3)
        stop_dist = max(3 * tick, 1.5 * cost_per_share)
        # 风控闸门: 回归空间须覆盖成本目标; 均线斜率过陡=单边趋势, 回避接飞刀/卖飞
        buy_gate = (cur_avg - cur) >= suggest_mv and live_slope > -CONFIG["SLOPE_MAX"]
        # 防卖飞双闸(因果, 与复盘同源): ①贴均时间 ②当日反T战绩
        above_mins = 0
        k = bi
        while k >= 0 and price[k] > avg[k]:
            above_mins += 1
            k -= 1
        sell_fly_reason = None
        # 尾盘禁卖(与复盘同源)
        if times[bi] >= CONFIG["SELL_CUTOFF"]:
            sell_fly_reason = f"已过{CONFIG['SELL_CUTOFF']},尾盘卖出没有时间等回落,不卖"
        # 首卖加强确认(与复盘同源): 仅开盘后 FIRST_UNTIL 分钟内生效, 之后切常规闸门
        elif not sell_emitted and bi < CONFIG["FIRST_UNTIL"]:
            if bi < CONFIG["OPEN_CALM"]:
                sell_fly_reason = f"首个卖出信号加强确认: 开盘未满{CONFIG['OPEN_CALM']}分钟方向未明,不卖(防卖飞)"
            elif not any(price[k] <= avg[k] for k in range(max(0, bi - CONFIG["TOUCH_N"]), bi + 1)):
                sell_fly_reason = f"首个卖出信号加强确认: 近{CONFIG['TOUCH_N']}分钟未回踩均价线,今日尚未证明是震荡市(单边拉升不卖)"
            elif (cur - cur_avg) < CONFIG["FIRST_SPACE"] * suggest_mv:
                sell_fly_reason = (f"首个卖出信号加强确认: 回归空间{(cur - cur_avg)*100:.1f}分"
                                   f"不足目标{suggest_mv*100:.1f}分的{CONFIG['FIRST_SPACE']:g}倍安全垫")
        if sell_fly_reason is None and above_mins > CONFIG["ABOVE_MAX"]:
            # 贴均超时+回归迹象豁免(与复盘同源)
            min_gap = min(price[k] - avg[k] for k in range(max(0, bi - CONFIG["GAP_N"]), bi + 1))
            if min_gap > CONFIG["GAP_X"] * suggest_mv:
                sell_fly_reason = (f"价格已连续{above_mins}分钟运行于均价线上方且近{CONFIG['GAP_N']}分钟未回归"
                                   f"(单边远离,卖出易卖飞)")
        else:
            # 当日反T战绩(与复盘同源): 失败卖点后再创新高不回踩=趋势确认 / 成功率<50%=趋势日
            closed = []
            for j in sell_emitted:
                endj = min(n, j + 1 + CONFIG["T_GAP"])
                if j + 1 >= endj:
                    continue
                ok = min(price[j + 1:endj]) <= price[j] - suggest_mv
                closed.append((j, ok))
                if (not ok and cur > price[j]
                        and all(price[k] > avg[k] for k in range(j + 1, bi + 1))):
                    sell_fly_reason = "此前卖出信号未按时回落,现价反创新高且不回踩均价(趋势确认,暂停卖出防卖飞)"
                    break
            if sell_fly_reason is None and len(closed) >= 2:
                ok_n = sum(1 for _, ok in closed if ok)
                if ok_n / len(closed) < 0.5:
                    sell_fly_reason = (f"今日卖出信号{len(closed)}次中仅{ok_n}次在{CONFIG['T_GAP']}分钟内回落达标"
                                       f"(趋势日反T胜率低,暂停卖出信号)")
        sell_gate = ((cur - cur_avg) >= suggest_mv and live_slope < CONFIG["SLOPE_MAX"]
                     and sell_fly_reason is None)
        if bc >= CONFIG["MIN_RESONANCE"] and buy_gate and ask1 is not None:
            stop_p = round(cur - stop_dist, 3)
            live = {
                "triggered": True, "action": "BUY_T", "strength": bc,
                "level": "极强" if bc >= 4 else ("高" if bc == 3 else "中"),
                "slope": round(live_slope, 3),
                "reasons": rb[:4], "orderSide": "buy", "orderPrice": ask1, "orderVol": ask1v,
                "fillText": f"挂卖1价 {ask1:.3f}（卖1挂单 {int(ask1v or 0)} 手）正T买入·确保成交",
                "stopText": f"止损纪律: 跌破{stop_p:.2f}止损买回(认亏≤{stop_dist * shares:.0f}元)",
                "advice": ("正T买入：挂卖1价 " + f"{ask1:.3f}" + " 吃卖盘挂单确保成交。理由：" + "；".join(rb[:4])
                           + "。若此前已有卖出未回补,该点即回补点；单纯买入信号(无前次卖出)仅参考"),
            }
        elif sc >= CONFIG["MIN_RESONANCE"] and sell_gate and bid1 is not None:
            stop_p = round(cur + stop_dist, 3)
            buyback_live = round(cur - suggest_mv, 3)
            live = {
                "triggered": True, "action": "SELL_T", "strength": sc,
                "level": "极强" if sc >= 4 else ("高" if sc == 3 else "中"),
                "slope": round(live_slope, 3),
                "reasons": rs[:4], "orderSide": "sell", "orderPrice": bid1, "orderVol": bid1v,
                "fillText": f"挂买1价 {bid1:.3f}（买1挂单 {int(bid1v or 0)} 手）反T卖出·确保成交",
                "stopText": (f"止损纪律: 突破{stop_p:.2f}止损买回,防卖飞(认亏≤{stop_dist * shares:.0f}元)；"
                             f"限{CONFIG['T_GAP']}分钟内回落至{buyback_live:.2f}下方买回,否则市价回补"),
                "advice": "反T卖出：挂买1价 " + f"{bid1:.3f}" + " 吃买盘挂单确保成交。理由：" + "；".join(rs[:4]),
            }
        else:
            pre = ""
            if ask1 is not None and bid1 is not None:
                pre = f"若提前挂单：正T挂卖1 {ask1:.3f} 买、反T挂买1 {bid1:.3f} 卖。"
            block = ""
            if bc >= CONFIG["MIN_RESONANCE"] and not buy_gate:
                why = []
                if (cur_avg - cur) < suggest_mv:
                    why.append(f"回归空间{(cur_avg - cur)*100:.1f}分<目标{suggest_mv*100:.1f}分")
                if live_slope <= -CONFIG["SLOPE_MAX"]:
                    why.append(f"均线{CONFIG['SLOPE_N']}分钟斜率{live_slope:.2f}%过陡(下跌趋势,回避接飞刀)")
                block = "买入共振达%d/%d但被风控拦截：%s。" % (bc, 5, "、".join(why))
            elif sc >= CONFIG["MIN_RESONANCE"] and not sell_gate:
                why = []
                if (cur - cur_avg) < suggest_mv:
                    why.append(f"回归空间{(cur - cur_avg)*100:.1f}分<目标{suggest_mv*100:.1f}分")
                if live_slope >= CONFIG["SLOPE_MAX"]:
                    why.append(f"均线{CONFIG['SLOPE_N']}分钟斜率+{live_slope:.2f}%过陡(上涨趋势,反T易卖飞)")
                if sell_fly_reason:
                    why.append(sell_fly_reason)
                block = "卖出共振达%d/%d但被风控拦截：%s。" % (sc, 5, "、".join(why))
            live = {
                "triggered": False, "action": None, "strength": best_cond,
                "level": "-", "slope": round(live_slope, 3), "reasons": [], "orderSide": None,
                "orderPrice": None, "orderVol": None, "fillText": None, "stopText": None,
                "advice": f"{block}当前技术条件 {best_cond}/5。观望为主。{pre}",
            }

    # ---- 实时决策成本判定: 至均价回归空间是否覆盖做T成本 ----
    if live.get("action") == "BUY_T":
        expect_mv = cur_avg - cur        # 正T: 现价买入, 回归均价卖出
    elif live.get("action") == "SELL_T":
        expect_mv = cur - cur_avg        # 反T: 现价卖出, 回归均价买回
    else:
        expect_mv = 0.0
    live["expectFen"] = round(expect_mv * 100, 1)
    live["targetFen"] = round(suggest_mv * 100, 1)
    if live.get("triggered"):
        if expect_mv >= suggest_mv:
            live["verdict"] = f"✅ 可做：至均价回归空间约 {expect_mv*100:.1f} 分 ≥ 目标 {suggest_mv*100:.1f} 分，净赚约 {(expect_mv - cost_per_share) * shares:.0f} 元"
        elif expect_mv > cost_per_share:
            live["verdict"] = f"⚠️ 微利：至均价回归空间约 {expect_mv*100:.1f} 分，在保本 {cost_per_share*100:.1f} 分之上但不足目标 {suggest_mv*100:.1f} 分，收益偏薄"
        else:
            live["verdict"] = f"❌ 不建议：至均价回归空间约 {expect_mv*100:.1f} 分 ≤ 保本 {cost_per_share*100:.1f} 分，覆盖不了成本，做了白做甚至亏"
    else:
        live["verdict"] = None

    return {
        "code": code, "name": name, "date": datetime.date.today().isoformat(),
        "preClose": pre_close, "curPrice": round(cur, 3), "curAvg": round(cur_avg, 3),
        "curDev": round(cur_dev, 2), "dayAmp": round(amp, 2), "zone": zone, "suit": suit,
        "src": msrc,
        "buyCount": sum(1 for s in signals if s["type"] == "BUY_T"),
        "sellCount": sum(1 for s in signals if s["type"] == "SELL_T"),
        "filteredSignals": n_filtered,
        "series": {
            "times": times, "price": [round(x, 3) for x in price],
            "avg": [round(x, 3) for x in avg], "macd": [round(x, 4) for x in macd_bar],
            "kdjJ": [round(x, 1) for x in j_list],
            "bollUp": [round(x, 3) if x is not None else None for x in bup],
            "bollLow": [round(x, 3) if x is not None else None for x in bdown],
        },
        "signals": signals,
        "live": live,
        "cost": {
            "shares": shares, "basePrice": round(base_price, 3), "tick": tick,
            "costTotal": round(cost_total, 1), "fee": round(fee_total, 1), "gap": round(gap_cost, 1),
            "breakevenFen": round(cost_per_share * 100, 1), "suggestFen": round(suggest_mv * 100, 1),
            "text": cost_text, "pnl": round(pnl, 1) if pnl is not None else None,
            "scenarioText": scenario_text, "scenarios": scenarios,
        },
        "quote": {
            "cur": q.get("cur"), "preClose": q.get("preClose"),
            "bid1": bid1, "ask1": ask1,
            "bid1Vol": bid1v, "ask1Vol": ask1v,
            "bid5": bid[4], "ask5": ask[4],
            "spread": spread, "spreadBp": spread_bp,
            "depthText": depth_text, "fillNow": fill_now,
        },
    }


def main():
    global OPENER
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(json.dumps({"error": "用法: python t_engine.py <代码> [--no-proxy|--proxy URL]"}, ensure_ascii=False))
        return
    code = argv[0]
    no_proxy = "--no-proxy" in argv
    proxy_arg = None
    for a in argv:
        if a.startswith("--proxy"):
            proxy_arg = a.split("=", 1)[1] if "=" in a else DEFAULT_PROXY
    proxy = _resolve_proxy(no_proxy, proxy_arg)
    OPENER = _get_opener(proxy)
    shares_in = cost_in = comm_in = None
    for a in argv:
        if a.startswith("--shares"):
            try: shares_in = int(a.split("=", 1)[1])
            except Exception: shares_in = None
        elif a.startswith("--cost"):
            try: cost_in = float(a.split("=", 1)[1])
            except Exception: cost_in = None
        elif a.startswith("--comm"):
            try: comm_in = float(a.split("=", 1)[1])
            except Exception: comm_in = None
    try:
        code, _ = normalize_code(code)
        out = analyze(code, shares_in=shares_in, cost_in=cost_in, comm_in=comm_in)
    except Exception as e:
        out = {"error": str(e)}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
