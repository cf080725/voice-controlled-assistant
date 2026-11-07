"""Known leading emotion metadata must not consume a valid spoken answer."""
import pytest
from canaan_companion import ChatStream


@pytest.mark.parametrize("pieces", [
    ["[pamper] [confident]\n直接在开始菜单搜索记事本。"],
    ["[pamper]", " [con", "fident]", "\n直接在开始菜单搜索记事本。"],
    ["[pamper] [confident] [tease] ", "直接在开始菜单搜索记事本。"],
])
@pytest.mark.parametrize("guard", [True, False])
def test_repeated_leading_tags_keep_body(pieces, guard):
    stream = ChatStream(iter(pieces), guard=guard)
    assert "".join(stream.iter_text()) == "直接在开始菜单搜索记事本。"
    assert stream.emotion == "pamper"


def test_action_metadata_without_a_spoken_proposal_cannot_offer_an_action():
    stream = ChatStream(iter(["<<web_search|四盒六支笔有多少支>>"]))
    assert "".join(stream.iter_text()) == ""
    assert stream.suggestion is None


def test_trailing_tag_and_suggestion_still_stay_out_of_speech():
    stream = ChatStream(iter(["我还不知道天气，要不要我帮你查查？ [confident]\n", "<<web_search|天气>>"]))
    assert "".join(stream.iter_text()).strip() == "我还不知道天气，要不要我帮你查查？"
    assert stream.suggestion == {"action": "web_search", "param": "天气"}


@pytest.mark.parametrize("pieces", [
    ["pamper\n相隔45分钟。"],
    ["con", "fident", "：", "相隔45分钟。"],
    ["[pamper] ", "confident\n", "相隔45分钟。"],
])
def test_known_bare_emotion_prefix_is_metadata_not_an_english_violation(pieces):
    stream = ChatStream(iter(pieces))
    assert "".join(stream.iter_text()).strip() == "相隔45分钟。"
    assert stream.guard_action != "canned"
    assert stream.suggestion is None


def test_bare_emotion_alone_does_not_create_an_answer_or_suggestion():
    stream = ChatStream(iter(["pamper\n"]))
    assert "".join(stream.iter_text()) == ""
    assert stream.suggestion is None


def test_a_longer_latin_word_cannot_impersonate_a_known_emotion_label():
    stream = ChatStream(iter(["pampered\n相隔45分钟。"]), regen=lambda: iter(["相隔45分钟。听清楚啦。 "]))
    assert "pampered" not in "".join(stream.iter_text())
    assert stream.guard_action == "regenerated"


