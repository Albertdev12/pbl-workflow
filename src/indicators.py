# -*- coding: utf-8 -*-
"""技术指标计算：均线、MACD、RSI、支撑/压力位、量能。输入日线DataFrame，输出最新指标值与信号。"""
import pandas as pd


def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def compute(df: pd.DataFrame) -> dict:
    """df 需含列: date/open/close/high/low/volume/amount（按日期升序）。返回最新一日的全部指标。"""
    c, v = df["close"], df["volume"]
    out = {}
    out["date"] = df["date"].iloc[-1]
    out["close"] = float(c.iloc[-1])
    for n in (5, 10, 20, 60):
        out[f"ma{n}"] = float(_sma(c, n).iloc[-1]) if len(df) >= n else None
    # 均线多头/空头排列
    mas = [out.get(f"ma{n}") for n in (5, 10, 20, 60)]
    if all(m is not None for m in mas):
        out["bull_align"] = mas[0] > mas[1] > mas[2] > mas[3]
        out["above_ma60"] = out["close"] > mas[3]
    else:
        out["bull_align"] = False
        out["above_ma60"] = False
    # MACD（12/26/9）
    if len(df) >= 35:
        dif = _ema(c, 12) - _ema(c, 26)
        dea = _ema(dif, 9)
        macd = (dif - dea) * 2
        out["dif"] = float(dif.iloc[-1]); out["dea"] = float(dea.iloc[-1])
        out["macd"] = float(macd.iloc[-1])
        prev_dif, prev_dea = float(dif.iloc[-2]), float(dea.iloc[-2])
        out["macd_golden"] = prev_dif <= prev_dea and out["dif"] > out["dea"]   # 金叉
        out["macd_dead"] = prev_dif >= prev_dea and out["dif"] < out["dea"]     # 死叉
        out["macd_hist_up"] = out["macd"] > float(macd.iloc[-2])                # 红柱放大
    # RSI（14，Wilder简化）
    if len(df) >= 15:
        diff = c.diff()
        up = diff.clip(lower=0).rolling(14).mean()
        dn = (-diff.clip(upper=0)).rolling(14).mean()
        rsi = 100 - 100 / (1 + up / dn.replace(0, pd.NA))
        out["rsi14"] = float(rsi.iloc[-1])
    # 支撑/压力：近60日高低点 + MA20/MA60
    w = df.tail(60)
    out["support_60d"] = float(w["low"].min())
    out["resist_60d"] = float(w["high"].max())
    # 量能：5日均量 / 60日均量
    if len(df) >= 60:
        out["vol_ratio_5v60"] = float(v.tail(5).mean() / v.tail(60).mean())
        out["volume_expand"] = out["vol_ratio_5v60"] > 1.3
    # 近20日涨跌幅
    if len(df) >= 21:
        out["chg_20d"] = out["close"] / float(c.iloc[-21]) - 1
    return out


def signal_and_judgment(ind: dict) -> tuple:
    """按任务书要求形成 指标→信号→判断 链条。返回 (signal, judgment, tech_score)。"""
    sig, judge = [], []
    score = 50.0
    if ind.get("bull_align"):
        sig.append("均线多头排列(5>10>20>60)")
        score += 12
    elif ind.get("above_ma60"):
        sig.append("价格站上60日均线")
        score += 6
    else:
        sig.append("价格位于60日均线下方，中期趋势偏弱")
        score -= 8
    if ind.get("macd_golden"):
        sig.append("MACD金叉")
        score += 10
    if ind.get("macd_dead"):
        sig.append("MACD死叉")
        score -= 10
    elif ind.get("macd_hist_up"):
        sig.append("MACD红柱放大")
        score += 4
    rsi = ind.get("rsi14")
    if rsi is not None:
        if rsi < 35:
            sig.append(f"RSI={rsi:.1f}超卖区")
            score += 5
        elif rsi > 75:
            sig.append(f"RSI={rsi:.1f}超买区，短期回调风险")
            score -= 6
        else:
            sig.append(f"RSI={rsi:.1f}中性区")
    if ind.get("volume_expand"):
        sig.append(f"5日均量为60日均量的{ind['vol_ratio_5v60']:.2f}倍，量能放大")
        score += 5
    chg = ind.get("chg_20d")
    if chg is not None:
        if chg > 0.12:
            sig.append(f"近20日已涨{chg*100:.1f}%，短期涨幅过大")
            score -= 5
        elif chg < -0.12:
            sig.append(f"近20日已跌{chg*100:.1f}%，接近超跌")
            score += 3
    judgment = "中期趋势向上，可关注/持有" if score >= 60 else ("趋势中性，观望" if score >= 45 else "趋势偏弱，回避或减仓")
    return "；".join(sig), judgment, min(max(score, 0), 100)


if __name__ == "__main__":
    from em_client import kline
    df = kline("1.600519", beg="20250601")
    ind = compute(df)
    print(signal_and_judgment(ind))
