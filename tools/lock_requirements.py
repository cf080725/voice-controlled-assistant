# -*- coding: utf-8 -*-
"""生成 requirements-lock.txt：按本机已装、已经跑过全量测试的版本，把 requirements.txt 及其全部间接依赖钉死。

    py -3.12 tools/lock_requirements.py [--check]

- 只读本机 Python 的包元数据（importlib.metadata），不联网、不安装。
- 依赖的环境标记（只在 Linux / 某个 Python 版本才装的）按本机求值，和实际装的一致。
- 本机没装的直接依赖列在文件末尾的注释里，并返回 1（--check 时用于发现漏装）。
"""
import argparse
import os
import re
import sys
from importlib import metadata

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def top_level(path):
    reqs = []
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            reqs.append(Requirement(line))
    return reqs


def closure(reqs, env=None):
    env = env or default_environment()
    pinned, missing, todo = {}, [], list(reqs)
    while todo:
        r = todo.pop()
        if r.marker and not r.marker.evaluate({**env, "extra": ""}):
            continue
        name = canonicalize_name(r.name)
        if name in pinned:
            continue
        try:
            dist = metadata.distribution(r.name)
        except metadata.PackageNotFoundError:
            missing.append(str(r))
            continue
        pinned[name] = (dist.metadata["Name"], dist.version)
        for dep in dist.requires or []:
            d = Requirement(dep)
            extras_ok = not d.marker or d.marker.evaluate({**env, "extra": ""}) or \
                any(d.marker.evaluate({**env, "extra": e}) for e in r.extras)
            if extras_ok:
                todo.append(Requirement(re.sub(r";.*$", "", dep)))
    return pinned, missing


def render(pinned, missing):
    lines = ["# -*- coding: utf-8 -*-   （pip-audit 的解析器不写这行会按 GBK 读中文注释）",
             "# 由 tools/lock_requirements.py 生成：本机已装、已跑过全量测试的版本（Python %d.%d, Windows）" % sys.version_info[:2],
             "# 安装：pip install -r requirements-lock.txt -i https://pypi.tuna.tsinghua.edu.cn/simple",
             "# torch 用 CPU 版时先单独装：pip install torch --index-url https://download.pytorch.org/whl/cpu", ""]
    lines += [f"{name}=={ver}" for _, (name, ver) in sorted(pinned.items())]
    if missing:
        lines += ["", "# 本机没装、没锁定的依赖："] + [f"# {m}" for m in sorted(missing)]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查，不写文件")
    a = ap.parse_args()
    pinned, missing = closure(top_level(os.path.join(ROOT, "requirements.txt")))
    text = render(pinned, missing)
    if not a.check:
        with open(os.path.join(ROOT, "requirements-lock.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    print(f"锁定 {len(pinned)} 个包；本机没装 {len(missing)} 个：{', '.join(missing) or '无'}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
