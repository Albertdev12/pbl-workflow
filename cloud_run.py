# -*- coding: utf-8 -*-
"""云端运行入口（GitHub Actions / 任意装了Python的机器）。

用法:
    python cloud_run.py [eod|riskwatch|weekly|daily|report|review|dailyreport|data|validate]

流程: 运行流水线 → 导出手机仪表盘JSON(data/dashboard/) → 在GitHub Actions环境中自动
git提交回传账本与成果。本地运行(非Actions环境)只导出JSON，不提交。
"""
import datetime as dt
import json
import os
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "src"))

from main import (CFG, run_daily, run_dailyreport, run_data, run_market,  # noqa: E402
                  run_pool, run_review, run_riskwatch, run_weekly,
                  benchmark_pct_since, last_trading_date)
import portfolio as pf  # noqa: E402
import records  # noqa: E402
import validator  # noqa: E402
from em_client import kline_until  # noqa: E402

DASH = os.path.join(BASE, "data", "dashboard")


# ---------------------------------------------------------------- 仪表盘导出
def _positions(acct, prices):
    out = []
    for code, pos in acct["positions"].items():
        p = prices.get(code, pos["cost"])
        out.append({
            "code": code, "name": pos.get("name", code), "shares": pos["shares"],
            "cost": round(pos["cost"], 3), "price": p,
            "value": round(p * pos["shares"], 2),
            "pnl": round((p - pos["cost"]) * pos["shares"], 2),
            "pnl_pct": round((p / pos["cost"] - 1) * 100, 2) if pos["cost"] else 0,
        })
    out.sort(key=lambda x: -x["value"])
    return out


def _jdump(name, obj):
    with open(os.path.join(DASH, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def export_dashboard():
    os.makedirs(DASH, exist_ok=True)
    date = last_trading_date()
    pool = records.latest_pool()
    acct = pf.replay(pf.read_trades(), CFG["strategy"]["initial_capital"])
    prices = {r["code"]: r["close"] for r in pool}
    for code in acct["positions"]:
        if code not in prices:
            for u in CFG["universe"]:
                if u["code"] == code:
                    df = kline_until(u["secid"], date)
                    if len(df):
                        prices[code] = float(df["close"].iloc[-1])
    total = pf.total_assets(acct, prices)
    rep = validator.validate()

    decisions = records.read_decisions()
    confirmed = records.read_confirmations()
    today_dec = [d for d in decisions if d["date"] == date]
    watch = records.read_market_watch()
    market = watch[-1] if watch else {}
    try:
        with open(os.path.join(BASE, "data", "riskwatch_log.jsonl"), encoding="utf-8") as f:
            alerts = [json.loads(x) for x in f if x.strip()][-5:]
    except Exception:
        alerts = []

    summary = {
        "as_of": date,
        "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "initial_capital": CFG["strategy"]["initial_capital"],
        "total_assets": total,
        "return_pct": round((total / CFG["strategy"]["initial_capital"] - 1) * 100, 2),
        "benchmark": benchmark_pct_since(CFG["course"]["start_date"]),
        "cash": acct["cash"],
        "cash_pct": round(acct["cash"] / total * 100, 1) if total else 0,
        "positions": _positions(acct, prices),
        "n_trades": len(pf.read_trades()),
        "fee": acct["fee"],
        "decisions_today": [{"id": d["decision_id"], "side": d["side"], "name": d["name"],
                             "code": d["code"], "price": d["price"], "shares": d["shares"],
                             "amount": d["amount"], "reason": d["reason"]}
                            for d in today_dec],
        "n_decisions": len(decisions),
        "confirmed": len(confirmed),
        "completion": rep["completion"],
        "ready_for_submission": rep["ready_for_submission"],
        "missing": rep["missing"],
        "pool_top": [{"code": r["code"], "name": r["name"], "industry": r["industry"],
                      "composite": r["composite"], "fin": r["fin_score"], "tech": r["tech_score"]}
                     for r in pool[:5]],
        "market": {"date": market.get("date"), "view": market.get("view"),
                   "opportunity": market.get("opportunity"), "risks": market.get("risks"),
                   "indices": market.get("indices", {})},
        "recent_alerts": alerts,
        "nav_points": len(records.read_nav()),
    }
    _jdump("summary.json", summary)
    _jdump("nav.json", records.read_nav())
    _jdump("decisions.json", [{"id": d["decision_id"], "date": d["date"], "side": d["side"],
                               "name": d["name"], "code": d["code"], "price": d["price"],
                               "shares": d["shares"], "amount": d["amount"],
                               "reason": d["reason"], "rule": (d.get("rule_check") or {}).get("status"),
                               "human": "CONFIRMED" if d["decision_id"] in confirmed else "PENDING"}
                              for d in decisions[-30:]])
    _jdump("pool.json", summary["pool_top"])
    _jdump("market.json", summary["market"])
    _jdump("alerts.json", alerts)
    print(f"[仪表盘] 已导出6个JSON到 data/dashboard/（数据截止 {date}，总资产 {total/10000:.2f}万）")


# ---------------------------------------------------------------- git 回传
def git_commit_push(task):
    """仅在 GitHub Actions 环境（或显式 --push）时提交回传账本与成果。"""
    if os.environ.get("GITHUB_ACTIONS") != "true" and "--push" not in sys.argv:
        print("[git] 非Actions环境，跳过提交回传")
        return
    def run(args, check=True):
        return subprocess.run(args, cwd=BASE, check=check,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        run(["git", "config", "user.name", "pbl-bot"])
        run(["git", "config", "user.email", "pbl-bot@users.noreply.github.com"])
        for path in ("data", "outputs"):
            run(["git", "add", "-A", path])
        r = run(["git", "diff", "--cached", "--quiet"], check=False)
        if r.returncode == 0:
            print("[git] 无变更，跳过提交")
            return
        run(["git", "commit", "-m", f"auto: {dt.date.today().isoformat()} {task}"])
        run(["git", "pull", "--rebase", "origin"], check=False)
        run(["git", "push", "origin", "HEAD"])
        print("[git] 已提交并回传到仓库")
    except subprocess.CalledProcessError as e:
        # 回传失败不影响流水线结果，本地数据仍在，下次运行再试
        print("[git] 警告：提交/推送失败（数据已保存在本地，下次运行会重试）：")
        print((e.stdout or "")[-800:])


# ---------------------------------------------------------------- 主入口
TASKS = {
    "eod": lambda: (run_daily(), run_review(), run_dailyreport()),
    "riskwatch": run_riskwatch,
    "weekly": run_weekly,
    "daily": run_daily,
    "report": lambda: run_daily(only_report=True),
    "review": run_review,
    "dailyreport": run_dailyreport,
    "data": lambda: run_data("数据更新"),
    "market": run_market,
    "pool": run_pool,
}


def main():
    task = sys.argv[1] if len(sys.argv) > 1 else "eod"
    if task not in TASKS:
        print(__doc__)
        return
    print(f"==== cloud_run: {task} @ {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")
    TASKS[task]()
    export_dashboard()
    git_commit_push(task)
    print(f"==== cloud_run 完成: {task} ====")


if __name__ == "__main__":
    main()
