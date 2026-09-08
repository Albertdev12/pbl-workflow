# -*- coding: utf-8 -*-
"""东方财富公开行情/财务接口封装，带本地CSV缓存，供全流程数据采集使用。"""
import json
import os
import time

import pandas as pd
import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_QUOTE = "https://qt.gtimg.cn/q="
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
KLINE_DIR = os.path.join(DATA_DIR, "kline")
FIN_DIR = os.path.join(DATA_DIR, "fin")
for _d in (KLINE_DIR, FIN_DIR):
    os.makedirs(_d, exist_ok=True)


def _tencent_symbol(secid):
    market, code = secid.split(".")
    return ("sh" if market == "1" else "sz") + code


def _tencent_kline(secid, count=420, is_index=False):
    """备用数据源（腾讯）前复权日K。个股成交额按 收盘价×成交量(手)×100 估算；
    指数没有可用成交额口径 → 标记 NaN（避免写入错误数字污染"两市成交额"）。"""
    sym = _tencent_symbol(secid)
    r = requests.get(TENCENT_KLINE, params={"param": f"{sym},day,,,{count},qfq"},
                     headers=HEADERS, timeout=12)
    r.raise_for_status()
    node = (r.json().get("data") or {}).get(sym) or {}
    rows = node.get("qfqday") or node.get("day") or []
    if not rows:
        raise ValueError("tencent kline empty")
    df = pd.DataFrame([x[:6] for x in rows],
                      columns=["date", "open", "close", "high", "low", "volume"])
    for c in df.columns:
        if c != "date":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["amount"] = float("nan") if is_index else df["volume"] * 100 * df["close"]
    return df[["date", "open", "close", "high", "low", "volume", "amount"]]


def _tencent_snapshot(secid):
    """备用数据源（腾讯）实时快照；PE/PB/市值等东财独有字段返回 None。"""
    sym = _tencent_symbol(secid)
    r = requests.get(TENCENT_QUOTE + sym, headers=HEADERS, timeout=12)
    r.raise_for_status()
    raw = r.content.decode("gbk", errors="replace")
    if "~" not in raw:
        raise ValueError("tencent quote empty")
    f = raw.split('"')[1].split("~")

    def num(i):
        try:
            return float(f[i]) if f[i] not in ("", "-") else None
        except Exception:
            return None

    return {"code": f[2], "name": f[1], "price": num(3), "pct_chg": num(32),
            "pe_ttm": None, "pb": None, "total_mv": None, "float_mv": None,
            "turnover_pct": num(38), "volume_ratio": num(49),
            "high": num(33), "low": num(34), "open": num(5), "prev_close": num(4)}


# ---------------------------------------------------------------- 主数据源熔断
# 东财接口从境外机房经常 502/超时：每次调用都要重试+超时，十几只标的会拖到十几分钟。
# 连续失败 2 次即认为主源不可用，本次运行后续请求直接走备用源（腾讯），大幅缩短运行时间。
# 按接口类别分别熔断：日K(push2his) 与 快照/板块(push2) 是两个不同子域，互不牵连。
_EM_STATE = {"kline": {"fails": 0, "down": False}, "quote": {"fails": 0, "down": False}}


def em_available(kind="quote"):
    return not _EM_STATE[kind]["down"]


def _em_ok(kind="quote"):
    _EM_STATE[kind]["fails"] = 0


