# -*- coding: utf-8 -*-
"""自动验收器（采纳自GPT规范第十八节）：核对任务书《项目最终成果清单》与过程要求，输出完成度。"""
import json
import os

import portfolio as pf
import records

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "outputs")

CHECKS = [
    # (检查项, 判定函数)  全部通过 => ready_for_submission
    ("是否完成市场观察记录", lambda: len(records.read_market_watch()) >= 1),
    ("候选池是否为5-10只", lambda: 5 <= len(records.latest_pool()) <= 10),
    ("候选池是否含代码/行业/纳入原因/初步判断/风险/信息来源",
     lambda: all(all(k in r for k in ("code", "industry", "include_reason", "tech_judgment", "risks", "info_source"))
                 for r in records.latest_pool())),
    ("是否有基本面分析记录", lambda: any(r.get("fin_reasons") for r in records.latest_pool())),
    ("是否有技术分析记录", lambda: any(r.get("tech_signal") for r in records.latest_pool())),
    ("是否生成初始投资方案", lambda: _exists("04_初始投资方案.docx")),
    ("是否有投资决策记录", lambda: len(records.read_decisions()) >= 1),
    ("决策记录是否含基本面依据/技术面依据/市场依据/风险判断", lambda: all(
        d.get("fund_basis") and d.get("tech_basis") and d.get("market_basis") and d.get("risk_judge")
        for d in records.read_decisions())),
    ("决策是否全部通过交易前规则校验", lambda: all(
        (d.get("rule_check") or {}).get("status") in ("GREEN", "YELLOW")
        for d in records.read_decisions())),
    ("是否有AI使用记录（Prompt→输出→核验→人工判断→采用）", lambda: all(
        all(k in r for k in ("prompt", "ai_output", "verify", "human_judgment", "final_use"))
        for r in records.read_ai_usage()) and len(records.read_ai_usage()) >= 1),
    ("人工判断是否已确认（confirmations.csv）", lambda: _confirmed_all()),
    ("是否生成交易日志与盈亏归因", lambda: _exists("09_交易日志与盈亏归因.xlsx")),
    ("是否生成策略调整记录", lambda: _exists("10_策略调整记录.docx")),
    ("是否生成中期路演PPT", lambda: _exists("12_中期路演.pptx")),
    ("是否生成投资总结报告", lambda: _exists("13_投资总结报告.docx")),
    ("是否生成市场观察/候选池/技术分析/AI记录表格", lambda: all(
        _exists(f) for f in ("02_市场观察记录.xlsx", "03_候选股票池及初步分析表.xlsx",
                             "06_技术分析记录.xlsx", "11_AI使用记录.xlsx"))),
    ("每两周至少1笔有效交易（首月内）", lambda: _trade_frequency_ok()),
    ("单只持仓未超过30%", lambda: _position_limit_ok()),
]


def _exists(name):
    return os.path.exists(os.path.join(OUT, name))


def _confirmed_all():
    conf = records.read_confirmations()
    return all(d["decision_id"] in conf for d in records.read_decisions()) and len(conf) >= 1


def _trade_frequency_ok():
    import datetime as dt
    import numpy as np
    trades = pf.read_trades()
    if not trades:
        return False
    ds = sorted({t["date"] for t in trades})
    for a, b in zip(ds, ds[1:]):
        if int(np.busday_count(dt.date.fromisoformat(a), dt.date.fromisoformat(b))) > 10:
            return False
    return True


def _position_limit_ok():
    cfg = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8"))
    acct = pf.replay(pf.read_trades(), cfg["strategy"]["initial_capital"])
    if not acct["positions"]:
        return True
    total = sum(p["cost"] * p["shares"] for p in acct["positions"].values()) + acct["cash"]
    worst = max(p["cost"] * p["shares"] for p in acct["positions"].values()) / total
    return worst <= cfg["strategy"]["max_single_position_pct"] + 0.02


def validate():
    results, missing = [], []
    for name, fn in CHECKS:
        try:
            ok = bool(fn())
        except Exception as e:
            ok = False
            name = f"{name}（校验异常: {e}）"
        results.append({"check": name, "ok": ok})
        if not ok:
            missing.append(name)
    completion = round(sum(1 for r in results if r["ok"]) / len(results) * 100)
    report = {
        "timestamp": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "completion": completion,
        "total_checks": len(results),
        "passed": sum(1 for r in results if r["ok"]),
        "missing": missing,
        "ready_for_submission": completion == 100,
        "detail": results,
    }
    path = os.path.join(OUT, "00_完成度自检报告.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


if __name__ == "__main__":
    r = validate()
    print(json.dumps(r, ensure_ascii=False, indent=2)[:1500])
