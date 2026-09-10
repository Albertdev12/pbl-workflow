# -*- coding: utf-8 -*-
"""持仓判断与调仓执行单（规则驱动，可解释，不依赖 AI 密钥）。

被两处复用：
  - cloud_run.export_dashboard → 写入 data/dashboard/holdings.json 供手机端展示
  - ai_advisor.collect        → 放进 DeepSeek 的上下文，请模型复核这份执行单
"""
import json

import indicators
from em_client import kline, main_fin_data, secid_of

ORDER = {"止损": 0, "减仓": 1, "止盈": 2, "观察": 3, "可加仓": 4, "持有": 5}


def holding_advice(acct, prices, cfg, date, total=None, cash=None):
    """逐只给出 减仓/加仓/持有，并把结论翻译成具体股数的调仓执行单。

    判断顺序（先纪律、后趋势、再基本面）：
      1) 触及止盈/止损线        → 止盈 / 止损
      2) 跌破60日线             → 减仓（中期趋势走坏）
      3) 跌破20日线 / RSI>78    → 观察（短线转弱或超买）
      4) 趋势+基本面双优且有余量 → 可加仓
      5) 其余                   → 持有
    宽基底仓特例：ETF 跌破均线但权重低于 etf_target_weight 时降级为「观察」，
    避免在欠配状态下按趋势信号卖在低点。
    """
    import portfolio as pf
    st = cfg["strategy"]
    if total is None:
        total = pf.total_assets(acct, prices)
    if cash is None:
        cash = acct["cash"]
    cash_pct = (cash / total * 100) if total else 0
    low_cash = cash_pct < st["min_cash_pct"] * 100
    out = []
    for code, pos in acct["positions"].items():
        px = prices.get(code, pos["cost"])
        pnl = (px / pos["cost"] - 1) * 100 if pos["cost"] else 0
        weight = (px * pos["shares"] / total * 100) if total else 0
        ind, fin, tech_date = {}, {}, None
        try:
            # 持仓只有几只：用 cache=True，取不到时能回退本地CSV缓存，比"直接失败"可靠得多
            kdf = kline(secid_of(code))
            tech_date = str(kdf["date"].iloc[-1])
            ind = indicators.compute(kdf)
        except Exception:
            ind = {}
        if code.startswith(("60", "00", "30")):  # ETF 无财务指标
            try:
                f = main_fin_data(secid_of(code))
                fin = f[0] if f else {}
            except Exception:
                pass
        ma20, ma60, rsi = ind.get("ma20"), ind.get("ma60"), ind.get("rsi14")
        tech_ok = bool(ma20 and ma60)
        kind = next((u.get("kind") for u in cfg["universe"] if u["code"] == code), "stock")
        reasons, action, stalled, base_hold = [], "持有", False, False
        if not tech_ok:
            # 技术面缺失时必须说清楚，否则会安静地给出偏乐观的判断
            reasons.append("⚠ 技术面数据缺失（均线取不到），本只仅按纪律与基本面判断")
        if pnl >= st["take_profit_pct"] * 100:
            action = "止盈"
            reasons.append(f"浮盈{pnl:.1f}%已达止盈线{st['take_profit_pct']*100:.0f}%")
        elif pnl <= -st["stop_loss_pct"] * 100:
            action = "止损"
            reasons.append(f"浮亏{pnl:.1f}%已达止损线-{st['stop_loss_pct']*100:.0f}%")
        else:
            if ma60 and px < ma60:
                if kind == "etf" and weight < st.get("etf_target_weight", 0.30) * 100:
                    action = "观察"
                    base_hold = True   # 底仓且欠配 → 不参与减持（见 rebalance）
                    reasons.append(f"跌破60日线{ma60:.2f}，但底仓目标权重"
                                   f"{st.get('etf_target_weight', 0.30)*100:.0f}%、当前仅"
                                   f"{weight:.1f}%属欠配，维持配置")
                else:
                    action = "减仓"
                    reasons.append(f"现价{px}跌破60日线{ma60:.2f}，中期趋势走坏")
            elif ma20 and px < ma20:
                action = "观察"
                reasons.append(f"现价{px}跌破20日线{ma20:.2f}，短线转弱不加仓")
            if rsi and rsi > 78 and action == "持有":
                action = "观察"
                reasons.append(f"RSI={rsi:.0f}超买，短期回调风险")
            if ind.get("bull_align"):
                reasons.append("均线多头排列")
            elif ind.get("above_ma60"):
                reasons.append("站上60日线")
            ny, ry = fin.get("net_profit_yoy"), fin.get("revenue_yoy")
            roe = fin.get("roe")
            if ny is not None and ry is not None:
                if ny < 0 and ry < 0 and action in ("持有", "观察"):
                    action = "减仓"
                    reasons.append(f"营收/净利双降（{ry:.0f}%/{ny:.0f}%），基本面恶化")
                elif ny < 5 and ry < 5:
                    stalled = True
                    reasons.append(f"增长停滞（营收{ry:.1f}%、净利{ny:.1f}%）")
                else:
                    reasons.append(f"ROE {roe:.1f}%、净利同比{ny:+.0f}%、营收同比{ry:+.0f}%")
            cap_ok = weight + 5 <= st["max_single_position_pct"] * 100 * 0.9
            if (action == "持有" and ma20 and ma60 and px > ma20 and ma20 > ma60
                    and rsi and 40 <= rsi <= 70 and cap_ok and not stalled
                    and (roe or 0) >= 6 and (ny or 0) >= 10):
                action = "可加仓"
                reasons.append(f"仓位{weight:.1f}%距上限仍有空间"
                               + ("；但现金不足，需先减仓腾出资金" if low_cash else ""))
            if not reasons:
                reasons.append("无明确信号")
        out.append({"code": code, "name": pos.get("name", code), "shares": pos["shares"],
                    "price": px, "cost": round(pos["cost"], 3), "pnl_pct": round(pnl, 2),
                    "weight": round(weight, 1), "action": action, "reasons": reasons,
                    "ma20": round(ma20, 3) if ma20 else None,
                    "ma60": round(ma60, 3) if ma60 else None,
                    "rsi": round(rsi, 1) if rsi else None,
                    "roe": roe, "np_yoy": ny, "stalled": stalled, "base_hold": base_hold,
                    "tech_ok": tech_ok, "tech_date": tech_date})
    out.sort(key=lambda r: (ORDER.get(r["action"], 9), -r["weight"]))

    hint = None
    if low_cash:
        pool_ = [r for r in out if r["action"] in ("持有", "可加仓") and r["np_yoy"] is not None]
        if not pool_:
            pool_ = [r for r in out if r["action"] == "持有"]
        if pool_:
            t = min(pool_, key=lambda r: (0 if r["stalled"] else 1, r["np_yoy"] or 0))
            need = total * st["min_cash_pct"] - cash
            hint = (f"现金占比{cash_pct:.1f}%低于下限{st['min_cash_pct']*100:.0f}%，"
                    f"回到下限需腾出约{need/10000:.1f}万元。"
                    f"按「趋势未坏但基本面最弱」筛选，优先减持【{t['name']}】"
                    f"（{'增长停滞：' if t['stalled'] else '净利增速最低：'}"
                    f"净利同比{t['np_yoy']:+.1f}%）；减持后现金即可回到纪律区间。")
    plan = rebalance(out, cfg, cash, total)
    bad = [r["name"] for r in out if not r.get("tech_ok")]
    warn = None
    if bad:
        warn = ("⚠ 以下持仓本次取不到日K/均线，判断已降级（仅按纪律与基本面）："
                + "、".join(bad) + "。原因通常是行情源限流，下次运行会自动恢复。")
    return {"date": date, "cash_pct": round(cash_pct, 1), "cash": round(cash, 2),
            "total": total, "low_cash": low_cash, "reduce_hint": hint,
            "data_warning": warn, "tech_ok_count": len(out) - len(bad), "n_positions": len(out),
            "min_cash_pct": st["min_cash_pct"] * 100, "rows": out, "plan": plan}


