import time
import tkinter as tk

import canaan_companion_pet as ccp
import canaan_events as ev
from tests.test_pet_pack import _atlas, _png, _zip
import canaan_pet_pack as pp

_ROOT = None


def _pet():
    from tests._tk import shared_root
    pet = ccp.CanaanCompanionPet(shared_root(), is_speaking_provider=lambda: False)
    pet.withdraw()
    return pet


def _hud_texts(pet):
    pet.canvas.delete("action_hud")
    pet._draw_action_hud()
    return [pet.canvas.itemcget(i, "text") for i in pet.canvas.find_withtag("action_hud") if pet.canvas.type(i) == "text"]


def test_status_events_drive_hud_without_task_ids():
    pet = _pet()
    pet.stats["calm_mode"] = False  # 完整模式下每种状态都显示
    pet.on_status_event(ev.make_event("step", "open_app", 2, 3))
    pet._run_ui_queue()
    texts = _hud_texts(pet)
    assert any("第 2/3 步" in t for t in texts)
    assert not any("TASK" in t or "task" in t for t in texts)
    pet.on_status_event(ev.make_event("awaiting_confirm", "close_app", result="confirm"))
    pet._run_ui_queue()
    assert any("等你确认" in t for t in _hud_texts(pet))
    pet.on_status_event(ev.make_event("failure", "run_cmd", result="blocked"))
    pet._run_ui_queue()
    assert pet.action_hud_status == "failed" and any("没办成" in t for t in _hud_texts(pet))
    pet.destroy()


def test_calm_mode_is_default_and_only_shows_what_needs_owner():
    pet = _pet()
    pet.stats.pop("calm_mode", None)
    pet.stats.pop("gaze_enabled", None)
    assert pet.calm and not pet.stats.get("gaze_enabled", False)
    for kind in ("listening", "thinking", "step", "success"):
        pet.on_status_event(ev.make_event(kind, "open_app", 1, 2, result="ok" if kind == "success" else ""))
        pet._run_ui_queue()
        assert _hud_texts(pet) == [] and not pet.floating_particles
    pet.on_status_event(ev.make_event("awaiting_confirm", "close_app", result="confirm"))
    pet._run_ui_queue()
    assert any("等你确认" in t for t in _hud_texts(pet))
    pet.on_status_event(ev.make_event("failure", "run_cmd", result="failed"))
    pet._run_ui_queue()
    assert any("没办成" in t for t in _hud_texts(pet)) and not pet.floating_particles and not pet.custom_bubble_text
    pet.destroy()


def test_calm_mode_ignores_ambient_poses_but_keeps_privacy_alert():
    from types import SimpleNamespace
    pet = _pet()
    pet.stats["calm_mode"] = True
    pet.state, pet.state_lock_until = "IDLE", 0
    pet.sense = SimpleNamespace(enabled=True, latest=SimpleNamespace(mode="code", typing=True, window_key="k", recorder=False))
    pet._apply_sense(time.time())
    assert pet.state == "IDLE"                      # 写代码时不再切到敲键盘动画
    pet.sense.latest = SimpleNamespace(mode="privacy", typing=False, window_key="pay", recorder=False)
    pet._apply_sense(time.time())
    assert "守着" in (pet.custom_bubble_text or "")  # 敏感窗口提醒照常
    pet.destroy()


def test_agent_progress_in_calm_mode_only_when_needed():
    pet = _pet()
    pet.stats["calm_mode"] = True
    pet.on_status_event(ev.make_event("agent", "muse", 1, 3))
    pet._run_ui_queue()
    assert _hud_texts(pet) == []                       # 平静模式：普通进度不显示
    pet.on_status_event(ev.make_event("agent", "muse", 3, 3, result="ok"))
    pet._run_ui_queue()
    assert any("Muse 办好了" in t for t in _hud_texts(pet))
    pet.stats["calm_mode"] = False
    pet.on_status_event(ev.make_event("agent", "dots", 2, 4))
    pet._run_ui_queue()
    assert any("Dots 第 2/4 步" in t for t in _hud_texts(pet))
    pet.destroy()


def test_chat_model_unloads_when_idle(monkeypatch):
    import canaan_companion, canaan_config
    assert canaan_companion.chat_keep_alive() == canaan_config.CHAT_KEEP_ALIVE != "24h"


def test_stale_step_shows_pending():
    pet = _pet()
    pet._status_last = ev.make_event("step", "open_app", 1, 2, ts=time.time() - 120)
    pet.action_hud_until = 0
    assert any("待确认" in t for t in _hud_texts(pet))
    pet._status_last = ev.make_event("step", "open_app", 1, 2, ts=time.time() - 700)
    assert _hud_texts(pet) == []
    pet.destroy()


def test_pack_frames_and_mirrored_gaze(tmp_path):
    pet = _pet()
    path = _zip(tmp_path, {"id": "hud-test", "spriteVersionNumber": 2},
                {"spritesheet.webp": _png(_atlas(11, {0: 6, 4: 3, 9: 8, 10: 8}))})
    pet.pack = pp.load_pack(path)
    img, key = pet._pack_image("cheer", 0.0)
    assert key.startswith("pack:hud-test:jumping:") and img.size == (pp.CELL_W, pp.CELL_H)
    pet._gaze_dir, pet.facing_left = 4, True
    _, key = pet._pack_image("idle", 0.0)
    assert key.endswith("gaze:12")                    # 翻转后方向镜像
    pet.stats["gaze_enabled"] = False
    pet.state = "IDLE"
    pet._update_gaze(time.time())
    assert pet._gaze_dir is None                       # 设置里关掉后不注视
    pet.destroy()
