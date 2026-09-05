# -*- coding: utf-8 -*-
"""基本面分析：基于F10主要财务指标形成评分与文字判断（回答'这些数据说明了什么'）。"""
from em_client import main_fin_data, secucode


def analyze(secid, kind="stock") -> dict:
    """返回 {fin_rows, score, judgment, reasons, risks}；ETF无财务数据时按指数型处理。"""
    if kind == "etf":
        return {
            "fin_rows": [], "score": 60.0,
            "judgment": "指数型ETF，跟踪指数成分整体基本面，个股财务风险分散",
            "reasons": ["跟踪宽基/行业指数，天然分散个股业绩风险", "费用低、流动性好，适合作为组合底仓"],
            "risks": ["跟踪标的指数系统性下行风险", "行业ETF受行业景气度波动影响较大"],
        }
    rows = main_fin_data(secid, periods=8)
    if not rows:
        return {"fin_rows": [], "score": 50.0, "judgment": "暂无财务数据", "reasons": [], "risks": ["财务数据缺失，无法评估基本面"]}
    latest = rows[0]
    score, reasons, risks = 50.0, [], []
    roe = latest.get("roe")
    if roe is not None:
        if roe >= 15:
            score += 15; reasons.append(f"加权ROE={roe:.2f}%（≥15%），盈利能力优秀，长期股东回报有保障")
        elif roe >= 8:
            score += 8; reasons.append(f"加权ROE={roe:.2f}%，盈利能力良好")
        elif roe > 0:
            score += 2; reasons.append(f"加权ROE={roe:.2f}%，盈利能力一般")
        else:
            score -= 10; risks.append(f"加权ROE={roe:.2f}%，盈利能力偏弱")
    rev = latest.get("revenue_yoy")
    if rev is not None:
        if rev >= 10:
            score += 12; reasons.append(f"营收同比+{rev:.2f}%，成长性较好")
        elif rev >= 0:
            score += 6; reasons.append(f"营收同比+{rev:.2f}%，保持正增长")
        else:
            score -= 6; risks.append(f"营收同比{rev:.2f}%，收入端承压")
    profit = latest.get("net_profit_yoy")
    if profit is not None:
        if profit >= 10:
            score += 12; reasons.append(f"归母净利润同比+{profit:.2f}%，利润端表现强劲")
        elif profit >= 0:
            score += 6; reasons.append(f"归母净利润同比+{profit:.2f}%，利润平稳")
        else:
            score -= 6; risks.append(f"归母净利润同比{profit:.2f}%，利润下滑需警惕")
    margin = latest.get("net_margin")
    if margin is not None:
        if margin >= 20:
            score += 8; reasons.append(f"销售净利率={margin:.2f}%，产品竞争力强")
        elif margin < 5:
            score -= 3; risks.append(f"销售净利率={margin:.2f}%，盈利空间薄")
    # 连续性：近4期营收是否持续正增长
    if len(rows) >= 4:
        ys = [r.get("revenue_yoy") for r in rows[:4] if r.get("revenue_yoy") is not None]
        if len(ys) == 4 and all(y >= 0 for y in ys):
            score += 5; reasons.append("近4期营收持续正增长，经营稳健")
    risks += [
        "宏观与行业景气度波动风险",
        "财务数据为历史披露数据，存在业绩变脸可能",
    ]
    score = min(max(score, 0), 100)
    judgment = "基本面优秀，具备中长期配置价值" if score >= 70 else ("基本面尚可，可少量配置" if score >= 55 else "基本面偏弱，暂不纳入重点配置")
    return {"fin_rows": rows, "score": score, "judgment": judgment, "reasons": reasons, "risks": risks}


def fmt_revenue(v):
    if v is None:
        return "—"
    if v >= 1e8:
        return f"{v/1e8:.2f}亿元"
    return f"{v/1e4:.2f}万元"


if __name__ == "__main__":
    import json
    r = analyze("1.600519")
    print(json.dumps({k: r[k] for k in ("score", "judgment")}, ensure_ascii=False))
    for x in r["reasons"]:
        print(" -", x)
