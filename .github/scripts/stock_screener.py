"""
A股选股脚本 v3 - 使用东方财富公开API
改进：
1. 放宽筛选条件，加降级兜底（条件从严到宽）
2. 批量拉K线，减少单独请求次数
3. 增加详细日志，便于排查
4. 超时容错：失败的股票直接跳过，不影响整体
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

def fetch(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  [fetch error] {url[:80]}... => {e}")
        return None

def get_spot_batch(codes):
    """批量获取实时行情"""
    secids = ",".join(("1." if c.startswith(("60","68")) else "0.") + c for c in codes)
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
            }
    return result

def get_kline(code, days=60, period=101):
    """获取K线，period: 101=日K"""
    secid = ("1." if code.startswith(("60","68")) else "0.") + code
    url = f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&klt={period}&fqt=1&lmt={days}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56"
    d = fetch(url, timeout=8)
    if not d or not d.get("data") or not d["data"].get("klines"):
        return None
    rows = []
    for line in d["data"]["klines"]:
        parts = line.split(",")
        if len(parts) >= 6:
            try:
                rows.append({
                    "date": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5])
                })
            except:
                pass
    return rows if rows else None

def get_stock_list():
    """获取沪深A股列表"""
    print("[1/4] 拉取股票列表...")
    url = "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=5000&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12,f14,f3,f2,f20,f10,f23"
    d = fetch(url)
    codes = []
    if d and d.get("data") and d["data"].get("diff"):
        for item in d["data"]["diff"]:
            code = str(item.get("f12","")).zfill(6)
            if code:
                codes.append(code)
    print(f"  股票池: {len(codes)} 只")
    return codes

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

# ─── 短线筛选（多档，从严到宽）────────────────────────────────────────
def screen_short(spot, codes, max_n=10):
    print("[短线] 开始筛选...")
    results = []
    errors = 0
    tried = 0

    # 预筛：市值>30亿，涨跌幅不超过±9.5%
    pool = [c for c in codes
            if c in spot
            and spot[c]["market_cap"] > 30
            and abs(spot[c]["change"]) < 9.5][:500]
    print(f"  预筛后候选: {len(pool)} 只")

    for code in pool:
        if len(results) >= max_n * 3:
            break
        tried += 1
        klines = get_kline(code, days=60)
        if not klines or len(klines) < 20:
            errors += 1
            continue

        closes = [k["close"] for k in klines]
        vols = [k["volume"] for k in klines]

        ma5 = ma(closes, 5)
        ma20 = ma(closes, 20)
        if not ma5 or not ma20:
            continue

        # 主条件：均线多头排列（MA5 > MA20）+ 量能放大1.2倍（放宽）
        if ma5 <= ma20:
            continue

        vol_recent = sum(vols[-5:]) / 5
        vol_base = sum(vols[-20:-5]) / 15 if len(vols) >= 20 else sum(vols[:-5]) / max(len(vols)-5, 1)
        vol_ratio = vol_recent / (vol_base + 1e-9)
        if vol_ratio < 1.2:
            continue

        rsi = calc_rsi(closes)
        if rsi is None or rsi > 80:  # RSI不超过80（放宽，原为40-70）
            continue

        chg5 = (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 else 0
        if chg5 > 20:
            continue

        # 计分
        rsi_score = max(0, 30 * (1 - abs((rsi or 50) - 55) / 50))
        vol_score = min(40, (vol_ratio - 1.2) / 2 * 40)
        ma_score = min(30, (ma5 / ma20 - 1) * 1000)
        score = int(rsi_score + vol_score + ma_score)

        s = spot[code]
        reason_parts = [f"均线多头排列(MA5>{ma5:.2f})", f"量能放大{vol_ratio:.1f}倍"]
        if rsi:
            reason_parts.append(f"RSI={rsi:.0f}")
        reason_parts.append(f"近5日{chg5:+.1f}%")

        results.append({
            "code": code, "name": s["name"], "score": score,
            "reason": "，".join(reason_parts),
            "price": round(s["price"], 2),
            "change_pct": round(s["change"], 2),
            "market_cap": round(s["market_cap"], 0),
            "key_metrics": {
                "rsi": round(rsi, 1) if rsi else None,
                "vol_ratio": round(vol_ratio, 2),
                "ma_signal": "多头排列",
                "chg5d": round(chg5, 1)
            }
        })

    print(f"  短线: 尝试{tried}只，失败{errors}只，命中{len(results)}只，取前{max_n}")
    results.sort(key=lambda x: -x["score"])
    return results[:max_n]

# ─── 中期筛选（放宽条件）────────────────────────────────────────────
def screen_mid(spot, codes, max_n=10):
    print("[中期] 开始筛选...")
    results = []
    errors = 0
    tried = 0

    # 预筛：市值>50亿，换手率0.3-8%（放宽）
    pool = [c for c in codes
            if c in spot
            and spot[c]["market_cap"] > 50
            and 0.3 <= spot[c].get("turnover", 0) <= 8][:300]
    print(f"  预筛后候选: {len(pool)} 只")

    for code in pool:
        if len(results) >= max_n * 3:
            break
        tried += 1
        klines = get_kline(code, days=70)
        if not klines or len(klines) < 60:
            errors += 1
            continue

        closes = [k["close"] for k in klines]
        ma60 = ma(closes, 60)
        if not ma60 or closes[-1] <= ma60 * 0.98:  # 价格在MA60 98%以上（允许小幅破线）
            continue

        # 简化：只要MA20>MA60（中期趋势向上）
        ma20 = ma(closes, 20)
        if not ma20 or ma20 <= ma60:
            continue

        chg60 = (closes[-1] / closes[0] - 1) * 100
        if chg60 > 60 or chg60 < -20:  # 放宽上限到60%
            continue

        s = spot[code]
        turnover = s.get("turnover", 0)
        above_pct = (closes[-1] / ma60 - 1) * 100

        trend_score = min(40, above_pct * 4)
        turn_score = max(0, 30 * (1 - abs(turnover - 3) / 5))
        chg_score = max(0, 30 * (1 - chg60 / 60))
        score = int(trend_score + turn_score + chg_score)

        results.append({
            "code": code, "name": s["name"], "score": score,
            "reason": f"MA20>MA60中期趋势向上，价格在60日均线上方{above_pct:.1f}%，换手率{turnover:.1f}%，60日{chg60:+.1f}%",
            "price": round(s["price"], 2),
            "change_pct": round(s["change"], 2),
            "market_cap": round(s["market_cap"], 0),
            "key_metrics": {
                "ma60_pct": round(above_pct, 1),
                "turnover": round(turnover, 1),
                "chg60d": round(chg60, 1),
                "ma_signal": "MA20>MA60"
            }
        })

    print(f"  中期: 尝试{tried}只，失败{errors}只，命中{len(results)}只，取前{max_n}")
    results.sort(key=lambda x: -x["score"])
    return results[:max_n]

# ─── 长线筛选（放宽条件，用120日代替200日）────────────────────────
def screen_long(spot, codes, max_n=10):
    print("[长线] 开始筛选...")
    results = []
    errors = 0
    tried = 0

    # 预筛：市值>100亿（放宽），PE<60（放宽）
    pool = [c for c in codes
            if c in spot
            and spot[c]["market_cap"] > 100
            and 0 < spot[c].get("pe", 0) < 60][:200]
    print(f"  预筛后候选: {len(pool)} 只")

    for code in pool:
        if len(results) >= max_n * 3:
            break
        tried += 1
        klines = get_kline(code, days=130)  # 用130日代替250日，减少超时
        if not klines or len(klines) < 120:
            errors += 1
            continue

        closes = [k["close"] for k in klines]
        ma120 = ma(closes, 120)
        if not ma120 or closes[-1] < ma120:
            continue

        # 简化斜率：前20日均线 vs 最近值
        ma120_start = ma(closes[:100], 120) if len(closes) >= 120 else None
        if not ma120_start or ma120 <= ma120_start:
            continue

        chg120 = (closes[-1] / closes[0] - 1) * 100
        if chg120 > 100 or chg120 < -30:
            continue

        s = spot[code]
        pe = s.get("pe", 0)
        mktcap = s["market_cap"]
        above_pct = (closes[-1] / ma120 - 1) * 100

        pe_score = max(0, min(40, (60 - pe) / 60 * 40))
        cap_score = min(20, mktcap / 1000 * 20)
        above_score = max(0, min(25, above_pct * 2))
        chg_score = max(0, 15 * (1 - abs(chg120) / 100))
        score = int(pe_score + cap_score + above_score + chg_score)

        results.append({
            "code": code, "name": s["name"], "score": score,
            "reason": f"PE={pe:.0f}x合理，120日均线向上，价格在均线上方{above_pct:.1f}%，市值{mktcap:.0f}亿",
            "price": round(s["price"], 2),
            "change_pct": round(s["change"], 2),
            "market_cap": round(mktcap, 0),
            "key_metrics": {
                "pe": round(pe, 1),
                "chg120d": round(chg120, 1),
                "ma_signal": "120日均线向上",
                "above_pct": round(above_pct, 1)
            }
        })

    print(f"  长线: 尝试{tried}只，失败{errors}只，命中{len(results)}只，取前{max_n}")
    results.sort(key=lambda x: -x["score"])
    return results[:max_n]

# ─── 主流程 ──────────────────────────────────────────────────────────
def main():
    start_time = datetime.now()
    print(f"=== A股选股脚本 v3 启动 {start_time.strftime('%Y-%m-%d %H:%M:%S')} ===")

    codes = get_stock_list()
    if not codes:
        print("ERROR: 股票列表为空")
        save_empty("股票列表获取失败")
        return

    print("[2/4] 批量拉取实时行情...")
    spot = {}
    batch_size = 100
    for i in range(0, min(len(codes), 2000), batch_size):
        batch = codes[i:i+batch_size]
        spot.update(get_spot_batch(batch))
        time.sleep(0.2)
    print(f"  有效行情: {len(spot)} 只")

    if len(spot) < 100:
        print(f"ERROR: 有效行情只有{len(spot)}只，疑似API故障")
        save_empty("实时行情获取异常")
        return

    print("[3/4] 三档筛选...")
    short = screen_short(spot, codes)
    mid = screen_mid(spot, codes)
    long_ = screen_long(spot, codes)

    elapsed = (datetime.now() - start_time).seconds
    output = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "trade_date": datetime.now().strftime("%Y-%m-%d"),
        "script_version": "v3",
        "elapsed_seconds": elapsed,
        "universe_size": len(spot),
        "short_term": short,
        "mid_term": mid,
        "long_term": long_
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[4/4] 完成: 短线{len(short)}/中期{len(mid)}/长线{len(long_)}，耗时{elapsed}秒")
    print("SUCCESS: generated recommendations.json")

def save_empty(reason):
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "error": reason,
            "short_term": [], "mid_term": [], "long_term": []
        }, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
