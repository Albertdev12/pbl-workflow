# -*- coding: utf-8 -*-
"""过程记录存储：市场观察、决策记录、AI使用记录（JSONL追加写，自动生成表单文档）。"""
import json
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def _append_jsonl(name, obj):
    path = os.path.join(DATA_DIR, name)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _read_jsonl(name):
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def save_market_watch(rec):
    """rec: {date, indices:{name:{close,pct}}, turnover, top_boards, bottom_boards, view, risks}"""
    rows = _read_jsonl("market_watch.jsonl")
    rows = [r for r in rows if r.get("date") != rec.get("date")]
    rows.append(rec)
    rows.sort(key=lambda x: x.get("date", ""))
    with open(os.path.join(DATA_DIR, "market_watch.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return rows


def read_market_watch():
    return _read_jsonl("market_watch.jsonl")


def save_decisions(date, decisions):
    """按日覆盖保存决策记录，带自增 decision_id。"""
    rows = _read_jsonl("decisions.jsonl")
    rows = [r for r in rows if r.get("date") != date]
    base = sum(len(r) if isinstance(r, list) else 1 for r in rows)
    for i, d in enumerate(decisions):
        d["decision_id"] = f"D{date.replace('-', '')}{i + 1:02d}"
        d["date"] = date
    rows.append({"date": date, "items": decisions})
    rows.sort(key=lambda x: x.get("date", ""))
    with open(os.path.join(DATA_DIR, "decisions.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return decisions


def save_manual_decision(date, dec):
    """人工主动决策（策略自动流程不会生成的那种），单独存 manual_decisions.jsonl，
    永远不会被当日自动流程覆盖；read_decisions() 会自动合并进来。"""
    dec["date"] = date
    dec.setdefault("manual", True)
    rows = _read_jsonl("manual_decisions.jsonl")
    for r in rows:
        if r.get("date") == date:
            r.setdefault("items", []).append(dec)
            break
    else:
        rows.append({"date": date, "items": [dec]})
    rows.sort(key=lambda x: x.get("date", ""))
    with open(os.path.join(DATA_DIR, "manual_decisions.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return dec


def read_decisions():
    """全部决策 = 策略自动生成 + 人工主动决策（按日期、编号排序）。"""
    out = []
    for r in _read_jsonl("decisions.jsonl"):
        out.extend(r.get("items", []))
    for r in _read_jsonl("manual_decisions.jsonl"):
        out.extend(r.get("items", []))
    out.sort(key=lambda d: (d.get("date", ""), d.get("decision_id", "")))
    return out


CONFIRM_CSV = os.path.join(DATA_DIR, "confirmations.csv")


def read_confirmations():
    """人工确认过的 decision_id 集合（用户在 confirmations.csv 中逐行填写）。"""
    if not os.path.exists(CONFIRM_CSV):
        return set()
    with open(CONFIRM_CSV, encoding="utf-8-sig") as f:
        return {line.strip() for line in f if line.strip() and not line.startswith("decision_id")}


def confirm(decision_id):
    with open(CONFIRM_CSV, "a", encoding="utf-8-sig") as f:
        f.write(f"{decision_id}\n")


def save_risk_block(date, proposed, check):
    """被规则引擎拦截（BLOCK_TRADE）的拟交易，留审计痕迹。"""
    _append_jsonl("risk_blocks.jsonl", {"date": date, "proposed": proposed, "check": check})


def read_risk_blocks():
    return _read_jsonl("risk_blocks.jsonl")


def save_pool(date, pool_res):
    rows = _read_jsonl("pool_history.jsonl")
    rows = [r for r in rows if r.get("date") != date]
    rows.append({"date": date, "pool": pool_res["pool"]})
    rows.sort(key=lambda x: x.get("date", ""))
    with open(os.path.join(DATA_DIR, "pool_history.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_pool_history():
    """全部候选池历史（按日期升序），用于周会材料包的"池子变化"对比。"""
    return _read_jsonl("pool_history.jsonl")


def latest_pool():
    rows = _read_jsonl("pool_history.jsonl")
    return rows[-1]["pool"] if rows else []


def log_ai_usage(prompt, output, verify, human_judgment, final_use):
    """按任务书链条记录：Prompt → AI输出 → 信息来源核验 → 人工判断 → 最终采用/修改。"""
    rows = _read_jsonl("ai_usage.jsonl")
    rec = {
        "seq": len(rows) + 1,
        "time": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "prompt": prompt,
        "ai_output": output,
        "verify": verify,
        "human_judgment": human_judgment,
        "final_use": final_use,
    }
    _append_jsonl("ai_usage.jsonl", rec)
    return rec


def read_ai_usage():
    return _read_jsonl("ai_usage.jsonl")


def save_snapshot(date, total_assets, net_value):
    rows = _read_jsonl("nav_history.jsonl")
    rows = [r for r in rows if r.get("date") != date]
    rows.append({"date": date, "total_assets": total_assets, "nav": round(net_value, 4)})
    rows.sort(key=lambda x: x.get("date", ""))
    with open(os.path.join(DATA_DIR, "nav_history.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_nav():
    return _read_jsonl("nav_history.jsonl")
