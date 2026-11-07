"""依赖锁定：只读本机包元数据；锁定文件第一行声明 UTF-8（pip-audit 否则按 GBK 读）；没装的列出来。"""
import os
import sys

from packaging.requirements import Requirement

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import lock_requirements as lr  # noqa: E402


def test_closure_pins_installed_and_reports_missing():
    pinned, missing = lr.closure([Requirement("packaging"), Requirement("surely-not-installed-pkg-xyz>=1")])
    assert "packaging" in pinned and missing == ["surely-not-installed-pkg-xyz>=1"]


def test_render_header_and_pins():
    text = lr.render({"a": ("A", "1.0")}, ["b>=2"])
    assert text.startswith("# -*- coding: utf-8 -*-") and "A==1.0" in text and "# b>=2" in text


def test_top_level_skips_comments(tmp_path):
    p = tmp_path / "r.txt"
    p.write_text("# 注释\nnumpy>=1.24  # 行尾注释\n\n-r other.txt\n", encoding="utf-8")
    assert [r.name for r in lr.top_level(str(p))] == ["numpy"]
