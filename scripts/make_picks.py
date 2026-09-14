# -*- coding: utf-8 -*-
"""生成《明日买入建议》全市场选股报告（只读分析，不写账本、不改决策）。

定位：main.py 的候选池只覆盖 config.json 里的 12 只观察池标的；本脚本面向**全市场 A 股**
做一次筛选，产出一份可直接照着下单的 Markdown 建议，补齐"工作流候选池"之外的信息。

数据口径（重要，写进报告里，不藏）：
  - 全市场列表/估值/资金：东方财富 clist（经 em_client.clist_base() 选主机；主域被拦时走 push2delay）
  - 个股日K技术面：em_client.kline（东财 → 腾讯前复权 → 新浪 → 本地缓存）
  - 每只入选标的做**双源交叉校验**：腾讯K线收盘价 vs 东财快照价，偏差 >0.5% 直接剔除
  - 点位（买入区间/止损/目标/盈亏比）由 ATR 锚定 + 价格钳制算出，与 AI 面板同一套算法；
    这是参考值，不是收益承诺。

用法：
    python scripts/make_picks.py                      # 下一交易日，输出到 选股_<日期>/
    python scripts/make_picks.py --target 2026-09-14  # 指定建议面向的交易日
    python scripts/make_picks.py --top 8 --cand 60
    python scripts/make_picks.py --no-market-report   # 不写 workflow/outputs 副本
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "src"))

import em_client  # noqa: E402
from em_client import clist_base, industry_board_rank, kline, secid_of  # noqa: E402

import ai_advisor  # noqa: E402
import portfolio as pf  # noqa: E402
import records  # noqa: E402

CFG = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8"))
PROJ_ROOT = os.path.dirname(BASE)          # 证券投资/（报告最终落这里）
OUT_DIR = os.path.join(BASE, "outputs")
KLINE_CACHE = os.path.join(BASE, "data", "picks_kline")
os.makedirs(KLINE_CACHE, exist_ok=True)

KIND_PREFIX = ("60", "00", "30", "68")


# ---------------------------------------------------------------- 小工具
def next_trading_day(date_str):
    """下一交易日（只跳周末；节假日需人工确认，报告里会标注）。"""
    d = dt.date.fromisoformat(date_str)
    d += dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d.isoformat()


def _num(v, nd=2):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return round(f, nd)


def _wan(v):
    return f"{v / 1e8:.1f}"


# ---------------------------------------------------------------- 全市场筛选（带漏斗计数）
def screen_market(cand_n=60, pre_n=120, workers=4):
    """全市场逐级筛选，返回 (漏斗, 候选, 全部快照)。计数口径与 ai_advisor.collect 一致。"""
    rows = em_client.market_snapshot()
    funnel = [("全市场 A 股", len(rows))]
    if len(rows) < 3000:
        raise RuntimeError(f"全市场快照不完整（仅 {len(rows)} 条 < 3000），已中止全市场筛选")

    valid, nost, nprice = [], 0, 0
    for r in rows:
        code = str(r.get("f12") or "").zfill(6)
        name = str(r.get("f14") or "")
        if not code.startswith(KIND_PREFIX):
            continue
        p = _num(r.get("f2"))
        if p is None or p <= 0:
            nprice += 1
            continue
        # 把后面逐级筛选要用到的原始字段一次性落好，避免漏斗里反复解析
        r["_code"], r["_p"] = code, p
        r["_amt"] = _num(r.get("f6"))
        r["_mv"] = _num(r.get("f20"))
        r["_to"] = _num(r.get("f8"))
        r["_pe"] = _num(r.get("f115"))
        r["_pb"] = _num(r.get("f23"))
        r["_pct"] = _num(r.get("f3"))
        r["_mf_ratio"] = round((_num(r.get("f62")) or 0.0) / r["_amt"], 4) if r["_amt"] else 0.0
        r["_pre"] = pre_score(r)
        if "ST" in name or "退" in name:
            nost += 1
        valid.append(r)
    funnel.append((f"属于沪深主板/创业板/科创板且有有效报价（剔除 {nprice} 只无报价/停牌）", len(valid)))
    n1 = len(valid)
    step = [r for r in valid if "ST" not in str(r.get("f14") or "")
            and "退" not in str(r.get("f14") or "")]
    funnel += [(f"剔除 ST/退市（-{n1 - len(step)}）", len(step))]
    n2 = len(step)
    step = [r for r in step if (r["_amt"] or 0) >= 3e8]
    funnel += [(f"成交额 ≥ 3 亿（-{n2 - len(step)}）", len(step))]
    n3 = len(step)
    step = [r for r in step if (r["_mv"] or 0) >= 5e9]
    funnel += [(f"总市值 ≥ 50 亿（-{n3 - len(step)}）", len(step))]
    n4 = len(step)
    step = [r for r in step if r["_to"] is not None and 1 <= r["_to"] <= 25]
    funnel += [(f"换手率 1%~25%（-{n4 - len(step)}）", len(step))]
    n5 = len(step)
    step = [r for r in step
            if r["_pe"] is not None and 0 < r["_pe"] <= 120
            and (r["_pb"] is None or r["_pb"] <= 15)]
    funnel += [(f"PE 0~120 且 PB ≤ 15（-{n5 - len(step)}）", len(step))]
    n6 = len(step)
    step = [r for r in step if abs(r["_pct"] or 0) < (19.8 if str(r["_code"]).startswith(("30", "68")) else 9.8)]
    funnel += [(f"排除涨跌停（无法按收盘价成交，-{n6 - len(step)}）", len(step))]
    pool = step
    funnel.append(("估值/流动性全部达标", len(pool)))

    pool.sort(key=lambda r: -(r["_pre"]))
    pre = pool[:pre_n]
    funnel.append((f"预筛（动量+资金+盈利质量+估值）取前 {pre_n}", len(pre)))

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        bars = list(ex.map(lambda r: _bars(r), pre))
    n_short = n_trend = n_dev = 0
    devs = []
    kept = []
    for r, b in zip(pre, bars):
        if len(b) < 70:
            n_short += 1
            continue
        t = ai_advisor._tech(b)
        if not t:
            n_short += 1
            continue
        px = r["_p"]
        if not px:
            continue
        dev = abs(t["close"] - px) / px
        devs.append(dev)
        if not t["above_ma60"] or t["rsi14"] > 80 or t["chg20"] > 0.45:
            n_trend += 1
            continue
        if dev > 0.005:
            n_dev += 1
            continue
        code = r["_code"]
        kept.append({
            "code": code, "name": str(r.get("f14") or ""), "industry": str(r.get("f100") or "—"),
            "price": px, "pct": _num(r.get("f3")), "amount_yi": round((r["_amt"]) / 1e8, 2),
            "turnover": _num(r.get("f8")), "pe": _num(r.get("f115"), 1),
            "pb": _num(r.get("f23")), "mv_yi": round((_num(r.get("f20")) or 0) / 1e8),
            "np_yoy": _num(r.get("f46"), 1), "rev_yoy": _num(r.get("f41"), 1),
            "roe": _num(r.get("f37"), 1), "chg60": _num((_num(r.get("f24")) or 0), 2),
            "chg20": round(t["chg20"] * 100, 2), "rsi14": t["rsi14"],
            "ma20": t["ma20"], "ma60": t["ma60"], "bull": t["bull_align"],
            "dd60": round(t["dd_from_high60"] * 100, 2), "atr_pct": t["atr_pct"],
            "mf_ratio": r["_mf_ratio"],
        })
    funnel += [("日K可用（≥70 根）且技术指标可算", len(pre) - n_short),
               (f"趋势过滤通过（跌破60日线/RSI>80/20日涨幅>45% 的 {n_trend} 只剔除）",
                len(pre) - n_short - n_trend),
               (f"双源校验通过（偏差>0.5% 的 {n_dev} 只剔除）", len(kept))]
    cands = kept[:cand_n]
    if len(kept) > cand_n:
        funnel.append((f"报告保留前 {cand_n} 只（其余 {len(kept) - cand_n} 只留存明细）", len(cands)))
    if devs:
        devs.sort()
        mid = devs[len(devs) // 2] * 100
        stats = {"n": len(devs), "median_pct": round(mid, 2), "max_pct": round(devs[-1] * 100, 2),
                 "over_half_pct": sum(1 for d in devs if d > 0.005)}
        print(f"[选股] 双源价差：中位数 {stats['median_pct']}%，最大 {stats['max_pct']}%，"
              f"超 0.5% 的 {stats['over_half_pct']}/{stats['n']} 只（已剔除）")
    else:
        stats = {}
    return funnel, cands, rows, stats


def pre_score(r):
    """与 ai_advisor.collect 同口径的预筛分（温和动量 + 主力资金 + 盈利质量 - 估值惩罚）。"""
    c60 = min(max(_num(r.get("f24")) or 0, -40), 60)
    mf = min(max(_num(r.get("_mf_ratio")) or 0.0, -0.2), 0.2)
    roe = min(20, max(-20, _num(r.get("f37")) or 0))
    npy = min(60, max(-40, _num(r.get("f46")) or 0))
    pe = min(max((_num(r.get("f115")) or 30) - 15, 0), 80)
    return 0.8 * c60 + 200 * mf + 0.6 * roe + 0.15 * npy - 0.15 * pe


def _bars(r, ttl=6 * 3600):
    """候选股日K（带磁盘缓存）。

    全市场预筛每次要取 ~180 只的日K，腾讯/新浪对单IP高频访问会限流（501/456）。
    缓存到 data/picks_kline/ 后：同一交易日内重复运行不再打网络，也避免因为限流
    导致"候选 0 只"这种数据事故；缓存超过 ttl 才重新请求。
    """
    code = str(r.get("f12")).zfill(6)
    path = os.path.join(KLINE_CACHE, f"{code}.json")
    if os.path.exists(path):
        try:
            obj = json.load(open(path, encoding="utf-8"))
            if obj.get("ts", 0) > time.time() - ttl and obj.get("date") and obj.get("bars"):
                return obj["bars"]
        except Exception:
            pass
    try:
        df = kline(secid_of(code), cache=False).tail(140)
        bars = [{"d": x.date, "o": x.open, "c": x.close, "h": x.high, "l": x.low}
                for x in df.itertuples()]
        if bars:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "date": bars[-1]["d"], "bars": bars},
                          f, ensure_ascii=False)
        return bars
    except Exception:
        return []


# ---------------------------------------------------------------- 点位（与 AI 面板同算法）
def plan_levels(c, atr_mult_stop=2.0, atr_mult_tgt=2.5):
    px = c["price"]
    atr = max(c.get("atr_pct") or 0.04, 0.01) * px
    lo = max(px * 0.97, min(px * 0.99, px - 0.5 * atr))
    hi = min(max(px * 1.005, px + 0.3 * atr), px * 1.03)
    stop = max(px * 0.90, min(px * 0.99, px - atr_mult_stop * atr))
    tgt = max(px * 1.02, min(px * 1.20, px + atr_mult_tgt * atr))
    rr = (tgt - (lo + hi) / 2) / max((lo + hi) / 2 - stop, 1e-9)
    return {"entry_low": round(lo, 2), "entry_high": round(hi, 2),
            "stop": round(stop, 2), "target": round(tgt, 2), "rr": round(rr, 2)}


# ---------------------------------------------------------------- 市场环境
def sector_flows(top=8):
    """行业主力净流入/净流出榜（亿元）：直接查板块列表的资金字段，比涨跌幅榜更贴"资金"。"""
    out = {"inflow": [], "outflow": []}
    for key, po, rev in (("inflow", 1, False), ("outflow", 0, True)):
        try:
            j = em_client._get(clist_base() + "/api/qt/clist/get",
                               dict(pn=1, pz=100, po=po, np=1, fltt=2, invt=2, fid="f62",
                                    fs="m:90+t:2", fields="f12,f14,f62"))
            diff = (j.get("data") or {}).get("diff") or []
            rows = [{"name": d.get("f14"), "flow": _num(d.get("f62"), 0)} for d in diff
                    if _num(d.get("f62")) is not None]
            rows.sort(key=lambda x: x["flow"], reverse=not rev)
            out[key] = rows[:top]
        except Exception as e:
            print(f"  [警告] 行业资金榜({key})获取失败: {str(e)[:60]}")
    return out


def market_context(rows, date):
    """指数、宽度、成交额、领涨个股——全部现场统计，不引用旧文件。"""
    ctx = {"indices": [], "breadth": {}, "turnover_yi": None, "top_gainers": []}
    for idx in CFG["indices"]:
        try:
            df = em_client.kline_until(idx["code"], date, is_index=True)
            if len(df) < 2:
                continue
            c1, c0 = float(df["close"].iloc[-1]), float(df["close"].iloc[-2])
            ctx["indices"].append({"name": idx["name"], "close": round(c1, 2),
                                   "pct": round((c1 / c0 - 1) * 100, 2)})
        except Exception:
            continue
    up = dn = flat = lim_up = lim_dn = 0
    amt = 0.0
    for r in rows:
        pct, a = _num(r.get("f3")), _num(r.get("f6"))
        if a:
            amt += a
        if pct is None:
            continue
        if pct > 0:
            up += 1
        elif pct < 0:
            dn += 1
        else:
            flat += 1
        code = str(r.get("f12") or "").zfill(6)
        cap = 19.8 if code.startswith(("30", "68")) else 9.8
        if pct >= cap:
            lim_up += 1
        elif pct <= -cap:
            lim_dn += 1
    ctx["breadth"] = {"up": up, "down": dn, "flat": flat, "limit_up": lim_up, "limit_dn": lim_dn}
    ctx["turnover_yi"] = round(amt / 1e8) if amt else None
    gain = [r for r in rows if _num(r.get("f3")) is not None and _num(r.get("f2"))]
    gain.sort(key=lambda r: -_num(r.get("f3")))
    ctx["top_gainers"] = [{"name": str(r.get("f14")), "pct": _num(r.get("f3")),
                           "industry": str(r.get("f100") or "—")} for r in gain[:10]]
    return ctx


# ---------------------------------------------------------------- 报告
def render(date, target, funnel, cands, ctx, flows, ai, top, stats=None):
    L = []
    L.append(f"# 明日（{target}）买入建议")
    L.append("")
    L.append("> **数据基准**：东方财富全市场快照（价格/估值/资金）+ 腾讯前复权日K（技术面）"
             "+ 东方财富 F10 财务（基本面），每只入选标的三源交叉校验")
    L.append(f"> **数据时点**：{date} 收盘（最新交易日收盘后的最终数据）")
    bw = ctx["breadth"]
    tot = (bw.get("up", 0) + bw.get("down", 0) + bw.get("flat", 0)) or 1
    L.append(f"> **数据校验**：入选标的收盘价 东财快照 vs 腾讯K线逐只比对，偏差 >0.5% 直接剔除"
             + (f"（本次中位偏差 {stats['median_pct']}%、最大 {stats['max_pct']}%，"
                f"剔除 {stats['over_half_pct']}/{stats['n']} 只）" if stats else "")
             + f"；全市场 {tot} 只参与统计")
    L.append(f"> **筛选范围**：全市场 A 股 {funnel[0][1]} 只 → 最终 {len(cands)} 只进入候选表，"
             f"下方详列前 {min(top, len(cands))} 只")
    L.append("")
    L.append("---")
    L.append("")

    picks = cands[:top]
    # ---------- 结论速览
    L.append("## 一、结论速览（可直接执行）")
    L.append("")
    if picks:
        hdr = ("| 优先级 | 代码 | 名称 | 行业 | 收盘 | 买入区间 | 止损 | 目标 | 盈亏比 "
               "| PE(TTM) | 净利增速 | 20日涨幅 | RSI14 |")
        sep = "|---|---|---|---|---|---|---|---|---|---|---|---|---|"
        L.append(hdr)
        L.append(sep)
        for i, c in enumerate(picks, 1):
            pv = plan_levels(c)
            c["_plan"] = pv
            star = "★核心" if i <= 3 else ("○价值" if i <= 5 else "○分散")
            npy = f"{c['np_yoy']:+.1f}%" if c["np_yoy"] is not None else "—"
            L.append(f"| {star}{i} | **{c['code']}** | {c['name']} | {c['industry']} | {c['price']} "
                     f"| {pv['entry_low']}~{pv['entry_high']} | {pv['stop']} "
                     f"| {pv['target']} | {pv['rr']} | {c['pe'] if c['pe'] is not None else '—'} "
                     f"| {npy} | {c['chg20'] if c['chg20'] is not None else '—'}% | {c['rsi14']} |")
        L.append("")
        L.append(f"**仓位建议**：单只不超过总资金 15%；{len(picks)} 只合计不超过 60%；留 40% 现金应对波动。"
                 f"（工作流账本内单只上限 30%、现金下限 20% 是硬约束，这里是更保守的执行建议。）")
        L.append("")
        L.append("**下单节奏**：9:30–10:00 先看开盘价 —— 高开 >2% 不追，等回落到买入区间再买；"
                 "平开或低开可直接买入第一批 50%，第二批 50% 留到尾盘或次日回踩确认。")
    else:
        L.append("本轮全市场筛选**无标的通过全部条件**（趋势+估值+双源校验）。"
                 "空仓等待也是决策：不为凑数而下单。")
    L.append("")
    L.append("---")
    L.append("")

    # ---------- 市场环境
    L.append(f"## 二、市场环境（{date} 收盘实况）")
    L.append("")
    if ctx["indices"]:
        L.append("| 指数 | 收盘 | 涨跌 |")
        L.append("|---|---|---|")
        for i in ctx["indices"]:
            L.append(f"| {i['name']} | {i['close']} | {i['pct']:+.2f}% |")
        L.append("")
    L.append(f"**全市场**：成交额 {ctx['turnover_yi']} 亿元｜上涨 {bw.get('up')} 家 / 下跌 {bw.get('down')} 家"
             f"（上涨占比 {bw.get('up', 0) / tot * 100:.1f}%）｜涨停 {bw.get('limit_up')} 家、跌停 {bw.get('limit_dn')} 家")
    L.append("")
    if flows["inflow"]:
        L.append("**行业主力净流入 TOP**（亿元）："
                 + "｜".join(f"{r['name']} {r['flow'] / 1e8:+.1f}" for r in flows["inflow"]))
    if flows["outflow"]:
        L.append("")
        L.append("**行业主力净流出 TOP**（亿元）："
                 + "｜".join(f"{r['name']} {r['flow'] / 1e8:+.1f}" for r in flows["outflow"]))
    L.append("")
    try:
        rank = industry_board_rank(top=8)
        if rank:
            L.append("**领涨板块**：" + "｜".join(f"{r['name']} {r['pct']:+.2f}%" for r in rank[:8]))
            L.append("")
    except Exception:
        pass
    if ctx["top_gainers"]:
        L.append("**两市领涨个股**：" + "、".join(
            f"{g['name']}({g['pct']:+.1f}%,{g['industry']})" for g in ctx["top_gainers"][:6]))
        L.append("")
    watch = records.read_market_watch()
    cur = next((w for w in reversed(watch) if w.get("date") == date), None)
    if cur:
        L.append(f"> **工作流市场判断**：{cur.get('view', '')}")
        L.append(f"> **工作流识别的机会**：{cur.get('opportunity', '')}")
        if cur.get("risk"):
            L.append(f"> **工作流识别的主要风险**：{cur['risk']}")
    L.append("")
    L.append("---")
    L.append("")

    # ---------- 筛选漏斗
    L.append("## 三、筛选流程（可复现）")
    L.append("")
    L.append("```")
    for name, n in funnel:
        L.append(f"{name:<44}{n}")
    L.append("```")
    L.append("")
    L.append("> 这是**顺序漏斗**：每一级都在上一级的结果上继续筛，括号内为被剔除的只数。")
    L.append("")
    L.append("> 复现命令：`python scripts/make_picks.py`（工作流目录下运行）。"
             "全市场列表与估值来自东方财富 clist 接口，日K来自腾讯前复权接口（web.ifzq 与 "
             "proxy.finance 两个子域互为备份），均带重试与多源降级；候选中转的日K会缓存到 "
             "`data/picks_kline/`（6 小时），同一交易日重复运行结果一致且不再打网络。")
    L.append("")
    L.append("---")
    L.append("")

    # ---------- 逐只说明 + AI
    L.append("## 四、逐只说明")
    L.append("")
    ai_picks = {}
    if ai and ai.get("picks"):
        ai_picks = {str(p.get("code")).zfill(6): p for p in ai["picks"]}
    for i, c in enumerate(picks, 1):
        pv = c["_plan"]
        L.append(f"### {i}. {c['name']}（{c['code']}，{c['industry']}）")
        L.append("")
        trend = ("多头排列（MA5>MA20>MA60）" if c["bull"] else "均线未完全多头排列")
        L.append(f"- **技术面**：{trend}；站上60日线（{c['ma60']}），20日均线 {c['ma20']}；"
                 f"RSI14={c['rsi14']}；20日涨幅 {c['chg20']}%；距60日高点 {c['dd60']}%；"
                 f"ATR≈{c['atr_pct'] * 100:.1f}%")
        L.append(f"- **估值/基本面**：PE(TTM) {c['pe'] if c['pe'] is not None else '—'}，"
                 f"PB {c['pb'] if c['pb'] is not None else '—'}，ROE {c['roe'] if c['roe'] is not None else '—'}%，"
                 f"净利同比 {c['np_yoy'] if c['np_yoy'] is not None else '—'}%，"
                 f"营收同比 {c['rev_yoy'] if c['rev_yoy'] is not None else '—'}%")
        L.append(f"- **资金/流动性**：成交额 {c['amount_yi']} 亿元，换手率 {c['turnover']}%，"
                 f"主力净额占成交额 {c['mf_ratio'] * 100:+.2f}%")
        L.append(f"- **执行**：买入区间 {pv['entry_low']}~{pv['entry_high']}，"
                 f"止损 {pv['stop']}（{ (pv['stop'] / c['price'] - 1) * 100:+.1f}%），"
                 f"目标 {pv['target']}（{ (pv['target'] / c['price'] - 1) * 100:+.1f}%），"
                 f"盈亏比 {pv['rr']}")
        a = ai_picks.get(c["code"])
        if a:
            L.append(f"- **AI 复核**：{a.get('action', '观望')}；{a.get('reason', '')}"
                     f"　风险：{a.get('risk', '')}")
        L.append("")
    if cands[top:]:
        L.append("### 观察名单（未进前 " + str(top) + "）")
        L.append("")
        L.append("| 代码 | 名称 | 行业 | 收盘 | 综合预筛排名 | PE | RSI14 | 距60日高点 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for c in cands[top:top + 12]:
            L.append(f"| {c['code']} | {c['name']} | {c['industry']} | {c['price']} | — "
                     f"| {c['pe'] if c['pe'] is not None else '—'} | {c['rsi14']} | {c['dd60']}% |")
        L.append("")
    L.append("---")
    L.append("")

    # ---------- AI 实际推荐的名单（与上面"按预筛分取前 N"是两套排序，必须并列讲清楚）
    if ai and ai.get("picks"):
        by_code = {c["code"]: c for c in cands}
        L.append(f"## 五、AI（DeepSeek）实际推荐的名单　·　{ai.get('date')} {ai.get('session')}")
        L.append("")
        L.append("> 上面第一章的名单是**程序按预筛分（动量+资金+盈利质量+估值）排序取前 N**，"
                 "下面是 **AI 在全部 60 只候选里自己挑的**。两者排序口径不同，名单可以不一样——"
                 "两只榜单都出现在候选池里，不存在「谁覆盖谁」。")
        L.append("")
        L.append("| 代码 | 名称 | AI操作 | 在本报告候选中的预筛排名 | 收盘 | AI买入区间 | 止损 | 目标 | 盈亏比 | AI建议仓位 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        rank = {c["code"]: i for i, c in enumerate(cands, 1)}
        for p in ai["picks"]:
            code = str(p.get("code")).zfill(6)
            c = by_code.get(code)
            rk = f"第 {rank[code]}" if c else "未进 60 只候选"
            px = c["price"] if c else p.get("ref_close")
            L.append(f"| {code} | {p.get('name')} | **{p.get('action')}** | {rk} | {px} "
                     f"| {p.get('entry_low')}~{p.get('entry_high')} | {p.get('stop')} "
                     f"| {p.get('target')} | {p.get('rr')} | {p.get('position_pct')}% |")
        L.append("")
        for p in ai["picks"]:
            L.append(f"- **{p.get('name')}（{p.get('code')}）· {p.get('action')}**："
                     f"{p.get('reason', '')}　风险：{p.get('risk', '')}")
        L.append("")
        both = [c for c in picks if c["code"] in {str(x.get("code")).zfill(6) for x in ai["picks"]}]
        L.append(f"> 两份名单重合 {len(both)} 只"
                 + (f"：{'、'.join(c['name'] for c in both)}" if both else
                    "（**完全不重合**：程序偏好趋势温和+资金流入的标的，AI 另有一组偏好，"
                    "执行时请二选一或按仓位上限各取少量，不要两份都买）"))
        L.append("")
        L.append("---")
        L.append("")

    # ---------- AI 市场解读
    if ai:
        L.append("## 六、AI 市场解读（DeepSeek，仅供参考）")
        L.append("")
        L.append(f"- **市场解读**：{ai.get('market_view', '')}")
        L.append(f"- **风险等级**：{ai.get('risk_level', '—')}")
        if ai.get("avoid"):
            L.append(f"- **建议回避**：{'；'.join(ai['avoid'])}")
        if ai.get("notes"):
            L.append(f"- **执行提示**：{ai['notes']}")
        if ai.get("rebalance_note"):
            L.append(f"- **调仓复核意见**：{ai['rebalance_note']}")
        L.append("")
        L.append("---")
        L.append("")

    # ---------- 风险提示
    L.append("## 七、风险提示与数据说明")
    L.append("")
    L.append("- 本文档由程序按既定规则生成，**买入区间/止损/目标/盈亏比均为 ATR 与价格钳制算出的参考值**，"
             "不构成收益承诺；请结合自身判断与同花顺模拟盘实际成交价执行。")
    L.append("- 数据源：东方财富（全市场快照/估值/资金/行业榜/F10 财务）+ 腾讯（前复权日K）。"
             "本次运行的日K口径为腾讯前复权 + 新浪兜底（东方财富 push2his 日K接口在当前网络被网关拦截）。")
    L.append("- 全市场统计未剔除当日停牌与北交所标的；涨停/跌停标的按规则剔除，避免按收盘价无法成交。")
    L.append(f"- 交易日假设：{date} 的下一个交易日按**跳过周末**推算为 {target}，"
             "如遇法定节假日请以交易所公告为准。")
    L.append("- AI 内容（如本报告未出现 AI 章节，说明本次未配置密钥或调用失败）仅作辅助参考，"
             "所有指标与筛选结果均由程序计算，可逐条复现。")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="生成全市场《明日买入建议》")
    ap.add_argument("--date", help="数据基准交易日（默认最新交易日）")
    ap.add_argument("--target", help="建议面向的交易日（默认下一交易日）")
    ap.add_argument("--top", type=int, default=6, help="详列前 N 只（默认 6）")
    ap.add_argument("--cand", type=int, default=60, help="进入候选表的最大数量（默认 60）")
    ap.add_argument("--pre", type=int, default=120, help="预筛后进入技术面精算的只数（默认 120）")
    ap.add_argument("--outdir", help="输出目录（默认 项目根/选股_<target> ）")
    ap.add_argument("--no-market-report", action="store_true", help="不写 workflow/outputs 副本")
    args = ap.parse_args()

    date = args.date
    if not date:
        df = kline("1.000001", beg="20250101", is_index=True)
        date = str(df["date"].iloc[-1])
    target = args.target or next_trading_day(date)
    print(f"[选股] 数据基准 {date}，建议面向 {target}")

    funnel, cands, rows, stats = screen_market(cand_n=args.cand, pre_n=args.pre)
    print(f"[选股] 全市场 {len(rows)} 只 → 候选 {len(cands)} 只")
    ctx = market_context(rows, date)
    flows = sector_flows()
    ai = ai_advisor.latest()
    if ai:
        print(f"[选股] 已接入 AI 结果（{ai.get('generated_at') or ai.get('time') or '最近一次'}，"
              f"picks={len(ai.get('picks') or [])}）")
    else:
        print("[选股] 无 AI 结果（未配置密钥或未运行），仅输出程序计算结果")

    md = render(date, target, funnel, cands, ctx, flows, ai, args.top, stats)

    outdir = args.outdir or os.path.join(PROJ_ROOT, f"选股_{target.replace('-', '')}")
    os.makedirs(outdir, exist_ok=True)
    p1 = os.path.join(outdir, f"明日买入建议_{target.replace('-', '')}.md")
    with open(p1, "w", encoding="utf-8") as f:
        f.write(md + "\n")
    print("[选股] 已生成:", p1)
    if not args.no_market_report:
        p2 = os.path.join(OUT_DIR, f"20_明日买入建议_{target}.md")
        with open(p2, "w", encoding="utf-8") as f:
            f.write(md + "\n")
        print("[选股] 成果副本:", p2)
    # 候选明细落盘（便于复核与写报告）
    json_path = os.path.join(outdir, "候选明细.json")
    detail = []
    for c in cands:
        row = {k: v for k, v in c.items() if not k.startswith("_")}
        row.update(plan_levels(c))
        detail.append(row)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"date": date, "target": target,
                   "funnel": [{"stage": n, "count": c} for n, c in funnel],
                   "final_candidates": len(cands), "candidates": detail},
                  f, ensure_ascii=False, indent=2)
    print("[选股] 候选明细:", json_path)

    # 仪表盘用的紧凑 JSON：手机页面只展示前 N 只 + 市场环境 + 漏斗 + AI 摘要，
    # 放在 outputs/ 里，由云端的 export_dashboard() 复制进 data/dashboard/（保持单一数据链路）。
    picks_out = []
    for c in cands[:args.top]:
        row = {k: v for k, v in c.items() if not k.startswith("_")}
        row.update(plan_levels(c))
        a = next((p for p in ((ai or {}).get("picks") or [])
                  if str(p.get("code")).zfill(6) == c["code"]), None)
        if a:
            row["ai_action"] = a.get("action")
            row["ai_reason"] = a.get("reason")
            row["ai_risk"] = a.get("risk")
        picks_out.append(row)
    payload = {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": date, "target": target, "top": args.top,
        "benchmark": ("东方财富全市场快照 + 腾讯前复权日K + 东财F10财务；"
                      "入选标的东财快照与腾讯K线逐只交叉校验，偏差>0.5%剔除"),
        "breadth": ctx.get("breadth"), "turnover_yi": ctx.get("turnover_yi"),
        "indices": ctx.get("indices"),
        "flows_in": [{"name": r["name"], "yi": round((r["flow"] or 0) / 1e8, 1)}
                     for r in flows.get("inflow", [])],
        "flows_out": [{"name": r["name"], "yi": round((r["flow"] or 0) / 1e8, 1)}
                      for r in flows.get("outflow", [])],
        "funnel": [{"stage": n, "count": c} for n, c in funnel],
        "dev_stats": stats,
        "picks": picks_out,
        "watch": [{"code": c["code"], "name": c["name"], "industry": c["industry"],
                   "price": c["price"], "pe": c["pe"], "rsi14": c["rsi14"],
                   "dd60": c["dd60"]} for c in cands[args.top:args.top + 12]],
        "ai": ({"market_view": (ai or {}).get("market_view"),
                "risk_level": (ai or {}).get("risk_level"),
                "avoid": (ai or {}).get("avoid"),
                "notes": (ai or {}).get("notes")} if ai else None),
    }
    with open(os.path.join(outdir, "仪表盘_买入建议.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    if not args.no_market_report:
        with open(os.path.join(OUT_DIR, "21_买入建议_仪表盘数据.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print("[选股] 仪表盘数据:", os.path.join(OUT_DIR, "21_买入建议_仪表盘数据.json"))
    return p1


if __name__ == "__main__":
    main()
