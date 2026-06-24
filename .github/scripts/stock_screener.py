"""
A股选股脚本 - 基于六层框架的三周期推荐
每个工作日15:35（A股收盘后）自动运行
"""
import json
import os
import time
import math
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd
import numpy as np

OUTPUT_PATH = "data/recommendations.json"
os.makedirs("data", exist_ok=True)

# ─── 工具函数 ───────────────────────────────────────────────────────────────

def safe_call(fn, default=None, retries=2):
    """安全调用akshare接口，失败返回default"""
    for i in range(retries):
        try:
            result = fn()
            if result is not None and (not isinstance(result, pd.DataFrame) or len(result) > 0):
                return result
        except Exception as e:
            print(f"  [warn] {fn.__name__ if hasattr(fn,'__name__') else 'fn'} 第{i+1}次失败: {e}")
            time.sleep(2)
    return default


def calc_rsi(close_series, period=14):
    """计算RSI"""
    delta = close_series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / (loss + 1e-9)
    return 100 - (100 / (1 + rs))


def get_stock_universe():
    """获取A股股票池（沪深主板+科创板，排除ST）"""
    print("[1/4] 拉取股票列表...")
    try:
        df_sh = ak.stock_info_sh_name_code(symbol="主板A股")
        df_sh = df_sh[["SECURITY_CODE_A", "SECURITY_ABBR_A"]].rename(
            columns={"SECURITY_CODE_A": "code", "SECURITY_ABBR_A": "name"}
        )
    except Exception as e:
        print(f"  沪市失败: {e}")
        df_sh = pd.DataFrame(columns=["code", "name"])

    try:
        df_sz = ak.stock_info_sz_name_code(symbol="A股列表")
        df_sz = df_sz[["A股代码", "A股简称"]].rename(
            columns={"A股代码": "code", "A股简称": "name"}
        )
    except Exception as e:
        print(f"  深市失败: {e}")
        df_sz = pd.DataFrame(columns=["code", "name"])

    df = pd.concat([df_sh, df_sz], ignore_index=True)
    df["code"] = df["code"].astype(str).str.zfill(6)
    # 排除ST、退市、科创板注册制新股
    df = df[~df["name"].str.contains("ST|退|N|C", na=False)]
    df = df[df["code"].str.match(r'^(60|00|30|68)')]
    df = df.dropna().drop_duplicates("code")
    print(f"  股票池: {len(df)} 只")
    return df


def get_realtime_data(codes):
    """批量获取实时行情（价格/涨跌幅/市值/换手率等）"""
    print("[2/4] 拉取实时行情...")
    try:
        df = ak.stock_zh_a_spot_em()
        df = df[df["代码"].isin(codes)]
        df = df.rename(columns={
            "代码": "code", "名称": "name", "最新价": "price",
            "涨跌幅": "change_pct", "成交量": "volume", "换手率": "turnover",
            "总市值": "market_cap", "市盈率-动态": "pe", "60日涨跌幅": "chg60"
        })
        keep = ["code", "name", "price", "change_pct", "volume", "turnover",
                "market_cap", "pe", "chg60"]
        keep = [c for c in keep if c in df.columns]
        df = df[keep].copy()
        df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce") / 1e8  # 亿元
        df["pe"] = pd.to_numeric(df["pe"], errors="coerce")
        df["change_pct"] = pd.to_numeric(df["change_pct"], errors="coerce")
        df["turnover"] = pd.to_numeric(df["turnover"], errors="coerce")
        df["chg60"] = pd.to_numeric(df["chg60"], errors="coerce")
        return df.set_index("code")
    except Exception as e:
        print(f"  实时行情失败: {e}")
        return pd.DataFrame()


def get_kline(code, days=90):
    """获取单只股票K线"""
    try:
        df = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq")
        df = df.tail(days).reset_index(drop=True)
        df.columns = [c.strip() for c in df.columns]
        # 统一列名
        col_map = {"日期": "date", "开盘": "open", "收盘": "close",
                   "最高": "high", "最低": "low", "成交量": "volume"}
        df = df.rename(columns=col_map)
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        return df.dropna(subset=["close"])
    except Exception:
        return None


# ─── 短线筛选 ────────────────────────────────────────────────────────────────

