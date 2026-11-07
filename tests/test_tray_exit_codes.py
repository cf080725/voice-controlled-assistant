"""托盘记录主程序退出码：区分正常关闭、出错退出和真的崩溃；日志只有时间、返回码、分类，最多 200 行。"""
import json

import pytest

tray = pytest.importorskip("canaan_tray")


def test_exit_kind():
    assert tray.exit_kind(0, True) == "正常关闭"
    assert tray.exit_kind(1, True) == "正常关闭"                     # terminate 的返回码
    assert tray.exit_kind(0xC0000005, False) == "崩溃（访问冲突）"
    assert tray.exit_kind(-1073741819, False) == "崩溃（访问冲突）"   # 同一个码的有符号写法
    assert "出错退出" in tray.exit_kind(2, False)
    assert tray.exit_kind(None, False) == "还在运行"


def test_record_exit_keeps_last_200(tmp_path):
    p = str(tmp_path / "exits.jsonl")
    for i in range(205):
        tray.record_exit(i % 3, manual=False, path=p)
    rows = [json.loads(x) for x in open(p, encoding="utf-8").read().splitlines()]
    assert len(rows) == 200 and set(rows[-1]) == {"time", "code", "manual", "kind"}


def test_native_log_false_alarms_are_explained():
    import canaan_crashlog as cl
    assert cl.is_benign_native("Windows fatal exception: code 0x80010108")
    assert not cl.is_benign_native("Windows fatal exception: access violation")
    assert "hub_exits.jsonl" in cl.NATIVE_LOG_NOTE.format(time="t")
