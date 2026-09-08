# -*- coding: utf-8 -*-
"""账户与交易账本：trades.csv 是唯一事实来源，账户状态由重放交易流水得出。"""
import csv
import os
import threading

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
TRADES_CSV = os.path.join(DATA_DIR, "trades.csv")
LOCK = threading.Lock()

TRADE_FIELDS = ["date", "code", "name", "side", "price", "shares", "amount",
                "decision_id", "source", "note"]


def ensure_trades_file():
    if not os.path.exists(TRADES_CSV):
        with open(TRADES_CSV, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(TRADE_FIELDS)


def read_trades():
    ensure_trades_file()
    out = []
    with open(TRADES_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if not row.get("date"):
                continue
            out.append({
                "date": row["date"], "code": row["code"], "name": row["name"],
                "side": row["side"], "price": float(row["price"]),
                "shares": int(row["shares"]), "amount": round(float(row["amount"]), 2),
                "decision_id": row.get("decision_id", ""),
                "source": row.get("source", "manual"),
                "note": row.get("note", ""),
            })
    out.sort(key=lambda x: (x["date"], x["code"], x["side"]))
    return out


def append_trade(date, code, name, side, price, shares, decision_id="", source="auto", note=""):
    """追加一笔成交（100股整数倍校验由决策引擎完成）。"""
    with LOCK:
        ensure_trades_file()
        with open(TRADES_CSV, "a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow([date, code, name, side, price, shares,
                                    round(price * shares, 2), decision_id, source, note])


def write_trades(trades):
    """整体写回账本（用于人工回填实际成交价等修正；调用方保证行内字段完整）。"""
    with LOCK:
        with open(TRADES_CSV, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(TRADE_FIELDS)
            for t in sorted(trades, key=lambda x: (x["date"], x["code"], x["side"])):
                w.writerow([t["date"], t["code"], t["name"], t["side"], t["price"], t["shares"],
                            round(float(t.get("amount", t["price"] * t["shares"])), 2),
                            t.get("decision_id", ""), t.get("source", "manual"), t.get("note", "")])
    return TRADES_CSV


def replay(trades, initial_capital):
    """重放交易流水 -> {cash, positions{code:{shares,cost}}, realized_by_code, fee}"""
    cash = float(initial_capital)
    positions = {}
    realized = {}
    buy_total, sell_total = 0.0, 0.0
    for t in trades:
        amt = t["price"] * t["shares"]
        pos = positions.setdefault(t["code"], {"shares": 0, "cost": 0.0, "name": t["name"]})
        if t["side"] == "买入":
            buy_total += amt
            total_cost = pos["cost"] * pos["shares"] + amt
            pos["shares"] += t["shares"]
            pos["cost"] = total_cost / pos["shares"] if pos["shares"] else 0.0
            cash -= amt
        else:  # 卖出
            if pos["shares"] <= 0:
                continue  # 无持仓卖出（T+1或重复），跳过
            shares = min(t["shares"], pos["shares"])
            cash += shares * t["price"]
            cost = pos["cost"] * shares
            pnl = shares * t["price"] - cost
            realized[t["code"]] = realized.get(t["code"], 0.0) + pnl
            pos["shares"] -= shares
            sell_total += shares * t["price"]
            if pos["shares"] == 0:
                pos["cost"] = 0.0
    positions = {k: v for k, v in positions.items() if v["shares"] > 0}
    # 简化费用模型：佣金万2.5双边 + 印花税0.05%单边（卖出）
    fee = round(buy_total * 0.00025 + sell_total * (0.00025 + 0.0005), 2)
    return {"cash": round(cash, 2), "positions": positions,
            "realized": realized, "fee": fee,
            "buy_total": round(buy_total, 2), "sell_total": round(sell_total, 2)}


def market_value(acct, prices):
    mv = 0.0
    for code, pos in acct["positions"].items():
        p = prices.get(code, pos["cost"])
        mv += p * pos["shares"]
    return round(mv, 2)


def total_assets(acct, prices):
    return round(acct["cash"] + market_value(acct, prices), 2)


def unrealized(acct, prices):
    out = {}
    for code, pos in acct["positions"].items():
        p = prices.get(code, pos["cost"])
        out[code] = round((p - pos["cost"]) * pos["shares"], 2)
    return out


def last_trade_date(trades, before_date=None):
    ds = [t["date"] for t in trades if before_date is None or t["date"] < before_date]
    return max(ds) if ds else None
