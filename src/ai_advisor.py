# -*- coding: utf-8 -*-
"""DeepSeek Flash 智能分析：把已采集的可靠行情压缩成上下文，生成"市场解读 + 买入推荐"。

数据准确性设计（三层）：
  1) 价格来自东财快照（权威口径），技术指标来自腾讯前复权日K；
  2) 每只候选都做双源交叉校验，收盘价偏差 >0.5% 的直接剔除，不送进模型；
  3) 模型返回的推荐做"防幻觉"清洗：代码必须存在于给定候选表、价格必须锚定真实收盘价。

密钥安全（API Key 绝不外泄）：
  - 只从环境变量 DEEPSEEK_API_KEY 读取，不写入任何文件、不落盘、不进 git；
  - 所有 print（含异常文本）统一经 _redact() 过滤，防止泄漏到 Actions 日志；
  - 落盘前再次扫描序列化结果，命中密钥则拒绝写入并报警。
"""
import datetime as dt
import json
import os
import time

import requests

from em_client import (industry_board_rank, kline, kline_until, market_snapshot,
                       secid_of, snapshot)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
DASH = os.path.join(DATA, "dashboard")
ADVICE = os.path.join(DATA, "ai_advice.jsonl")
CONFIG = os.path.join(BASE, "config.json")

API_URL = "https://api.deepseek.com/chat/completions"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


# ---------------------------------------------------------------- 基础工具
def cfg():
    return json.load(open(CONFIG, encoding="utf-8")).get("ai", {})


def _key():
    return (os.environ.get("DEEPSEEK_API_KEY") or "").strip()


def _redact(s):
    """把密钥从任意文本中抹掉（日志/异常安全兜底）。"""
    k = _key()
    s = str(s)
    return s.replace(k, "***REDACTED***") if k else s


def _log(*a):
    print(_redact(" ".join(str(x) for x in a)))


def active(today=None):
    """运行期守卫：超过 run_until 自动停止 AI 分析（默认 2026-12-18）。"""
    until = cfg().get("run_until", "2026-12-18")
    today = today or dt.date.today().isoformat()
    return today <= until, until


def session_now(now=None):
    """按北京时间判定分析时段：盘前 / 盘中 / 盘后。"""
    now = now or dt.datetime.now()
    hm = now.hour * 60 + now.minute
    if hm < 9 * 60 + 25:
        return "premarket"
    if hm <= 15 * 60:
        return "intraday"
    return "eod"


def _bars(code, n=140):
    """候选股日K，不落盘缓存（避免候选股每天新增CSV把仓库撑大）。

    数据源交给 em_client.kline 统一负责：东财 → 腾讯 → 新浪 三级降级，
    任一源可用即可拿到技术面数据（实测腾讯被限流、东财被网关拦截时新浪仍可用）。
    """
    try:
        df = kline(secid_of(code), cache=False).tail(n)
        return [{"d": r.date, "o": r.open, "c": r.close, "h": r.high, "l": r.low}
                for r in df.itertuples()]
    except Exception:
        return []


def _tech(bars):
    """由日K算趋势/动量指标（全部基于真实收盘价）。"""
    c = [b["c"] for b in bars]
    if len(c) < 60:
        return None
    ma = lambda n: sum(c[-n:]) / n
    ma5, ma20, ma60 = ma(5), ma(20), ma(60)
    dif = [c[i] - c[i - 1] for i in range(1, len(c))]
    up = sum(max(x, 0) for x in dif[-14:]) / 14
    dn = sum(max(-x, 0) for x in dif[-14:]) / 14
    rsi = 100.0 if dn == 0 else 100 * up / (up + dn)
    hi60 = max(b["h"] for b in bars[-60:])
    atr = sum(b["h"] - b["l"] for b in bars[-14:]) / 14
    return {"close": round(c[-1], 3), "ma20": round(ma20, 3), "ma60": round(ma60, 3),
            "rsi14": round(rsi, 1), "chg20": round(c[-1] / c[-21] - 1, 4),
            "chg60": round(c[-1] / c[-61] - 1, 4),
            "dd_from_high60": round(c[-1] / hi60 - 1, 4), "atr_pct": round(atr / c[-1], 4),
            "bull_align": ma5 > ma20 > ma60, "above_ma60": c[-1] > ma60}


