# -*- coding: utf-8 -*-
"""报告素材包 / 周会材料包生成器。

把散落在账本、决策、候选池、回测、决策后验证里的数据汇总成"可直接改写"的 Markdown 素材，
解决"知道有数据、但写报告时还得一条条翻文件"的问题。

- 报告素材包：按日生成，服务《09_交易记录与盈亏归因》《14_交易复盘》与《13_投资总结报告》四段手写内容
- 周会材料包：按周生成，服务周六周会（净值回顾 / 池子变化 / 议题建议）
"""
import datetime as dt
import json
import os

import portfolio as pf
import records

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "outputs")
DATA_DIR = os.path.join(BASE, "data")
os.makedirs(OUT, exist_ok=True)


# ---------------------------------------------------------------- 小工具
def _cfg():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def _pct(v, digits=2):
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def _wan(v):
    return "—" if v is None else f"{v / 10000:,.2f}"


def _num(v, digits=2):
    return "—" if v is None else f"{v:,.{digits}f}"


def _table(headers, rows):
    lines = ["| " + " | ".join(str(h) for h in headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        lines.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(lines)


def _backtest_summary():
    """解析 data/backtest_result.txt 里的 JSON 块，提取关键结论。"""
    path = os.path.join(DATA_DIR, "backtest_result.txt")
    if not os.path.exists(path):
        return {}, []
    blocks, title, buf = {}, None, []
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s.startswith("=== ") and s.endswith(" ==="):
                if title and buf:
                    blocks[title] = "".join(buf)
                title, buf = s[4:-4], []
            elif title is not None:
                if not s and buf:
                    blocks[title] = "".join(buf)
                    title, buf = None, []
                else:
                    buf.append(line)
    if title and buf:
        blocks[title] = "".join(buf)
    parsed = {}
    for k, v in blocks.items():
        try:
            parsed[k] = json.loads(v)
        except Exception:
            pass
    order = ["线上策略(基本面50%+技术面50%)", "无偏-固定止盈9%/止损8%",
             "无偏-跌破20日线离场", "无偏-高点回撤12%离场", "等权持有12只"]
    return parsed, [k for k in order if k in parsed]


def _market_rows(days=5):
    rows = records.read_market_watch()[-days:]
    return [(r.get("date"), (r.get("view") or "")[:120]) for r in rows]


def _confirmed():
    return records.read_confirmations()


def _account(cfg):
    st = cfg["strategy"]
    acct = pf.replay(pf.read_trades(), st["initial_capital"])
    pool = records.latest_pool()
    prices = {r["code"]: r["close"] for r in pool}
    for code, pos in acct["positions"].items():
        prices.setdefault(code, pos["cost"])
    total = pf.total_assets(acct, prices)
    return acct, prices, total


# ---------------------------------------------------------------- 16 报告素材包
def build_report_pack(asof=None, verify_report=None):
    cfg = _cfg()
    st = cfg["strategy"]
    acct, prices, total = _account(cfg)
    trades = pf.read_trades()
    decisions = records.read_decisions()
    conf = _confirmed()
    pool = records.latest_pool()
    nav = records.read_nav()
    asof = asof or (nav[-1]["date"] if nav else dt.date.today().isoformat())
    ret = total / st["initial_capital"] - 1
    bench = None
    try:
        from em_client import kline
        bdf = kline(st["market_benchmark"], beg="20250101", is_index=True)
        bdf = bdf[bdf["date"] >= cfg["course"]["start_date"]]
        if len(bdf) >= 2:
            bench = float(bdf["close"].iloc[-1]) / float(bdf["close"].iloc[0]) - 1
    except Exception:
        pass
    if verify_report is not None:
        vrep = verify_report
    else:
        try:
            import verify as verify_mod
            vrep = verify_mod.build_report()
        except Exception as e:
            vrep = {"summary": {}, "rows": [], "error": str(e)}
    bt, bt_order = _backtest_summary()

    L = []
    L.append(f"# 报告素材包 · {asof}")
    L.append("")
    L.append(f"> 自动生成：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}　数据截止：{asof}")
    L.append("> 用途：为《09_交易记录与盈亏归因》《14_交易复盘》《13_投资总结报告》手写内容提供**可直接改写的数据与要点**。")
    L.append("> 说明：这是素材，不是成品；请用你自己的话改写后再填入正式文档，保留真实数据。")
    L.append("")

    # 一、绩效快照
    L.append("## 一、账户绩效快照")
    L.append("")
    L.append(_table(["项目", "数值"], [
        ["初始资金", f"{_wan(st['initial_capital'])} 万元"],
        ["当前总资产", f"{_wan(total)} 万元"],
        ["累计收益率", _pct(ret)],
        ["同期沪深300", _pct(bench)],
        ["相对基准超额", _pct(ret - bench) if bench is not None else "—"],
        ["现金", f"{_wan(acct['cash'])} 万元（{acct['cash'] / total * 100:.1f}%）"],
        ["累计交易", f"{len(trades)} 笔"],
        ["累计费用", f"{acct['fee']:,.0f} 元（占初始资金 {acct['fee'] / st['initial_capital'] * 100:.3f}%）"],
        ["净值记录", f"{len(nav)} 个交易日"],
    ]))
    L.append("")
    if acct["positions"]:
        rows = []
        for code, pos in sorted(acct["positions"].items(), key=lambda x: -(prices.get(x[0], x[1]["cost"]) * x[1]["shares"])):
            p = prices.get(code, pos["cost"])
            rows.append([pos.get("name", code), code, f"{pos['shares']:,}", _num(pos["cost"], 3),
                         _num(p, 3), _wan(p * pos["shares"]), _wan((p - pos["cost"]) * pos["shares"]),
                         _pct(p / pos["cost"] - 1 if pos["cost"] else None)])
        L.append("**持仓明细**")
        L.append("")
        L.append(_table(["证券", "代码", "持仓(股)", "成本价", "现价", "市值(万)", "浮动盈亏(万)", "浮动%"], rows))
        L.append("")

    # 二、台账
    L.append("## 二、交易与决策台账（全部）")
    L.append("")
    trade_by_did = {t.get("decision_id"): t for t in trades if t.get("decision_id")}
    rows = []
    for d in decisions:
        t = trade_by_did.get(d["decision_id"])
        rows.append([d["date"], d["decision_id"], d["side"], f"{d['name']}({d['code']})",
                     _num(float((t or d)["price"]), 3), f"{int((t or d)['shares']):,}",
                     _wan(float((t or d)["price"]) * int((t or d)["shares"])),
                     (d.get("rule_check") or {}).get("status", "—"),
                     "已确认" if d["decision_id"] in conf else "待确认",
                     "已成交" if t else "未成交"])
    L.append(_table(["日期", "编号", "方向", "证券", "价格", "数量", "金额(万)", "规则校验", "人工确认", "账本"], rows))
    L.append("")

    # 三、盈亏归因
    L.append("## 三、盈亏归因")
    L.append("")
    if acct["realized"]:
        rows = [[k, _wan(v), "盈利" if v > 0 else "亏损"] for k, v in sorted(acct["realized"].items(), key=lambda x: -x[1])]
        L.append(_table(["证券", "已实现盈亏(万)", "结果"], rows))
    else:
        L.append("- 暂无平仓交易，已实现盈亏为 0；全部盈亏仍为浮动盈亏。")
    L.append("")
    float_total = sum((prices.get(c, p["cost"]) - p["cost"]) * p["shares"] for c, p in acct["positions"].items())
    L.append(f"- 当前浮动盈亏合计：{_wan(float_total)} 万元（占总资产 {float_total / total * 100:+.2f}%）")
    L.append(f"- 交易费用累计 {acct['fee']:,.0f} 元，相当于吞掉初始资金的 {acct['fee'] / st['initial_capital'] * 100:.3f}%"
             "（回测显示换手越高、费用拖累越明显，这也是本阶段下调交易频率的原因之一）")
    L.append("")

    # 四、候选池
    L.append("## 四、候选池与评分（最新）")
    L.append("")
    if pool:
        rows = []
        for i, r in enumerate(pool, 1):
            rows.append([i, f"{r['name']}({r['code']})", r["industry"], r["composite"],
                         r["fin_score"], r["tech_score"], r["tech_judgment"]])
        L.append(_table(["#", "证券", "行业", "综合", "基本面", "技术面", "技术判断"], rows))
        L.append("")
        L.append("**纳入原因（Top3）**")
        for r in pool[:3]:
            L.append(f"- {r['name']}：{r['include_reason']}")
    else:
        L.append("- 暂无候选池数据。")
    L.append("")

    # 五、市场观察
    L.append("## 五、市场观察（近5个交易日）")
    L.append("")
    for d, v in _market_rows(5):
        L.append(f"- **{d}**：{v}")
    L.append("")

    # 六、回测结论
    L.append("## 六、策略回测结论（无偏对照）")
    L.append("")
    L.append("> 回测区间与口径见 `data/backtest_result.txt`；关键发现：用「今天的财务数据」回测会产生**前视偏差**，"
             "把基本面分数随机打乱后收益从 +52.8% 掉到 +3.5%，因此下表以**基本面中性（统一60分）**的无偏对照为准。")
    L.append("")
    if bt_order:
        rows = []
        for k in bt_order:
            m = bt[k]
            rows.append([k, m.get("总收益"), m.get("最大回撤"), m.get("夏普"), m.get("交易笔数"), m.get("卖出胜率")])
        L.append(_table(["策略", "总收益", "最大回撤", "夏普", "交易笔数", "卖出胜率"], rows))
        L.append("")
        L.append("- 结论1：固定止盈+9% 在震荡市中被反复洗出，无偏对照下反而略亏；**改用移动止损（自高点回撤12%）后"
                 "收益 +20.76%、回撤 -12.54%**。")
        L.append("- 结论2：交易越频繁收益越低（12笔 > 64笔 > 110笔 > 359笔），费用与择时损耗是主要拖累。")
        L.append("- 结论3：宽基ETF底仓提供基准收益，个股负责超额，二者缺一不可。")
    else:
        L.append("- 未找到回测结果文件，请先运行 `python backtest.py`。")
    L.append("")

    # 七、决策后验证
    L.append("## 七、决策后验证（T+5 / T+10 / T+20）")
    L.append("")
    vsum = vrep.get("summary", {})
    L.append(f"- 决策 {vsum.get('n_decisions', 0)} 笔：已验证 {vsum.get('n_verified', 0)} 笔，"
             f"待观察 {vsum.get('n_pending', 0)} 笔"
             + (f"，样本胜率 {vsum['win_rate'] * 100:.0f}%，平均超额 {_pct(vsum['avg_excess'])}"
                if vsum.get("win_rate") is not None else "（尚无到期样本）"))
    L.append("")
    if vrep.get("rows"):
        rows = []
        for r in vrep["rows"]:
            h = r["horizons"]
            rows.append([r["decision_id"], r["date"], r["side"], f"{r['name']}({r['code']})",
                         _num(r["price"], 3), r["days_elapsed"],
                         "待观察" if h["T+5"]["ret"] is None else _pct(h["T+5"]["ret"]),
                         "待观察" if h["T+10"]["ret"] is None else _pct(h["T+10"]["ret"]),
                         "待观察" if h["T+20"]["ret"] is None else _pct(h["T+20"]["ret"]),
                         r["status"]])
        L.append(_table(["编号", "日期", "方向", "证券", "成交价", "已过交易日", "T+5", "T+10", "T+20", "状态"], rows))
    L.append("")

    # 八、四段手写素材
    L.append("## 八、手写四段素材（可直接改写）")
    L.append("")
    L.append("### ① 投资过程记录（对应 09_交易记录与盈亏归因 / 14_交易复盘）")
    L.append("")
    L.append(f"- {cfg['course']['start_date']} 项目启动，初始资金 {_wan(st['initial_capital'])} 万元，"
             "确定「宽基ETF底仓 + 评分个股」的组合框架。")
    for d in decisions:
        t = trade_by_did.get(d["decision_id"])
        L.append(f"- {d['date']}：{d['side']} {d['name']}（{d['code']}）"
                 f"{int((t or d)['shares']):,} 股 @ {_num(float((t or d)['price']), 3)}"
                 f"（{_wan(float((t or d)['price']) * int((t or d)['shares']))} 万元）——{d['reason'][:60]}。")
    L.append(f"- 截至 {asof}，组合持有 {len(acct['positions'])} 只标的，现金占比 {acct['cash'] / total * 100:.1f}%，"
             f"累计收益 {_pct(ret)}（同期沪深300 {_pct(bench)}）。")
    L.append("")
    L.append("### ② 决策依据（对应 07_投资决策记录，四要素）")
    L.append("")
    for d in decisions[-3:]:
        L.append(f"**{d['decision_id']} {d['side']}{d['name']}**")
        L.append(f"- 基本面依据：{d.get('fund_basis', '—')}")
        L.append(f"- 技术面依据：{d.get('tech_basis', '—')}")
        L.append(f"- 市场依据：{d.get('market_basis', '—')}")
        L.append(f"- 风险判断：{d.get('risk_judge', '—')}")
        L.append(f"- 规则校验：{(d.get('rule_check') or {}).get('status', '—')}"
                 + (f"，交易后现金占比 {(d['rule_check'].get('cash_ratio_after') or 0) * 100:.0f}%"
                    if (d.get("rule_check") or {}).get("cash_ratio_after") else ""))
        L.append("")
    L.append("### ③ 盈亏归因（对应 09）")
    L.append("")
    L.append(f"- 组合层面：浮动盈亏 {_wan(float_total)} 万元，费用 {acct['fee']:,.0f} 元。")
    if acct["positions"]:
        best = max(acct["positions"].items(), key=lambda x: (prices.get(x[0], x[1]["cost"]) / x[1]["cost"] - 1))
        worst = min(acct["positions"].items(), key=lambda x: (prices.get(x[0], x[1]["cost"]) / x[1]["cost"] - 1))
        for tag, (code, pos) in (("贡献最大", best), ("拖累最大", worst)):
            p = prices.get(code, pos["cost"])
            L.append(f"- {tag}：{pos.get('name', code)}（{code}）{_pct(p / pos['cost'] - 1)}，"
                     f"浮盈 {(p - pos['cost']) * pos['shares'] / 10000:+.2f} 万元。")
    L.append("- 归因框架建议：① 市场（Beta）② 行业与个股（Alpha）③ 交易成本 ④ 择时失误。")
    L.append("")
    L.append("### ④ 策略反思与改进（对应 10_策略调整记录 / 13_投资总结报告）")
    L.append("")
    L.append("- 发现的问题：原策略固定止盈+9% 在震荡市反复被洗出；回测显示该规则在无偏口径下收益为负。")
    L.append("- 验证方式：把基本面分数随机打乱 / 统一为中性分，检验收益是否只是「用今天的数据挑过去的好公司」。")
    L.append("- 已上线调整：移动止损（自持仓最高价回撤12%）替代固定止盈；ETF 底仓目标权重提到 30%；"
             "单日最多建仓由 3 只降到 2 只；数据源增加腾讯备用通道。")
    L.append("- 后续观察指标：决策后 T+5/T+10/T+20 超额收益、换手率、最大回撤、现金占比。")
    L.append("")

    # 九、验收与待办
    L.append("## 九、验收状态与待办")
    L.append("")
    try:
        import validator
        rep = validator.validate()
        L.append(f"- 自动验收：{rep['completion']}%（{rep['passed']}/{rep['total_checks']}）"
                 + ("" if rep["ready_for_submission"] else f"；待完成：{'、'.join(rep['missing'])}"))
    except Exception as e:
        L.append(f"- 自动验收失败：{e}")
    pending = [d["decision_id"] for d in decisions if d["decision_id"] not in conf]
    if pending:
        L.append(f"- 待人工确认决策：{'、'.join(pending)}（执行后运行 `python main.py confirm <编号>`）")
    unfilled = [t["decision_id"] for t in trades if t.get("source") == "auto" and "自动成交" in (t.get("note") or "")]
    if unfilled:
        L.append(f"- 待回填实际成交价：{'、'.join(x for x in unfilled if x)}"
                 "（运行 `python main.py fill <编号> <实际成交价> [成交日期]`）")
    L.append("")

    text = "\n".join(L)
    path = os.path.join(OUT, f"16_报告素材包_{asof}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path, text


# ---------------------------------------------------------------- 18 周会材料包
def build_weekly_pack(asof=None):
    cfg = _cfg()
    st = cfg["strategy"]
    nav = records.read_nav()
    asof = asof or (nav[-1]["date"] if nav else dt.date.today().isoformat())
    d0 = dt.date.fromisoformat(asof)
    week_start = d0 - dt.timedelta(days=d0.weekday())
    week_end = week_start + dt.timedelta(days=6)
    iso = d0.isocalendar()
    week_label = f"{iso[0]}-W{iso[1]:02d}"

    def in_week(ds):
        return week_start.isoformat() <= ds <= week_end.isoformat()

    acct, prices, total = _account(cfg)
    trades = [t for t in pf.read_trades() if in_week(t["date"])]
    decisions = [d for d in records.read_decisions() if in_week(d["date"])]
    conf = _confirmed()
    week_nav = [r for r in nav if in_week(r["date"])]
    prev_nav = [r for r in nav if r["date"] < week_start.isoformat()]
    start_val = week_nav[0]["total_assets"] if week_nav else (prev_nav[-1]["total_assets"] if prev_nav else st["initial_capital"])
    week_ret = total / start_val - 1 if start_val else 0
    pool_now = records.latest_pool()
    pools = records.read_pool_history()
    pool_prev = []
    for p in reversed(pools):
        if p["date"] < week_start.isoformat():
            pool_prev = p["pool"]
            break
    try:
        import verify as verify_mod
        vrep = verify_mod.build_report()
    except Exception:
        vrep = {"summary": {}, "rows": []}

    L = []
    L.append(f"# 周会材料包 · {week_label}")
    L.append("")
    L.append(f"> 周期：{week_start.isoformat()} ~ {week_end.isoformat()}　生成时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    L.append("")
    L.append("## 一、本周概览")
    L.append("")
    L.append(_table(["项目", "数值"], [
        ["周初总资产", f"{_wan(start_val)} 万元"],
        ["周末总资产", f"{_wan(total)} 万元"],
        ["本周收益率", _pct(week_ret)],
        ["累计收益率", _pct(total / st["initial_capital"] - 1)],
        ["本周交易", f"{len(trades)} 笔"],
        ["本周决策", f"{len(decisions)} 条"],
        ["现金占比", f"{acct['cash'] / total * 100:.1f}%"],
        ["持仓只数", len(acct["positions"])],
    ]))
    L.append("")
    L.append("## 二、本周交易与决策")
    L.append("")
    if decisions:
        rows = [[d["date"], d["decision_id"], d["side"], f"{d['name']}({d['code']})",
                 _num(d["price"], 3), f"{d['shares']:,}",
                 "已确认" if d["decision_id"] in conf else "待确认"] for d in decisions]
        L.append(_table(["日期", "编号", "方向", "证券", "价格", "数量", "人工确认"], rows))
    else:
        L.append("- 本周无交易决策（按纪律持有）。")
    L.append("")
    L.append("## 三、候选池变化")
    L.append("")
    now_codes = {r["code"] for r in pool_now}
    prev_codes = {r["code"] for r in pool_prev}
    if pool_prev:
        enter = [r for r in pool_now if r["code"] not in prev_codes]
        leave = [r for r in pool_prev if r["code"] not in now_codes]
        enter_txt = "、".join("%s(%s)" % (r["name"], r["composite"]) for r in enter) or "无"
        leave_txt = "、".join(r["name"] for r in leave) or "无"
        L.append("- 新进：" + enter_txt)
        L.append("- 移出：" + leave_txt)
    else:
        L.append("- 暂无上周候选池可比对。")
    L.append("")
    if pool_now:
        rows = [[i, f"{r['name']}({r['code']})", r["composite"], r["fin_score"], r["tech_score"]]
                for i, r in enumerate(pool_now, 1)]
        L.append(_table(["#", "证券", "综合", "基本面", "技术面"], rows))
    L.append("")
    L.append("## 四、风险与预警")
    L.append("")
    alerts = []
    try:
        with open(os.path.join(DATA_DIR, "riskwatch_log.jsonl"), encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                r = json.loads(line)
                if in_week(r["time"][:10]) and r.get("alerts"):
                    alerts.extend(r["alerts"])
    except Exception:
        pass
    L.append("\n".join(f"- {a}" for a in alerts) if alerts else "- 本周无盘中风险预警触发。")
    L.append("")
    L.append("## 五、决策后验证")
    L.append("")
    vs = vrep.get("summary", {})
    L.append(f"- 累计决策 {vs.get('n_decisions', 0)} 笔，已验证 {vs.get('n_verified', 0)} 笔，待观察 {vs.get('n_pending', 0)} 笔。")
    L.append("")
    L.append("## 六、下周计划与会议议题（建议）")
    L.append("")
    L.append("1. 复核候选池评分变化，确认是否有标的触发移动止损/技术面离场。")
    L.append("2. 检查现金占比是否仍 ≥ 20%，评估是否分批补仓或再平衡。")
    L.append("3. 复盘本周决策的 T+5 表现，讨论「评分门槛 60 分」是否合适。")
    L.append("4. 准备中期路演材料：净值曲线 vs 沪深300、持仓结构、策略调整记录。")
    L.append("")
    L.append("## 七、待人工处理")
    L.append("")
    pending = [d["decision_id"] for d in records.read_decisions() if d["decision_id"] not in conf]
    L.append(f"- 待确认决策：{'、'.join(pending) if pending else '无'}")
    L.append("")
    text = "\n".join(L)
    path = os.path.join(OUT, f"18_周会材料包_{week_label}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path, text


if __name__ == "__main__":
    p, _ = build_report_pack()
    print("报告素材包:", p)
    p2, _ = build_weekly_pack()
    print("周会材料包:", p2)
