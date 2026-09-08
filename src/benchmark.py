# -*- coding: utf-8 -*-
"""基准（沪深300）净值序列工具：把指数点位归一化成与组合同起点的净值曲线，
供净值对比图（13号报告 / 中期路演PPT / 手机仪表盘）统一使用。"""
import json
import os

from em_client import kline

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cfg():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def series(start_date=None, dates=None, secid=None):
    """返回 [{date, close, nav}]：nav = 当日收盘 / 起点收盘。
    dates 不为空时，对齐到这些日期（缺失日用最近可得值前向填充）。
    起点默认取组合首个净值记录日（与组合同起点比较），无记录时取课程开始日。"""
    cfg = _cfg()
    if start_date is None:
        try:
            import records
            nav = records.read_nav()
            start_date = nav[0]["date"] if nav else cfg["course"]["start_date"]
        except Exception:
            start_date = cfg["course"]["start_date"]
    secid = secid or cfg["strategy"]["market_benchmark"]
    try:
        df = kline(secid, beg="20250101", is_index=True)
    except Exception:
        return []
    df = df[df["date"] >= start_date]
    if not len(df):
        return []
    base = float(df["close"].iloc[0])
    if not base:
        return []
    out = [{"date": str(r["date"]), "close": round(float(r["close"]), 2),
            "nav": round(float(r["close"]) / base, 4)} for _, r in df.iterrows()]
    if not dates:
        return out
    by_date = {r["date"]: r for r in out}
    filled, last = [], None
    for d in dates:
        if d in by_date:
            last = by_date[d]
        if last:
            filled.append({"date": d, "close": last["close"], "nav": last["nav"]})
    return filled


def pct_since(start_date=None, secid=None):
    """区间涨幅（百分数字符串），用于报告里的"同期沪深300"。"""
    s = series(start_date, secid=secid)
    if len(s) < 2:
        return None
    return f"{(s[-1]['nav'] - 1) * 100:+.2f}%"


if __name__ == "__main__":
    s = series()
    print(len(s), s[0] if s else None, s[-1] if s else None)
