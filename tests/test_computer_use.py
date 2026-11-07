# -*- coding: utf-8 -*-
import os
import sys
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from canaan_computer_use import CanaanComputerUse
from canaan_action_framework import ActionRegistry, ActionContext
import canaan_intent as ci


@pytest.fixture(autouse=True)
def no_native_input(monkeypatch):
    monkeypatch.setattr(CanaanComputerUse, "release_all_modifiers", lambda: None)


def test_screen_metrics_and_coords():
    w, h = CanaanComputerUse.get_screen_size()
    assert w > 0 and h > 0
    # 绝对坐标保持不变
    assert CanaanComputerUse.normalize_coords(500, 300) == (500, 300)
    # 归一化浮点坐标自动映射为物理像素
    nx, ny = CanaanComputerUse.normalize_coords(0.5, 0.5)
    assert nx == w // 2 and ny == h // 2


def test_deterministic_task_parser():
    # 快捷键
    t1 = CanaanComputerUse._parse_deterministic_task("按快捷键 win+d")
    assert t1 == [{"action": "hotkey", "params": {"keys": ["win", "d"]}}]

    t_desk = CanaanComputerUse._parse_deterministic_task("返回桌面")
    assert t_desk == [{"action": "hotkey", "params": {"keys": ["win", "d"]}}]

    # 点击
    t2 = CanaanComputerUse._parse_deterministic_task("点击坐标 500, 300")
    assert t2 == [{"action": "click", "params": {"x": 500, "y": 300}}]

    # 双击与右键
    t_dbl = CanaanComputerUse._parse_deterministic_task("双击 100, 200")
    assert t_dbl == [{"action": "double_click", "params": {"x": 100, "y": 200}}]

    t_r = CanaanComputerUse._parse_deterministic_task("右键点击 400, 150")
    assert t_r == [{"action": "right_click", "params": {"x": 400, "y": 150}}]

    # 滚轮
    t_scroll = CanaanComputerUse._parse_deterministic_task("向下滚动 3 次")
    assert t_scroll == [{"action": "scroll", "params": {"clicks": 3, "direction": "down"}}]

    # 窗口聚焦
    t_win = CanaanComputerUse._parse_deterministic_task("切换到记事本窗口")
    assert t_win == [{"action": "focus_window", "params": {"window": "记事本"}}]


def test_compound_ast_macro_parsing():
    # 复合指令：打开并写入
    t_compound = CanaanComputerUse._parse_deterministic_task("打开记事本并写下'你好团团'")
    assert len(t_compound) == 3
    assert t_compound[0]["action"] == "launch_app"
    assert t_compound[0]["params"]["app"] == "记事本"
    assert t_compound[1]["action"] == "wait_window"
    assert t_compound[2]["action"] == "type_text"
    assert "你好团团" in t_compound[2]["params"]["text"]

    # 复合指令：全选复制
    t_copy = CanaanComputerUse._parse_deterministic_task("全选复制")
    assert t_copy == [
        {"action": "hotkey", "params": {"keys": ["ctrl", "a"]}},
        {"action": "hotkey", "params": {"keys": ["ctrl", "c"]}}
    ]

    # 复合指令：新建标签页
    t_tab = CanaanComputerUse._parse_deterministic_task("新建标签页")
    assert t_tab == [{"action": "hotkey", "params": {"keys": ["ctrl", "t"]}}]

    # 复合指令：语义按钮点击
    t_btn = CanaanComputerUse._parse_deterministic_task("点击确定按钮")
    assert t_btn == [{"action": "click_element", "params": {"name": "确定"}}]


def test_smooth_bezier_spline_calculation():
    # 测试三次 Ease-Out 贝塞尔曲线系数单调增且平滑靠拢
    factors = [CanaanComputerUse._ease_out_cubic(i / 10.0) for i in range(11)]
    assert factors[0] == 0.0
    assert factors[-1] == 1.0
    # 验证单调递增性
    for i in range(len(factors) - 1):
        assert factors[i] < factors[i + 1]
    # 中点 (t=0.5) 时完成度超过 80%（人手物理加速特征）
    assert factors[5] > 0.80


def test_execute_action_primitives():
    # 测试等待原语（纳秒级高精度）
    res_wait = CanaanComputerUse.execute_action("wait", {"seconds": 0.01})
    assert res_wait["success"] is True

    # 测试未知原语安全降级
    res_unk = CanaanComputerUse.execute_action("non_existent_primitive_123")
    assert res_unk["success"] is False
    assert "Unknown primitive" in res_unk["error"]


