# -*- coding: utf-8 -*-
"""一次性修复：历史决策记录里的"技术面依据"文本被逐字符拆开（"价；格；站；上；…"）。

原因：decision.py 早期写成 "；".join(r["tech_signal"])，而 tech_signal 本身已经是字符串，
导致按字符拼接。代码已修复，本脚本按当时的候选池记录（pool_history.jsonl）还原正确文本，
只改 tech_basis 一个字段，其余内容不动。

用法：python tools/fix_tech_basis.py [--apply]
不带 --apply 时只做检查（dry-run）。
"""
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))
import records  # noqa: E402

CORRUPT = re.compile(r"(?:[一-鿿0-9.=><]；){4,}")


def _pool_lookup():
    out = {}
    for row in records.read_pool_history():
        for r in row.get("pool", []):
            out[(row["date"], r["code"])] = r
    return out


def main():
    apply = "--apply" in sys.argv
    pool = _pool_lookup()
    path = os.path.join(BASE, "data", "decisions.jsonl")
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    fixed, skipped = 0, 0
    for row in rows:
        for d in row.get("items", []):
            tb = d.get("tech_basis") or ""
            if not CORRUPT.search(tb):
                continue
            rec = pool.get((d["date"], d["code"]))
            if not rec:
                skipped += 1
                print(f"  [跳过] {d['decision_id']} {d['name']}：候选池无当日记录")
                continue
            new = f"{rec['tech_judgment']}。指标信号：{rec['tech_signal']}"
            print(f"  [修复] {d['decision_id']} {d['name']}")
            print(f"        旧: {tb[:80]}…")
            print(f"        新: {new[:80]}…")
            d["tech_basis"] = new
            fixed += 1
    print(f"共需修复 {fixed} 条，跳过 {skipped} 条")
    if not apply:
        print("dry-run：加 --apply 才会写回 data/decisions.jsonl")
        return
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("已写回", path)


if __name__ == "__main__":
    main()