def screen_short_term(spot_df, candidates, max_count=10):
    """
    短线（3-10日）：技术面为主
    - MA5上穿MA20（金叉）
    - 近5日成交量放大1.5倍
    - RSI(14) 40-70
    - 近5日涨幅 < 15%
    - 市值 > 50亿
    """
    print("[短线] 开始筛选...")
    results = []
    screened = 0

    # 预筛：市值>50亿，近5日涨幅不超过15%
    pre = spot_df[
        (spot_df["market_cap"] > 50) &
        (spot_df["change_pct"].abs() < 15)
    ].copy()
    pool = [c for c in pre.index if c in candidates][:200]  # 限制200只防超时

    for code in pool:
        screened += 1
        df = get_kline(code, days=60)
        if df is None or len(df) < 30:
            continue

        close = df["close"]
        volume = df["volume"]

        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        rsi = calc_rsi(close)

        if ma5.isna().iloc[-1] or ma20.isna().iloc[-1] or rsi.isna().iloc[-1]:
            continue

        # 金叉：MA5当前在MA20之上，且5日前MA5在MA20之下
        golden_cross = (ma5.iloc[-1] > ma20.iloc[-1]) and (ma5.iloc[-5] <= ma20.iloc[-5])
        # 量能：近5日均量 vs 前20日均量
        vol_recent = volume.iloc[-5:].mean()
        vol_base = volume.iloc[-25:-5].mean()
        vol_ratio = vol_recent / (vol_base + 1e-9)
        # RSI
        rsi_val = rsi.iloc[-1]
        # 近5日涨幅
        chg5 = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) >= 6 else 99

        if not golden_cross:
            continue
        if vol_ratio < 1.5:
            continue
        if not (40 <= rsi_val <= 70):
            continue
        if chg5 > 15:
            continue

        # 评分：RSI越接近50越好(30分) + 量能(40分) + 金叉强度(30分)
        rsi_score = 30 * (1 - abs(rsi_val - 50) / 50)
        vol_score = min(40, (vol_ratio - 1.5) / 2 * 40)
        cross_strength = (ma5.iloc[-1] / ma20.iloc[-1] - 1) * 100
        cross_score = min(30, cross_strength * 10)
        score = int(rsi_score + vol_score + cross_score)

        row = spot_df.loc[code]
        results.append({
            "code": code,
            "name": str(row.get("name", code)),
            "score": score,
            "reason": f"MA5/MA20金叉，量能放大{vol_ratio:.1f}倍，RSI={rsi_val:.0f}处于健康区间，近5日涨{chg5:.1f}%",
            "price": float(row.get("price", 0)),
            "change_pct": float(row.get("change_pct", 0)),
            "market_cap": float(row.get("market_cap", 0)),
            "key_metrics": {
                "rsi": round(float(rsi_val), 1),
                "vol_ratio": round(float(vol_ratio), 2),
                "ma_signal": "金叉",
                "chg5d": round(float(chg5), 1)
            }
        })

        if screened % 20 == 0:
            print(f"  已扫描{screened}只，命中{len(results)}只")
        if len(results) >= max_count * 3:
            break

    results.sort(key=lambda x: -x["score"])
    print(f"  短线完成，命中{len(results)}只，取前{max_count}只")
    return results[:max_count]


# ─── 中期筛选 ────────────────────────────────────────────────────────────────

def screen_mid_term(spot_df, candidates, max_count=10):
    """
    中期（1-3月）：趋势+景气度
    - 60日均线向上，价格在60日均线之上
    - 换手率1%-5%
    - 近60日涨幅 < 40%
    - 市值 > 100亿
    """
    print("[中期] 开始筛选...")
    results = []

    pre = spot_df[
        (spot_df["market_cap"] > 100) &
        (spot_df["turnover"].between(1, 5)) &
        (spot_df["chg60"].between(-10, 40))
    ].copy()
    pool = [c for c in pre.index if c in candidates][:150]

    for code in pool:
        df = get_kline(code, days=90)
        if df is None or len(df) < 65:
            continue

        close = df["close"]
        ma60 = close.rolling(60).mean()

        if ma60.isna().iloc[-1]:
            continue

        # 价格在60日均线之上
        if close.iloc[-1] <= ma60.iloc[-1]:
            continue

        # 60日均线斜率向上（用线性回归近似）
        ma60_vals = ma60.iloc[-20:].values
        x = np.arange(len(ma60_vals))
        slope = np.polyfit(x, ma60_vals, 1)[0]
        if slope <= 0:
            continue

        row = spot_df.loc[code]
        turnover = float(row.get("turnover", 0))
        chg60 = float(row.get("chg60", 0))
        mktcap = float(row.get("market_cap", 0))

        # 评分
        trend_score = min(40, slope / ma60_vals[-1] * 100 * 400)
        turnover_score = 30 * (1 - abs(turnover - 3) / 3)  # 换手率3%最优
        chg_score = 30 * (1 - chg60 / 40)  # 涨幅越小空间越大

        score = int(max(0, trend_score) + max(0, turnover_score) + max(0, chg_score))

        results.append({
            "code": code,
            "name": str(row.get("name", code)),
            "score": score,
            "reason": f"60日均线向上，价格在均线上方{((close.iloc[-1]/ma60.iloc[-1]-1)*100):.1f}%，换手率{turnover:.1f}%健康，60日涨{chg60:.1f}%未过度透支",
            "price": float(row.get("price", 0)),
            "change_pct": float(row.get("change_pct", 0)),
            "market_cap": mktcap,
            "key_metrics": {
                "ma60_pct": round((close.iloc[-1] / ma60.iloc[-1] - 1) * 100, 1),
                "ma60_slope": round(float(slope), 3),
                "turnover": round(turnover, 1),
                "chg60d": round(chg60, 1)
            }
        })

        if len(results) >= max_count * 3:
            break

    results.sort(key=lambda x: -x["score"])
    print(f"  中期完成，命中{len(results)}只，取前{max_count}只")
    return results[:max_count]


