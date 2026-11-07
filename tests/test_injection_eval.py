"""屏幕注入评测集（tools/injection_eval_cases.json）作为回归基线：该停的一条不漏，不该停的不误拦。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import injection_eval as ie  # noqa: E402


def test_injection_cases_all_pass():
    cases = json.load(open(ie.CASES, encoding="utf-8"))["cases"]
    assert len(cases) >= 20 and {c["expect"] for c in cases} == {"stop", "go"}
    bad = [(c["id"], c["expect"], ie.run_case(c)[0]) for c in cases if ie.run_case(c)[0] != c["expect"]]
    assert bad == []