def test_execute_task_pipeline_with_on_step(monkeypatch):
    monkeypatch.setattr(CanaanComputerUse, "hotkey", lambda *args, **kwargs: True)
    steps_reported = []

    def mock_on_step(cur, total, desc):
        steps_reported.append((cur, total, desc))

    res = CanaanComputerUse.execute_task("按快捷键 win+d", on_step=mock_on_step)
    assert res["success"] is True
    assert res["steps_count"] == 1
    assert len(steps_reported) == 1
    assert steps_reported[0][0] == 1
    assert steps_reported[0][1] == 1
    assert res["plan_source"] == "ast_macro_fastpath"


def test_computer_use_action_registered(monkeypatch):
    monkeypatch.setattr(CanaanComputerUse, "hotkey", lambda *args, **kwargs: True)
    act = ActionRegistry.get_action("computer_use")
    assert act is not None
    assert "computer_control" in act.aliases
    assert "desktop_control" in act.aliases

    # 通过 ActionRegistry.dispatch 执行
    res = ActionRegistry.dispatch("computer_use", "按快捷键 win+d", source="rule", confirmed=True)  # 2026-10-13 起规则来源也要先确认
    assert res.success is True
    assert res.action == "computer_use"
    assert "完成" in res.speech_override


def test_computer_use_sensitive_window_gate(monkeypatch):
    # 模拟当前处于网银支付等敏感窗口
    monkeypatch.setattr(
        CanaanComputerUse,
        "get_active_window",
        lambda: {"hwnd": 1234, "title": "中国工商银行个人网上银行 - 安全支付", "rect": (0, 0, 800, 600)}
    )

    res = ActionRegistry.dispatch("computer_use", "点击坐标 500, 300", source="rule", confirmed=True)
    # 敏感护盾必须拦截
    assert res.success is False
    assert "防窥安全护盾" in res.message
    assert "隐私安全" in res.speech_override


def test_intent_integration():
    # SLOW_ACTIONS 包含 computer_use
    assert "computer_use" in ci.SLOW_ACTIONS
    assert "computer_control" in ci.SLOW_ACTIONS

    # describe_action 具备亲昵口吻描述
    desc = ci.describe_action({"action": "computer_use", "param": "按快捷键 win+d"})
    assert "操控电脑" in desc

    # 命令检测识别
    assert ci.looks_like_command("帮我操控电脑点击一下那个确定按钮") is True
    assert ci.looks_like_command("按下快捷键 win+d") is True


def test_hardened_ast_macros():
    # 带有礼貌前缀的应用拉起
    t_open = CanaanComputerUse._parse_deterministic_task("帮我打开记事本")
    assert t_open is not None
    assert t_open[0]["action"] == "launch_app"
    assert t_open[0]["params"]["app"] == "记事本"

    # 关闭应用
    t_close1 = CanaanComputerUse._parse_deterministic_task("把记事本关掉")
    assert t_close1 is not None
    assert t_close1[0]["action"] == "focus_window"
    assert t_close1[2]["action"] == "hotkey"
    assert t_close1[2]["params"]["keys"] == ["alt", "f4"]

    t_close2 = CanaanComputerUse._parse_deterministic_task("关闭计算器")
    assert t_close2 is not None
    assert t_close2[0]["params"]["window"] == "计算器"

    # 在已有应用中打字
    t_type = CanaanComputerUse._parse_deterministic_task("在记事本里写下hello")
    assert t_type is not None
    assert t_type[0]["action"] == "focus_window"
    assert t_type[2]["action"] == "type_text"
    assert t_type[2]["params"]["text"] == "hello"


def test_failsafe_watchdog_graceful_stop(monkeypatch):
    from canaan_computer_use import EmergencyFailsafeTriggered

    # 模拟在执行第2步时触发看门狗
    def mock_action(act, params):
        if act == "type_text":
            raise EmergencyFailsafeTriggered("测试避险触发")
        return {"action": act, "success": True}

    monkeypatch.setattr(CanaanComputerUse, "execute_action", mock_action)

    res = CanaanComputerUse.execute_task("打开记事本并写下测试")
    assert res["success"] is False
    assert res.get("stopped_by_user") is True
    assert "紧急避险" in res["message"]
