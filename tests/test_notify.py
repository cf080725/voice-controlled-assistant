"""Windows 系统通知队列：去重、限频、脱敏、只读新追加的、游戏模式和关掉时不弹。"""
import json

import canaan_events as ev
import canaan_notify as cn


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_dedup_and_rate_limit(tmp_path):
    clock = Clock()
    n = cn.Notifier(tmp_path / "q.jsonl", clock)
    assert n.post("failure", "没办成", "关闭应用", key="a")
    assert not n.post("failure", "没办成", "关闭应用", key="a")          # 10 分钟内同 key 不再弹
    assert n.post("failure", "没办成", "x", key="b") and n.post("failure", "没办成", "y", key="c")
    assert not n.post("failure", "没办成", "z", key="d")                 # 每类每分钟最多 3 条
    assert n.post("reminder", "提醒", "交报告", key="e")                 # 别的类不受影响
    clock.t += 61
    assert n.post("failure", "没办成", "z", key="d")
    clock.t += cn.DEDUP_SECONDS
    assert n.post("failure", "没办成", "关闭应用", key="a")


def test_unknown_kind_and_body_masked(tmp_path):
    settings = tmp_path / "settings.json"
    cn.set_preview_enabled(True, settings)
    n = cn.Notifier(tmp_path / "q.jsonl", Clock(), settings)
    assert not n.post("progress", "第 1/3 步")                            # 普通进度不弹
    n.post("message", "有重要消息需核对", "手机 13812345678 回电")
    entry = json.loads((tmp_path / "q.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert "13812345678" not in entry["body"]


def test_mask_failure_drops_body(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "_mask", lambda text: None)
    settings = tmp_path / "settings.json"
    cn.set_preview_enabled(True, settings)
    n = cn.Notifier(tmp_path / "q.jsonl", Clock(), settings)
    n.post("agent", "Muse 办好了", "私密结果")
    assert json.loads((tmp_path / "q.jsonl").read_text(encoding="utf-8"))["body"] == ""


def test_default_notification_queue_has_no_semantic_private_content(tmp_path):
    path = tmp_path / "q.jsonl"
    n = cn.Notifier(path, Clock(), tmp_path / "missing-settings.json")
    assert n.post("reminder", "为虚构员工看诊", "虚构员工的诊断结果", key="fake-case")
    text = path.read_text(encoding="utf-8")
    assert "虚构员工" not in text and "诊断" not in text
    assert json.loads(text)["body"] == ""


def test_notification_switch_preserves_preview_choice(tmp_path):
    settings = tmp_path / "settings.json"
    cn.set_preview_enabled(True, settings)
    cn.set_enabled(False, settings)
    assert cn.show_preview(settings) and not cn.enabled(settings)


def test_reader_only_new_entries_and_survives_truncation(tmp_path):
    path, clock = tmp_path / "q.jsonl", Clock()
    n = cn.Notifier(path, clock)
    n.post("reminder", "提醒", "旧的", key="old")
    reader = cn.NotifyReader(path, clock)
    assert reader.read_new() == []                                        # 启动前的不回放
    n.post("reminder", "提醒", "新的", key="new")
    assert len(reader.read_new()) == 1
    assert reader.read_new() == []
    path.write_text("", encoding="utf-8")                                 # 队列被清空后从头读
    n.post("reminder", "提醒", "再来", key="again")
    assert len(reader.read_new()) == 1


def test_reader_skips_stale_entries(tmp_path):
    path, clock = tmp_path / "q.jsonl", Clock()
    reader = cn.NotifyReader(path, clock)
    cn.Notifier(path, clock).post("reminder", "提醒", "早就过了")
    clock.t += cn.MAX_AGE + 1
    assert reader.read_new() == []


def test_failed_queue_write_leaves_notification_retryable(tmp_path):
    path = tmp_path / "queue.jsonl"
    path.mkdir()
    notifier = cn.Notifier(path, Clock())
    assert not notifier.post("reminder", "提醒", key="retry")
    path.rmdir()
    assert notifier.post("reminder", "提醒", key="retry")


def test_reader_skips_invalid_rows_and_keeps_a_partial_row(tmp_path):
    path = tmp_path / "queue.jsonl"
    reader = cn.NotifyReader(path, Clock())
    path.write_bytes(b'null\n{"kind":"reminder","ts":"now"}\n'
                     b'{"kind":"reminder","ts":1001,"title":"future"}\n'
                     b'{"kind":"reminder","ts":1000,"title":"\\ud800"}\n'
                     b'{"kind":"reminder","ts":1000,"title":"ready"}\n'
                     b'{"kind":"reminder","ts":1000,"title":"partial"}')
    assert [entry["title"] for entry in reader.read_new()] == ["ready"]
    with path.open("ab") as stream:
        stream.write(b"\n")
    assert [entry["title"] for entry in reader.read_new()] == ["partial"]
    assert reader.read_new() == []


def test_queue_can_append_after_an_interrupted_write(tmp_path):
    path = tmp_path / "queue.jsonl"
    clock = Clock()
    reader = cn.NotifyReader(path, clock)
    path.write_bytes(b'{"kind":"reminder"')
    assert reader.read_new() == []
    assert cn.Notifier(path, clock).post("reminder", "Valid")
    assert len(reader.read_new()) == 1


def test_reader_finds_replaced_queue_at_same_size(tmp_path):
    path = tmp_path / "queue.jsonl"
    old = b'{"kind":"reminder","ts":1000,"title":"old"}\n'
    path.write_bytes(old)
    reader = cn.NotifyReader(path, Clock())
    rotated = path.with_suffix(".new")
    rotated.write_bytes(old.replace(b"old", b"new"))
    rotated.replace(path)
    assert [entry["title"] for entry in reader.read_new()] == ["new"]


def test_reader_skips_oversize_line_and_reads_following_notice(tmp_path):
    path = tmp_path / "queue.jsonl"
    reader = cn.NotifyReader(path, Clock())
    path.write_bytes(b"x" * (cn.MAX_QUEUE_BYTES + 5) + b'\n'
                     b'{"kind":"reminder","ts":1000,"title":"ready"}\n')
    assert reader.read_new() == []
    assert [entry["title"] for entry in reader.read_new()] == ["ready"]


def test_notification_content_rechecks_preview_setting(tmp_path):
    settings = tmp_path / "settings.json"
    cn.set_preview_enabled(True, settings)
    entry = {"kind": "reminder", "title": "Preview", "body": "Details"}
    assert cn.notification_content(entry, settings) == ("Preview", "Details")
    cn.set_preview_enabled(False, settings)
    assert cn.notification_content(entry, settings) == (cn.PRIVATE_TITLES["reminder"], "")


def test_should_show_respects_game_mode_and_switch(tmp_path):
    settings = tmp_path / "s.json"
    entry = {"kind": "failure", "title": "没办成"}
    assert cn.should_show(entry, settings_path=settings)
    assert not cn.should_show(entry, game_mode=True, settings_path=settings)
    cn.set_enabled(False, settings)
    assert not cn.should_show(entry, settings_path=settings)


def test_status_events_become_notices(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(cn, "post", lambda *a, **k: sent.append(a))
    cn.status_event_sink(ev.make_event("awaiting_confirm", "close_app", result="confirm"))
    cn.status_event_sink(ev.make_event("failure", "run_cmd", result="blocked"))
    cn.status_event_sink(ev.make_event("step", "open_app", 1, 2))
    assert [s[0] for s in sent] == ["confirm", "failure"] and sent[1][1] == "被拦下了"
