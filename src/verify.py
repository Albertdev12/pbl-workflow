# -*- coding: utf-8 -*-
"""决策后验证：对每笔决策计算 T+5 / T+10 / T+20 个交易日后的表现，并与沪深300对照，
回答"当时这笔决策到底对不对"——把"决策记录"升级成"可验证的证据链"。

只读账本与行情缓存，不修改任何交易数据。
"""
import datetime as dt
import json
import os

import portfolio as pf
import records
from em_client import kline

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data")
HORIZONS = (5, 10, 20)
BENCH = "1.000300"


def _is_index(secid):
    """指数代码（1.000xxx / 0.399xxx）：备用数据源下成交额口径不同，需要显式标记。"""
    return secid.startswith(("1.000", "0.399"))


def _load_series(secid):
    """取全量日K（含最新），用于计算决策日之后的走势。"""
    try:
        df = kline(secid, is_index=_is_index(secid))
        return df.sort_values("date").reset_index(drop=True)
    except Exception:
        return None


def _forward(df, date, price, n):
    """决策日之后第 n 个交易日的收盘价相对 price 的收益。
    返回 (ret, 实际已过交易日数, 对应日期)；数据不足返回 (None, elapsed, None)。"""
    if df is None or not len(df):
        return None, 0, None
    after = df[df["date"] > date]
    elapsed = len(after)
    if elapsed < n:
        return None, elapsed, None
    row = after.iloc[n - 1]
    if not price:
        return None, elapsed, None
    return float(row["close"]) / price - 1, elapsed, str(row["date"])


def _decision_rows():
    """待验证的决策：以实际成交（trades.csv）为准，未成交的决策按决策价验证。"""
    universe = {}
    cfg_path = os.path.join(BASE, "config.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    for u in cfg["universe"]:
        universe[u["code"]] = u
    trades = pf.read_trades()
    by_did = {}
    for t in trades:
        if t.get("decision_id"):
            by_did[t["decision_id"]] = t
    rows = []
    for d in records.read_decisions():
        t = by_did.get(d["decision_id"])
        rows.append({
            "decision_id": d["decision_id"],
            "date": (t or d)["date"],
            "side": (t or d)["side"],
            "code": d["code"], "name": d["name"],
            "price": float((t or d)["price"]),
            "shares": int((t or d)["shares"]),
            "filled": bool(t),
            "source": (t or {}).get("source", "decision"),
            "rule": (d.get("rule_check") or {}).get("status"),
        })
    return rows, universe, cfg


def build_report(asof=None):
    """计算全部决策的前瞻表现，返回可直接导出的报告结构。"""
    rows, universe, cfg = _decision_rows()
    bench_df = _load_series(BENCH)
    series = {}
    out = []
    for r in rows:
        u = universe.get(r["code"])
        if u is None:
            continue
        secid = u["secid"]
        if secid not in series:
            series[secid] = _load_series(secid)
        df = series[secid]
        item = dict(r)
        item["horizons"] = {}
        elapsed = 0
        for n in HORIZONS:
            ret, elapsed, d1 = _forward(df, r["date"], r["price"], n)
            # 基准用"决策日收盘价"归一，口径与个股一致
            bret, bd = None, None
            if bench_df is not None:
                bclose = bench_df[bench_df["date"] >= r["date"]]
                if len(bclose):
                    base = float(bclose["close"].iloc[0])
                    bret, _, bd = _forward(bench_df, r["date"], base, n)
            excess = (ret - bret) if (ret is not None and bret is not None) else None
            if excess is not None and r["side"] == "卖出":
                excess = -excess  # 卖出后跌得比基准多 = 决策正确
            ok = None
            if ret is not None:
                ok = (ret > 0) if r["side"] == "买入" else (ret < 0)
            item["horizons"][f"T+{n}"] = {
                "ret": None if ret is None else round(ret, 4),
                "bench": None if bret is None else round(bret, 4),
                "excess": None if excess is None else round(excess, 4),
                "date": d1, "ok": ok,
            }
        item["days_elapsed"] = elapsed
        verified = [v for v in item["horizons"].values() if v["ret"] is not None]
        item["status"] = "已验证" if verified else "待观察"
        out.append(item)

    verified_all = [(r, v) for r in out for v in r["horizons"].values() if v["ret"] is not None]
    excesses = [v["excess"] for _, v in verified_all if v["excess"] is not None]
    oks = [v["ok"] for _, v in verified_all if v["ok"] is not None]
    summary = {
        "n_decisions": len(out),
        "n_verified": sum(1 for r in out if r["status"] == "已验证"),
        "n_pending": sum(1 for r in out if r["status"] == "待观察"),
        "n_samples": len(verified_all),
        "win_rate": round(sum(1 for x in oks if x) / len(oks), 4) if oks else None,
        "avg_excess": round(sum(excesses) / len(excesses), 4) if excesses else None,
        "asof": asof or (bench_df["date"].iloc[-1] if bench_df is not None and len(bench_df) else dt.date.today().isoformat()),
    }
    return {"generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": summary, "rows": out}


def save_log(report):
    """把每次验证的汇总追加到 data/verify_log.jsonl，便于观察"决策质量"随时间变化。"""
    rec = dict(report["summary"])
    rec["time"] = report["generated_at"]
    with open(os.path.join(DATA_DIR, "verify_log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


if __name__ == "__main__":
    rep = build_report()
    print(json.dumps(rep["summary"], ensure_ascii=False, indent=2))
    for r in rep["rows"]:
        hs = " ".join(
            f"{k}={'待观察' if v['ret'] is None else format(v['ret'] * 100, '+.2f') + '%'}"
            for k, v in r["horizons"].items())
        print(f"{r['decision_id']} {r['date']} {r['side']}{r['name']}({r['code']}) @{r['price']} "
              f"已过{r['days_elapsed']}日 {hs} {r['status']}")
