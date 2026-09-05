# -*- coding: utf-8 -*-
"""候选股票池筛选：对观察池逐只做基本面+技术面打分，选出前N只构成候选池。"""
import indicators
from em_client import kline_until, snapshot
from fundamental import analyze


def screen(universe, trade_date, min_candidates=6, max_candidates=8, min_buy_score=60):
    """返回 {pool: [按评分排序的候选记录], all: 全部评分明细}"""
    all_r, pool = [], []
    for u in universe:
        secid = u["secid"]
        try:
            df = kline_until(secid, trade_date)
            if len(df) < 70:
                continue
            ind = indicators.compute(df)
            sig, judge, tech_score = indicators.signal_and_judgment(ind)
            fin = analyze(secid, u["kind"])
            comp = 0.5 * fin["score"] + 0.5 * tech_score
            rec = {
                "code": u["code"], "name": u["name"], "kind": u["kind"],
                "industry": u["industry"], "secid": secid,
                "date": ind["date"], "close": ind["close"],
                "pe_ttm": None, "pb": None,
                "fin_score": round(fin["score"], 1), "tech_score": round(tech_score, 1),
                "composite": round(comp, 1),
                "tech_signal": sig, "tech_judgment": judge,
                "fin_judgment": fin["judgment"], "fin_reasons": fin["reasons"], "risks": fin["risks"],
                "support_60d": ind.get("support_60d"), "resist_60d": ind.get("resist_60d"),
                "rsi14": ind.get("rsi14"), "ma20": ind.get("ma20"), "ma60": ind.get("ma60"),
                "bull_align": ind.get("bull_align"), "macd_golden": ind.get("macd_golden"),
                "macd_dead": ind.get("macd_dead"),
                "include_reason": "",
                "info_source": "东方财富公开行情接口（日K线、F10财务指标）；同花顺APP核对",
            }
            try:
                snap = snapshot(secid)
                rec["pe_ttm"] = snap.get("pe_ttm") if u["kind"] == "stock" else None
                rec["pb"] = snap.get("pb")
            except Exception:
                pass
            all_r.append(rec)
        except Exception as e:
            all_r.append({"code": u["code"], "name": u["name"], "error": str(e)})
    ok = [r for r in all_r if "composite" in r]
    ok.sort(key=lambda x: x["composite"], reverse=True)
    pool = ok[:max_candidates]
    # 任务书要求组合含ETF底仓：保证至少1只ETF进入候选池
    if not any(r.get("kind") == "etf" for r in pool):
        etfs = [r for r in ok if r.get("kind") == "etf"]
        if etfs:
            pool = pool[:-1] + [etfs[0]]
            pool.sort(key=lambda x: x["composite"], reverse=True)
    out_pool = []
    for r in pool:
        r_in = dict(r)
        r_in["include_reason"] = (
            f"综合评分{r['composite']}分（基本面{r['fin_score']}/技术面{r['tech_score']}），"
            f"{'技术面' + r['tech_judgment']}; 基本面判断: " + r["fin_judgment"]
        )
        out_pool.append(r_in)
    pool_codes = {r["code"] for r in out_pool}
    for r in all_r:
        if "composite" not in r:
            r["eliminate_reason"] = "DATA_UNAVAILABLE：行情/财务数据缺失，无法评分"
        elif r["code"] not in pool_codes:
            etf_kept = r.get("kind") == "etf" and any(x.get("kind") == "etf" for x in out_pool)
            if etf_kept:
                r["eliminate_reason"] = f"综合评分{r['composite']}分，已有更高评分ETF入池"
            else:
                r["eliminate_reason"] = f"综合评分{r['composite']}分，未进入前{max_candidates}名"
    return {"pool": out_pool, "all": all_r, "date": trade_date}


if __name__ == "__main__":
    import json, os
    cfg = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"), encoding="utf-8"))
    res = screen(cfg["universe"], "2026-09-04", cfg["strategy"]["min_candidates"], cfg["strategy"]["max_candidates"])
    for r in res["pool"]:
        print(r["code"], r["name"], r["composite"], r["fin_score"], r["tech_score"], "|", r["tech_judgment"])
