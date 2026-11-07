"""少问一句：问天气说了地方就直接查（用原话搜）；没说地方先问城市；只放宽“打开搜索页”这种低风险动作，其他照旧（自己写的说法）。"""
import pytest

from canaan_intent import apply_command_guard, confidence_threshold


def guard(text, action="web_search", param="天气", conf=.97):
    return apply_command_guard(text, {"action": action, "param": param, "_conf": conf}, require_confidence=True)


@pytest.mark.parametrize("text", ["广州今天啥天气？", "成都明天冷不冷？", "深圳今天会下雨吗", "帮我查一下东京的天气"])
def test_weather_with_place_searches_original_words(text):
    g = guard(text)
    assert g["action"] == "web_search" and g["param"] in text and len(g["param"]) >= 4


@pytest.mark.parametrize("text", ["今天天气咋样？", "外面冷不冷", "明天要不要带伞"])
def test_weather_without_place_asks_city(text):
    g = guard(text)
    assert g["action"] == "chat" and "城市" in g["reply"] and not g.get("_pending")


def test_low_risk_threshold_only_for_grounded_search():
    assert confidence_threshold({"action": "web_search", "param": "成都天气"}, "成都天气怎么样", .6) == .3
    assert confidence_threshold({"action": "web_search", "param": "比特币价格"}, "今天天气怎么样", .6) == .6   # 搜的词不在原话里
    assert confidence_threshold({"action": "close_app", "param": "记事本"}, "记事本关掉", .6) == .6
    assert confidence_threshold({"action": "run_cmd", "param": "shutdown"}, "shutdown", .6) == .6


def test_low_confidence_still_asks_for_risky_actions():
    g = guard("把记事本关了吧？", action="close_app", param="notepad", conf=.45)
    assert g["action"] == "chat" and g.get("_pending")


def test_questions_still_ask_for_non_search_actions():
    g = guard("要不要锁屏？", action="lock_screen", param="", conf=.97)
    assert g["action"] == "chat"


def test_weather_chat_without_place_asks_city():
    g = apply_command_guard("今天天气咋样？", {"action": "chat", "param": "", "reply": "挺好的"})
    assert "城市" in g["reply"]
    keep = apply_command_guard("天气真好啊", {"action": "chat", "param": "", "reply": "是呀"})
    assert keep["reply"] == "是呀"          # 感叹不是提问，照常聊