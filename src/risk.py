# -*- coding: utf-8 -*-
"""规则引擎（采纳自GPT规范第六节）：任何交易生成前必须通过 risk_check，违规则 BLOCK_TRADE。

风险状态定义：
  GREEN   —— 全部规则满足，可生成交易计划
  YELLOW  —— 存在警告（不违规），可生成交易计划但需在记录中标注
  RED     —— 存在硬性违规，BLOCK_TRADE，不得生成可执行交易计划
"""
import portfolio as pf

HARD_RULES = {
    "max_single_security": 0.30,   # 任务书：单只证券持仓不得超过总资产30%
    "min_cash_ratio": 0.20,        # 策略预设：现金留存
    "min_shares": 100,             # A股最小交易单位
    "no_same_day_round_trip": True,  # T+1：当日买入不可当日卖出
}


def risk_check(acct, prices, proposed, rules) -> dict:
    """对单笔拟执行交易做前置校验。
    proposed: {code, name, side, price, shares}
    返回 {status, violations, warnings, position_pct_after, cash_ratio_after}
    """
    st = rules
    code, side = proposed["code"], proposed["side"]
    price, shares = proposed["price"], proposed["shares"]
    total = pf.total_assets(acct, prices)
    violations, warnings = [], []

    if total <= 0:
        return {"status": "RED", "violations": ["账户总资产异常(≤0)"], "warnings": []}

    # 模拟持仓（含本笔交易后的状态）
    pos = acct["positions"].get(code, {"shares": 0, "cost": 0.0})
    new_shares = pos["shares"] + shares if side == "买入" else pos["shares"] - shares
    if side == "卖出" and new_shares < 0:
        violations.append(f"卖出数量超过可用持仓（持有{pos['shares']}股）")

    if shares % 100 != 0 or shares < HARD_RULES["min_shares"]:
        violations.append(f"数量{shares}股不是100股整数倍")

    # 单只持仓上限
    prices2 = dict(prices)
    if side == "买入":
        prices2[code] = price
    mv = 0.0
    for c, p in acct["positions"].items():
        mv += prices2.get(c, p["cost"]) * (new_shares if c == code and side == "买入" else p["shares"])
    if side == "卖出":
        mv -= prices.get(code, pos["cost"]) * min(shares, pos["shares"])
    single_pct = (prices2.get(code, pos["cost"]) * new_shares / total) if side == "买入" else 0.0
    if side == "买入" and single_pct > HARD_RULES["max_single_security"] + 1e-9:
        violations.append(f"买入后单只持仓{single_pct*100:.1f}%超过上限{HARD_RULES['max_single_security']*100:.0f}%")

    cash_after = acct["cash"] + (-(price * shares) if side == "买入" else price * shares)
    cash_ratio = cash_after / total
    if cash_ratio < st["min_cash_pct"] - 1e-9:
        violations.append(f"买入后现金占比{cash_ratio*100:.1f}%低于下限{st['min_cash_pct']*100:.0f}%")

    # 止盈止损线提示（YELLOW级）
    if side == "卖出" and pos["cost"]:
        pnl = price / pos["cost"] - 1
        if pnl >= st["take_profit_pct"]:
            warnings.append(f"止盈纪律触发（浮盈{pnl*100:.1f}%≥{st['take_profit_pct']*100:.0f}%）")
        elif pnl <= -st["stop_loss_pct"]:
            warnings.append(f"止损纪律触发（浮亏{pnl*100:.1f}%≤-{st['stop_loss_pct']*100:.0f}%）")

    status = "RED" if violations else ("YELLOW" if warnings else "GREEN")
    return {
        "status": status,
        "violations": violations,
        "warnings": warnings,
        "position_pct_after": round(single_pct, 4) if side == "买入" else None,
        "cash_ratio_after": round(cash_ratio, 4),
    }


def market_risk_state(indices_pct, benchmark_pct_20d_above) -> str:
    """市场环境风险状态（影响开仓激进度，不影响既有持仓纪律）。"""
    chg = indices_pct if indices_pct is not None else 0
    if chg <= -2.5:
        return "RED"
    if chg <= -1.5 or not benchmark_pct_20d_above:
        return "YELLOW"
    return "GREEN"
