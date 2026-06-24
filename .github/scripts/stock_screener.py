"""
A股选股脚本 v2 - 使用东方财富公开API（境外可访问）
每个工作日15:35（A股收盘后）自动运行
"""
import json, os, time, math
from datetime import datetime
import urllib.request, urllib.error

OUTPUT_PATH = "data/recommendations.json"
os.makedirs("data", exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.eastmoney.com/"
}

def fetch(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  [fetch error] {url[:80]}... => {e}")
        return None

def get_spot_batch(codes):
    """批量获取实时行情，codes是6位代码列表"""
    secids = ",".join(("1." if c.startswith(("60","68","11")) else "0.") + c for c in codes)
    url = f"https://push2.eastmoney.com/api/qt/ulist.np/get?secids={secids}&fields=f2,f3,f4,f5,f6,f10,f12,f14,f15,f16,f17,f18,f20,f23"
    d = fetch(url)
    if not d or not d.get("data") or not d["data"].get("diff"):
        return {}
    result = {}
    for item in d["data"]["diff"]:
        code = item.get("f12","")
        if code:
            result[code] = {
                "name": item.get("f14",""),
                "price": (item.get("f2") or 0) / 100,
                "change": (item.get("f3") or 0) / 100,
                "volume": item.get("f5") or 0,
                "turnover": (item.get("f10") or 0) / 100,
                "market_cap": (item.get("f20") or 0) / 1e8,
                "pe": (item.get("f23") or 0) / 100,
                "high": (item.get("f15") or 0) / 100,
                "low": (item.get("f16") or 0) / 100,
            }
    return result

def get_kline(code, days=90, period=101):
    """获取K线，period: 101=日K"""
    secid = ("1." if code.startswith(("60","68","11")) else "0.") + code
    url = f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&klt={period}&fqt=1&lmt={days}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56"
    d = fetch(url)
    if not d or not d.get("data") or not d["data"].get("klines"):
        return None
    rows = []
    for line in d["data"]["klines"]:
        parts = line.split(",")
        if len(parts) >= 6:
            rows.append({
                "date": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5])
            })
    return rows

def get_stock_list():
    """获取沪深A股列表（东方财富全市场接口）"""
    print("[1/4] 拉取股票列表...")
    # 东方财富全量股票列表
    url = "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=5000&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12,f14,f3,f2,f20,f10,f23"
    d = fetch(url)
    codes = []
    if d and d.get("data") and d["data"].get("diff"):
        for item in d["data"]["diff"]:
            code = str(item.get("f12","")).zfill(6)
            if code and not code.startswith(("688","300","8","4","9")):  # 排除科创板、创业板
                codes.append(code)
        # 加回科创板和创业板
        for item in d["data"]["diff"]:
            code = str(item.get("f12","")).zfill(6)
            if code.startswith(("688","300")):
                codes.append(code)
    print(f"  股票池: {len(codes)} 只")
    return codes[:3000]  # 取前3000只

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)

def ma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n

