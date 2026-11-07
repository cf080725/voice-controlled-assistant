"""Read-only local requests and complete targets across intent and execution."""
import json
from types import SimpleNamespace

import pytest

import canaan_intent as ci
import canaan_os_executor as executor
from canaan_action_framework import ActionRegistry
from canaan_action_framework.actions import app_actions as aa
from canaan_security_policy import READONLY, classify_command


def resolve(text):
    decision = ci.fast_match_intent(text)
    return ci.apply_command_guard(text, decision) if decision else None


@pytest.mark.parametrize("text,field", [
    ("C盘现在还有多少容量", "Free"),
    ("看看磁盘剩余空间", "FreeSpace"),
    ("磁盘空间还剩几个G", "FreeSpace"),
    ("查一下系统版本", "Version"),
    ("查一下网络配置", "ipconfig"),
    ("查一下本机MAC地址", "MacAddress"),
    ("看看开了哪些进程", "ProcessName"),
    ("看看有哪些启动项", "Win32_StartupCommand"),
    ("查一下系统补丁版本", "HotFixID"),
    ("查一下电脑序列号", "SerialNumber"),
    ("看看有哪些定时任务", "Get-ScheduledTask"),
    ("查一下显卡驱动版本", "DriverVersion"),
    ("看看磁盘读写速度", "DiskReadBytesPersec"),
])
def test_local_queries_produce_grounded_readonly_commands(text, field):
    decision = resolve(text)
    assert decision and decision["action"] == "run_cmd", decision
    assert field.lower() in decision["param"].lower()
    assert classify_command(decision["param"]).level == READONLY


@pytest.mark.parametrize("text,command", [
    ("把任务管理器调出来", "taskmgr"),
    ("控制面板给我调出来", "control"),
    ("打开服务管理器", "services.msc"),
    ("打开设备管理器", "devmgmt.msc"),
    ("打开注册表编辑器", "regedit"),
])
def test_named_system_tools_have_fixed_commands(text, command):
    decision = resolve(text)
    assert decision and (decision["action"], decision["param"]) == ("run_cmd", command), decision


@pytest.mark.parametrize("text", ["查一下网络延迟", "看看网络丢包率"])
def test_network_measurement_asks_for_the_destination(text):
    decision = resolve(text)
    assert decision and decision["action"] == "chat" and "？" in decision["reply"], decision
    assert not decision.get("_pending")


@pytest.mark.parametrize("text", [
    "百度一下系统版本怎么看", "上网搜网络配置教程", "搜索磁盘剩余空间不足怎么办",
])
def test_explicit_online_help_stays_online(text):
    assert resolve(text)["action"] == "web_search"


@pytest.mark.parametrize("text", [
    "系统版本怎么查", "设备管理器是干嘛的", "我昨天已经打开设备管理器了", "不要打开服务管理器",
])
def test_local_query_words_in_questions_or_negations_do_not_run_commands(text):
    decision = resolve(text)
    assert decision is None or decision["action"] == "chat", decision


@pytest.mark.parametrize("text,root,query", [
    ("打开桌面的待办.txt", "desktop", "待办.txt"),
    ("桌面上那个报告帮我打开", "desktop", "报告"),
    ("打开项目里的README", "project", "README"),
    ("打开下载文件夹里的“测 试.txt”", "downloads", "测 试.txt"),
])
def test_file_requests_keep_the_named_directory_and_filename(text, root, query):
    decision = resolve(text)
    assert decision and decision["action"] == "search_file", decision
    param = decision["param"]
    if isinstance(param, str):
        param = json.loads(param)
    assert param == {"value": query, "root": root}


@pytest.mark.parametrize("text", [
    "在Excel里新建一个表格", "在Excel里把第三列求和", "在Excel里把重复项删掉",
    "在浏览器里收藏这个页面", "在浏览器里把广告关掉",
])
def test_in_app_operations_keep_the_complete_task_and_require_confirmation(text):
    decision = resolve(text)
    assert decision and (decision["action"], decision["param"]) == ("ufo_task", text), decision
    result = ActionRegistry.dispatch("ufo_task", decision["param"], source="rule", user_text=text)
    assert result.error_code == "CONFIRM_REQUIRED"


