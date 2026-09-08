# -*- coding: utf-8 -*-
"""策略回测（只读项目数据，不修改任何项目文件）。
与线上逻辑对齐：indicators/signal_and_judgment、候选池top8、min_buy_score、止盈9%/止损8%、
MACD死叉+技术分<45离场、目标仓位24%、单只<=30%、现金>=20%、每日最多3只、每两周至少1笔、
100股整数倍、佣金万2.5双边+印花税0.05%卖出。执行价：决策日收盘生成 -> 次日开盘成交。
"""
import os, sys, json, math
import pandas as pd, numpy as np

PROJ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.join(PROJ, "src"))
import em_client
# 断网模式：强制走本地缓存（保证可复现、不产生网络请求）
em_client._get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline-backtest"))
import indicators, fundamental

CFG = json.load(open(os.path.join(PROJ, "config.json"), encoding="utf-8"))
ST = CFG["strategy"]
UNIVERSE = CFG["universe"]
KLINE_DIR = os.path.join(PROJ, "data", "kline")
BENCH = ST["market_benchmark"]
COMM, STAMP = 0.00025, 0.0005

def load(secid):
    p = os.path.join(KLINE_DIR, secid.replace(".", "_") + ".csv")
    df = pd.read_csv(p, dtype={"date": str})
    for c in df.columns:
        if c != "date":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)

DATA = {u["code"]: load(u["secid"]) for u in UNIVERSE}
BM = load(BENCH)
BY_CODE = {u["code"]: u for u in UNIVERSE}

# 基本面分数（用缓存F10，回测期内视为恒定；存在前视偏差，作为局限说明）
FIN = {}
for u in UNIVERSE:
    try:
        FIN[u["code"]] = float(fundamental.analyze(u["secid"], u["kind"])["score"])
    except Exception:
        FIN[u["code"]] = 60.0 if u["kind"] == "etf" else 50.0

DATES = sorted(set(BM["date"]) & set().union(*[set(d["date"]) for d in DATA.values()]))
START_IDX = 70  # 指标需要>=70根

RSI_EVENTS = {"total": 0, "nan": 0, "detail": []}

def tech_score(df):
    try:
        ind = indicators.compute(df)
    except Exception as e:
        # 复现线上潜在崩溃点：RSI 为 NaN 时 float() 抛错
        if "NAType" in str(e) or "NaN" in str(e):
            RSI_EVENTS["nan"] += 1
            if len(RSI_EVENTS["detail"]) < 5:
                RSI_EVENTS["detail"].append(f"{df['date'].iloc[-1]} {str(e)[:50]}")
        RSI_EVENTS["total"] += 1
        return None, None, None, None
    sig, judge, sc = indicators.signal_and_judgment(ind)
    return sc, ind, sig, judge

def busdays(d1, d2):
    return int(np.busday_count(d1, d2))

