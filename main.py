# -*- coding: utf-8 -*-
"""
模拟证券投资大赛自动化工作流（同花顺模拟炒股配套）

每日调度（已注册Windows计划任务，交易日2次 + 周六周报）:
    14:00  python main.py riskwatch    盘中风险监控（止盈止损预警，唯一盘中任务）
    16:00  python main.py eod          收盘全流程=市场观察+候选池+决策+全部文档+复盘+日报+验收
    周六10:00 python main.py weekly    周报（候选池变化/组合分析/策略复盘/AI统计）

分时段子命令（可单独手动运行，eod已按顺序包含）:
    python main.py daily         决策与全部成果文档+验收
    python main.py data/close    行情/财务缓存刷新
    python main.py market/pool   市场观察/候选池评分
    python main.py review        当日交易复盘文档
    python main.py dailyreport   工作日报

其他命令:
    python main.py init      # 首次初始化：小组表 + 市场观察 + 候选池 + 初始建仓
    python main.py report    # 仅根据已有数据重新生成全部成果文档
    python main.py validate  # 自动验收：成果清单完成度
    python main.py package   # 打包最终提交包
    python main.py confirm ID [ID...]  # 人工确认决策
"""
import datetime as dt
import json
import os
import sys
import time

# 统一输出编码，保证 run.log 为 UTF-8
for _s in (sys.stdout, sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "src"))

import decision
import indicators  # noqa
import package as packager
import portfolio as pf
import records
import reports
import screening
import validator
from em_client import industry_board_rank, kline, kline_until

CFG = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8"))
STATE_PATH = os.path.join(BASE, "data", "pipeline_state.json")

# 任务状态机（采纳自GPT规范第十七节）
PIPELINE_STEPS = [
    "DATA_COLLECTION", "MARKET_OBSERVATION", "STOCK_SCREENING",
    "CANDIDATE_POOL_READY", "RISK_CHECK", "SIMULATION_TRADING",
    "PERFORMANCE_ANALYSIS", "REPORT_GENERATION", "PROJECT_COMPLETE",
]


