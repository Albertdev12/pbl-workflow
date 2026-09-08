# -*- coding: utf-8 -*-
"""投资决策引擎：指标→信号→判断→投资决策。

流程（采纳GPT规范）：先由策略层产出拟交易(proposed)，再经规则引擎 risk_check 前置校验，
RED/BLOCK_TRADE 的拟交易不会成为可执行决策，仅留审计痕迹。所有决策的人工判断状态初始为 PENDING。
"""
import datetime as dt

import numpy as np

import indicators
import portfolio as pf
import records
import risk as risk_engine
from em_client import kline_until


def _business_days_between(d1, d2):
    try:
        a = dt.date.fromisoformat(d1)
        b = dt.date.fromisoformat(d2)
        return int(np.busday_count(a, b))
    except Exception:
        return 0


def _close_price(code, secid, date):
    df = kline_until(secid, date)
    return float(df["close"].iloc[-1]) if len(df) else None


def _round_shares(amount, price):
    if price is None or price <= 0:
        return 0
    return int(amount / price // 100) * 100


def _market_basis(date):
    for r in records.read_market_watch():
        if r.get("date") == date:
            return f"{r['view']}；机会：{r['opportunity']}"
    return "DATA_UNAVAILABLE"


def _peak_since_open(code, secid, date, trades):
    """当前持仓期间的最高价（移动止损用）。建仓日 = 该标的 shares 由 0 变正的那天。"""
    shares, open_date = 0, None
    for t in trades:
        if t["code"] != code:
            continue
        if t["side"] == "买入":
            if shares == 0:
                open_date = t["date"]
            shares += t["shares"]
        else:
            shares -= t["shares"]
            if shares <= 0:
                shares, open_date = 0, None
    if not open_date:
        return None
    df = kline_until(secid, date)
    df = df[df["date"] >= open_date]
    return float(df["high"].max()) if len(df) else None


def run_decision(date, pool, cfg, mode=None):
    """对 date（收盘后）生成决策。返回 decisions 列表。"""
    st = cfg["strategy"]
    mode = mode or st.get("trade_execution", "auto")
    universe = {u["code"]: u for u in cfg["universe"]}
    trades = pf.read_trades()
    acct = pf.replay(trades, st["initial_capital"])

    # ---- 最新价格与候选信息（以 date 收盘为准）----
    prices, pool_by_code = {}, {}
    for r in pool:
        prices[r["code"]] = r["close"]
        pool_by_code[r["code"]] = r
    for code in list(acct["positions"].keys()):
        if code not in prices and code in universe:
            p = _close_price(code, universe[code]["secid"], date)
            if p:
                prices[code] = p
    total = pf.total_assets(acct, prices)
    market_txt = _market_basis(date)

    # ================= 第一层：策略层产出拟交易 =================
    proposed = []

    # 1) 持仓风控：止盈 / 止损 / 技术面恶化
    for code, pos in list(acct["positions"].items()):
        price = prices.get(code)
        if not price:
            continue
        pnl_pct = price / pos["cost"] - 1 if pos["cost"] else 0.0
        reason = basis_tech = None
        action = None
        trail = st.get("trailing_stop_pct")
        peak = _peak_since_open(code, universe.get(code, {}).get("secid", code), date, trades) if trail else None
        if pnl_pct <= -st["stop_loss_pct"]:
            action, reason = "卖出", f"止损触发：浮亏{pnl_pct*100:.1f}% ≤ 止损线-{st['stop_loss_pct']*100:.0f}%"
        elif trail and peak and price <= peak * (1 - trail):
            action, reason = "卖出", (f"移动止损触发：自持仓最高价{peak:.2f}回撤{(1 - price / peak) * 100:.1f}% "
                                      f"≥ {trail*100:.0f}%（当前浮盈{pnl_pct*100:+.1f}%）")
        elif not trail and pnl_pct >= st["take_profit_pct"]:
            action, reason = "卖出", f"止盈触发：浮盈{pnl_pct*100:.1f}% ≥ 止盈线{st['take_profit_pct']*100:.0f}%"
        else:
            u = universe.get(code, {})
            df = kline_until(u.get("secid", code), date)
            if len(df) >= 70:
                ind = indicators.compute(df)
                sig, judge, tech_score = indicators.signal_and_judgment(ind)
                if ind.get("macd_dead") and tech_score < 45:
                    action = "卖出"
                    reason = f"技术面恶化：MACD死叉且技术评分{tech_score:.0f}分"
                    basis_tech = sig
        if action == "卖出":
            shares = pos["shares"] // 100 * 100
            if shares <= 0:
                continue
            proposed.append({
                "code": code, "name": pos.get("name", code), "side": "卖出",
                "price": price, "shares": shares,
                "reason": reason,
                "fund_basis": ("持有期内基本面未见恶化，触发为价格纪律"
                               if ("止盈" in (reason or "") or "移动止损" in (reason or "")) else "基本面/价格双重纪律"),
                "tech_basis": basis_tech or f"浮盈{pnl_pct*100:.1f}%触发价格纪律",
                "risk_judge": "锁定收益/防止进一步亏损，执行交易纪律",
            })

    # 2) 开仓：候选池中综合评分达标者（首次建仓时宽基ETF底仓优先）
    sells_amt = sum(p["price"] * p["shares"] for p in proposed if p["side"] == "卖出")
    cash_now = acct["cash"] + sells_amt
    new_buys = 0
    first_build = len(acct["positions"]) == 0
    buy_order = list(pool)
    if first_build:
        etfs = [r for r in pool if r.get("kind") == "etf"]
        rest = [r for r in pool if r.get("kind") != "etf"]
        buy_order = etfs + rest
    for r in buy_order:
        if new_buys >= st["max_batch_build"]:
            break
        code = r["code"]
        if r["composite"] < st["min_buy_score"] and not first_build and not _frequency_due(trades, date, st):
            continue
        if code in [p["code"] for p in proposed if p["side"] == "买入"]:
            continue
        pos = acct["positions"].get(code)
        weight_now = (pos["shares"] * prices.get(code, pos["cost"]) / total) if pos else 0.0
        tgt_w = (st.get("etf_target_weight", st["target_single_weight"])
                 if r.get("kind") == "etf" else st["target_single_weight"])
        target_amt = total * tgt_w
        budget = min(target_amt - weight_now * total, cash_now - total * st["min_cash_pct"])
        if budget < total * 0.05:
            continue
        shares = _round_shares(budget, r["close"])
        if shares < 100:
            continue
        amount = shares * r["close"]
        weight_after = ((weight_now * total) + amount) / total
        cash_after = cash_now - amount
        if weight_after > st["max_single_position_pct"] + 1e-9 or cash_after < total * st["min_cash_pct"]:
            continue
        sig_all = "；".join(r["tech_signal"])
        proposed.append({
            "code": code, "name": r["name"], "side": "买入",
            "price": r["close"], "shares": shares,
            "reason": f"综合评分{r['composite']}分（基本面{r['fin_score']}/技术面{r['tech_score']}）达标，"
                      f"目标仓位{tgt_w*100:.0f}%，买入后占比{weight_after*100:.1f}%",
            "fund_basis": "；".join(r["fin_reasons"][:3]) if r["fin_reasons"] else r["fin_judgment"],
            "tech_basis": f"{r['tech_judgment']}。指标信号：{sig_all}",
            "risk_judge": "；".join(r["risks"][:2]),
        })
        cash_now -= amount
        new_buys += 1

    # 3) 交易频率约束：每两周至少1笔有效交易
    if not proposed and _frequency_due(trades, date, st):
        top = pool[0] if pool else None
        if top:
            budget = min(total * st["target_single_weight"],
                         cash_now - total * st["min_cash_pct"])
            shares = _round_shares(budget, top["close"])
            if shares >= 100:
                proposed.append({
                    "code": top["code"], "name": top["name"], "side": "买入",
                    "price": top["close"], "shares": shares,
                    "reason": f"交易频率约束（每两周至少{st['min_trades_per_2weeks']}笔）触发，选择候选池评分最高标的维持组合运作",
                    "fund_basis": "；".join(top["fin_reasons"][:2]) if top["fin_reasons"] else top["fin_judgment"],
                    "tech_basis": f"{top['tech_judgment']}；{'；'.join(top['tech_signal'][:3])}",
                    "risk_judge": "；".join(top["risks"][:2]),
                })

    # ================= 第二层：规则引擎前置校验 =================
    prices_snapshot = dict(prices)
    decisions, blocked = [], []
    for p in proposed:
        check = risk_engine.risk_check(acct, prices_snapshot, p, st)
        if check["status"] == "RED":
            blocked.append({"proposed": p, "check": check})
            records.save_risk_block(date, p, check)
            continue
        decisions.append({
            "code": p["code"], "name": p["name"], "side": p["side"],
            "price": p["price"], "shares": p["shares"],
            "amount": round(p["price"] * p["shares"], 2),
            "reason": p["reason"],
            "fund_basis": p["fund_basis"],
            "tech_basis": p["tech_basis"],
            "market_basis": market_txt,
            "risk_judge": p["risk_judge"],
            "rule_check": {"status": check["status"],
                           "position_pct_after": check.get("position_pct_after"),
                           "cash_ratio_after": check.get("cash_ratio_after"),
                           "warnings": check["warnings"]},
            "ai_used": "是（数据采集、指标计算与评分由自动化分析流水线完成）",
            "ai_adopted": "采纳系统评分，价格与数量按预设策略确定",
            "human_judgment": "PENDING（待人工确认：按预设策略自动生成，可在同花顺下单执行后于 confirmations.csv 确认）",
        })
    if blocked:
        print(f"  [规则引擎] BLOCK_TRADE {len(blocked)} 笔（已记录审计）："
              + "；".join(b["check"]["violations"][0] for b in blocked))

    # ---- 保存与执行 ----
    decisions = records.save_decisions(date, decisions)
    if mode == "auto":
        for d in decisions:
            pf.append_trade(d["date"], d["code"], d["name"], d["side"], d["price"], d["shares"],
                            decision_id=d["decision_id"], source="auto", note="系统自动成交（模拟账本）")
        records.log_ai_usage(
            prompt=f"对{date}收盘数据执行选股与决策流水线：候选池评分、持仓风控、规则引擎前置校验",
            output=(f"生成{len(decisions)}条决策：" + "；".join(f"{d['side']}{d['name']}({d['code']})" for d in decisions)
                    if decisions else "无操作信号") + (f"；拦截{len(blocked)}笔" if blocked else ""),
            verify="行情与财务数据来自东方财富公开接口，非AI生成数据；指标计算结果可与同花顺APP核对",
            human_judgment="PENDING——按config.json预设策略自动执行，待人工复盘确认",
            final_use="通过规则引擎校验后写入决策记录/交易账本",
        )
    return decisions


def _frequency_due(trades, date, st):
    last = pf.last_trade_date(trades, date)
    if last is None:
        return _business_days_between(st.get("start_date", "2026-09-07"), date) >= 10
    return _business_days_between(last, date) >= 10
