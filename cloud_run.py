# -*- coding: utf-8 -*-
"""云端运行入口（GitHub Actions / 任意装了Python的机器）。

用法:
    python cloud_run.py [eod|ai|riskwatch|weekly|daily|report|review|dailyreport|data|validate]

流程: 运行流水线 → DeepSeek 智能分析 → 导出手机仪表盘JSON(data/dashboard/) → 在GitHub
Actions环境中自动git提交回传账本与成果。本地运行(非Actions环境)只导出JSON，不提交。
AI 分析需要环境变量 DEEPSEEK_API_KEY（只从环境变量读取，绝不落盘）。
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
                  run_pool, run_review, run_riskwatch, run_verify, run_weekly,
                  benchmark_pct_since, last_trading_date, data_freshness)
import benchmark  # noqa: E402
from advice import holding_advice  # noqa: E402
import portfolio as pf  # noqa: E402
import records  # noqa: E402
import validator  # noqa: E402
from em_client import (kline, kline_until, live_prices,  # noqa: E402
                       main_fin_data, secid_of, snapshot)

try:
    import ai_advisor  # noqa: E402
except Exception as _e:  # 缺依赖/文件时不影响其余流程
    ai_advisor = None
    print("[AI] 模块不可用，AI 分析将被跳过:", _e)

DASH = os.path.join(BASE, "data", "dashboard")


def safe_ai(session=None):
    """DeepSeek 分析入口：任何异常都隔离在此，绝不影响主流水线。"""
    if ai_advisor is None:
        return None
    if not CFG.get("ai", {}).get("enabled", True):
        print("[AI] 配置中已禁用（config.json → ai.enabled=false），跳过")
        return None
    try:
        return ai_advisor.run(session=session)
    except Exception as e:
        print("[AI] 异常已隔离，主流程继续:", str(e)[:200])
        return None


# ---------------------------------------------------------------- 仪表盘导出
def _price_map(acct, pool, date):
    """估值取价：委托 em_client.live_prices（快照 > 最新K线 > 候选池旧价）。

    原实现直接采用候选池里记录的 close —— 那是"候选池生成当日"的收盘价，
    在 14:00 盘中巡检 / 盘前 AI 等"不重建候选池"的运行里会滞后整整一个交易日，
    导致仪表盘总资产、盈亏全部失真（实测偏差 2.9 万元）。
    """
    pool_close = {r["code"]: r["close"] for r in pool}
    return live_prices(set(list(acct["positions"]) + list(pool_close)), date, pool_close)


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


def build_todos(summary, acct, confirmed=None):
    """手机端"今天该做什么"清单：把需要人工处理的事项集中成可勾选列表。"""
    todos = []
    date = summary.get("as_of")
    confirmed = confirmed or set()
    for d in summary.get("decisions_today", []):
        if d["id"] in confirmed:   # 已确认=已执行，不再列入待办
            continue
        todos.append({"level": "action", "type": "指令",
                      "title": f"{d['side']} {d['name']}（{d['code']}）",
                      "detail": f"参考价 {d['price']} × {d['shares']:,} 股 ≈ {d['amount']/10000:.2f} 万元",
                      "action": f"在同花顺模拟炒股APP下单后运行 python main.py fill {d['id']} <实际成交价> --confirm"})
    for d in summary.get("pending_decisions", []):
        todos.append({"level": "warn", "type": "确认",
                      "title": f"待确认决策 {d['id']}",
                      "detail": f"{d['date']} {d['side']} {d['name']}（{d['code']}）",
                      "action": f"python main.py confirm {d['id']}"})
    for t in summary.get("unfilled_trades", []):
        todos.append({"level": "warn", "type": "回填",
                      "title": f"待回填实际成交价 {t['id']}",
                      "detail": f"{t['date']} {t['side']} {t['name']}（{t['code']}）系统按 {t['price']} 记账",
                      "action": f"python main.py fill {t['id']} <实际成交价> [成交日期] --confirm"})
    if summary.get("data_stale"):
        todos.append({"level": "error", "type": "数据",
                      "title": "行情数据未更新到最新交易日",
                      "detail": "可能数据源波动或定时任务延迟，可手动触发一次 eod",
                      "action": "仪表盘高级面板 → 触发收盘全流程"})
    for m in summary.get("missing", []):
        todos.append({"level": "warn", "type": "验收",
                      "title": f"验收缺项：{m}", "detail": "完成该检查项后验收回到100%",
                      "action": "python main.py validate"})
    alerts = [a for rec in summary.get("recent_alerts", []) for a in rec.get("alerts", [])]
    for a in alerts[:3]:
        todos.append({"level": "error", "type": "风险", "title": a,
                      "detail": "盘中风险监控触发，请在收盘前处理",
                      "action": "python main.py riskwatch"})
    try:
        up = dt.datetime.strptime(summary.get("updated_at", ""), "%Y-%m-%d %H:%M:%S")
        hours = (dt.datetime.now() - up).total_seconds() / 3600
        if hours > 30:
            todos.append({"level": "warn", "type": "运行",
                          "title": f"已 {hours:.0f} 小时没有新数据",
                          "detail": "云端定时任务可能未投递（GitHub cron 延迟），建议手动触发一次",
                          "action": "仪表盘高级面板 → 触发收盘全流程"})
    except Exception:
        pass
    if not todos:
        todos.append({"level": "ok", "type": "状态", "title": "暂无待办",
                      "detail": "组合运行正常，纪律执行中", "action": ""})
    return todos


def export_dashboard():
    os.makedirs(DASH, exist_ok=True)
    date, data_stale = data_freshness()
    pool = records.latest_pool()
    acct = pf.replay(pf.read_trades(), CFG["strategy"]["initial_capital"])
    prices = _price_map(acct, pool, date)
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

    trades = pf.read_trades()
    pending_decisions = [{"id": d["decision_id"], "date": d["date"], "side": d["side"],
                          "name": d["name"], "code": d["code"]}
                         for d in decisions if d["decision_id"] not in confirmed]
    unfilled_trades = [{"id": t.get("decision_id", ""), "date": t["date"], "side": t["side"],
                        "name": t["name"], "code": t["code"], "price": t["price"]}
                       for t in trades
                       if t.get("decision_id") and t.get("source") == "auto"
                       and "自动成交" in (t.get("note") or "")]

    summary = {
        "as_of": date,
        "data_stale": data_stale,
        "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "initial_capital": CFG["strategy"]["initial_capital"],
        "total_assets": total,
        "return_pct": round((total / CFG["strategy"]["initial_capital"] - 1) * 100, 2),
        "benchmark": benchmark_pct_since(),
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
        "pending_decisions": pending_decisions,
        "unfilled_trades": unfilled_trades,
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
    # 持仓操作建议 + 调仓执行单（规则驱动、无需AI密钥；AI 复核意见可选叠加）
    try:
        adv = holding_advice(acct, prices, CFG, date, total=total, cash=acct["cash"])
        try:
            ai_rec = json.load(open(os.path.join(DASH, "ai.json"), encoding="utf-8"))
            if ai_rec.get("date") == date and ai_rec.get("rebalance_note"):
                adv["ai_note"] = ai_rec["rebalance_note"]
                adv["ai_at"] = ai_rec.get("generated_at")
        except Exception:
            pass
        _jdump("holdings.json", adv)
        print("[仪表盘] 持仓建议: " + "；".join(
            f"{r['name']}={r['action']}" for r in adv["rows"]))
        pl = adv.get("plan") or {}
        if pl.get("sells") or pl.get("buys"):
            print(f"[调仓执行单] 卖 {len(pl.get('sells', []))} 笔 / 买 {len(pl.get('buys', []))} 笔，"
                  f"现金 {pl.get('cash_pct_before')}% → {pl.get('cash_pct_after')}%")
    except Exception as e:
        print("[仪表盘] 持仓建议导出失败:", str(e)[:150])
    # 净值 vs 沪深300：同一批日期、同起点归一
    try:
        nav = records.read_nav()
        _jdump("benchmark.json", benchmark.series(dates=[r["date"] for r in nav]))
    except Exception as e:
        print("[仪表盘] 基准序列导出失败:", e)
        _jdump("benchmark.json", [])
    # 决策后验证（T+5/T+10/T+20）
    try:
        vrep = run_verify()
        _jdump("verify.json", {"summary": vrep.get("summary", {}),
                               "rows": [{"id": r["decision_id"], "date": r["date"], "side": r["side"],
                                         "name": r["name"], "code": r["code"], "price": r["price"],
                                         "days": r["days_elapsed"], "status": r["status"],
                                         "h": {k: v for k, v in r["horizons"].items()}}
                                        for r in vrep.get("rows", [])]})
    except Exception as e:
        print("[仪表盘] 决策后验证导出失败:", e)
        _jdump("verify.json", {"summary": {}, "rows": []})
    _jdump("todo.json", build_todos(summary, acct, confirmed))
    _jdump("decisions.json", [{"id": d["decision_id"], "date": d["date"], "side": d["side"],
                               "name": d["name"], "code": d["code"], "price": d["price"],
                               "shares": d["shares"], "amount": d["amount"],
                               "reason": d["reason"], "rule": (d.get("rule_check") or {}).get("status"),
                               "human": "CONFIRMED" if d["decision_id"] in confirmed else "PENDING"}
                              for d in decisions[-30:]])
    _jdump("pool.json", summary["pool_top"])
    _jdump("market.json", summary["market"])
    _jdump("alerts.json", alerts)
    print(f"[仪表盘] 已导出 {len(os.listdir(DASH))} 个JSON到 data/dashboard/（数据截止 {date}，"
          f"总资产 {total/10000:.2f}万）")


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
    "eod": lambda: (run_daily(), run_review(), run_dailyreport(), safe_ai("eod")),
    "riskwatch": lambda: (run_riskwatch(), safe_ai("intraday")),
    "ai": lambda: safe_ai(),          # 时段按北京时间自动判定（盘前定时任务调用）
    "weekly": run_weekly,
    "daily": run_daily,
    "report": lambda: run_daily(only_report=True),
    "review": run_review,
    "dailyreport": run_dailyreport,
    "data": lambda: run_data("数据更新"),
    "market": run_market,
    "pool": run_pool,
    "verify": run_verify,
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