def rebalance(rows, cfg, cash, total):
    """把"该减/该加"翻译成**具体股数**：先卖后买，最终现金回到策略下限。

    卖出优先级（从最该卖的排起）：基本面停滞/恶化 → 跌破60日线 → 跌破20日线 → 权重最高。
    只按需要卖：目标现金 = 总资产 × min_cash_pct，再加回买入所需金额。
    买入：把「可加仓」标的补到 target_add_pct（默认4%），且不超过单只上限。
    """
    st = cfg["strategy"]
    if not total:
        return None
    target_add = float(cfg.get("ai", {}).get("target_add_pct", 4)) / 100

    buys = []
    for r in rows:
        if r["action"] != "可加仓":
            continue
        cur = r["price"] * r["shares"]
        need_sh = int((total * target_add - cur) / r["price"] // 100) * 100
        cap_sh = int((total * st["max_single_position_pct"] - cur) / r["price"] // 100) * 100
        need_sh = min(need_sh, max(cap_sh, 0))
        if need_sh >= 100:
            buys.append({"code": r["code"], "name": r["name"], "shares": need_sh,
                         "amount": round(need_sh * r["price"], 2), "price": r["price"],
                         "weight_before": r["weight"],
                         "weight_after": round((cur + need_sh * r["price"]) / total * 100, 1),
                         "reason": "趋势与基本面双优且当前严重欠配"})
    buy_amt = sum(b["amount"] for b in buys)

    target_cash = total * st["min_cash_pct"]
    need = target_cash + buy_amt - cash

    def sell_key(r):
        # 底仓欠配特例：排在最后，尽量不动（与"观察-维持配置"的判断保持一致）
        if r.get("base_hold"):
            return (9, 9, 9, 0)
        return (0 if r.get("stalled") else 1,
                0 if (r["ma60"] and r["price"] < r["ma60"]) else 1,
                0 if (r["ma20"] and r["price"] < r["ma20"]) else 1,
                (r["np_yoy"] if r["np_yoy"] is not None else 0))

    sells, capped = [], []
    min_trade = max(total * 0.01, 10000)   # 小于总资产1%（且不足1万）的调仓没有意义，不做
    for r in sorted(rows, key=sell_key):
        if need <= 0:
            break
        if r["weight"] < 3:            # 尘埃仓位不参与调仓
            continue
        if r["action"] == "可加仓":     # 要加仓的标的绝不卖
            continue
        val = r["price"] * r["shares"]
        if need < min_trade:           # 剩余缺口太小 → 停在上一笔，避免噪音交易
            break
        # 单只最多减半，避免为了凑现金把好仓位砍残；基本面停滞的可清仓
        cap_sh = r["shares"] if r.get("stalled") else int(r["shares"] * 0.5 // 100) * 100
        take = min(val, need, cap_sh * r["price"])
        sh = int(take / r["price"] // 100) * 100
        if val - sh * r["price"] < r["price"] * 100 and sh < cap_sh:  # 剩不到1手就一次清掉
            sh = cap_sh
        if sh < 100:
            continue
        sh = min(sh, r["shares"], cap_sh)
        amt = round(sh * r["price"], 2)
        if amt < min_trade and need >= min_trade:
            continue
        why = []
        if r.get("stalled"):
            why.append("基本面增长停滞")
        if r["ma60"] and r["price"] < r["ma60"]:
            why.append("跌破60日线")
        elif r["ma20"] and r["price"] < r["ma20"]:
            why.append("跌破20日线")
        if not why:
            why.append("为补足现金纪律")
        if r.get("base_hold"):
            why.append("（底仓，最后才动）")
        if sh >= cap_sh and not r.get("stalled"):
            why.append("已减半、不再多减")
        sells.append({"code": r["code"], "name": r["name"], "shares": sh, "amount": amt,
                      "price": r["price"], "weight_before": r["weight"],
                      "weight_after": round((val - amt) / total * 100, 1),
                      "reason": "、".join(why)})
        need -= amt

    sell_amt = sum(s["amount"] for s in sells)
    cash_after = cash + sell_amt - buy_amt
    short = cash_after / total * 100 < st["min_cash_pct"] * 100 - 0.5  # 容差0.5个百分点
    note = []
    if sells and buys:
        note.append("先卖后买：卖出到账后再买入，避免资金不足交割。")
    if short:
        note.append(f"受「底仓不动、单只最多减半、不做万元以下调仓」约束，"
                    f"现金只能到{cash_after/total*100:.1f}%（目标{st['min_cash_pct']*100:.0f}%）；"
                    f"若要严格执行，需再减持或分几日完成。")
    elif sells:
        note.append("已达现金下限要求。")
    return {"sells": sells, "buys": buys,
            "sell_amount": round(sell_amt, 2), "buy_amount": round(buy_amt, 2),
            "cash_before": round(cash, 2), "cash_after": round(cash_after, 2),
            "cash_pct_before": round(cash / total * 100, 1),
            "cash_pct_after": round(cash_after / total * 100, 1),
            "target_cash_pct": round(st["min_cash_pct"] * 100, 1),
            "target_met": not short, "note": "".join(note)}


def plan_brief(plan, limit=6):
    """给 AI 看的紧凑版执行单（省 token）。"""
    if not plan:
        return {}
    return {"现金": f"{plan['cash_pct_before']}%→{plan['cash_pct_after']}%",
            "卖出": [{"代码": s["code"], "名称": s["name"], "股数": s["shares"],
                      "金额": s["amount"], "权重": f"{s['weight_before']}%→{s['weight_after']}%",
                      "原因": s["reason"]} for s in plan["sells"][:limit]],
            "买入": [{"代码": b["code"], "名称": b["name"], "股数": b["shares"],
                      "金额": b["amount"], "权重": f"{b['weight_before']}%→{b['weight_after']}%",
                      "原因": b["reason"]} for b in plan["buys"][:limit]]}
