"""音量 / 亮度设到具体数值：中文数字也认，音量和亮度不混（自己写的说法，不照抄评测题）。"""
import pytest

from canaan_intent import apply_command_guard, cn_number, fast_match_intent, numbers_in


@pytest.mark.parametrize("s,v", [("四十", 40), ("二十五", 25), ("百分之六十", 60), ("60%", 60), ("一百", 100), ("十", 10), ("七", 7)])
def test_cn_number(s, v):
    assert cn_number(s) == v


@pytest.mark.parametrize("text,action,param", [
    ("把音量调到百分之五十五", "volume", "55"),
    ("电脑声音设成三十", "volume", "30"),
    ("声音开到80%", "volume", "80"),
    ("屏幕亮度设为百分之七十五", "set_brightness", "75"),
    ("亮度调成45", "set_brightness", "45"),
])
def test_value_rules(text, action, param):
    d = fast_match_intent(text)
    assert (d["action"], d["param"]) == (action, param)


def test_volume_not_taken_as_brightness():
    assert fast_match_intent("声音调到百分之二十")["action"] == "volume"
    assert fast_match_intent("屏幕调到百分之二十")["action"] == "set_brightness"


def test_chinese_numbers_count_as_grounded():
    assert 60.0 in numbers_in("亮度设成百分之六十")
    g = apply_command_guard("亮度设成百分之六十", {"action": "set_brightness", "param": "60", "_conf": .99})
    assert g["action"] == "set_brightness"


@pytest.mark.parametrize("text,action,param", [
    ("声音调到四十还是六十好呢", "volume", "40"),       # 两个数：先问
    ("亮度再加十个百分点", "set_brightness", "10"),       # 相对调整：不能当成设到 10
    ("调到百分之二十", "set_brightness", "20"),           # 没说调什么：先问
    ("调到百分之二十", "volume", "20"),
])
def test_ambiguous_values_ask(text, action, param):
    g = apply_command_guard(text, {"action": action, "param": param, "_conf": .99})
    assert g["action"] == "chat" and not g.get("_pending")


def test_values_combine_into_steps():
    import json
    d = fast_match_intent("亮度调到六十五，然后把声音设成十五")
    assert [s["action"] for s in json.loads(d["param"])] == ["set_brightness", "volume"]