# ─── 长线筛选 ────────────────────────────────────────────────────────────────

def screen_long_term(spot_df, candidates, max_count=10):
    """
    长线（6月+）：基本面为主
    - ROE > 15%
    - 毛利率 > 30%
    - PE < 50 且 > 0
    - 市值 > 200亿
    """
    print("[长线] 开始筛选...")
    results = []

    pre = spot_df[
        (spot_df["market_cap"] > 200) &
        (spot_df["pe"].between(0, 50))
    ].copy()
    pool = [c for c in pre.index if c in candidates][:120]

    for code in pool:
        try:
            # 获取财务指标
            df_fin = ak.stock_financial_abstract_ths(symbol=code, indicator="按年度")
            if df_fin is None or len(df_fin) < 1:
                continue

            df_fin = df_fin.head(3)  # 近3年

            # ROE
            roe_col = [c for c in df_fin.columns if "净资产收益率" in c or "ROE" in c]
            if not roe_col:
                continue
            roe = pd.to_numeric(df_fin[roe_col[0]].iloc[0], errors="coerce")
            if pd.isna(roe) or roe < 15:
                continue

            # 毛利率
            gp_col = [c for c in df_fin.columns if "毛利率" in c]
            if not gp_col:
                continue
            gp = pd.to_numeric(df_fin[gp_col[0]].iloc[0], errors="coerce")
            if pd.isna(gp) or gp < 30:
                continue

            # 营收增速（近3年均值）
            rev_col = [c for c in df_fin.columns if "营业收入" in c and "增长" in c]
            rev_growth = 10.0  # 默认值
            if rev_col:
                rev_vals = pd.to_numeric(df_fin[rev_col[0]], errors="coerce").dropna()
                if len(rev_vals) > 0:
                    rev_growth = float(rev_vals.mean())

            row = spot_df.loc[code]
            pe = float(row.get("pe", 50))
            mktcap = float(row.get("market_cap", 0))

            # 评分
            roe_score = min(30, (roe - 15) / 15 * 30)
            gp_score = min(20, (gp - 30) / 30 * 20)
            rev_score = min(20, rev_growth / 20 * 20)
            pe_score = min(30, (50 - pe) / 50 * 30)
            score = int(max(0, roe_score) + max(0, gp_score) + max(0, rev_score) + max(0, pe_score))

            results.append({
                "code": code,
                "name": str(row.get("name", code)),
                "score": score,
                "reason": f"ROE={roe:.1f}%，毛利率={gp:.1f}%，PE={pe:.0f}x，营收增速{rev_growth:.1f}%，大盘龙头市值{mktcap:.0f}亿",
                "price": float(row.get("price", 0)),
                "change_pct": float(row.get("change_pct", 0)),
                "market_cap": mktcap,
                "key_metrics": {
                    "roe": round(float(roe), 1),
                    "gross_margin": round(float(gp), 1),
                    "pe": round(pe, 1),
                    "rev_growth": round(rev_growth, 1)
                }
            })

            time.sleep(0.5)  # 防止请求过快
        except Exception as e:
            continue

        if len(results) >= max_count * 3:
            break

    results.sort(key=lambda x: -x["score"])
    print(f"  长线完成，命中{len(results)}只，取前{max_count}只")
    return results[:max_count]


# ─── 主流程 ──────────────────────────────────────────────────────────────────

def main():
    print(f"=== A股选股脚本启动 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")

    # 1. 获取股票池
    universe_df = get_stock_universe()
    all_codes = set(universe_df["code"].tolist())

    # 2. 实时行情
    spot_df = get_realtime_data(all_codes)
    if spot_df.empty:
        print("ERROR: 无法获取实时行情，退出")
        # 写空JSON避免前端报错
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump({"generated_at": datetime.now().isoformat(),
                       "error": "行情数据获取失败",
                       "short_term": [], "mid_term": [], "long_term": []}, f, ensure_ascii=False, indent=2)
        return

    candidates = list(spot_df.index)
    print(f"  有效候选: {len(candidates)} 只")

    # 3. 三档筛选
    print("[3/4] 开始三档筛选...")
    short_term = screen_short_term(spot_df, candidates)
    mid_term = screen_mid_term(spot_df, candidates)
    long_term = screen_long_term(spot_df, candidates)

    # 4. 输出JSON
    output = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "trade_date": datetime.now().strftime("%Y-%m-%d"),
        "short_term": short_term,
        "mid_term": mid_term,
        "long_term": long_term
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[4/4] 输出完成")
    print(f"  短线: {len(short_term)} 只")
    print(f"  中期: {len(mid_term)} 只")
    print(f"  长线: {len(long_term)} 只")
    print(f"SUCCESS: generated recommendations.json")


if __name__ == "__main__":
    main()