def test_scoped_file_execution_does_not_open_a_same_named_file_elsewhere(tmp_path, monkeypatch):
    desktop = tmp_path / "Desktop"
    project = tmp_path / "Project"
    desktop.mkdir()
    project.mkdir()
    wanted = desktop / "待办.txt"
    wanted.write_text("desktop", encoding="utf-8")
    (project / "待办.txt").write_text("other", encoding="utf-8")
    opened = []
    monkeypatch.setattr(aa, "_user_folder_path", lambda key: str(desktop))
    monkeypatch.setattr(aa, "_executor", lambda: SimpleNamespace(search_local_files=lambda *a, **k: pytest.fail("Exact scoped file must not fall back to global search")))
    monkeypatch.setattr(aa.os, "startfile", opened.append)
    result = ActionRegistry.dispatch("search_file", {"value": "待办.txt", "root": "desktop"}, source="rule")
    assert result.success, result
    assert opened == [str(wanted)]


def test_missing_scoped_file_does_not_search_other_folders(tmp_path, monkeypatch):
    from canaan_file_search import FileSearchResult
    queries = []
    monkeypatch.setattr(aa, "_user_folder_path", lambda key: str(tmp_path))
    def search(keyword, limit=5, roots=None):
        queries.append((keyword, roots))
        assert roots == [str(tmp_path)]
        return FileSearchResult((), "complete", "exhausted")
    monkeypatch.setattr(aa, "_executor", lambda: SimpleNamespace(search_local_files_result=search))
    monkeypatch.setattr(aa.os, "startfile", lambda *a: pytest.fail("No matching file"))
    result = ActionRegistry.dispatch("search_file", {"value": "不存在.txt", "root": "desktop"}, source="rule")
    assert not result.success and result.error_code == "FILE_NOT_FOUND"
    assert queries == [("不存在.txt", [str(tmp_path)])]


def test_scoped_executable_still_requires_confirmation(tmp_path, monkeypatch):
    target = tmp_path / "demo.cmd"
    target.write_text("echo fixture", encoding="utf-8")
    monkeypatch.setattr(aa, "_user_folder_path", lambda key: str(tmp_path))
    monkeypatch.setattr(aa.os, "startfile", lambda *a: pytest.fail("Executable must not run before confirmation"))
    result = ActionRegistry.dispatch("search_file", {"value": "demo.cmd", "root": "desktop"}, source="rule")
    assert result.error_code == "CONFIRM_REQUIRED"


def test_scoped_file_intent_survives_the_real_executor_wrapper(tmp_path, monkeypatch):
    target = tmp_path / "待办.txt"
    target.write_text("fixture", encoding="utf-8")
    opened = []
    monkeypatch.setattr(aa, "_user_folder_path", lambda key: str(tmp_path))
    monkeypatch.setattr(aa.os, "startfile", opened.append)
    text = "打开桌面的待办.txt"
    decision = resolve(text)
    result = executor.CanaanOSExecutor.execute(decision["action"], decision["param"], source="rule", user_text=text)
    assert result["success"], result
    assert opened == [str(target)]


def test_scoped_search_requires_a_choice_when_multiple_files_match(tmp_path, monkeypatch):
    folder = tmp_path / "Desktop"
    folder.mkdir()
    (folder / "报告一.txt").write_text("a", encoding="utf-8")
    (folder / "报告二.txt").write_text("b", encoding="utf-8")
    monkeypatch.setattr(aa, "_user_folder_path", lambda key: str(folder))
    monkeypatch.setattr(aa.os, "startfile", lambda *a: pytest.fail("Multiple files need a target choice"))
    result = ActionRegistry.dispatch("search_file", {"value": "报告", "root": "desktop"}, source="rule")
    assert not result.success and result.error_code == "AMBIGUOUS_TARGET"


@pytest.mark.parametrize("param", [
    {"value": "../outside.txt", "root": "desktop"},
    {"value": "C:\\outside.txt", "root": "desktop"},
    {"value": "demo.txt", "root": "invented"},
])
def test_invalid_scoped_targets_do_not_turn_into_global_search(param, monkeypatch):
    monkeypatch.setattr(aa.os, "startfile", lambda *a: pytest.fail("Invalid target"))
    monkeypatch.setattr(aa, "_executor", lambda: pytest.fail("Invalid scope must not search"))
    result = ActionRegistry.dispatch("search_file", param, source="rule")
    assert not result.success and result.error_code == "INVALID_TARGET"