def _em_fail(kind, err):
    st = _EM_STATE[kind]
    st["fails"] += 1
    if st["fails"] >= 2 and not st["down"]:
        st["down"] = True
        print(f"[DATA_FALLBACK] 东财{kind}接口连续失败（{str(err)[:60]}），"
              "本次运行后续同类请求直接使用备用数据源")


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
def kline(secid, beg="20250101", end="20500101", klt="101", fqt="1", is_index=False):
    """日K线 -> DataFrame[date, open, close, high, low, volume, amount]。网络失败时回退本地缓存。"""
    cache = os.path.join(KLINE_DIR, f"{secid.replace('.', '_')}.csv")
    url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = dict(secid=secid, fields1="f1,f2,f3,f4,f5,f6",
                  fields2="f51,f52,f53,f54,f55,f56,f57", klt=klt, fqt=fqt, beg=beg, end=end)
    from_fallback = False
    df, em_err = None, None
    if em_available("kline"):
        try:
            j = _get(url, params)
            d = j.get("data") or {}
            rows = [x.split(",") for x in d.get("klines", [])]
            df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume", "amount"])
            _em_ok("kline")
        except Exception as e:
            em_err = e
            _em_fail("kline", e)
    if df is None:
        try:  # 第二数据源：腾讯（只补缺口，不覆盖东财已有日期）
            df = _tencent_kline(secid, is_index=is_index)
            from_fallback = True
            print(f"[DATA_FALLBACK] 东财行情失败（{str(em_err or '主源已熔断')[:50]}），已切换腾讯数据源")
        except Exception:
            if os.path.exists(cache):  # DATA_STALE回退：使用本地缓存
                df = pd.read_csv(cache, dtype={"date": str})
                print(f"[DATA_STALE] 双数据源均失败，回退本地缓存: {cache}")
                return df.sort_values("date").reset_index(drop=True)
            raise
    for c in df.columns:
        if c != "date":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # 增量合并缓存
    if os.path.exists(cache):
        old = pd.read_csv(cache, dtype={"date": str})
        if from_fallback:  # 备用源只补东财没有的日期，避免两套复权口径混用
            df = pd.concat([old, df[~df["date"].isin(set(old["date"]))]], ignore_index=True)
        else:
            df = pd.concat([old[~old["date"].isin(set(df["date"]))], df], ignore_index=True)
        # 备用数据源下指数成交额缺失（NaN）→ 保留缓存中已有的真实成交额，避免污染"两市成交额"
        if "amount" in df.columns:
            amt_map = old.set_index("date")["amount"]
            df["amount"] = df["amount"].fillna(df["date"].map(amt_map))
    df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    df.to_csv(cache, index=False)
    return df


def kline_until(secid, trade_date, lookback=260, is_index=False):
    """取 trade_date 当日（含）前 lookback 根日K，保证回测/决策不引入未来数据。"""
    df = kline(secid, is_index=is_index)
    df = df[df["date"] <= trade_date]
    return df.tail(lookback).reset_index(drop=True)


# ---------------------------------------------------------------- 实时快照
def snapshot(secid):
    """个股/ETF实时快照：名称、现价、涨跌幅、PE、PB、总市值、换手率、量比等。"""
    url = "http://push2.eastmoney.com/api/qt/stock/get"
    params = dict(secid=secid, invt="2", fltt="2",
                  fields="f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f62,f84,f116,f117,f162,f164,f167,f168,f169,f170,f171,f292")
    j, em_err = None, None
    if em_available("quote"):
        try:
            j = _get(url, params)
            _em_ok("quote")
        except Exception as e:
            em_err = e
            _em_fail("quote", e)
    if j is None:
        print(f"[DATA_FALLBACK] 东财快照失败（{str(em_err or '主源已熔断')[:50]}），已切换腾讯数据源")
        return _tencent_snapshot(secid)
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
    if not em_available("quote"):
        if os.path.exists(BOARD_CACHE):
            with open(BOARD_CACHE, encoding="utf-8") as f:
                return json.load(f)
        return []
    try:
        j = _get(url, params)
        diff = (j.get("data") or {}).get("diff") or []
        rows = [{"name": d.get("f14"), "pct": d.get("f3"), "lead": d.get("f128")} for d in diff]
        with open(BOARD_CACHE, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        _em_ok("quote")
        return rows
    except Exception as e:
        _em_fail("quote", e)
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
