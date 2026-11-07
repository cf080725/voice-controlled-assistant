"""灵动岛：状态通道（节流、原子写、seq）、动作队列只认已知类型、光标命中判断、弹簧。"""
import json

import pytest

import canaan_island_state as st
from canaan_island import IslandApi, inside

class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_state_writes_only_on_change_and_bumps_seq(tmp_path):
    path = tmp_path / "s.json"
    s = st.IslandState(str(path))
    assert s.update(mode="speaking", text="好的")
    first = json.loads(path.read_text(encoding="utf-8"))["seq"]
    assert not s.update(mode="speaking", text="好的")                 # 没变化不写
    assert s.update(mode="bogus") is False                           # 未知模式忽略
    s.update(notices=[{"id": "1", "source": "Muse", "title": "完成"}])
    assert json.loads(path.read_text(encoding="utf-8"))["seq"] == first + 1
    assert len(s.update.__self__.data["text"]) <= 120


def test_level_is_throttled(tmp_path):
    clock = Clock()
    s = st.IslandState(str(tmp_path / "s.json"), clock=clock)
    s.level(0.5)
    seq = s.snapshot()["seq"]
    s.level(0.9)                                                      # 同一时刻：不写
    assert s.snapshot()["seq"] == seq
    clock.t += 0.2
    s.level(0.2)
    assert s.snapshot()["seq"] == seq + 1 and s.snapshot()["level"] == 0.2


def test_actions_only_known_kinds_and_reader_reads_new(tmp_path):
    path = tmp_path / "a.jsonl"
    st.post_action("click", {}, path=str(path))
    reader = st.ActionReader(str(path))
    assert reader.read_new() == []                                    # 启动前的不回放
    assert not st.post_action("run_cmd", {"cmd": "del"}, path=str(path))
    st.post_action("notice_action", {"id": "n1", "action": "open", "extra": "x" * 999}, path=str(path))
    got = reader.read_new()
    assert len(got) == 1 and got[0]["action"] == "open" and "extra" not in got[0]


def test_api_reads_state_and_records_rect(tmp_path):
    path = tmp_path / "s.json"
    st.IslandState(str(path)).update(mode="thinking")
    api = IslandApi(str(path))
    assert api.get_state()["mode"] == "thinking"
    api.event("rect", {"x": 38, "y": 8, "w": 384, "h": 100, "dpr": 1.5})
    assert api.rect == (38.0, 8.0, 384.0, 100.0, 1.5)
    assert api.event("rect", {"x": "bad"}) and api.rect == (38.0, 8.0, 384.0, 100.0, 1.5)


@pytest.mark.parametrize("cursor,expect", [((100 + 38 * 1.5 + 10, 8 * 1.5 + 5), True), ((100 + 5, 300), False), ((100 + (38 + 384 + 10) * 1.5, 20), False)])
def test_hit_test_uses_dpr(cursor, expect):
    assert inside((38, 8, 384, 100, 1.5), cursor, (100, 0)) is expect
    assert inside(None, cursor, (100, 0)) is False


def test_spring_converges_without_big_overshoot_and_is_stable_at_low_fps():
    import canaan_island_render as R
    s = R.Spring(126, 0.55, 0.825)
    s.set(384)
    peak, t = 0.0, 0.0
    while t < 3:
        s.step(1 / 60)
        peak = max(peak, s.value)
        t += 1 / 60
    assert s.resting() and peak <= 384 * 1.03
    s2 = R.Spring(0, 0.55, 0.825)
    s2.set(100)
    for _ in range(20):
        s2.step(0.25)                                                 # 低帧率大步长也稳定
    assert abs(s2.value - 100) < 0.01