# ─── 短线筛选 ────────────────────────────────────────────────────────
def screen_short(spot, codes, max_n=10):
    print("[短线] 开始筛选...")
    results = []
    # 预筛：市值>50亿，涨跌幅不超过±9%（防连板）
    pool = [c for c in codes if c in spot
            and spot[c]["market_cap"] > 50
            and abs(spot[c]["change"]) < 9][:300]

    for code in pool:
        klines = get_kline(code, days=60)
        if not klines or len(klines) < 25:
            continue
        closes = [k["close"] for k in klines]
        vols = [k["volume"] for k in klines]

        ma5_now = ma(closes, 5)
        ma20_now = ma(closes, 20)
        ma5_prev = ma(closes[:-5], 5)
        ma20_prev = ma(closes[:-5], 20)
        if not all([ma5_now, ma20_now, ma5_prev, ma20_prev]):
            continue

        # 金叉：当前MA5>MA20，5日前MA5<MA20
        golden = ma5_now > ma20_now and ma5_prev <= ma20_prev
        if not golden:
            continue

        # 量能放大
        vol_recent = sum(vols[-5:]) / 5
        vol_base = sum(vols[-25:-5]) / 20
        vol_ratio = vol_recent / (vol_base + 1e-9)
        if vol_ratio < 1.5:
            continue

        rsi = calc_rsi(closes)
        if rsi is None or not (40 <= rsi <= 70):
            continue

        chg5 = (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 else 99
        if chg5 > 15:
            continue

        rsi_score = 30 * (1 - abs(rsi - 50) / 50)
        vol_score = min(40, (vol_ratio - 1.5) / 2 * 40)
        cross_score = min(30, (ma5_now / ma20_now - 1) * 1000)
        score = int(rsi_score + vol_score + cross_score)

        s = spot[code]
        results.append({
            "code": code, "name": s["name"], "score": score,
            "reason": f"MA5/MA20金叉，量能放大{vol_ratio:.1f}倍，RSI={rsi:.0f}健康区间，近5日涨{chg5:.1f}%",
            "price": round(s["price"], 2),
            "change_pct": round(s["change"], 2),
            "market_cap": round(s["market_cap"], 0),
            "key_metrics": {"rsi": round(rsi,1), "vol_ratio": round(vol_ratio,2), "ma_signal": "金叉", "chg5d": round(chg5,1)}
        })
        if len(results) >= max_n * 3:
            break

    results.sort(key=lambda x: -x["score"])
    print(f"  短线命中 {len(results)} 只，取前{max_n}")
    return results[:max_n]

# ─── 中期筛选 ────────────────────────────────────────────────────────
def screen_mid(spot, codes, max_n=10):
    print("[中期] 开始筛选...")
    results = []
    pool = [c for c in codes if c in spot
            and spot[c]["market_cap"] > 100
            and 0.5 <= spot[c].get("turnover", 0) <= 6][:200]

    for code in pool:
        klines = get_kline(code, days=90)
        if not klines or len(klines) < 65:
            continue
        closes = [k["close"] for k in klines]

        ma60 = ma(closes, 60)
        if not ma60 or closes[-1] <= ma60:
            continue

        # MA60斜率向上（简单线性回归最近20期）
        ma60_vals = [ma(closes[:i+1], 60) for i in range(len(closes)-20, len(closes)) if len(closes[:i+1]) >= 60]
        if len(ma60_vals) < 5:
            continue
        slope = (ma60_vals[-1] - ma60_vals[0]) / len(ma60_vals)
        if slope <= 0:
            continue

        chg60 = (closes[-1] / closes[-61] - 1) * 100 if len(closes) >= 61 else 0
        if chg60 > 40 or chg60 < -10:
            continue

        s = spot[code]
        turnover = s.get("turnover", 0)
        above_pct = (closes[-1] / ma60 - 1) * 100

        trend_score = min(40, slope / ma60 * 10000)
        turn_score = 30 * (1 - abs(turnover - 3) / 4)
        chg_score = max(0, 30 * (1 - chg60 / 40))
        score = int(max(0,trend_score) + max(0,turn_score) + max(0,chg_score))

        results.append({
            "code": code, "name": s["name"], "score": score,
            "reason": f"60日均线向上，价格在均线上方{above_pct:.1f}%，换手率{turnover:.1f}%适中，60日涨{chg60:.1f}%未过度透支",
            "price": round(s["price"], 2),
            "change_pct": round(s["change"], 2),
            "market_cap": round(s["market_cap"], 0),
            "key_metrics": {"ma60_pct": round(above_pct,1), "turnover": round(turnover,1), "chg60d": round(chg60,1)}
        })
        if len(results) >= max_n * 3:
            break

    results.sort(key=lambda x: -x["score"])
    print(f"  中期命中 {len(results)} 只，取前{max_n}")
    return results[:max_n]

# ─── 长线筛选 ────────────────────────────────────────────────────────
def screen_long(spot, codes, max_n=10):
    """长线：用PE+市值+近1年涨幅作为代理指标（基本面数据境外难以获取）"""
    print("[长线] 开始筛选...")
    results = []
    pool = [c for c in codes if c in spot
            and spot[c]["market_cap"] > 200
            and 0 < spot[c].get("pe", 0) < 40][:200]

    for code in pool:
        klines = get_kline(code, days=250)
        if not klines or len(klines) < 200:
            continue
        closes = [k["close"] for k in klines]

        # 近1年涨幅适中（不追高）
        chg250 = (closes[-1] / closes[0] - 1) * 100
        if chg250 > 80 or chg250 < -20:
            continue

        # 长期均线向上
        ma200 = ma(closes, 200)
        if not ma200 or closes[-1] < ma200:
            continue

        # 均线斜率
        ma200_vals = [ma(closes[:i+1], 200) for i in range(len(closes)-20, len(closes)) if len(closes[:i+1]) >= 200]
        if len(ma200_vals) < 5:
            continue
        slope = (ma200_vals[-1] - ma200_vals[0]) / len(ma200_vals)
        if slope <= 0:
            continue

        s = spot[code]
        pe = s.get("pe", 0)
        mktcap = s["market_cap"]

        pe_score = max(0, min(40, (40 - pe) / 40 * 40))
        cap_score = min(20, mktcap / 500 * 20)
        trend_score = min(30, slope / ma200 * 5000)
        chg_score = max(0, 10 * (1 - abs(chg250) / 80))
        score = int(pe_score + cap_score + trend_score + chg_score)

        results.append({
            "code": code, "name": s["name"], "score": score,
            "reason": f"PE={pe:.0f}x合理，200日均线向上，市值{mktcap:.0f}亿，近1年涨{chg250:.1f}%未过热",
            "price": round(s["price"], 2),
            "change_pct": round(s["change"], 2),
            "market_cap": round(mktcap, 0),
            "key_metrics": {"pe": round(pe,1), "chg250d": round(chg250,1), "ma200_trend": "向上"}
        })
        if len(results) >= max_n * 3:
            break

    results.sort(key=lambda x: -x["score"])
    print(f"  长线命中 {len(results)} 只，取前{max_n}")
    return results[:max_n]

# ─── 主流程 ──────────────────────────────────────────────────────────
def main():
    print(f"=== A股选股脚本 v2 启动 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")

    codes = get_stock_list()
    if not codes:
        print("ERROR: 股票列表为空")
        json.dump({"generated_at": datetime.now().isoformat(), "error": "股票列表获取失败",
                   "short_term":[], "mid_term":[], "long_term":[]},
                  open(OUTPUT_PATH,"w"), ensure_ascii=False)
        return

    print("[2/4] 批量拉取实时行情...")
    spot = {}
    batch_size = 100
    for i in range(0, min(len(codes), 1000), batch_size):
        batch = codes[i:i+batch_size]
        spot.update(get_spot_batch(batch))
        time.sleep(0.3)
    print(f"  有效行情: {len(spot)} 只")

    if not spot:
        print("ERROR: 实时行情为空")
        json.dump({"generated_at": datetime.now().isoformat(), "error": "实时行情获取失败",
                   "short_term":[], "mid_term":[], "long_term":[]},
                  open(OUTPUT_PATH,"w"), ensure_ascii=False)
        return

    print("[3/4] 三档筛选...")
    short = screen_short(spot, codes)
    mid = screen_mid(spot, codes)
    long = screen_long(spot, codes)

    output = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "trade_date": datetime.now().strftime("%Y-%m-%d"),
        "short_term": short,
        "mid_term": mid,
        "long_term": long
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[4/4] 完成: 短线{len(short)}/中期{len(mid)}/长线{len(long)}")
    print("SUCCESS: generated recommendations.json")

if __name__ == "__main__":
    main()