# ---------------------------------------------------------------- 上下文组装
def collect(date, cand_n=40):
    """组装 AI 上下文：市场宽度 + 指数 + 板块 + 组合 + 双源校验过的候选股。"""
    rows = market_snapshot()
    stocks, up, dn, flat = [], 0, 0, 0
    for r in rows:
        try:
            p, pct = float(r["f2"]), float(r["f3"])
            code = str(r["f12"]).zfill(6)
        except (TypeError, ValueError):
            continue
        name = str(r.get("f14") or "")
        if pct > 0:
            up += 1
        elif pct < 0:
            dn += 1
        else:
            flat += 1
        if "ST" in name or "退" in name or not code.startswith(("60", "00", "30", "68")):
            continue
        try:
            amt, mv, to = float(r["f6"]), float(r["f20"]), float(r["f8"])
            pe, pb = float(r["f115"]), float(r["f23"])
        except (TypeError, ValueError):
            continue
        if amt < 3e8 or mv < 5e9 or not (1 <= to <= 25) or not (0 < pe <= 120) or not (0 < pb <= 15):
            continue
        if abs(pct) >= (19.8 if code.startswith(("30", "68")) else 9.8):
            continue  # 涨跌停无法按收盘价成交
        try:
            mf = float(r["f62"]) / amt
        except (TypeError, ValueError):
            mf = 0.0
        num = lambda k, d=1: (round(float(r[k]), d)
                              if str(r.get(k, "-")) not in ("-", "None", "") else None)
        stocks.append({"code": code, "name": name, "ind": r.get("f100") or "",
                       "price": p, "pct": pct, "amt_yi": round(amt / 1e8, 1),
                       "turn": to, "pe": round(pe, 1), "pb": round(pb, 2),
                       "mv_yi": round(mv / 1e8), "roe": num("f37"),
                       "np_yoy": num("f46", 0), "rev_yoy": num("f41", 0),
                       "chg60s": num("f24"), "mf_ratio": round(mf, 4)})

    # 预筛：温和动量 + 主力资金 + 盈利质量 + 估值，取前 cand_n 只做技术面精算。
    # 60日涨幅设上限（不做抛物线追高），并加入估值惩罚，避免候选池全是爆炒股。
    def pre(s):
        c60 = min(max(s["chg60s"] or 0, -40), 60)
        return (c60 * 0.8
                + max(-0.2, min(0.2, s["mf_ratio"])) * 200
                + min(20, max(-20, s["roe"] or 0)) * 0.6
                + min(60, max(-40, s["np_yoy"] or 0)) * 0.15
                - min(max(s["pe"] - 15, 0), 80) * 0.15)
    stocks.sort(key=pre, reverse=True)

    # 并行取候选日K（串行 120 只要 30~45 秒，并行后约 6~10 秒，降低云端超时风险）
    import concurrent.futures as cf
    slice_ = stocks[:cand_n * 3]
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        barlist = list(ex.map(lambda s: _bars(s["code"]), slice_))

    cands, dropped = [], 0
    for s, bars in zip(slice_, barlist):
        if len(cands) >= cand_n:
            break
        if len(bars) < 70:
            continue
        t = _tech(bars)
        if not t:
            continue
        # 双源校验：腾讯K线收盘 vs 东财快照价，偏差过大说明数据可疑 → 剔除
        dev = abs(t["close"] - s["price"]) / s["price"] if s["price"] else 1
        if dev > 0.005:
            dropped += 1
            continue
        if not t["above_ma60"] or t["rsi14"] > 80 or t["chg20"] > 0.45:
            continue  # 趋势过滤：跌破60日线 / 极端超买 / 短期暴涨不推荐
        s.update(t)
        cands.append(s)

    idx = {}
    for secid, nm in (("1.000001", "上证指数"), ("0.399001", "深证成指"),
                      ("1.000300", "沪深300"), ("1.000688", "科创50")):
        try:
            df = kline_until(secid, date, is_index=True).tail(2)
            if len(df) == 2:
                idx[nm] = {"close": round(float(df["close"].iloc[-1]), 2),
                           "pct": round(float(df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100, 2)}
        except Exception:
            pass

    import portfolio as pf
    import records
    st = json.load(open(CONFIG, encoding="utf-8"))["strategy"]
    acct = pf.replay(pf.read_trades(), st["initial_capital"])
    # 持仓估值：优先用全市场快照里的真实成交价；ETF 等不在A股快照中的标的单独取快照
    prices = {}
    for c, p in acct["positions"].items():
        px = next((s["price"] for s in stocks if s["code"] == c), None)
        if not px:
            try:
                px = snapshot(("1." if c.startswith(("6", "9", "5")) else "0.") + c).get("price")
            except Exception:
                px = None
        prices[c] = float(px) if px else p["cost"]
    total = pf.total_assets(acct, prices)
    port = {"cash_pct": round(acct["cash"] / total * 100, 1) if total else 0,
            "total_assets": total, "return_pct": round((total / st["initial_capital"] - 1) * 100, 2),
            "positions": [{"code": c, "name": p["name"], "shares": p["shares"],
                           "cost": round(p["cost"], 3), "price": prices.get(c)}
                          for c, p in acct["positions"].items()]}

    try:
        top = [{"name": b["name"], "pct": b["pct"]} for b in industry_board_rank(5)]
        bot = [{"name": b["name"], "pct": b["pct"]} for b in industry_board_rank(5, asc=True)]
    except Exception:
        top, bot = [], []

    return {"date": date, "indices": idx,
            "breadth": {"上涨": up, "下跌": dn, "平盘": flat,
                        "上涨占比": round(up / max(up + dn, 1) * 100, 1),
                        "样本": len(rows)},
            "boards_top": top, "boards_bottom": bot, "portfolio": port,
            "candidates": cands,
            "data_quality": {"来源": ["东方财富快照（价格）", "日K：东财→腾讯→新浪三级降级"],
                             "校验": "逐只比对K线收盘价与快照价，偏差>0.5%剔除",
                             "剔除数据可疑": dropped, "候选数": len(cands)}}


# ---------------------------------------------------------------- 提示词与调用
SYSTEM = ("你是严谨的A股量化分析师。只依据用户提供的真实行情数据分析，"
          "绝不编造代码、名称或数字。直接输出JSON对象，不要markdown代码块、不要多余文字。")

SCHEMA = """输出JSON结构（严格遵守字段名）：
{"market_view":"150字内市场解读","risk_level":"偏低|中性|偏高",
 "picks":[{"code":"6位代码","name":"名称","action":"买入|观望|回避",
   "entry_low":数字,"entry_high":数字,"stop":数字,"target":数字,
   "horizon":"持有周期如1-3周","position_pct":整数(占组合%%,0-20),
   "reason":"买入理由,<=80字","risk":"主要风险,<=50字"}],
 "avoid":["需回避的方向,<=3条"],"notes":"执行提示,<=80字"}
要求：picks 最多6只，按信心从高到低排序；代码必须出自 candidates；
entry_low/entry_high 应在现价附近(±3%%)；stop 不低于现价的-10%%；target 不高于现价的+20%%；
必须结合给出的技术指标(均线/RSI/距60日高点)与市场宽度，不要只看涨幅。"""


def _messages(ctx, session):
    label = {"premarket": "盘前计划（用上一交易日收盘数据，给出今日可执行的买入计划）",
             "intraday": "盘中分析（数据为当前盘中快照，需在收盘前完成操作窗口）",
             "eod": "收盘复盘（数据为今日收盘，给出下一交易日计划）"}[session]
    user = (f"任务：{label}\n\n以下是系统采集并经双源校验的真实数据（JSON）：\n"
            + json.dumps(ctx, ensure_ascii=False, separators=(",", ":")) + "\n\n" + SCHEMA)
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def _call(messages, retries=None):
    """调用 DeepSeek Chat Completions（OpenAI 兼容）。返回 (文本, usage)。

    deepseek-v4-flash 是推理模型：默认会把内容写进 reasoning_content 而 content 可能为空，
    且思考会先吃掉 max_tokens，容易导致 JSON 被截断。因此显式传
    {"thinking": {"type": "disabled"}} 关闭思考——实测输出更稳定、token 消耗从 230 降到 9。
    若接口未来不再接受该参数，自动去掉后重试一次（不影响可用性）。
    """
    key = _key()
    if not key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY（请在 GitHub 仓库 Secrets 中配置）")
    c = cfg()
    payload = {"model": c.get("model", "deepseek-v4-flash"), "messages": messages,
               "temperature": c.get("temperature", 0.3),
               "max_tokens": c.get("max_tokens", 4000),
               "response_format": {"type": "json_object"},
               "thinking": {"type": "disabled"}, "stream": False}
    retries = retries if retries is not None else c.get("retries", 3)
    last = None
    for i in range(retries):
        try:
            r = requests.post(API_URL, json=payload, timeout=c.get("timeout", 120),
                              headers={"Authorization": f"Bearer {key}",
                                       "Content-Type": "application/json"})
            if r.status_code == 200:
                j = r.json()
                msg = (j.get("choices") or [{}])[0].get("message") or {}
                text = msg.get("content") or ""
                if not text.strip() and msg.get("reasoning_content"):
                    # 兜底：万一思考未被关闭，至少把推理内容当文本解析（_json_of 会尝试提取JSON）
                    text = msg.get("reasoning_content") or ""
                return text, j.get("usage") or {}
            if r.status_code == 400 and "thinking" in r.text and "thinking" in payload:
                print("[AI] 接口不接受 thinking 参数，去掉后重试")
                payload.pop("thinking", None)
                continue
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code in (401, 403):
                break  # 密钥问题不重试
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(2.0 * (i + 1))
    raise RuntimeError(_redact(f"DeepSeek 调用失败：{last}"))


def _json_of(text):
    """容错解析：优先整体 JSON，其次提取首个平衡花括号块。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        return json.loads(text)
    except Exception:
        pass
    i = text.find("{")
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[i:j + 1])
    raise ValueError("模型未返回可解析的JSON")


# ---------------------------------------------------------------- 防幻觉清洗
def sanitize(rec, ctx):
    """只保留真实存在的代码；价格锚定真实收盘价，异常值直接修正而非放行。"""
    cand = {c["code"]: c for c in ctx["candidates"]}
    out, dropped = [], []
    for p in (rec.get("picks") or [])[:6]:
        code = str(p.get("code") or "").zfill(6)
        c = cand.get(code)
        if not c:
            dropped.append(code)
            continue
        px = c["price"]
        atr = max(c.get("atr_pct", 0.04), 0.01) * px
        num = lambda k, dv: (float(p[k]) if str(p.get(k, "")).replace(".", "").replace("-", "").isdigit()
                             else dv)
        lo = min(max(num("entry_low", px * 0.99), px * 0.97), px * 1.03)
        hi = min(max(num("entry_high", px * 1.005), lo), px * 1.03)
        stop = min(max(num("stop", px - 2 * atr), px * 0.90), px * 0.99)
        tgt = min(max(num("target", px + 2.5 * atr), px * 1.02), px * 1.20)
        pos = int(min(max(num("position_pct", 5), 0), 20))
        act = p.get("action") if p.get("action") in ("买入", "观望", "回避") else "观望"
        out.append({"code": code, "name": c["name"], "action": act,
                    "ref_close": px, "entry_low": round(lo, 2), "entry_high": round(hi, 2),
                    "stop": round(stop, 2), "target": round(tgt, 2),
                    "rr": round((tgt - (lo + hi) / 2) / max((lo + hi) / 2 - stop, 1e-6), 2),
                    "horizon": str(p.get("horizon") or "1-3周")[:12],
                    "position_pct": pos, "reason": str(p.get("reason") or "")[:120],
                    "risk": str(p.get("risk") or "")[:80],
                    "industry": c.get("ind"), "pe": c.get("pe"), "rsi14": c.get("rsi14"),
                    "chg60": c.get("chg60"), "dd_from_high60": c.get("dd_from_high60"),
                    "above_ma60": c.get("above_ma60"), "bull_align": c.get("bull_align")})
    rec["picks"] = out
    if dropped:
        rec["dropped_codes"] = dropped
        _log(f"[AI] 已剔除模型中不存在于候选表的代码: {dropped}")
    rec.setdefault("market_view", "")
    if rec.get("risk_level") not in ("偏低", "中性", "偏高"):
        rec["risk_level"] = "中性"
    rec["avoid"] = [str(x)[:60] for x in (rec.get("avoid") or [])][:3]
    rec["notes"] = str(rec.get("notes") or "")[:120]
    return rec


# ---------------------------------------------------------------- 落盘
def _save(rec):
    c = cfg()
    os.makedirs(DASH, exist_ok=True)
    blob = json.dumps(rec, ensure_ascii=False)
    if _key() and _key() in blob:  # 最后一道防线：任何情况下不允许密钥落盘
        raise RuntimeError("检测到密钥将写入文件，已中止落盘")
    with open(ADVICE, "a", encoding="utf-8") as f:
        f.write(blob + "\n")
    max_lines = c.get("keep_runs", 400)
    lines = [l for l in open(ADVICE, encoding="utf-8").read().splitlines() if l.strip()]
    if len(lines) > max_lines:  # 限制历史长度，避免仓库无限膨胀
        open(ADVICE, "w", encoding="utf-8").write("\n".join(lines[-max_lines:]) + "\n")
    with open(os.path.join(DASH, "ai.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)


def latest():
    try:
        lines = [l for l in open(ADVICE, encoding="utf-8").read().splitlines() if l.strip()]
        return json.loads(lines[-1]) if lines else None
    except Exception:
        return None


# ---------------------------------------------------------------- 主入口
def run(session=None, date=None):
    """执行一次 AI 分析并落盘。返回结果 dict；跳过/失败时返回 None（不影响主流程）。"""
    ok, until = active(date)
    if not ok:
        _log(f"[AI] 已超过运行截止日 {until}，跳过 AI 分析")
        return None
    if not _key():
        _log("[AI] 未配置 DEEPSEEK_API_KEY，跳过 AI 分析（其余流程不受影响）")
        return None
    date = date or dt.date.today().isoformat()
    session = session or session_now()
    t0 = time.time()
    try:
        ctx = collect(date, cand_n=int(cfg().get("candidate_pool", 40)))
        if len(ctx["candidates"]) < 3:
            _log(f"[AI] 候选股不足（{len(ctx['candidates'])} 只，疑似数据源异常），本次跳过")
            return None
        text, usage = _call(_messages(ctx, session))
        rec = sanitize(_json_of(text), ctx)
        rec.update({"date": date, "session": session, "model": cfg().get("model", "deepseek-v4-flash"),
                    "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "indices": ctx["indices"], "breadth": ctx["breadth"],
                    "boards_top": ctx["boards_top"], "boards_bottom": ctx["boards_bottom"],
                    "portfolio": ctx["portfolio"], "data_quality": ctx["data_quality"],
                    "usage": usage, "elapsed_s": round(time.time() - t0, 1)})
        _save(rec)
        _log(f"[AI] {date} {session} 完成：推荐 {len(rec['picks'])} 只，"
             f"候选 {ctx['data_quality']['候选数']} 只，耗时 {rec['elapsed_s']}s，"
             f"tokens in/out = {usage.get('prompt_tokens')}/{usage.get('completion_tokens')}")
        return rec
    except Exception as e:
        _log(f"[AI] 分析失败（不影响其他流程）：{_redact(e)}")
        return None