def test_explicit_search_roots_do_not_fall_back_to_the_drive(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    other = tmp_path / "other"
    allowed.mkdir()
    other.mkdir()
    wanted = allowed / "README.txt"
    wanted.touch()
    (other / "README.txt").touch()
    monkeypatch.setattr(executor, "BASE_DIR", str(other))
    monkeypatch.setattr(executor, "FISHSIM_DIR", str(other))
    assert executor.CanaanOSExecutor.search_local_files("README", roots=[str(allowed)]) == [str(wanted)]


def test_query_execution_keeps_readonly_policy_and_returns_actual_result(monkeypatch):
    import canaan_action_framework.actions.system_actions as system
    received = []
    def run(args, **kwargs):
        received.append(args[-1])
        assert classify_command(args[-1]).level == READONLY
        assert kwargs["timeout"] == 15
        return SimpleNamespace(returncode=0, stdout="Caption Version BuildNumber\nTestOS 10.0 12345", stderr="")
    monkeypatch.setattr(system.subprocess, "run", run)
    text = "查一下系统版本"
    decision = resolve(text)
    result = executor.CanaanOSExecutor.execute(decision["action"], decision["param"], source="rule", user_text=text)
    assert result["success"] and "12345" in result["detail"]
    assert received == [decision["param"]]


@pytest.mark.parametrize("text,name", [
    ("打开桌面的图片文件夹", "图片"),
    ("打开桌面的“图片文件夹”", "图片文件夹"),
])
def test_explicit_scoped_folder_is_not_searched_as_a_file(text, name, tmp_path, monkeypatch):
    folder = tmp_path / name
    folder.mkdir()
    opened = []
    monkeypatch.setattr(aa, "_user_folder_path", lambda key: str(tmp_path))
    monkeypatch.setattr(aa.os, "startfile", opened.append)
    decision = resolve(text)
    assert decision and decision["action"] == "open_path", decision
    result = ActionRegistry.dispatch("open_path", decision["param"], source="rule", user_text=text)
    assert result.success and opened == [str(folder)], result


def test_quoted_folder_name_does_not_fall_back_to_another_folder(tmp_path, monkeypatch):
    (tmp_path / "图片").mkdir()
    monkeypatch.setattr(aa, "_user_folder_path", lambda key: str(tmp_path))
    monkeypatch.setattr(aa.os, "startfile", lambda *a: pytest.fail("Quoted name must remain exact"))
    decision = resolve("打开桌面的“图片文件夹”")
    assert decision and decision["action"] == "open_path", decision
    result = ActionRegistry.dispatch("open_path", decision["param"], source="rule")
    assert not result.success and result.error_code == "PATH_NOT_FOUND"


@pytest.mark.parametrize("encode", [False, True])
@pytest.mark.parametrize("text,action,param", [
    ("打开桌面的待办.txt", "search_file", {"value": "待办.txt", "root": "downloads"}),
    ("打开桌面的待办.txt", "search_file", {"value": "其他.txt", "root": "desktop"}),
    ("打开桌面的待办.txt", "open_path", {"value": "待办.txt", "root": "desktop"}),
    ("打开桌面的“图片文件夹”", "open_path", {"value": "图片文件夹", "root": "desktop"}),
])
def test_model_cannot_change_a_scoped_target_or_remove_its_exact_name(text, action, param, encode):
    raw = json.dumps(param, ensure_ascii=False) if encode else param
    guarded = ci.apply_command_guard(text, {"action": action, "param": raw, "reply": ""})
    assert guarded["action"] == "chat" and not guarded.get("_pending"), guarded


def test_unquoted_folder_suffix_with_two_matches_requires_a_choice(tmp_path, monkeypatch):
    (tmp_path / "图片").mkdir()
    (tmp_path / "图片文件夹").mkdir()
    monkeypatch.setattr(aa, "_user_folder_path", lambda key: str(tmp_path))
    monkeypatch.setattr(aa.os, "startfile", lambda *a: pytest.fail("Both names exist"))
    decision = resolve("打开桌面的图片文件夹")
    result = ActionRegistry.dispatch("open_path", decision["param"], source="rule")
    assert not result.success and result.error_code == "AMBIGUOUS_TARGET"


@pytest.mark.parametrize("text", [
    "在Excel里不要新建表格", "在Excel里把重复项先别删掉",
    "在浏览器里不要收藏这个页面", "在浏览器里把广告先别关掉",
])
def test_in_app_negations_do_not_create_a_confirmation_for_an_action(text):
    decision = resolve(text)
    assert decision is None or decision["action"] == "chat", decision
    guessed = ci.apply_command_guard(text, {"action": "ufo_task", "param": text, "reply": ""})
    assert guessed["action"] == "chat" and not guessed.get("_pending"), guessed


@pytest.mark.parametrize("text", ["在记事本里输入", "在记事本里写"])
def test_in_app_text_entry_requires_the_content(text):
    decision = resolve(text)
    assert decision and decision["action"] == "chat" and "？" in decision["reply"], decision


@pytest.mark.parametrize("body", ["先别删掉这行字", "怎么打开设备管理器"])
def test_quoted_text_entry_is_content_not_a_question_or_negation(body):
    text = f"在记事本里输入“{body}”"
    decision = resolve(text)
    assert decision and (decision["action"], decision["param"]) == ("ufo_task", text), decision
