"""提醒的更多说法：叫我 / 喊我 / 通知我、今晚 / 明早 / 下周一 / 周三、一个半小时、一刻、先说“提醒我”再说时间（自己写的说法，不照抄评测题）。"""
from datetime import datetime

import pytest

import canaan_proactive as cp

NOW = datetime(2026, 10, 12, 10, 0)   # 星期一 10:00


@pytest.mark.parametrize("text,due,what", [
    ("今晚九点喊我倒垃圾", datetime(2026, 10, 12, 21, 0), "倒垃圾"),
    ("明早七点半叫我起床", datetime(2026, 10, 13, 7, 30), "起床"),
    ("明晚八点提醒一下我给妈妈打电话", datetime(2026, 10, 13, 20, 0), "给妈妈打电话"),
    ("下周一上午九点提醒我交周报", datetime(2026, 10, 19, 9, 0), "交周报"),
    ("周三下午三点叫我开组会", datetime(2026, 10, 14, 15, 0), "开组会"),
    ("星期一早上八点提醒我晨跑", datetime(2026, 10, 19, 8, 0), "晨跑"),          # 今天周一但已过点：下周一
    ("一个半小时后叫我收衣服", datetime(2026, 10, 12, 11, 30), "收衣服"),
    ("半小时后喊我关火", datetime(2026, 10, 12, 10, 30), "关火"),
    ("提醒我今晚十点一刻吃药", datetime(2026, 10, 12, 22, 15), "吃药"),
    ("提醒我二十分钟后看邮件", datetime(2026, 10, 12, 10, 20), "看邮件"),
    ("下午四点三刻通知我去取快递", datetime(2026, 10, 12, 16, 45), "去取快递"),
    ("明早六点叫我", datetime(2026, 10, 13, 6, 0), cp.DEFAULT_WHAT),
    ("凌晨一点提醒我看比赛", datetime(2026, 10, 13, 1, 0), "看比赛"),
])
def test_phrasings(text, due, what):
    got = cp.parse_reminder(text, NOW)
    assert got == (due, what)


@pytest.mark.parametrize("text", ["叫我小明就行", "别叫我", "今晚九点倒垃圾", "以后别提醒我了", "分钟后提醒我", "二十五点叫我",
                                  "九点六十分叫我"])
def test_not_reminders(text):
    assert cp.parse_reminder(text, NOW) is None


def test_reminder_wins_over_recycle_bin_rule(tmp_path):
    policy = cp.ProactivePolicy(state_path=str(tmp_path / "p.json"), clock=lambda: NOW)
    decision = cp.handle_command("今晚九点喊我倒垃圾", policy)
    assert decision["action"] == "chat" and "21:00" in decision["reply"]
    assert policy.state["reminders"][-1]["what"] == "倒垃圾"