def run(variant, exec_next_open=True, start_i=START_IDX):
    """variant: 'composite'(50/50) 或 'tech'"""
    cash = float(ST["initial_capital"])
    pos = {}   # code -> {shares, cost, name, open_date}
    equity, trades, last_trade_date = [], [], None
    pending = []   # 待次日开盘执行的指令
    for i in range(start_i, len(DATES)):
        d = DATES[i]
        # ---------- 1) 开盘执行前一日指令 ----------
        for od in pending:
            code = od["code"]
            df = DATA[code]
            row = df[df["date"] == d]
            if row.empty:
                continue
            px = float(row["open"].iloc[0])
            if not px or math.isnan(px) or px <= 0:
                px = float(row["close"].iloc[0])
            sh = od["shares"]
            amt = px * sh
            if od["side"] == "买入":
                if amt > cash:
                    sh = int(cash / px // 100) * 100
                    amt = px * sh
                    if sh < 100: continue
                cash -= amt + amt * COMM
                p = pos.setdefault(code, {"shares": 0, "cost": 0.0, "name": od["name"], "open_date": d})
                p["cost"] = (p["cost"] * p["shares"] + amt) / (p["shares"] + sh)
                p["shares"] += sh
            else:
                if code not in pos: continue
                sh = min(sh, pos[code]["shares"])
                cash += px * sh - px * sh * (COMM + STAMP)
                pos[code]["shares"] -= sh
                if pos[code]["shares"] == 0: pos.pop(code)
            trades.append({"date": d, "code": code, "name": od["name"], "side": od["side"],
                           "price": round(px, 3), "shares": sh, "reason": od["reason"]})
            last_trade_date = d
        pending = []
        # ---------- 2) 收盘估值与信号 ----------
        prices, scores = {}, {}
        for u in UNIVERSE:
            code = u["code"]; df = DATA[code]
            sub = df[df["date"] <= d]
            if len(sub) < 70:
                continue
            px = float(sub["close"].iloc[-1]); prices[code] = px
            sc, ind, sig, judge = tech_score(sub)
            if sc is None:
                continue
            comp = 0.5 * FIN[code] + 0.5 * sc if variant == "composite" else sc
            scores[code] = {"composite": round(comp, 1), "tech": sc, "ind": ind,
                            "sig": sig, "judge": judge, "close": px, "kind": u["kind"]}
        if not prices:
            continue
        total = cash + sum(pos[c]["shares"] * prices.get(c, pos[c]["cost"]) for c in pos)
        equity.append({"date": d, "total": round(total, 2), "cash": round(cash, 2)})
        pool = sorted([c for c in scores], key=lambda c: -scores[c]["composite"])[: ST["max_candidates"]]
        if not any(scores[c]["kind"] == "etf" for c in pool):
            etfs = [c for c in scores if scores[c]["kind"] == "etf"]
            if etfs:
                pool = pool[:-1] + [max(etfs, key=lambda c: scores[c]["composite"])]
        # ---------- 3) 卖出纪律 ----------
        proposed = []
        for c in list(pos):
            px = prices.get(c); p = pos[c]
            if not px or not p["cost"]: continue
            pnl = px / p["cost"] - 1
            if pnl >= ST["take_profit_pct"]:
                proposed.append({"code": c, "name": p["name"], "side": "卖出", "shares": p["shares"] // 100 * 100,
                                 "reason": f"止盈{pnl*100:.1f}%"})
            elif pnl <= -ST["stop_loss_pct"]:
                proposed.append({"code": c, "name": p["name"], "side": "卖出", "shares": p["shares"] // 100 * 100,
                                 "reason": f"止损{pnl*100:.1f}%"})
            else:
                ind = scores.get(c, {}).get("ind") or {}
                if ind.get("macd_dead") and scores[c]["tech"] < 45:
                    proposed.append({"code": c, "name": p["name"], "side": "卖出", "shares": p["shares"] // 100 * 100,
                                     "reason": "MACD死叉+技术分<45"})
        sells = sum(prices.get(p["code"], 0) * p["shares"] for p in proposed)
        cash_now = cash + sells * (1 - COMM - STAMP)
        # ---------- 4) 买入 ----------
        new_buys = 0
        first_build = len(pos) == 0
        order = ([c for c in pool if scores[c]["kind"] == "etf"] + [c for c in pool if scores[c]["kind"] != "etf"]
                 if first_build else pool)
        freq_due = (last_trade_date is None and busdays(ST.get("start_date", DATES[start_i]), d) >= 10) or \
                   (last_trade_date is not None and busdays(last_trade_date, d) >= 10)
        for c in order:
            if new_buys >= ST["max_batch_build"]: break
            s = scores[c]
            if s["composite"] < ST["min_buy_score"] and not first_build and not freq_due: continue
            if any(p["code"] == c and p["side"] == "买入" for p in proposed): continue
            cur = pos.get(c)
            w_now = (cur["shares"] * s["close"] / total) if cur else 0.0
            target = total * ST["target_single_weight"]
            budget = min(target - w_now * total, cash_now - total * ST["min_cash_pct"])
            if budget < total * 0.05: continue
            sh = int(budget / s["close"] // 100) * 100
            if sh < 100: continue
            amt = sh * s["close"]
            w_after = (w_now * total + amt) / total
            if w_after > ST["max_single_position_pct"] + 1e-9: continue
            if cash_now - amt < total * ST["min_cash_pct"]: continue
            proposed.append({"code": c, "name": BY_CODE[c]["name"], "side": "买入", "shares": sh,
                             "reason": f"评分{s['composite']}达标"})
            cash_now -= amt
            new_buys += 1
        # 频率约束兜底
        if not proposed and freq_due and pool:
            c = pool[0]; s = scores[c]
            budget = min(total * ST["target_single_weight"], cash_now - total * ST["min_cash_pct"])
            sh = int(budget / s["close"] // 100) * 100
            if sh >= 100:
                proposed.append({"code": c, "name": BY_CODE[c]["name"], "side": "买入", "shares": sh,
                                 "reason": "频率约束"})
        pending = proposed
    return pd.DataFrame(equity), trades

def metrics(eq, trades, name):
    if eq.empty: return {}
    start_t, end_t = eq["total"].iloc[0], eq["total"].iloc[-1]
    ret = end_t / start_t - 1
    days = (pd.to_datetime(eq["date"].iloc[-1]) - pd.to_datetime(eq["date"].iloc[0])).days or 1
    ann = (1 + ret) ** (365.0 / days) - 1
    curve = eq["total"] / eq["total"].iloc[0]
    dd = (curve / curve.cummax() - 1).min()
    daily = eq["total"].pct_change().dropna()
    sharpe = (daily.mean() / daily.std() * math.sqrt(252)) if len(daily) > 2 and daily.std() > 0 else 0
    sells = [t for t in trades if t["side"] == "卖出"]
    wins = 0
    for s in sells:
        buys = [b for b in trades if b["code"] == s["code"] and b["side"] == "买入" and b["date"] <= s["date"]]
        if buys and s["price"] > buys[-1]["price"]: wins += 1
    wr = wins / len(sells) if sells else 0
    return {"策略": name, "期末资产(万)": round(end_t / 1e4, 2), "总收益": f"{ret*100:.2f}%",
            "年化": f"{ann*100:.2f}%", "最大回撤": f"{dd*100:.2f}%", "夏普": round(sharpe, 2),
            "交易笔数": len(trades), "卖出胜率": f"{wr*100:.0f}%", "回测天数": days}

def run_buyhold(start_i=START_IDX):
    """等权买入持有观察池全部12只（作为'选股池本身'的收益基准）"""
    first = DATES[start_i]
    cash = float(ST["initial_capital"]); pos = {}
    for u in UNIVERSE:
        df = DATA[u["code"]]
        row = df[df["date"] == first]
        if row.empty: continue
        px = float(row["open"].iloc[0])
        sh = int((cash / len(UNIVERSE)) / px // 100) * 100
        if sh >= 100:
            pos[u["code"]] = {"shares": sh, "cost": px, "name": u["name"], "open_date": first}
            cash -= sh * px * (1 + COMM)
    eq = []
    for d in DATES[start_i:]:
        val = cash
        for c, p_ in pos.items():
            df = DATA[c]; row = df[df["date"] == d]
            if not row.empty: val += p_["shares"] * float(row["close"].iloc[0])
        eq.append({"date": d, "total": round(val, 2), "cash": round(cash, 2)})
    return pd.DataFrame(eq), [{"date": first, "code": c, "name": p_["name"], "side": "买入",
                               "price": round(p_["cost"], 3), "shares": p_["shares"], "reason": "等权持有"} for c, p_ in pos.items()]

def run_norebuy(days=5, start_i=START_IDX):
    """线上策略 + 卖出后N个交易日内不回购同一标的（消除同日卖出又买回的手续费损耗）"""
    cash = float(ST["initial_capital"]); pos = {}; pending = []
    equity, trades, last_trade_date, last_sell = [], [], None, {}
    for i in range(start_i, len(DATES)):
        d = DATES[i]
        for od in pending:
            code = od["code"]; df = DATA[code]; row = df[df["date"] == d]
            if row.empty: continue
            px = float(row["open"].iloc[0]) or float(row["close"].iloc[0]); sh = od["shares"]; amt = px * sh
            if od["side"] == "买入":
                if amt > cash:
                    sh = int(cash / px // 100) * 100; amt = px * sh
                    if sh < 100: continue
                cash -= amt + amt * COMM
                p_ = pos.setdefault(code, {"shares": 0, "cost": 0.0, "name": od["name"]})
                p_["cost"] = (p_["cost"] * p_["shares"] + amt) / (p_["shares"] + sh); p_["shares"] += sh
            else:
                if code not in pos: continue
                sh = min(sh, pos[code]["shares"])
                cash += px * sh - px * sh * (COMM + STAMP)
                pos[code]["shares"] -= sh
                if pos[code]["shares"] == 0: pos.pop(code)
                last_sell[code] = d
            trades.append({"date": d, "code": code, "name": od["name"], "side": od["side"],
                           "price": round(px, 3), "shares": sh, "reason": od["reason"]})
            last_trade_date = d
        pending = []
        prices, scores = {}, {}
        for u in UNIVERSE:
            code = u["code"]; sub = DATA[code]; sub = sub[sub["date"] <= d]
            if len(sub) < 70: continue
            prices[code] = float(sub["close"].iloc[-1])
            sc, ind, sig, judge = tech_score(sub)
            if sc is None: continue
            scores[code] = {"composite": round(0.5 * FIN[code] + 0.5 * sc, 1), "tech": sc, "ind": ind,
                            "close": prices[code], "kind": u["kind"]}
        if not prices: continue
        total = cash + sum(pos[c]["shares"] * prices.get(c, pos[c]["cost"]) for c in pos)
        equity.append({"date": d, "total": round(total, 2), "cash": round(cash, 2)})
        pool = sorted([c for c in scores], key=lambda c: -scores[c]["composite"])[: ST["max_candidates"]]
        proposed = []
        for c in list(pos):
            px = prices.get(c); p_ = pos[c]
            if not px or not p_["cost"]: continue
            pnl = px / p_["cost"] - 1
            if pnl >= ST["take_profit_pct"] or pnl <= -ST["stop_loss_pct"]:
                proposed.append({"code": c, "name": p_["name"], "side": "卖出", "shares": p_["shares"] // 100 * 100, "reason": "价格纪律"})
            elif scores.get(c, {}).get("ind", {}).get("macd_dead") and scores[c]["tech"] < 45:
                proposed.append({"code": c, "name": p_["name"], "side": "卖出", "shares": p_["shares"] // 100 * 100, "reason": "MACD死叉"})
        sells = sum(prices.get(x["code"], 0) * x["shares"] for x in proposed)
        cash_now = cash + sells * (1 - COMM - STAMP)
        new_buys = 0; first_build = len(pos) == 0
        order = ([c for c in pool if scores[c]["kind"] == "etf"] + [c for c in pool if scores[c]["kind"] != "etf"]
                 if first_build else pool)
        freq_due = (last_trade_date is not None and busdays(last_trade_date, d) >= 10)
        for c in order:
            if new_buys >= ST["max_batch_build"]: break
            if c in last_sell and busdays(last_sell[c], d) < days: continue
            s = scores[c]
            if s["composite"] < ST["min_buy_score"] and not first_build and not freq_due: continue
            cur = pos.get(c); w_now = (cur["shares"] * s["close"] / total) if cur else 0.0
            budget = min(total * ST["target_single_weight"] - w_now * total, cash_now - total * ST["min_cash_pct"])
            if budget < total * 0.05: continue
            sh = int(budget / s["close"] // 100) * 100
            if sh < 100: continue
            amt = sh * s["close"]
            if (w_now * total + amt) / total > ST["max_single_position_pct"] + 1e-9: continue
            if cash_now - amt < total * ST["min_cash_pct"]: continue
            proposed.append({"code": c, "name": BY_CODE[c]["name"], "side": "买入", "shares": sh, "reason": "评分达标"})
            cash_now -= amt; new_buys += 1
        pending = proposed
    return pd.DataFrame(equity), trades

out = []
out.append("回测区间: %s ~ %s (%d 个交易日)" % (DATES[START_IDX], DATES[-1], len(DATES) - START_IDX))
bm = BM[BM["date"] >= DATES[START_IDX]]
bm_ret = float(bm["close"].iloc[-1]) / float(bm["close"].iloc[0]) - 1
out.append("基准 沪深300 同期: %+.2f%%" % (bm_ret * 100))
etf = DATA["510300"]; etf = etf[etf["date"] >= DATES[START_IDX]]
etf_ret = float(etf["close"].iloc[-1]) / float(etf["close"].iloc[0]) - 1
out.append("参照 510300ETF 同期: %+.2f%%" % (etf_ret * 100))
out.append("指标计算异常(RSI NaN)次数: %d（样本 %s）" % (RSI_EVENTS["nan"], RSI_EVENTS["detail"]))
out.append("")
rows = []
for variant, label in [("composite", "线上策略(基本面50%+技术面50%)"), ("tech", "纯技术面变体")]:
    eq, tr = run(variant)
    m = metrics(eq, tr, label)
    rows.append(m)
    out.append("=== %s ===" % label)
    out.append(json.dumps(m, ensure_ascii=False, indent=2))
    buys = [t for t in tr if t["side"] == "买入"]
    out.append("买入笔数=%d 卖出笔数=%d" % (len(buys), len(tr) - len(buys)))
    if tr:
        out.append("前5笔: " + "; ".join("%s %s %s %s@%s" % (t["date"], t["side"], t["name"], t["shares"], t["price"]) for t in tr[:5]))
        out.append("末5笔: " + "; ".join("%s %s %s %s@%s" % (t["date"], t["side"], t["name"], t["shares"], t["price"]) for t in tr[-5:]))
    out.append("")
eq3, tr3 = run_buyhold()
out.append("=== 对照：等权买入持有观察池12只（'选股池本身'的收益）===")
out.append(json.dumps(metrics(eq3, tr3, "等权持有12只"), ensure_ascii=False, indent=2))
out.append("")
orig_fin = dict(FIN)
import random
keys = list(FIN.keys())
# 检验1：基本面分数随机打乱（同分布、错误对应）——若收益崩塌，说明超额来自"已知好公司"
random.seed(42); vals = list(FIN.values()); random.shuffle(vals)
FIN.clear(); FIN.update(dict(zip(keys, vals)))
eq5, tr5 = run("composite")
out.append("=== 前视偏差检验A：基本面分数随机打乱（同分布/错误对应）===")
out.append(json.dumps(metrics(eq5, tr5, "打乱基本面"), ensure_ascii=False, indent=2))
out.append("")
# 检验2：基本面统一60（完全中性）
FIN.clear(); FIN.update({k: 60.0 for k in keys})
eq6, tr6 = run("composite")
out.append("=== 前视偏差检验B：基本面统一60分（中性）===")
out.append(json.dumps(metrics(eq6, tr6, "基本面中性60"), ensure_ascii=False, indent=2))
out.append("")
FIN.clear(); FIN.update(orig_fin)
eq4, tr4 = run_norebuy()
out.append("=== 改进实验：线上策略 + 卖出后5个交易日内不回购 ===")
out.append(json.dumps(metrics(eq4, tr4, "线上策略+5日不回购"), ensure_ascii=False, indent=2))
out.append("")
def run_exit(sell_mode, start_i=START_IDX):
    """离场规则实验：fixed=固定+9%/-8%；ma20=跌破20日均线离场；trail=从最高回撤12%离场"""
    cash = float(ST["initial_capital"]); pos = {}; pending = []; peak = {}
    equity, trades = [], []
    for i in range(start_i, len(DATES)):
        d = DATES[i]
        for od in pending:
            code = od["code"]; df = DATA[code]; row = df[df["date"] == d]
            if row.empty: continue
            px = float(row["open"].iloc[0]) or float(row["close"].iloc[0]); sh = od["shares"]; amt = px * sh
            if od["side"] == "买入":
                if amt > cash:
                    sh = int(cash / px // 100) * 100; amt = px * sh
                    if sh < 100: continue
                cash -= amt + amt * COMM
                p_ = pos.setdefault(code, {"shares": 0, "cost": 0.0, "name": od["name"]})
                p_["cost"] = (p_["cost"] * p_["shares"] + amt) / (p_["shares"] + sh); p_["shares"] += sh
                peak[code] = max(peak.get(code, 0), px)
            else:
                if code not in pos: continue
                sh = min(sh, pos[code]["shares"])
                cash += px * sh - px * sh * (COMM + STAMP)
                pos[code]["shares"] -= sh
                if pos[code]["shares"] == 0: pos.pop(code); peak.pop(code, None)
            trades.append({"date": d, "code": code, "name": od["name"], "side": od["side"],
                           "price": round(px, 3), "shares": sh, "reason": od["reason"]})
        pending = []
        prices, scores, ma20 = {}, {}, {}
        for u in UNIVERSE:
            code = u["code"]; sub = DATA[code]; sub = sub[sub["date"] <= d]
            if len(sub) < 70: continue
            prices[code] = float(sub["close"].iloc[-1]); ma20[code] = float(sub["close"].tail(20).mean())
            sc, ind, sig, judge = tech_score(sub)
            if sc is None: continue
            scores[code] = {"composite": round(0.5 * FIN[code] + 0.5 * sc, 1), "tech": sc, "ind": ind,
                            "close": prices[code], "kind": u["kind"]}
            if code in pos: peak[code] = max(peak.get(code, prices[code]), prices[code])
        if not prices: continue
        total = cash + sum(pos[c]["shares"] * prices.get(c, pos[c]["cost"]) for c in pos)
        equity.append({"date": d, "total": round(total, 2), "cash": round(cash, 2)})
        pool = sorted([c for c in scores], key=lambda c: -scores[c]["composite"])[: ST["max_candidates"]]
        proposed = []
        for c in list(pos):
            px = prices.get(c); p_ = pos[c]
            if not px or not p_["cost"]: continue
            pnl = px / p_["cost"] - 1
            sell = False; why = ""
            if pnl <= -ST["stop_loss_pct"]:
                sell, why = True, "止损"
            elif sell_mode == "fixed" and pnl >= ST["take_profit_pct"]:
                sell, why = True, "止盈+9%"
            elif sell_mode == "ma20" and c in ma20 and px < ma20[c]:
                sell, why = True, "跌破20日线"
            elif sell_mode == "trail" and peak.get(c, px) and px / peak[c] - 1 <= -0.12:
                sell, why = True, "高点回撤12%"
            if not sell and scores.get(c, {}).get("ind", {}).get("macd_dead") and scores[c]["tech"] < 45:
                sell, why = True, "MACD死叉+技术<45"
            if sell:
                proposed.append({"code": c, "name": p_["name"], "side": "卖出", "shares": p_["shares"] // 100 * 100, "reason": why})
        sells = sum(prices.get(x["code"], 0) * x["shares"] for x in proposed)
        cash_now = cash + sells * (1 - COMM - STAMP)
        new_buys = 0; first_build = len(pos) == 0
        order = ([c for c in pool if scores[c]["kind"] == "etf"] + [c for c in pool if scores[c]["kind"] != "etf"]
                 if first_build else pool)
        freq_due = False
        for c in order:
            if new_buys >= ST["max_batch_build"]: break
            s = scores[c]
            if s["composite"] < ST["min_buy_score"] and not first_build and not freq_due: continue
            cur = pos.get(c); w_now = (cur["shares"] * s["close"] / total) if cur else 0.0
            budget = min(total * ST["target_single_weight"] - w_now * total, cash_now - total * ST["min_cash_pct"])
            if budget < total * 0.05: continue
            sh = int(budget / s["close"] // 100) * 100
            if sh < 100: continue
            amt = sh * s["close"]
            if (w_now * total + amt) / total > ST["max_single_position_pct"] + 1e-9: continue
            if cash_now - amt < total * ST["min_cash_pct"]: continue
            proposed.append({"code": c, "name": BY_CODE[c]["name"], "side": "买入", "shares": sh, "reason": "评分达标"})
            cash_now -= amt; new_buys += 1
        pending = proposed
    return pd.DataFrame(equity), trades

FIN.clear(); FIN.update({k: 60.0 for k in keys})   # 无偏：基本面中性
out.append("########## 以下为无偏对照（基本面统一60分，剔除前视偏差）##########")
for mode, label in [("fixed", "无偏-固定止盈9%/止损8%"), ("ma20", "无偏-跌破20日线离场"), ("trail", "无偏-高点回撤12%离场")]:
    eqe, tre = run_exit(mode)
    out.append("=== %s ===" % label)
    out.append(json.dumps(metrics(eqe, tre, label), ensure_ascii=False, indent=2))
    out.append("")
# 无偏：不交易（纯持有510300 ETF 对照已在上方给出）
FIN.clear(); FIN.update(orig_fin)
with open(os.path.join(PROJ, "data", "backtest_result.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("DONE")
