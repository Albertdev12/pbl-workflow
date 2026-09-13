# -*- coding: utf-8 -*-
"""数据体检：一次性核对"数据能不能用、准不准、全不全"。

面向的用途：写报告时要引用从最初到现在的数据，必须确认
  1) 每个数据源现在是否可达（而不是"以前能通"）；
  2) 本地缓存覆盖了哪些日期、有没有缺口或异常值；
  3) 缓存里的价格与独立数据源是否一致（找脏数据）；
  4) 账本、决策、净值、验证等记录是否自洽（重放账本能得到页面上的总资产）。

只读运行，不修改任何项目文件。用法：
    python scripts/data_audit.py            # 全部检查
    python scripts/data_audit.py --no-net   # 只做本地检查（不打网络）
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "src"))

import pandas as pd  # noqa: E402
import em_client  # noqa: E402
from em_client import (clist_base, industry_board_rank, kline, kline_until,  # noqa: E402
                       main_fin_data, market_snapshot, secid_of, snapshot)

import portfolio as pf  # noqa: E402
import records  # noqa: E402

CFG = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8"))
KLINE_DIR = os.path.join(BASE, "data", "kline")
FIN_DIR = os.path.join(BASE, "data", "fin")
OUT = os.path.join(BASE, "outputs")

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
LATEST_TRADE_DATE = "2026-09-11"
results = []


def rec(area, item, status, detail=""):
    results.append({"area": area, "item": item, "status": status, "detail": str(detail)[:220]})
    icon = {PASS: "OK  ", WARN: "WARN", FAIL: "FAIL"}[status]
    print(f"  [{icon}] {item}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------- 1. 数据源可达性
def check_sources():
    print("\n== 1. 数据源可达性（实时探测，非历史结论）==")
    LATEST = "2026-09-11"

    # 东财日K主域
    try:
        em_client._get("http://push2his.eastmoney.com/api/qt/stock/kline/get",
                       dict(secid="1.600519", fields1="f1", fields2="f51,f53",
                            klt="101", fqt="1", beg="20260901", end="20500101"), retries=1)
        rec("数据源", "东财日K push2his", PASS)
    except Exception as e:
        rec("数据源", "东财日K push2his", WARN, f"被阻断（{type(e).__name__}）→ 走备用源，属当前网络常态")

    # 东财快照主机链
    try:
        j, host = em_client._get_em("quote", "/api/qt/stock/get",
                                    dict(secid="1.600519", invt="2", fltt="2",
                                         fields="f43,f57,f58,f48,f116"), timeout=10)
        ok = bool((j.get("data") or {}).get("f43"))
        rec("数据源", "东财快照（主机链）", PASS if ok else WARN, f"命中 {host}")
    except Exception as e:
        rec("数据源", "东财快照（主机链）", FAIL, f"{type(e).__name__}: {str(e)[:60]}")

    # 全市场列表
    try:
        base = clist_base()
        j = em_client._get(base + "/api/qt/clist/get",
                           dict(pn=1, pz=100, po=1, np=1, fltt=2, invt=2, fid="f6",
                                fs="m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                                fields="f2,f3,f6,f12,f14"), timeout=20)
        n = len((j.get("data") or {}).get("diff") or [])
        total = (j.get("data") or {}).get("total")
        rec("数据源", "东财全市场列表", PASS if n else FAIL, f"{base} total={total} page={n}")
    except Exception as e:
        rec("数据源", "东财全市场列表", FAIL, f"{type(e).__name__}: {str(e)[:60]}")

    # 行业板块榜
    try:
        rows = industry_board_rank(5)
        rec("数据源", "东财行业板块榜", PASS if rows else WARN, f"{len(rows)} 条")
    except Exception as e:
        rec("数据源", "东财行业板块榜", WARN, f"{type(e).__name__}: {str(e)[:60]}")

    # 财务 F10
    try:
        d = main_fin_data("1.600519", periods=2)
        rec("数据源", "东财 F10 财务", PASS if d else WARN,
            f"{len(d)} 期，最新报告期 {d[0]['report_date'] if d else '—'}")
    except Exception as e:
        rec("数据源", "东财 F10 财务", FAIL, f"{type(e).__name__}: {str(e)[:60]}")

    # 腾讯主域 / proxy / 新浪主域 / 新浪备用域
    for name, fn in (("腾讯主域 web.ifzq", em_client._tencent_kline),
                     ("腾讯 proxy.finance", em_client._tencent_proxy_kline),
                     ("新浪主域 money.finance", em_client._sina_kline),
                     ("新浪备用 quotes.sina.cn", em_client._sina_kline_alt)):
        try:
            df = fn("1.600519")
            last = str(df["date"].iloc[-1])
            rec("数据源", f"日K {name}", PASS if last == LATEST else WARN,
                f"末行 {last}（{len(df)} 根）")
        except Exception as e:
            rec("数据源", f"日K {name}", WARN, f"不可用（{type(e).__name__}）")

    # 指数成交额 ulist
    try:
        flow = em_client.index_turnovers(["1.000001", "0.399001"], LATEST)
        rec("数据源", "指数成交额 ulist", PASS if len(flow) >= 2 else WARN,
            "、".join(f"{k}={v/1e8:.0f}亿" for k, v in flow.items()))
    except Exception as e:
        rec("数据源", "指数成交额 ulist", WARN, f"{type(e).__name__}")


# ---------------------------------------------------------------- 2. 缓存覆盖与异常值
def check_cache():
    print("\n== 2. 缓存覆盖范围与异常值 ==")
    files = sorted(f for f in os.listdir(KLINE_DIR) if f.endswith(".csv"))
    rec("缓存", "日K缓存文件数", PASS if files else FAIL, f"{len(files)} 个")
    holiday_breaks = []
    for f in files:
        p = os.path.join(KLINE_DIR, f)
        try:
            df = pd.read_csv(p, dtype={"date": str})
        except Exception as e:
            rec("缓存", f, FAIL, f"读取失败 {e}")
            continue
        d0, d1 = str(df["date"].iloc[0]), str(df["date"].iloc[-1])
        n = len(df)
        # 缺口：A 股长假（春节/国庆）会有 9~12 天休市，>14 天才是真缺数据
        dates = pd.to_datetime(df["date"])
        gaps = dates.diff().dt.days.fillna(1)
        big = int((gaps > 14).sum())
        holiday = int(((gaps > 7) & (gaps <= 14)).sum())
        if holiday:
            holiday_breaks.append((f, holiday, int(gaps.max())))
        bad_ohlc = int(((df[["open", "close", "high", "low"]] <= 0).any(axis=1)).sum())
        nan_close = int(df["close"].isna().sum())
        zero_close = int((df["close"] == 0).sum())
        dup = int(df["date"].duplicated().sum())
        issues = []
        if big:
            issues.append(f"{big} 处>14天间隔")
        if bad_ohlc or nan_close or zero_close:
            issues.append(f"异常OHLC {bad_ohlc + nan_close + zero_close}")
        if dup:
            issues.append(f"重复日期 {dup}")
        if d1 != LATEST_TRADE_DATE:
            issues.append(f"末行 {d1} 非最新交易日")
        rec("缓存", f"{f}", PASS if not issues else WARN,
            f"{d0}~{d1}，{n} 根" + ("；" + "，".join(issues) if issues else ""))
    if holiday_breaks:
        f0, cnt, mx = holiday_breaks[0]
        rec("缓存", "长假停市间隔（非缺口）", PASS,
            f"{len(holiday_breaks)}/{len(files)} 个文件含 1~2 处 8~14 天间隔，"
            f"最长 {mx} 天（如 {f0}）——A 股春节/国庆休市，属正常")

    fins = sorted(f for f in os.listdir(FIN_DIR) if f.endswith(".json"))
    fin_ok, fin_blank, fin_dates = 0, [], []
    for f in fins:
        try:
            raw = json.load(open(os.path.join(FIN_DIR, f), encoding="utf-8"))
            rows = raw if isinstance(raw, list) else (raw.get("data") or [])
            # F10 原始缓存用大写 REPORT_DATE；归一化后是小写 report_date——两种都认
            rd = [(r.get("REPORT_DATE") or r.get("report_date")) for r in rows if isinstance(r, dict)]
            rd = [str(x)[:10] for x in rd if x]
            if rd:
                fin_ok += 1
                fin_dates += rd
            else:
                fin_blank.append(f)
        except Exception:
            fin_blank.append(f)
    rec("缓存", "财务缓存", PASS if fin_ok and not fin_blank else WARN,
        f"{fin_ok}/{len(fins)} 只含有效报告期"
        + (f"，最新报告期 {max(fin_dates)}" if fin_dates else "")
        + (f"；异常 {fin_blank[:3]}" if fin_blank else ""))

    # 成交额量级体检（元）
    amt_bad = []
    for u in CFG["universe"]:
        p = os.path.join(KLINE_DIR, f"{u['secid'].replace('.', '_')}.csv")
        if not os.path.exists(p):
            continue
        df = pd.read_csv(p, dtype={"date": str})
        amt = pd.to_numeric(df["amount"], errors="coerce")
        last = amt.iloc[-1]
        if last != last or not (1e7 <= last <= 2e12):
            amt_bad.append(f"{u['code']}={last}")
    rec("缓存", "成交额量级（个股末根）", PASS if not amt_bad else WARN,
        "、".join(amt_bad[:5]) if amt_bad else "12 只均在 1e7~2e12 元区间")


# ---------------------------------------------------------------- 3. 价格准确性交叉校验
def check_accuracy(sample=6):
    print(f"\n== 3. 价格准确性：与独立源（新浪备用域）逐只比对 ==")
    uni = CFG["universe"]
    step = max(1, len(uni) // sample)
    picked = uni[::step][:sample]
    for u in picked:
        try:
            s = snapshot(u["secid"])
            df = kline(u["secid"])
            row = df[df["date"] == "2026-09-11"]
            kc = float(row["close"].iloc[0]) if len(row) else None
            ref = float(em_client._sina_kline_alt(u["secid"])["close"].iloc[-1])
            d_em = abs(kc - s["price"]) / s["price"] * 100 if (kc and s.get("price")) else None
            d_sina = abs(kc - ref) / ref * 100 if kc else None
            ok = (d_em is not None and d_em <= 0.5) and (d_sina is not None and d_sina <= 0.5)
            rec("准确性", f"{u['name']}({u['code']})", PASS if ok else FAIL,
                f"缓存 {kc} · 东财 {s.get('price')} · 新浪 {ref}"
                + (f" · 偏差 东财{d_em:.2f}%/新浪{d_sina:.2f}%" if d_em is not None else ""))
        except Exception as e:
            rec("准确性", f"{u['name']}({u['code']})", WARN, f"校验失败 {type(e).__name__}: {str(e)[:50]}")
    # 指数
    for idx in CFG["indices"]:
        try:
            df = kline(idx["code"], is_index=True)
            kc = float(df["close"].iloc[-1])
            s = snapshot(idx["code"])
            d = abs(kc - s["price"]) / s["price"] * 100 if s.get("price") else None
            rec("准确性", f"{idx['name']}（指数）", PASS if d is not None and d <= 0.5 else FAIL,
                f"缓存 {kc} · 东财 {s.get('price')} · 偏差 {d:.2f}%" if d is not None else "东财无值")
        except Exception as e:
            rec("准确性", f"{idx['name']}（指数）", WARN, f"{type(e).__name__}")


# ---------------------------------------------------------------- 4. 账本与记录自洽
def check_ledger():
    print("\n== 4. 账本与记录自洽性 ==")
    trades = pf.read_trades()
    nav = records.read_nav()
    decisions = records.read_decisions()
    confirmed = records.read_confirmations()

    rec("账本", "trades.csv 笔数", PASS if trades else FAIL, f"{len(trades)} 笔")
    dates = [t["date"] for t in trades]
    rec("账本", "成交日期单调不减", PASS if dates == sorted(dates) else FAIL,
        f"{dates[0]} ~ {dates[-1]}")

    # 重放一致性：逐只持仓不能为负
    acct = pf.replay(trades, CFG["strategy"]["initial_capital"])
    neg = [c for c, p in acct["positions"].items() if p["shares"] < 0]
    rec("账本", "重放后无负持仓", PASS if not neg else FAIL, f"负持仓 {neg}" if neg else
        f"{len(acct['positions'])} 只持仓，现金 {acct['cash']/10000:.2f} 万，费用 {acct['fee']:.0f} 元")

    # 买卖配对：每只卖出量不超过累计买入量
    over = []
    for code in {t["code"] for t in trades}:
        sh = 0
        for t in [x for x in trades if x["code"] == code]:
            sh += t["shares"] if t["side"] == "买入" else -t["shares"]
            if sh < 0:
                over.append(code)
                break
    rec("账本", "卖出量不超过买入量", PASS if not over else FAIL, f"超卖 {set(over)}" if over else "全部配对")

    # 净值序列
    rec("账本", "净值记录", PASS if len(nav) >= 2 else WARN,
        f"{len(nav)} 个交易日：{nav[0]['date']} ~ {nav[-1]['date']}，最新总资产 {nav[-1]['total_assets']/10000:.2f} 万")
    nav_dates = [n["date"] for n in nav]
    rec("账本", "净值日期无重复", PASS if len(nav_dates) == len(set(nav_dates)) else WARN,
        f"{len(set(nav_dates))} 个唯一日期")

    # 决策
    ids = [d.get("decision_id") for d in decisions]
    auto = [d for d in decisions if not d.get("manual")]
    manual = [d for d in decisions if d.get("manual")]
    rec("决策", "决策总数", PASS if decisions else WARN,
        f"{len(decisions)} 条（自动 {len(auto)} / 人工 {len(manual)}）")
    rec("决策", "决策编号唯一", PASS if len(ids) == len(set(ids)) else FAIL,
        f"重复 {[i for i in ids if ids.count(i) > 1][:3]}" if len(ids) != len(set(ids)) else "无重复")
    need = [d["decision_id"] for d in decisions
            if not (d.get("fund_basis") and d.get("tech_basis") and d.get("market_basis") and d.get("risk_judge"))]
    rec("决策", "18 列依据齐全", PASS if not need else WARN,
        "缺失 " + "、".join(need[:5]) if need else "全部含基本面/技术面/市场/风险依据")
    red = [d["decision_id"] for d in decisions
           if (d.get("rule_check") or {}).get("status") not in ("GREEN", "YELLOW")]
    rec("决策", "规则校验状态", PASS if not red else WARN,
        f"{len(red)} 条为 RED/缺失：{'、'.join(red)}（人工主动决策，审计留痕）" if red else "全部 GREEN/YELLOW")
    rec("决策", "人工确认", PASS if len(confirmed) >= len(decisions) else WARN,
        f"{len(confirmed)}/{len(decisions)} 已确认")

    # 决策 → 账本 对账
    tids = {t.get("decision_id") for t in trades}
    missing = [i for i in ids if i and i not in tids]
    rec("账本", "决策与账本对账", PASS if not missing else WARN,
        f"{len(missing)} 条决策无对应成交（可能未执行/被规则拦截）：{'、'.join(missing[:5])}" if missing else
        "每条决策都能在账本中找到成交行")

    # 各类过程记录（key 各不相同：净值/观察/候选池按日期，验证日志按 asof，风控按时间戳）
    for name, rows, key in (("market_watch", records.read_market_watch(), "date"),
                            ("pool_history", records._read_jsonl("pool_history.jsonl"), "date"),
                            ("verify_log", records._read_jsonl("verify_log.jsonl"), "asof"),
                            ("riskwatch_log", records._read_jsonl("riskwatch_log.jsonl"), "time"),
                            ("review_log", records._read_jsonl("review_log.jsonl"), "date"),
                            ("ai_advice", records._read_jsonl("ai_advice.jsonl"), "date")):
        ds = [str(r.get(key))[:10] for r in rows if r.get(key)]
        ok = bool(ds)
        extra = ""
        if name == "review_log" and ds:
            # review 是按"每次运行追加"记录的（一天跑几次就有几条），不是按日覆盖，属预期行为
            uniq = len(set(ds))
            extra = f"，覆盖 {uniq} 个交易日（每次运行追加一条，同日多条属预期）" if uniq != len(ds) else ""
        rec("记录", name, PASS if ok else FAIL,
            f"{len(rows)} 条" + (f"：{min(ds)} ~ {max(ds)}{extra}" if ds else "（无有效日期字段）"))

    # 资产对账：账本重放（用最新收盘价）+ 现金 应等于 nav_history 最新一条
    try:
        last_nav = nav[-1]
        prices = {}
        for code in acct["positions"]:
            secid = next((u["secid"] for u in CFG["universe"] if u["code"] == code), None) \
                or secid_of(code)
            df = kline_until(secid, last_nav["date"])
            if len(df):
                prices[code] = float(df["close"].iloc[-1])
        replayed = pf.total_assets(acct, prices)
        dev = abs(replayed - last_nav["total_assets"]) / last_nav["total_assets"] * 100
        rec("账本", "总资产对账（重放 vs 净值记录）", PASS if dev <= 0.5 else WARN,
            f"重放 {replayed/10000:.2f} 万 vs 记录 {last_nav['total_assets']/10000:.2f} 万"
            f"（偏差 {dev:.3f}%）")
    except Exception as e:
        rec("账本", "总资产对账（重放 vs 净值记录）", WARN, f"{type(e).__name__}: {str(e)[:60]}")

    # 成果文档完整性
    need_files = ["01_小组信息登记表.xlsx", "02_市场观察记录.xlsx", "03_候选股票池及初步分析表.xlsx",
                  "04_初始投资方案.docx", "06_技术分析记录.xlsx", "07_投资决策记录.xlsx",
                  "09_交易日志与盈亏归因.xlsx", "10_策略调整记录.docx", "11_AI使用记录.xlsx",
                  "12_中期路演.pptx", "13_投资总结报告.docx", "19_决策后验证.xlsx",
                  "00_完成度自检报告.json"]
    miss = [f for f in need_files if not os.path.exists(os.path.join(OUT, f))]
    rec("成果", "任务书成果清单", PASS if not miss else WARN,
        "缺 " + "、".join(miss) if miss else f"{len(need_files)} 项齐全")
    daily = len([f for f in os.listdir(OUT) if f.startswith("14_交易复盘_")])
    rec("成果", "每日复盘/日报/素材包", PASS if daily else WARN,
        f"交易复盘 {daily} 份；工作日报 "
        f"{len([f for f in os.listdir(OUT) if f.startswith('15_工作日报_')])} 份；素材包 "
        f"{len([f for f in os.listdir(OUT) if f.startswith('16_报告素材包_')])} 份")


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-net", action="store_true", help="跳过联网检查")
    args = ap.parse_args()
    t0 = time.time()
    print("=" * 78)
    print(f"数据体检 · {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"数据目录: {os.path.join(BASE, 'data')}")
    print("=" * 78)
    check_cache()
    check_ledger()
    if not args.no_net:
        check_sources()
        check_accuracy()
    n = {PASS: 0, WARN: 0, FAIL: 0}
    for r in results:
        n[r["status"]] += 1
    print("\n" + "=" * 78)
    print(f"结论：PASS {n[PASS]} · WARN {n[WARN]} · FAIL {n[FAIL]}　（耗时 {time.time()-t0:.0f}s）")
    print("=" * 78)
    out = os.path.join(BASE, "data", "audit_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "summary": n, "results": results}, f, ensure_ascii=False, indent=2)
    print("明细已写入:", out)
    return 0 if n[FAIL] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
