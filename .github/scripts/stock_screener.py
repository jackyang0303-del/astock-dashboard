#!/usr/bin/env python3
"""
A股智能选股脚本 - 生成短/中/长线推荐池
输出: data/recommendations.json
"""

import json
import os
import sys
import warnings
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ─── 工具函数 ───

def safe_get(func, *args, **kwargs):
    """安全调用 akshare 接口，失败返回 None"""
    try:
        return func(*args, **kwargs)
    except Exception as e:
        print(f"  ⚠️ 接口失败: {e}", file=sys.stderr)
        return None


def calc_rsi(series, period=14):
    """计算 RSI"""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_ma(series, period):
    """计算均线"""
    return series.rolling(window=period).mean()


# ─── 数据获取 ───

def get_all_stocks():
    """获取全部A股实时行情"""
    print("⏳ 获取A股实时行情...")
    import akshare as ak
    df = safe_get(ak.stock_zh_a_spot_em)
    if df is None or df.empty:
        print("❌ 无法获取A股行情数据")
        return None
    # 统一列名
    col_map = {}
    for col in df.columns:
        if "代码" in col: col_map[col] = "code"
        elif "名称" in col: col_map[col] = "name"
        elif "最新价" in col: col_map[col] = "price"
        elif "涨跌幅" in col: col_map[col] = "change_pct"
        elif "总市值" in col: col_map[col] = "total_mv"
        elif "换手率" in col: col_map[col] = "turnover"
        elif "成交量" in col: col_map[col] = "volume"
        elif "成交额" in col: col_map[col] = "amount"
    df = df.rename(columns=col_map)
    # 数值转换
    for c in ["price", "change_pct", "total_mv", "turnover", "volume", "amount"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # 过滤无效数据
    df = df.dropna(subset=["code", "price"])
    # 过滤 ST、退市、北交所
    df = df[~df["name"].str.contains("ST|退市", na=False)]
    if "code" in df.columns:
        df = df[df["code"].str.match(r"^(00|30|60|68)", na=False)]
    print(f"  获取到 {len(df)} 只股票")
    return df


def get_stock_history(code, days=120):
    """获取个股历史K线"""
    import akshare as ak
    df = safe_get(ak.stock_zh_a_hist, symbol=code, period="daily", adjust="qfq")
    if df is None or df.empty:
        return None
    col_map = {}
    for col in df.columns:
        if "日期" in col: col_map[col] = "date"
        elif "开盘" in col: col_map[col] = "open"
        elif "收盘" in col: col_map[col] = "close"
        elif "最高" in col: col_map[col] = "high"
        elif "最低" in col: col_map[col] = "low"
        elif "成交量" in col: col_map[col] = "volume"
        elif "成交额" in col: col_map[col] = "amount"
        elif "换手率" in col: col_map[col] = "turnover"
        elif "涨跌幅" in col: col_map[col] = "change_pct"
    df = df.rename(columns=col_map)
    for c in ["open", "close", "high", "low", "volume", "amount", "turnover", "change_pct"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df.sort_values("date").tail(days).reset_index(drop=True)
    return df


def get_sector_fund_flow():
    """获取行业板块资金流"""
    import akshare as ak
    df = safe_get(ak.stock_sector_fund_flow_rank, indicator="今日", sector_type="行业资金流")
    if df is None or df.empty:
        return {}
    col_map = {}
    for col in df.columns:
        if "名称" in col or "板块" in col: col_map[col] = "name"
        elif "主力净流入" in col or "主力净额" in col or "净流入" in col: col_map[col] = "net_flow"
    df = df.rename(columns=col_map)
    if "net_flow" in df.columns:
        df["net_flow"] = pd.to_numeric(df["net_flow"], errors="coerce")
    result = {}
    if "name" in df.columns and "net_flow" in df.columns:
        for _, row in df.iterrows():
            result[row["name"]] = row["net_flow"]
    return result


def get_stock_financials(code):
    """获取个股财务指标"""
    import akshare as ak
    result = {"roe": None, "gross_margin": None, "revenue_growth": None, "pe": None}
    try:
        # 盈利能力
        profit_df = ak.stock_financial_abstract_ths(symbol=code, indicator="盈利能力")
        if profit_df is not None and not profit_df.empty:
            col_map = {}
            for col in profit_df.columns:
                if "净资产收益率" in col or "ROE" in col: col_map[col] = "roe"
                elif "毛利率" in col: col_map[col] = "gross_margin"
            profit_df = profit_df.rename(columns=col_map)
            if "roe" in profit_df.columns:
                profit_df["roe"] = pd.to_numeric(profit_df["roe"], errors="coerce")
                valid = profit_df["roe"].dropna()
                if len(valid) > 0:
                    result["roe"] = valid.iloc[0]
            if "gross_margin" in profit_df.columns:
                profit_df["gross_margin"] = pd.to_numeric(profit_df["gross_margin"], errors="coerce")
                valid = profit_df["gross_margin"].dropna()
                if len(valid) > 0:
                    result["gross_margin"] = valid.iloc[0]
    except Exception:
        pass

    try:
        # 成长能力
        growth_df = ak.stock_financial_abstract_ths(symbol=code, indicator="成长能力")
        if growth_df is not None and not growth_df.empty:
            col_map = {}
            for col in growth_df.columns:
                if "营业收入增长率" in col or "营收增长" in col: col_map[col] = "revenue_growth"
            growth_df = growth_df.rename(columns=col_map)
            if "revenue_growth" in growth_df.columns:
                growth_df["revenue_growth"] = pd.to_numeric(growth_df["revenue_growth"], errors="coerce")
                valid = growth_df["revenue_growth"].dropna()
                if len(valid) > 0:
                    result["revenue_growth"] = valid.iloc[0]
    except Exception:
        pass

    try:
        # 估值 - 用实时行情里的PE
        spot_df = ak.stock_zh_a_spot_em()
        if not spot_df.empty:
            col_map = {}
            for col in spot_df.columns:
                if "代码" in col: col_map[col] = "code"
                elif "市盈率" in col: col_map[col] = "pe"
            spot_df = spot_df.rename(columns=col_map)
            if "code" in spot_df.columns and "pe" in spot_df.columns:
                spot_df["pe"] = pd.to_numeric(spot_df["pe"], errors="coerce")
                match = spot_df[spot_df["code"] == code]
                if not match.empty:
                    pe_val = match.iloc[0]["pe"]
                    if pd.notna(pe_val):
                        result["pe"] = pe_val
    except Exception:
        pass

    return result


# ─── 筛选逻辑 ───

def screen_short_term(all_stocks, top_n=10):
    """短线推荐: 技术面金叉 + 放量 + RSI健康"""
    print("🔴 筛选短线推荐(3-10日)...")
    results = []
    candidates = all_stocks[
        (all_stocks["price"] > 0) &
        (all_stocks.get("total_mv", pd.Series([np.inf]*len(all_stocks))) > 50e8)
    ].head(100)  # 限制候选池加速

    for idx, row in candidates.iterrows():
        code = row["code"]
        hist = get_stock_history(code, days=60)
        if hist is None or len(hist) < 25:
            continue

        close = hist["close"]
        volume = hist["volume"] if "volume" in hist.columns else pd.Series([np.nan]*len(hist))

        # 金叉: 5日线上穿20日线
        ma5 = calc_ma(close, 5)
        ma20 = calc_ma(close, 20)
        if len(ma5) < 2 or pd.isna(ma5.iloc[-1]) or pd.isna(ma20.iloc[-1]):
            continue
        golden_cross = ma5.iloc[-1] > ma20.iloc[-1] and ma5.iloc[-2] <= ma20.iloc[-2]

        # 成交量放大
        if "volume" in hist.columns and hist["volume"].dropna().iloc[-5:].sum() > 0:
            vol_5d = volume.iloc[-5:].mean()
            vol_20d = volume.iloc[-20:].mean() if len(volume) >= 20 else volume.mean()
            vol_ratio = vol_5d / vol_20d if vol_20d > 0 else 0
        else:
            vol_ratio = 0

        # RSI
        rsi = calc_rsi(close)
        rsi_val = rsi.iloc[-1] if len(rsi) > 0 and not pd.isna(rsi.iloc[-1]) else 50

        # 近5日涨幅
        if len(close) >= 5:
            change_5d = (close.iloc[-1] / close.iloc[-5] - 1) * 100
        else:
            change_5d = 0

        # 筛选条件
        if not golden_cross:
            continue
        if vol_ratio < 1.5:
            continue
        if rsi_val < 40 or rsi_val > 70:
            continue
        if change_5d > 15:
            continue

        # 评分
        rsi_score = max(0, 30 - abs(rsi_val - 50))
        vol_score = min(30, vol_ratio * 15)
        cross_strength = ((ma5.iloc[-1] - ma20.iloc[-1]) / ma20.iloc[-1]) * 1000
        ma_score = min(30, cross_strength * 10) if cross_strength > 0 else 0
        score = int(rsi_score + vol_score + ma_score)
        score = min(100, max(0, score))

        mv = row.get("total_mv", 0)
        mv_yi = mv / 1e8 if mv > 0 else 0

        reason_parts = []
        if golden_cross:
            reason_parts.append("5/20日均线金叉")
        if vol_ratio > 0:
            reason_parts.append(f"成交量放大{vol_ratio:.1f}倍")
        reason_parts.append(f"RSI={int(rsi_val)}")
        if change_5d > 0:
            reason_parts.append(f"近5日涨幅{change_5d:.1f}%")

        results.append({
            "code": code,
            "name": str(row.get("name", "")),
            "score": score,
            "reason": "，".join(reason_parts),
            "price": round(float(row["price"]), 2),
            "change_pct": round(float(row.get("change_pct", 0)), 2),
            "market_cap": round(mv_yi, 0),
            "key_metrics": {
                "rsi": int(rsi_val),
                "vol_ratio": round(vol_ratio, 1),
                "ma_signal": "金叉"
            }
        })

        if len(results) >= top_n:
            break

    # 如果结果不够，降低门槛
    if len(results) < 3:
        print("  ⚠️ 短线推荐不足3只，降低门槛重新筛选...")
        results = screen_short_term_relaxed(all_stocks, top_n)

    print(f"  ✅ 短线推荐: {len(results)} 只")
    return results


def screen_short_term_relaxed(all_stocks, top_n=10):
    """短线推荐 - 宽松版"""
    results = []
    candidates = all_stocks[(all_stocks["price"] > 0)].head(150)

    for idx, row in candidates.iterrows():
        code = row["code"]
        hist = get_stock_history(code, days=60)
        if hist is None or len(hist) < 25:
            continue

        close = hist["close"]
        volume = hist["volume"] if "volume" in hist.columns else pd.Series([np.nan]*len(hist))

        ma5 = calc_ma(close, 5)
        ma20 = calc_ma(close, 20)
        if len(ma5) < 2 or pd.isna(ma5.iloc[-1]) or pd.isna(ma20.iloc[-1]):
            continue

        golden_cross = ma5.iloc[-1] > ma20.iloc[-1] and ma5.iloc[-2] <= ma20.iloc[-2]

        if "volume" in hist.columns:
            vol_5d = volume.iloc[-5:].mean()
            vol_20d = volume.iloc[-20:].mean() if len(volume) >= 20 else volume.mean()
            vol_ratio = vol_5d / vol_20d if vol_20d > 0 else 0
        else:
            vol_ratio = 1.0

        rsi = calc_rsi(close)
        rsi_val = rsi.iloc[-1] if len(rsi) > 0 and not pd.isna(rsi.iloc[-1]) else 50

        if len(close) >= 5:
            change_5d = (close.iloc[-1] / close.iloc[-5] - 1) * 100
        else:
            change_5d = 0

        # 宽松条件: 金叉 OR 放量
        if not golden_cross and vol_ratio < 1.2:
            continue
        if rsi_val < 30 or rsi_val > 80:
            continue
        if change_5d > 20:
            continue

        rsi_score = max(0, 30 - abs(rsi_val - 50))
        vol_score = min(30, vol_ratio * 15)
        cross_strength = ((ma5.iloc[-1] - ma20.iloc[-1]) / ma20.iloc[-1]) * 1000
        ma_score = min(30, max(0, cross_strength * 10))
        score = int(rsi_score + vol_score + ma_score)
        score = min(100, max(0, score))

        mv = row.get("total_mv", 0)
        mv_yi = mv / 1e8 if mv > 0 else 0

        results.append({
            "code": code,
            "name": str(row.get("name", "")),
            "score": score,
            "reason": f"5/20日均线{'金叉' if golden_cross else '多头排列'}，RSI={int(rsi_val)}",
            "price": round(float(row["price"]), 2),
            "change_pct": round(float(row.get("change_pct", 0)), 2),
            "market_cap": round(mv_yi, 0),
            "key_metrics": {
                "rsi": int(rsi_val),
                "vol_ratio": round(vol_ratio, 1),
                "ma_signal": "金叉" if golden_cross else "多头"
            }
        })
        if len(results) >= top_n:
            break

    return results


def screen_mid_term(all_stocks, top_n=10):
    """中期推荐: 趋势向上 + 板块资金流 + 换手率健康"""
    print("🟡 筛选中期推荐(1-3月)...")
    results = []

    # 获取板块资金流
    sector_flow = get_sector_fund_flow()

    candidates = all_stocks[
        (all_stocks["price"] > 0) &
        (all_stocks.get("total_mv", pd.Series([np.inf]*len(all_stocks))) > 100e8)
    ].head(100)

    for idx, row in candidates.iterrows():
        code = row["code"]
        hist = get_stock_history(code, days=120)
        if hist is None or len(hist) < 65:
            continue

        close = hist["close"]
        ma60 = calc_ma(close, 60)

        if pd.isna(ma60.iloc[-1]) or close.iloc[-1] <= ma60.iloc[-1]:
            continue

        # 60日均线斜率(向上)
        if len(ma60) >= 10:
            recent_ma60 = ma60.iloc[-10:]
            slope = (recent_ma60.iloc[-1] - recent_ma60.iloc[0]) / recent_ma60.iloc[0] * 100
        else:
            slope = 0

        if slope <= 0:
            continue

        # 换手率
        turnover = row.get("turnover", 0)
        if turnover and (turnover < 1 or turnover > 5):
            continue

        # 近60日涨幅
        if len(close) >= 60:
            change_60d = (close.iloc[-1] / close.iloc[-60] - 1) * 100
        else:
            change_60d = 0
        if change_60d > 40:
            continue

        # 评分
        slope_score = min(40, slope * 5)
        # 换手率健康度(2%-3%最优)
        turnover_score = 30 - abs(turnover - 2.5) * 10 if turnover else 15
        turnover_score = max(0, min(30, turnover_score))

        # 板块资金流评分(简化)
        sector_score = 15  # 默认中位

        score = int(slope_score + turnover_score + sector_score)
        score = min(100, max(0, score))

        mv = row.get("total_mv", 0)
        mv_yi = mv / 1e8 if mv > 0 else 0

        reason_parts = []
        reason_parts.append("60日均线向上")
        if turnover:
            reason_parts.append(f"换手率{turnover:.1f}%")
        reason_parts.append(f"近60日涨幅{change_60d:.1f}%")
        reason_parts.append(f"均线斜率{slope:.2f}%")

        results.append({
            "code": code,
            "name": str(row.get("name", "")),
            "score": score,
            "reason": "，".join(reason_parts),
            "price": round(float(row["price"]), 2),
            "change_pct": round(float(row.get("change_pct", 0)), 2),
            "market_cap": round(mv_yi, 0),
            "key_metrics": {
                "ma60_slope": round(slope, 2),
                "turnover": round(turnover, 1) if turnover else None,
                "change_60d": round(change_60d, 1)
            }
        })

        if len(results) >= top_n:
            break

    if len(results) < 3:
        print("  ⚠️ 中期推荐不足3只，降低门槛重新筛选...")
        results = screen_mid_term_relaxed(all_stocks, top_n)

    print(f"  ✅ 中期推荐: {len(results)} 只")
    return results


def screen_mid_term_relaxed(all_stocks, top_n=10):
    """中期推荐 - 宽松版"""
    results = []
    candidates = all_stocks[all_stocks["price"] > 0].head(150)

    for idx, row in candidates.iterrows():
        code = row["code"]
        hist = get_stock_history(code, days=120)
        if hist is None or len(hist) < 65:
            continue

        close = hist["close"]
        ma60 = calc_ma(close, 60)

        if pd.isna(ma60.iloc[-1]) or close.iloc[-1] <= ma60.iloc[-1]:
            continue

        if len(ma60) >= 10:
            recent_ma60 = ma60.iloc[-10:]
            slope = (recent_ma60.iloc[-1] - recent_ma60.iloc[0]) / recent_ma60.iloc[0] * 100
        else:
            slope = 0

        turnover = row.get("turnover", 0)

        if len(close) >= 60:
            change_60d = (close.iloc[-1] / close.iloc[-60] - 1) * 100
        else:
            change_60d = 0

        # 宽松: 价格在60日线上即可
        slope_score = min(40, max(0, slope * 5))
        turnover_score = 20
        score = int(slope_score + turnover_score + 15)
        score = min(100, max(0, score))

        mv = row.get("total_mv", 0)
        mv_yi = mv / 1e8 if mv > 0 else 0

        results.append({
            "code": code,
            "name": str(row.get("name", "")),
            "score": score,
            "reason": f"60日均线向上(斜率{slope:.2f}%)，价格站上均线",
            "price": round(float(row["price"]), 2),
            "change_pct": round(float(row.get("change_pct", 0)), 2),
            "market_cap": round(mv_yi, 0),
            "key_metrics": {
                "ma60_slope": round(slope, 2),
                "turnover": round(turnover, 1) if turnover else None,
                "change_60d": round(change_60d, 1)
            }
        })
        if len(results) >= top_n:
            break

    return results


def screen_long_term(all_stocks, top_n=10):
    """长线推荐: ROE + 毛利率 + 营收增速 + PE"""
    print("🟢 筛选长线推荐(6月+)...")
    results = []

    candidates = all_stocks[
        (all_stocks["price"] > 0) &
        (all_stocks.get("total_mv", pd.Series([np.inf]*len(all_stocks))) > 200e8)
    ].head(50)  # 财务数据获取慢，限制候选池

    for idx, row in candidates.iterrows():
        code = row["code"]
        fin = get_stock_financials(code)

        roe = fin.get("roe")
        gm = fin.get("gross_margin")
        rg = fin.get("revenue_growth")
        pe = fin.get("pe")

        # 筛选条件 (严格)
        if roe is None or roe < 15:
            continue
        if gm is None or gm < 30:
            continue
        if pe is None or pe <= 0 or pe > 50:
            continue

        # 评分
        roe_score = min(30, (roe / 30) * 30)  # ROE=30%满分
        gm_score = min(20, (gm / 60) * 20)    # 毛利率=60%满分
        rg_score = min(20, (rg / 30) * 20) if rg and rg > 0 else 10  # 增速=30%满分
        pe_score = max(0, 30 * (1 - pe / 50))  # PE越低分越高

        score = int(roe_score * 0.3 / 0.3 + gm_score * 0.2 / 0.2 + rg_score * 0.2 / 0.2 + pe_score * 0.3 / 0.3)
        # 简化: 直接加权
        score = int(roe_score + gm_score + rg_score + pe_score)
        score = min(100, max(0, score))

        mv = row.get("total_mv", 0)
        mv_yi = mv / 1e8 if mv > 0 else 0

        reason_parts = []
        reason_parts.append(f"ROE={roe:.1f}%")
        reason_parts.append(f"毛利率={gm:.1f}%")
        if rg:
            reason_parts.append(f"营收增速={rg:.1f}%")
        reason_parts.append(f"PE={pe:.1f}")

        results.append({
            "code": code,
            "name": str(row.get("name", "")),
            "score": score,
            "reason": "，".join(reason_parts),
            "price": round(float(row["price"]), 2),
            "change_pct": round(float(row.get("change_pct", 0)), 2),
            "market_cap": round(mv_yi, 0),
            "key_metrics": {
                "roe": round(roe, 1),
                "gross_margin": round(gm, 1),
                "revenue_growth": round(rg, 1) if rg else None,
                "pe": round(pe, 1)
            }
        })

        if len(results) >= top_n:
            break

    # 严格条件可能筛选不到，放宽
    if len(results) < 3:
        print("  ⚠️ 长线推荐不足3只，降低门槛重新筛选...")
        results = screen_long_term_relaxed(all_stocks, top_n)

    print(f"  ✅ 长线推荐: {len(results)} 只")
    return results


def screen_long_term_relaxed(all_stocks, top_n=10):
    """长线推荐 - 宽松版"""
    results = []
    candidates = all_stocks[all_stocks["price"] > 0].head(80)

    for idx, row in candidates.iterrows():
        code = row["code"]
        fin = get_stock_financials(code)

        roe = fin.get("roe")
        gm = fin.get("gross_margin")
        rg = fin.get("revenue_growth")
        pe = fin.get("pe")

        # 宽松条件
        if roe is None or roe < 10:
            continue
        if pe is None or pe <= 0 or pe > 80:
            continue

        roe_score = min(30, (roe / 25) * 30)
        gm_score = min(20, (gm / 50) * 20) if gm else 10
        rg_score = 10 if rg and rg > 0 else 5
        pe_score = max(0, 30 * (1 - pe / 80))

        score = int(roe_score + gm_score + rg_score + pe_score)
        score = min(100, max(0, score))

        mv = row.get("total_mv", 0)
        mv_yi = mv / 1e8 if mv > 0 else 0

        results.append({
            "code": code,
            "name": str(row.get("name", "")),
            "score": score,
            "reason": f"ROE={roe:.1f}%，PE={pe:.1f}" + (f"，毛利率={gm:.1f}%" if gm else ""),
            "price": round(float(row["price"]), 2),
            "change_pct": round(float(row.get("change_pct", 0)), 2),
            "market_cap": round(mv_yi, 0),
            "key_metrics": {
                "roe": round(roe, 1),
                "gross_margin": round(gm, 1) if gm else None,
                "revenue_growth": round(rg, 1) if rg else None,
                "pe": round(pe, 1)
            }
        })
        if len(results) >= top_n:
            break

    return results


# ─── 主流程 ───

def main():
    print("=" * 50)
    print("A股智能选股系统")
    print("=" * 50)

    # 获取全部行情
    all_stocks = get_all_stocks()
    if all_stocks is None:
        print("❌ 获取行情失败，退出")
        sys.exit(1)

    # 执行三轮筛选
    short_term = screen_short_term(all_stocks, top_n=10)
    mid_term = screen_mid_term(all_stocks, top_n=10)
    long_term = screen_long_term(all_stocks, top_n=10)

    # 按评分排序
    short_term.sort(key=lambda x: x["score"], reverse=True)
    mid_term.sort(key=lambda x: x["score"], reverse=True)
    long_term.sort(key=lambda x: x["score"], reverse=True)

    # 输出JSON
    output = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "short_term": short_term,
        "mid_term": mid_term,
        "long_term": long_term
    }

    os.makedirs("data", exist_ok=True)
    output_path = "data/recommendations.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n📄 推荐结果已保存到 {output_path}")
    print(f"   短线: {len(short_term)} 只")
    print(f"   中期: {len(mid_term)} 只")
    print(f"   长线: {len(long_term)} 只")
    print("SUCCESS: generated recommendations.json")


if __name__ == "__main__":
    main()