def set_state(step, note=""):
    state = {"current": step, "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "note": note}
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def data_staleness_check():
    """DATA_STALE 检查：交易日16:00后数据仍未更新到当日则告警。"""
    today = dt.date.today()
    if today.weekday() >= 5:
        return
    df = kline("1.000001", beg="20250601", is_index=True)
    if df["date"].iloc[-1] < today.isoformat() and dt.datetime.now().hour >= 16:
        print("  [DATA_STALE] 警告：今日为交易日且已过16:00，但行情数据仅更新到", df["date"].iloc[-1])


# ---------------------------------------------------------------- 工具
def last_trading_date():
    df = kline("1.000001", beg="20250601", is_index=True)
    return df["date"].iloc[-1]


def _expected_trading_date():
    """预期的最新交易日：工作日应更新到当日；周六周报应更新到周五；周日不用运行。"""
    today = dt.date.today()
    wd = today.weekday()
    expected = today if wd < 5 else today - dt.timedelta(days=1 if wd == 5 else 2)
    return today, expected


def data_freshness():
    """返回 (最近交易日, 是否明显陈旧)。"""
    last = last_trading_date()
    _, expected = _expected_trading_date()
    return last, last < expected.isoformat()


def _refresh_if_stale():
    """收盘数据自愈：若行情落后于应有最新交易日（数据源瞬时502/超时），隔60秒重试最多3次。"""
    last, stale = data_freshness()
    if not stale:
        return last
    _, expected = _expected_trading_date()
    print(f"[DATA_STALE] 行情停留在 {last}，预期应更新到 {expected.isoformat()}；"
          "疑似数据源故障，60秒后重试（最多3次）...")
    for i in range(3):
        time.sleep(60)
        last, stale = data_freshness()
        print(f"[DATA_STALE] 重试{i + 1}/3：行情仍为 {last}")
        if not stale:
            print(f"[数据恢复] 行情已更新至 {last}")
            return last
    print(f"[DATA_STALE] 重试结束，将使用 {last} 的缓存数据继续（已标记陈旧）")
    return last


def benchmark_pct_since(date0):
    df = kline(CFG["strategy"]["market_benchmark"], beg="20250101", is_index=True)
    df = df[df["date"] >= date0].reset_index(drop=True)
    if len(df) < 2:
        return None
    return f"{(df['close'].iloc[-1] / df['close'].iloc[0] - 1) * 100:+.2f}%"


# ---------------------------------------------------------------- 市场观察
def market_observe(date):
    indices = {}
    for idx in CFG["indices"]:
        df = kline_until(idx["code"], date, is_index=True)
        if len(df) >= 2:
            indices[idx["name"]] = {
                "close": float(df["close"].iloc[-1]),
                "pct": (df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100,
            }
    sh = kline_until("1.000001", date, is_index=True).tail(6)
    if sh["amount"].notna().any():  # 备用数据源下指数成交额不可用 → 降级
        turnover = float(sh["amount"].iloc[-1]) / 1e8  # 亿元
        avg5 = float(sh["amount"].tail(6).head(5).mean()) / 1e8
    else:
        turnover = avg5 = None
    try:
        boards = industry_board_rank(15)
    except Exception as e:
        print(f"  [警告] 板块涨幅榜获取失败: {e}")
        boards = []
    hs300 = indices.get("沪深300", {})
    above_ma20 = False
    df300 = kline_until(CFG["strategy"]["market_benchmark"], date, is_index=True)
    if len(df300) >= 20:
        ma20 = float(df300["close"].tail(20).mean())
        above_ma20 = float(df300["close"].iloc[-1]) > ma20
    if turnover is None:
        view = (f"沪深300收于{hs300.get('close', 0):.2f}点（{hs300.get('pct', 0):+.2f}%），"
                f"{'站上' if above_ma20 else '跌破'}20日均线；成交额数据暂不可用（备用数据源口径）。")
    else:
        view = (f"沪深300收于{hs300.get('close', 0):.2f}点（{hs300.get('pct', 0):+.2f}%），"
                f"{'站上' if above_ma20 else '跌破'}20日均线；两市成交额约{turnover:.0f}亿元，"
                f"较前5日均量{'放大' if turnover > avg5 else '萎缩'}"
                f"（前5日均值约{avg5:.0f}亿元），市场{'活跃度提升' if turnover > avg5 else '情绪偏谨慎'}。")
    lead_names = {b["name"] for b in boards[:5]}
    watch_industries = {u["industry"] for u in CFG["universe"]}
    hit = lead_names & {w for w in watch_industries if len(w) >= 2}
    if not boards:
        opp = "板块涨幅榜数据暂不可用（数据源波动），维持现有组合，等待评分触发。"
    elif hit:
        opp = f"领涨方向（{'、'.join(list(hit))}）与团队关注行业重合，可在候选池中关注相关标的的回调低吸机会。"
    else:
        opp = f"当日领涨板块为{('、'.join(b['name'] for b in boards[:3]))}，与观察池重合度低，维持现有组合，等待评分触发。"
    risks = []
    if hs300.get("pct", 0) <= -1.5:
        risks.append("沪深300单日跌幅超1.5%，警惕系统性回调，必要时降低仓位")
    if turnover is not None and avg5 is not None and turnover < avg5 * 0.8:
        risks.append("量能持续萎缩，反弹持续性存疑")
    risks.append("个股业绩披露期业绩变脸风险；宏观消息面突发扰动")
    rec = {
        "date": date,
        "indices": indices,
        "turnover": turnover,
        "top_boards": [{"name": b["name"], "pct": b["pct"]} for b in boards[:5]],
        "bottom_boards": [{"name": b["name"], "pct": b["pct"]} for b in boards[-5:]],
        "view": view,
        "opportunity": opp,
        "risks": "；".join(risks),
    }
    records.save_market_watch(rec)
    return rec


# ---------------------------------------------------------------- 每日主流程
def run_daily(only_report=False):
    date = last_trading_date() if only_report else _refresh_if_stale()
    print(f"[工作流] 数据截止交易日: {date}")
    data_staleness_check()
    set_state("DATA_COLLECTION")

    done = any(d.get("date") == date for d in records.read_decisions())
    pool = records.latest_pool()

    if not only_report:
        # 1) 市场观察
        watch = market_observe(date)
        set_state("MARKET_OBSERVATION")
        print(f"[1/4] 市场观察完成: {watch['view'][:40]}...")
        # 2) 候选池筛选（评分变化时自动更新）
        pool_res = screening.screen(CFG["universe"], date,
                                    CFG["strategy"]["min_candidates"],
                                    CFG["strategy"]["max_candidates"])
        records.save_pool(date, pool_res)
        set_state("CANDIDATE_POOL_READY")
        pool = pool_res["pool"]
        print(f"[2/4] 候选池更新: " + "、".join(f"{r['name']}({r['composite']})" for r in pool[:4]) + " ...")
        # 3) 决策（同日幂等：已生成过则跳过）
        if done:
            print("[3/4] 本交易日决策已生成，跳过（幂等）")
            decisions = [d for d in records.read_decisions() if d["date"] == date]
        else:
            set_state("RISK_CHECK")
            decisions = decision.run_decision(date, pool, CFG)
            set_state("SIMULATION_TRADING")
            print(f"[3/4] 生成决策 {len(decisions)} 条")
    else:
        decisions = [d for d in records.read_decisions() if d["date"] == date]

    # 4) 账户与净值
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
    records.save_snapshot(date, total, total / CFG["strategy"]["initial_capital"])
    set_state("PERFORMANCE_ANALYSIS")
    print(f"[4/4] 总资产 {total/10000:.2f} 万元（{(total/CFG['strategy']['initial_capital']-1)*100:+.2f}%）")

    generate_docs(date, pool, acct, prices, total, decisions)
    set_state("REPORT_GENERATION")

    # 5) 自动验收
    rep = validator.validate()
    print(f"[验收] 完成度 {rep['completion']}%（{rep['passed']}/{rep['total_checks']}）"
          + ("" if rep["ready_for_submission"] else f"；待完成: {rep['missing'][:3]}"))
    set_state("PROJECT_COMPLETE" if rep["ready_for_submission"] else "REPORT_GENERATION",
              f"completion={rep['completion']}")
    return date, total


# ---------------------------------------------------------------- 文档生成
def generate_docs(date, pool, acct, prices, total, decisions):
    all_decisions = records.read_decisions()
    bm = benchmark_pct_since(CFG["course"]["start_date"])
    made = []
    for fn, *args in [
        (reports.market_watch_xlsx,),
        (reports.pool_xlsx, pool),
        (reports.tech_xlsx, pool, decisions),
        (reports.decision_xlsx, all_decisions),
        (reports.trades_xlsx, CFG),
        (reports.ai_xlsx,),
        (reports.strategy_docx, CFG, pool),
        (reports.order_sheet_docx, date, decisions),
        (reports.final_report_docx, CFG, pool, acct, prices, total, all_decisions, bm),
        (reports.roadshow_pptx, CFG, pool, acct, prices, total, bm),
    ]:
        try:
            res = fn(*args)
            if isinstance(res, tuple):
                res = res[0]
            if res:
                made.append(res)
        except Exception as e:
            print(f"  [警告] 生成 {fn.__name__} 失败: {e}")
    print("[输出] 已生成/更新成果文档:")
    for m in made:
        print("   -", os.path.basename(m))


def run_init():
    print("== 初始化：小组信息登记表 ==")
    reports.group_form(CFG)
    date, total = run_daily()
    # 初始投资方案 + 前三名个股基本面分析报告
    pool = records.latest_pool()
    if pool:
        reports.initial_plan_docx(CFG, pool)
        print("[输出] - 04_初始投资方案.docx")
        for r in pool[:3]:
            if r["kind"] == "stock":
                reports.fundamental_docx(r, CFG)
                print(f"[输出] - 05_基本面分析_{r['code']}_{r['name']}.docx")
    print("== 初始化完成。请核对 outputs/ 下文档，并在同花顺模拟炒股APP中按《委托指令单》下单 ==")


def run_weekly():
    date, total = run_daily()
    print("== 周任务完成（含中期路演PPT刷新） ==")


# ================================================================ 分时段任务（GPT规范第十六节：一天8次）
def run_data(label="数据更新"):
    """08:00/15:10：预取观察池与指数行情、财务数据到本地缓存。"""
    set_state("DATA_COLLECTION")
    date = last_trading_date()
    ok = 0
    for u in CFG["universe"]:
        try:
            kline(u["secid"])
            ok += 1
        except Exception as e:
            print(f"  [警告] {u['name']} 行情更新失败: {e}")
    for idx in CFG["indices"]:
        try:
            kline(idx["code"])
        except Exception:
            pass
    from em_client import main_fin_data
    for u in CFG["universe"]:
        if u["kind"] == "stock":
            try:
                main_fin_data(u["secid"])
            except Exception:
                pass
    print(f"[{label}] 行情缓存 {ok}/{len(CFG['universe'])} 只，财务数据已刷新，数据截止 {date}")
    data_staleness_check()


def run_market():
    """08:30 市场分析：基于最新收盘数据生成/更新市场观察。"""
    date = last_trading_date()
    watch = market_observe(date)
    reports.market_watch_xlsx()
    print(f"[市场分析] {watch['view']}")


def run_pool():
    """09:00 候选池更新：重新评分并刷新候选池与技术分析记录。"""
    date = last_trading_date()
    res = screening.screen(CFG["universe"], date,
                           CFG["strategy"]["min_candidates"], CFG["strategy"]["max_candidates"])
    records.save_pool(date, res)
    reports.pool_xlsx(res["pool"])
    reports.tech_xlsx(res["pool"], [d for d in records.read_decisions() if d["date"] == date])
    print("[候选池更新] " + "、".join(f"{r['name']}({r['composite']})" for r in res["pool"][:4]) + " ...")


def run_riskwatch():
    """盘中风险监控：实时快照对照止盈止损线与市场状态，触发即生成预警。"""
    from em_client import snapshot
    st = CFG["strategy"]
    acct = pf.replay(pf.read_trades(), st["initial_capital"])
    alerts, prices = [], {}
    for code, pos in acct["positions"].items():
        u = next((x for x in CFG["universe"] if x["code"] == code), None)
        if not u:
            continue
        try:
            p = snapshot(u["secid"])
        except Exception:
            continue
        prices[code] = p["price"]
        pnl = p["price"] / pos["cost"] - 1 if pos["cost"] else 0
        if pnl >= st["take_profit_pct"]:
            alerts.append(f"【止盈预警】{pos['name']}({code}) 浮盈{pnl*100:.1f}% ≥ {st['take_profit_pct']*100:.0f}%，现价{p['price']}")
        elif pnl <= -st["stop_loss_pct"]:
            alerts.append(f"【止损预警】{pos['name']}({code}) 浮亏{pnl*100:.1f}% ≤ -{st['stop_loss_pct']*100:.0f}%，现价{p['price']}")
    try:
        hs_pct = snapshot(CFG["strategy"]["market_benchmark"]).get("pct_chg")
        if hs_pct is not None and hs_pct <= -2.0:
            alerts.append(f"【市场预警】沪深300盘中下跌{hs_pct:.2f}%，警惕系统性回撤，可降低仓位")
    except Exception:
        pass
    rec = {"time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "alerts": alerts, "total_assets": pf.total_assets(acct, prices)}
    with open(os.path.join(BASE, "data", "riskwatch_log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if alerts:
        path = os.path.join(reports.OUT, f"盘中风险预警_{dt.date.today().isoformat()}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(alerts) + f"\n\n当前总资产约 {rec['total_assets']/10000:.2f} 万元\n"
                    "请在同花顺模拟炒股APP中及时处理（卖出/减仓），并在处理后将成交回填 data/trades.csv\n")
        print("[风险监控] ⚠ " + "；".join(alerts))
        print(f"[风险监控] 预警已写入: {os.path.basename(path)}")
    else:
        print(f"[风险监控] 正常：全部持仓处于止盈止损线内，总资产约 {rec['total_assets']/10000:.2f} 万元")


def run_review():
    """17:00 交易复盘：刷新盈亏归因并生成当日复盘文档。"""
    date = last_trading_date()
    acct = pf.replay(pf.read_trades(), CFG["strategy"]["initial_capital"])
    prices = {}
    for code in acct["positions"]:
        for u in CFG["universe"]:
            if u["code"] == code:
                df = kline_until(u["secid"], date)
                if len(df):
                    prices[code] = float(df["close"].iloc[-1])
    total = pf.total_assets(acct, prices)
    reports.trades_xlsx(CFG)
    p = reports.review_docx(date, CFG, acct, prices, total)
    with open(os.path.join(BASE, "data", "review_log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"date": date, "total": total,
                            "realized": acct["realized"],
                            "n_trades": len(pf.read_trades())}, ensure_ascii=False) + "\n")
    print(f"[交易复盘] 总资产 {total/10000:.2f} 万元，复盘文档: {os.path.basename(p)}")


def run_dailyreport():
    """18:00 工作日报：汇总当日全部运行结果并刷新成果文档与验收。"""
    date, total = run_daily(only_report=True)
    today_dec = [d for d in records.read_decisions() if d["date"] == date]
    rep = validator.validate()
    navs = records.read_nav()
    lines = [
        f"工作日报 {date}",
        f"生成时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"总资产: {total/10000:.2f} 万元（{(total/CFG['strategy']['initial_capital']-1)*100:+.2f}%）",
        f"当日决策: {len(today_dec)} 条 " + ("；".join(f"{d['side']}{d['name']}" for d in today_dec) if today_dec else "（无操作）"),
        f"累计净值记录: {len(navs)} 个交易日",
        f"人工确认状态: {len(records.read_confirmations())}/{len(records.read_decisions())} 已确认",
        f"成果验收: {rep['completion']}%（{rep['passed']}/{rep['total_checks']}）"
        + ("" if rep["ready_for_submission"] else f"；待完成: {'、'.join(rep['missing'][:3])}"),
    ]
    path = os.path.join(reports.OUT, f"15_工作日报_{date}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("[工作日报] 已生成:", os.path.basename(path))
    for ln in lines[2:]:
        print("   ", ln)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if cmd == "init":
        run_init()
    elif cmd == "daily":
        run_daily()
    elif cmd == "weekly":
        run_weekly()
    elif cmd == "report":
        run_daily(only_report=True)
    elif cmd == "validate":
        print(json.dumps(validator.validate(), ensure_ascii=False, indent=2))
    elif cmd == "package":
        zp, n = packager.build()
        print(f"提交包已生成: {zp}（共{n}个文件，目录结构见 FINAL_SUBMISSION/）")
    elif cmd == "confirm":
        for did in sys.argv[2:]:
            records.confirm(did)
            print("已人工确认:", did)
    elif cmd == "data":
        run_data("数据更新")
    elif cmd == "close":
        run_data("收盘行情更新")
    elif cmd == "market":
        run_market()
    elif cmd == "pool":
        run_pool()
    elif cmd == "riskwatch":
        run_riskwatch()
    elif cmd == "review":
        run_review()
    elif cmd == "dailyreport":
        run_dailyreport()
    elif cmd == "eod":
        # 16:00 收盘全流程：决策与文档 → 复盘 → 日报（一次跑完）
        run_daily()
        run_review()
        run_dailyreport()
    else:
        print(__doc__)
