# -*- coding: utf-8 -*-
"""屏幕注入 / 越权评测：tools/injection_eval_cases.json 里的合成场景过一遍 canaan_step_guard
（电脑操作每一步的核对、窗口智能体动手前的核对），统计“该停没停”（漏拦）和“不该停却停了”（误拦）。

    py -3.12 tools/injection_eval.py [--out reports]

不操作电脑、不开窗口；窗口智能体那一路用替身的前台窗口。
"""
import argparse
import hashlib
import json
import os
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "core"), os.path.join(ROOT, "plugins")]
CASES = os.path.join(ROOT, "tools", "injection_eval_cases.json")


def run_case(c):
    import canaan_step_guard as g
    if c["kind"] == "step":
        guard = g.StepGuard(c["instruction"], lambda: {"hwnd": 1, "title": c["title"]})
        res = guard.check(c["action"], c.get("params", {}))
        return ("stop" if res else "go"), (res or {}).get("error_code", "")
    import canaan_ufo_agent as u
    host = object.__new__(u.CanaanHostAgent)
    host.get_active_window = lambda: {"hwnd": 5, "title": c["title"]}

    def el(d):
        return types.SimpleNamespace(name=d["name"], clean_label=d["name"], auto_id="", raw_control=None,
                                     control_type=d.get("type", ""), center=tuple(d.get("center", (0, 0))))
    target = el(c["target"])
    elements = [el(d) for d in c.get("elements", [])] + [target]
    res = host.check_step(5, c["title"], c["instruction"], elements, target, c.get("action", "click"), c.get("value"))
    return ("stop" if res else "go"), (res or "")[:30]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "reports"))
    a = ap.parse_args()
    raw = open(CASES, encoding="utf-8").read()
    cases = json.loads(raw)["cases"]
    rows, missed, false_stop = [], 0, 0
    for c in cases:
        got, why = run_case(c)
        ok = got == c["expect"]
        missed += c["expect"] == "stop" and got == "go"
        false_stop += c["expect"] == "go" and got == "stop"
        rows.append({"id": c["id"], "expect": c["expect"], "got": got, "ok": ok, "why": why})
        print(f"{c['id']} {'✓' if ok else '✗'} 期望 {c['expect']} 实际 {got} {why}")
    summary = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "cases_sha256": hashlib.sha256(raw.encode()).hexdigest(),
               "n": len(cases), "ok": sum(r["ok"] for r in rows), "missed_stop": missed, "false_stop": false_stop}
    os.makedirs(a.out, exist_ok=True)
    tag = time.strftime("%Y%m%d")
    with open(os.path.join(a.out, f"injection_eval_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "results": rows}, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if missed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
