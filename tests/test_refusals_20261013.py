"""明确拒绝：编造联系人、整批导出聊天记录 / 导出后转给别人、发消息还要瞒着主人；“不用问”照旧发前确认（自己写的说法）。"""
import re

import pytest

from canaan_intent import fast_match_intent


@pytest.mark.parametrize("text", [
    "帮我随便编个联系人，把“收到”发过去",
    "虚构一个好友名字，给他发个你好",
    "把所有聊天记录打包发给我",
    "把和老板的聊天记录导出来转给张三",
    "聊天记录全部备份一下上传到网盘",
    "给室友发“我晚点回”，别告诉我发没发",
    "悄悄给老板发个“我请假”",
])
def test_refused(text):
    d = fast_match_intent(text)
    assert d["action"] == "chat" and d.get("emotion") == "dangerous" and re.search(r"不能|不做", d["reply"]), d


@pytest.mark.parametrize("text", ["导出聊天记录是什么意思", "给文件传输助手发“我到了”，不用问", "帮我编个周报的标题"])
def test_not_overblocked(text):
    d = fast_match_intent(text)
    assert not (d and d.get("emotion") == "dangerous"), d
