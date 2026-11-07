"""Synthetic execution-boundary checks, never model quality or a real-audio holdout."""
import pytest
from canaan_intent import apply_command_guard

OPERATIONS=[
    ("lock_screen","","锁屏"), ("screenshot","","截图"),
    ("show_desktop","","显示桌面"), ("volume_mute","","静音"),
    ("open_app","记事本","打开记事本"), ("close_app","记事本","关闭记事本"),
    ("open_url","https://example.com","打开https://example.com"),
    ("media_control","next","播放下一首"), ("set_brightness","50","亮度设为50"),
    ("computer_use","打开记事本","操作电脑打开记事本"),
    ("openmuse_task","研究测试资料","Muse研究测试资料"),
    ("opendots_task","整理测试资料","Dots整理测试资料"),
]


@pytest.mark.parametrize("action,param,text",OPERATIONS)
@pytest.mark.parametrize("prefix",["不要","不用","先别","千万别","请不要","团团不要","你别","别",
                                  "暂时别","千万不要","你先别","你可别","现在不要","请帮我不要","团团，别","你帮我不要"])
def test_negation_never_creates_executable_confirmation(action,param,text,prefix):
    result=apply_command_guard(prefix+text,{"action":action,"param":param,"_conf":.99},True)
    assert result["action"]=="chat" and not result.get("_pending")


@pytest.mark.parametrize("action,param,text",OPERATIONS)
@pytest.mark.parametrize("prefix",["同事说","老板让我","朋友叫我","老师说","我妈让我","他们说"])
def test_reported_instruction_does_not_execute_immediately(action,param,text,prefix):
    assert apply_command_guard(prefix+text,{"action":action,"param":param,"_conf":.99},True)["action"]=="chat"


@pytest.mark.parametrize("action,param,text",OPERATIONS)
@pytest.mark.parametrize("confidence",[None,.1,float('nan'),True,-1,2])
def test_missing_or_invalid_confidence_does_not_execute(action,param,text,confidence):
    assert apply_command_guard(text,{"action":action,"param":param,"_conf":confidence},True)["action"]=="chat"


@pytest.mark.parametrize("value",["unknown","-1","101",None])
def test_invalid_brightness_never_creates_executable_confirmation(value):
    result=apply_command_guard("亮度设为50",{"action":"set_brightness","param":value,"_conf":.99},True)
    assert result["action"]=="chat" and not result.get("_pending")
