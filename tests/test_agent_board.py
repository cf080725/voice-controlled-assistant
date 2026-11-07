# -*- coding: utf-8 -*-
import canaan_agent_board_data as ab

MUSE_SNAP = {"tasks": [
    {"id": "m1", "title": "监控京东价格", "prompt": "x", "status": "running", "updatedAt": "2026-10-08T10:00:00Z",
     "plan": [{"id": "s1", "title": "打开网页", "status": "succeeded"}, {"id": "s2", "title": "比价", "status": "running"},
              {"id": "s3", "title": "汇报", "status": "pending"}]},
    {"id": "m2", "title": "填报销单", "status": "waiting_input", "question": "发票号码是多少？电话 13812345678",
     "updatedAt": "2026-10-08T09:00:00Z", "plan": []},
    {"id": "m3", "title": "写周报", "status": "succeeded", "result": "已完成周报，共 3 节", "updatedAt": "2026-10-08T08:00:00Z", "plan": []},
    {"id": "m4", "title": "订机票", "status": "failed", "error": "登录超时", "updatedAt": "2026-10-08T07:00:00Z", "plan": []},
]}
DOTS_STATE = {"tasks": [
    {"id": "d1", "prompt": "每天整理行业新闻", "status": "queued", "intervalSeconds": 86400, "createdAt": 1760000000000,
     "updatedAt": 1760000000000, "error": None},
    {"id": "d2", "prompt": "写一份竞品研报", "status": "completed", "updatedAt": 1760000100000, "error": None},
    {"id": "d3", "prompt": "查资料", "status": "interrupted", "updatedAt": 1760000200000, "error": "网络中断"},
]}


def test_normalize_muse_status_steps_and_order():
    ts = ab.normalize_muse(MUSE_SNAP)
    assert [t["id"] for t in ts] == ["m2", "m1", "m4", "m3"]          # 等你处理 > 进行中 > 失败 > 完成
    m1 = next(t for t in ts if t["id"] == "m1")
    assert m1["steps"] == (1, 3) and m1["current"] == "比价"
    m2 = ts[0]
    assert m2["status"] == "waiting" and "13812345678" not in m2["note"]   # 显示前脱敏
    assert next(t for t in ts if t["id"] == "m4")["note"] == "登录超时"


def test_normalize_dots_and_counts():
    ts = ab.normalize_dots(DOTS_STATE)
    st = {t["id"]: t["status"] for t in ts}
    assert st == {"d1": "scheduled", "d2": "done", "d3": "failed"}
    c = ab.counts(ts)
    assert c["scheduled"] == 1 and c["done"] == 1 and c["failed"] == 1 and c["running"] == 0
    assert ts[0]["updated"] > 1e9                                       # 毫秒转成秒


def test_fetch_offline_and_online_read_only():
    calls = []

    def get_ok(url, data=None, headers=None, timeout=2.5):
        calls.append((url, data))
        if url.endswith("/api/session"):
            return {"token": "t"}
        if url.endswith("/api/agent"):
            assert headers == {"Authorization": "Bearer t"}
            return MUSE_SNAP
        return DOTS_STATE
    m = ab.fetch_muse(get_ok)
    d = ab.fetch_dots(get_ok)
    assert m["online"] and len(m["tasks"]) == 4 and d["online"] and len(d["tasks"]) == 3
    assert m["web_url"] == ab.MUSE_WEB and d["web_url"] == ab.DOTS_WEB
    assert all(u.startswith("http://127.0.0.1") for u, _ in calls)     # 只访问本机（不用 localhost：Windows 上先试 IPv6，每次白等约 2 秒）
    assert [u.rsplit("/", 2)[-2:] for u, _ in calls] == [["api", "session"], ["api", "agent"], ["api", "state"]]  # 不创建/修改任务

    def get_down(*a, **k):
        raise ConnectionRefusedError("拒绝连接")
    off = ab.fetch_muse(get_down)
    assert off["online"] is False and off["tasks"] == [] and "拒绝" in off["error"]
    assert off["web_url"] == ab.MUSE_WEB


def test_ago():
    assert ab.ago(100, now=130) == "30 秒前" and ab.ago(0) == "" and ab.ago(100, now=100 + 7200) == "2 小时前"


def test_muse_session_reused_by_board_watcher_and_coordinator(monkeypatch):
    import json
    from unittest.mock import MagicMock
    from canaan_agent_coordinator import OpenMuseProvider
    from canaan_agent_watcher import AgentTaskWatcher

    calls = []
    clock = [100.0]
    monkeypatch.setattr(OpenMuseProvider, "_token_cache", None)
    monkeypatch.setattr("canaan_agent_coordinator.time.monotonic", lambda: clock[0])

    def request(req, timeout):
        calls.append(req.full_url)
        session = req.full_url.endswith("/api/session")
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"token": "test-session"} if session else MUSE_SNAP).encode()
        return response

    monkeypatch.setattr("urllib.request.urlopen", request)
    assert OpenMuseProvider.get_token() == "test-session"
    assert AgentTaskWatcher._get_muse_token(None) == "test-session"
    assert ab.fetch_muse()["online"]
    assert ab.fetch_muse()["online"]
    assert sum(url.endswith("/api/session") for url in calls) == 1
    clock[0] += 301
    assert OpenMuseProvider.get_token() == "test-session"
    assert sum(url.endswith("/api/session") for url in calls) == 2


def test_muse_expired_session_renews_once_and_retries_read(monkeypatch):
    import json
    import urllib.error
    from unittest.mock import MagicMock
    from canaan_agent_coordinator import OpenMuseProvider, MUSE_API_URL

    monkeypatch.setattr(OpenMuseProvider, "_token_cache", (MUSE_API_URL, "old-session", ab.time.monotonic()))
    calls = []

    def request(req, timeout):
        calls.append(req.full_url)
        session = req.full_url.endswith("/api/session")
        if not session and req.get_header("Authorization") == "Bearer old-session":
            raise urllib.error.HTTPError(req.full_url, 401, "expired", {}, None)
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"token": "new-session"} if session else MUSE_SNAP).encode()
        return response

    monkeypatch.setattr("urllib.request.urlopen", request)
    assert ab.fetch_muse()["online"]
    assert [url.rsplit("/", 1)[-1] for url in calls] == ["agent", "session", "agent"]
    assert OpenMuseProvider.get_token() == "new-session"
