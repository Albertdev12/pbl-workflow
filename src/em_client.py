# -*- coding: utf-8 -*-
"""东方财富公开行情/财务接口封装，带本地CSV缓存，供全流程数据采集使用。"""
import json
import os
import time

import pandas as pd
import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
KLINE_DIR = os.path.join(DATA_DIR, "kline")
FIN_DIR = os.path.join(DATA_DIR, "fin")
for _d in (KLINE_DIR, FIN_DIR):
    os.makedirs(_d, exist_ok=True)


def _get(url, params, timeout=12, retries=3):
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            j = r.json()
            # 数据完整性：记录每次采集的时间戳（数据来源审计）
            with open(os.path.join(DATA_DIR, "fetch_log.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "url": url.split("/")[-1], "params_secid": params.get("secid", "")},
                                   ensure_ascii=False) + "\n")
            return j
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (i + 1))
    raise last_err


# ---------------------------------------------------------------- 日K线
def kline(secid, beg="20250101", end="20500101", klt="101", fqt="1"):
    """日K线 -> DataFrame[date, open, close, high, low, volume, amount]。网络失败时回退本地缓存。"""
    cache = os.path.join(KLINE_DIR, f"{secid.replace('.', '_')}.csv")
    url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = dict(secid=secid, fields1="f1,f2,f3,f4,f5,f6",
                  fields2="f51,f52,f53,f54,f55,f56,f57", klt=klt, fqt=fqt, beg=beg, end=end)
    try:
        j = _get(url, params)
        d = j.get("data") or {}
        rows = [x.split(",") for x in d.get("klines", [])]
        df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume", "amount"])
    except Exception:
        if os.path.exists(cache):  # DATA_STALE回退：使用本地缓存
            df = pd.read_csv(cache, dtype={"date": str})
            return df.sort_values("date").reset_index(drop=True)
        raise
    for c in df.columns:
        if c != "date":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # 增量合并缓存
    if os.path.exists(cache):
        old = pd.read_csv(cache, dtype={"date": str})
        df = pd.concat([old[~old["date"].isin(set(df["date"]))], df], ignore_index=True)
    df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    df.to_csv(cache, index=False)
    return df


def kline_until(secid, trade_date, lookback=260):
    """取 trade_date 当日（含）前 lookback 根日K，保证回测/决策不引入未来数据。"""
    df = kline(secid)
    df = df[df["date"] <= trade_date]
    return df.tail(lookback).reset_index(drop=True)


# ---------------------------------------------------------------- 实时快照
def snapshot(secid):
    """个股/ETF实时快照：名称、现价、涨跌幅、PE、PB、总市值、换手率、量比等。"""
    url = "http://push2.eastmoney.com/api/qt/stock/get"
    params = dict(secid=secid, invt="2", fltt="2",
                  fields="f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f62,f84,f116,f117,f162,f164,f167,f168,f169,f170,f171,f292")
    j = _get(url, params)
    d = j.get("data") or {}
    out = {
        "code": d.get("f57"), "name": d.get("f58"),
        "price": d.get("f43"), "pct_chg": d.get("f170"),
        "pe_ttm": d.get("f164"), "pb": d.get("f167"),
        "total_mv": d.get("f116"), "float_mv": d.get("f117"),
        "turnover_pct": d.get("f168"), "volume_ratio": d.get("f50"),
        "high": d.get("f44"), "low": d.get("f45"),
        "open": d.get("f46"), "prev_close": d.get("f60"),
    }
    return out


# ---------------------------------------------------------------- 行业板块
BOARD_CACHE = os.path.join(DATA_DIR, "board_rank_cache.json")


def industry_board_rank(top=15):
    """东财行业板块涨幅榜 [{name, pct, lead_stock}]。接口失败时回退本地缓存，缓存也没有则返回空列表。"""
    url = "http://push2.eastmoney.com/api/qt/clist/get"
    params = dict(pn=1, pz=top, po=1, np=1, fltt=2, invt=2, fid="f3",
                  fs="m:90+t:2", fields="f3,f14,f136,f128")
    try:
        j = _get(url, params)
        diff = (j.get("data") or {}).get("diff") or []
        rows = [{"name": d.get("f14"), "pct": d.get("f3"), "lead": d.get("f128")} for d in diff]
        with open(BOARD_CACHE, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        return rows
    except Exception as e:
        if os.path.exists(BOARD_CACHE):
            with open(BOARD_CACHE, encoding="utf-8") as f:
                rows = json.load(f)
            print(f"[DATA_STALE] 板块涨幅榜接口失败（{e}），改用本地缓存")
            return rows
        print(f"[DATA_STALE] 板块涨幅榜接口失败且无缓存（{e}），跳过")
        return []


# ---------------------------------------------------------------- 财务指标(F10)
def main_fin_data(secid, periods=8):
    """主要财务指标：ROE、EPS、营收及增速、归母净利及增速、毛利率、净利率等。入参为 secid（如 1.600519）。"""
    sec = secucode(secid)
    cache = os.path.join(FIN_DIR, f"{sec.replace('.', '_')}.json")
    url = "http://datacenter.eastmoney.com/securities/api/data/get"
    params = dict(type="RPT_F10_FINANCE_MAINFINADATA", sty="APP_F10_MAINFINADATA",
                  quoteColumns="", filter=f'(SECUCODE="{sec}")', p=1, ps=periods,
                  sr=-1, st="REPORT_DATE", source="HSF10", client="PC")
    try:
        j = _get(url, params)
        data = (j.get("result") or {}).get("data") or []
    except Exception:
        data = []
    if data:
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    elif os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            data = json.load(f)
    out = []
    for row in data:
        out.append({
            "report_date": str(row.get("REPORT_DATE", ""))[:10],
            "report_type": row.get("REPORT_TYPE"),
            "eps": row.get("EPSJB"),
            "bps": row.get("BPS"),
            "revenue": row.get("TOTALOPERATEREVE"),
            "revenue_yoy": row.get("TOTALOPERATEREVETZ"),
            "net_profit": row.get("PARENTNETPROFIT"),
            "net_profit_yoy": row.get("PARENTNETPROFITTZ"),
            "roe": row.get("ROEJQ"),
            "gross_margin": row.get("XSMLL"),
            "net_margin": row.get("XSJLL"),
        })
    return out


# ---------------------------------------------------------------- 组合工具
def secucode(secid):
    market, code = secid.split(".")
    return f"{code}.{'SH' if market == '1' else 'SZ'}"


if __name__ == "__main__":
    df = kline("1.000300", beg="20260801")
    print(df.tail(3))
    print(snapshot("1.600519"))
    print(industry_board_rank(5)[0])
    print(main_fin_data("1.600519")[0])
