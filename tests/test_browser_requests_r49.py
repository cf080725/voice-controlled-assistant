"""Real browser actors/registry/scheduler with a single, entirely fake browser port.

AST loading excludes application startup, native imports, models and HTTP clients.
Only the registered browser actions and synthetic marker actions can be scheduled.
"""
import ast
import builtins
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Type
import urllib.parse

import pytest

from canaan_execution_guard import execution_allowed, execution_guard


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def browser_runtime(monkeypatch):
    models_path = ROOT / "core/canaan_action_framework/models.py"
    spec = importlib.util.spec_from_file_location("_r49_browser_models", models_path)
    models = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, models)
    spec.loader.exec_module(models)
    events, lifecycle = [], []
    runtime = SimpleNamespace(result=True, failure=None)
    registry_holder = {}

    pure_query_helpers = {}
    pure_query_stdlib = {"contextlib", "contextvars", "dataclasses", "json", "re", "decimal", "enum", "ipaddress"}

    def pure_query_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name in pure_query_helpers:
            return pure_query_helpers[name]
        if level == 0 and name in pure_query_stdlib:
            return builtins.__import__(name, globals, locals, fromlist, level)
        raise AssertionError(f"External/native/application import escaped pure helper isolation: {name}")

    for name in ("canaan_query_answers", "canaan_query_context", "canaan_command_reply"):
        helper_spec = importlib.util.spec_from_file_location("_r49_browser_" + name, ROOT / "core" / (name + ".py"))
        helper = importlib.util.module_from_spec(helper_spec)
        monkeypatch.setitem(sys.modules, helper_spec.name, helper)
        helper.__dict__["__builtins__"] = dict(vars(builtins), __import__=pure_query_import)
        helper_spec.loader.exec_module(helper)
        pure_query_helpers[name] = helper

    def isolated_import(name, *args, **kwargs):
        if name in pure_query_helpers:
            return pure_query_helpers[name]
        if name == "canaan_action_framework":
            return SimpleNamespace(ActionRegistry=registry_holder["registry"])
        if name == "canaan_events":
            return SimpleNamespace(publish=lambda *args, **kwargs: lifecycle.append(args))
        raise AssertionError(f"External/native/application import escaped isolation: {name}")

    ns = {"__builtins__": dict(vars(builtins), __import__=isolated_import),
          "Any": Any, "Callable": Callable, "Dict": Dict, "List": List,
          "Optional": Optional, "Type": Type, "json": json, "re": re,
          "urllib": urllib, "time": SimpleNamespace(time=time.time, sleep=lambda _: None),
          "os": SimpleNamespace(environ={}), "execution_allowed": execution_allowed}
    ns.update({name: getattr(models, name) for name in
               ("ActionContext", "ActionResult", "VerificationResult", "ActionCategory", "PermissionLevel")})
    from abc import ABC, abstractmethod
    ns.update(ABC=ABC, abstractmethod=abstractmethod)

    def load(path, select):
        source = ROOT / path
        tree = ast.parse(source.read_text(encoding="utf-8"))
        body = [node for node in tree.body if select(node)]
        exec(compile(ast.Module(body=body, type_ignores=[]), str(source), "exec"), ns)

    load("core/canaan_action_framework/base_action.py",
         lambda node: isinstance(node, ast.ClassDef) and node.name == "BaseAction")
    load("core/canaan_action_framework/action_registry.py",
         lambda node: isinstance(node, ast.ClassDef) and node.name == "ActionRegistry" or
         isinstance(node, ast.FunctionDef) and node.name in {"dry_run_enabled", "_revoked_result"})
    registry = ns["ActionRegistry"]
    registry_holder["registry"] = registry
    registry._audit_log = classmethod(lambda cls, context, result: None)

    def browser_open(url):
        events.append(("browser", url))
        if runtime.failure is not None:
            raise runtime.failure
        return runtime.result

    ns["webbrowser"] = SimpleNamespace(open=browser_open)
    load("core/canaan_action_framework/actions/app_actions.py",
         lambda node: isinstance(node, ast.ClassDef) and node.name in {"OpenUrlAction", "WebSearchAction"} or
         isinstance(node, ast.FunctionDef) and node.name.startswith("_browser_"))
    source = ROOT / "core/canaan_os_executor.py"
    executor_tree = ast.parse(source.read_text(encoding="utf-8"))
    executor_class = next(node for node in executor_tree.body
                          if isinstance(node, ast.ClassDef) and node.name == "CanaanOSExecutor")
    executor_class.body = [node for node in executor_class.body
                           if isinstance(node, ast.FunctionDef) and node.name in {"execute", "_execute"}]
    exec(compile(ast.Module(body=[executor_class], type_ignores=[]), str(source), "exec"), ns)

    @registry.register
    class Marker(ns["BaseAction"]):
        name = "_r49_marker"

        def execute(self, context):
            events.append(("marker", context.param_value()))
            return models.ActionResult(success=True, action=self.name, message="Synthetic marker completed")

    def invoke(action, param, metadata=None, name=None):
        actor = registry.get_action(action)
        context = models.ActionContext(name or action, raw_param=param,
                                       metadata={} if metadata is None else metadata)
        return actor, context, actor.execute(context)

    return SimpleNamespace(ns=ns, models=models, registry=registry, executor=ns["CanaanOSExecutor"],
                           events=events, lifecycle=lifecycle, state=runtime, invoke=invoke)


def test_ast_registry_respects_real_execution_guard_without_browser_effect(browser_runtime):
    f = browser_runtime
    with execution_guard(lambda: False):
        result = f.registry.dispatch("open_url", "example.com", source="rule")
    assert not result.success and result.error_code == "EXECUTION_REVOKED"
    assert result.detail["phase"] == "before_authorize" and result.detail["execution_started"] is False
    assert not f.events


@pytest.mark.parametrize("action,param", [("open_url", "https://example.com/a?x=1#part"),
                                        ("web_search", "树莓派 / 中文 + &")])
@pytest.mark.parametrize("returned", [False, None, 0, 1, "True", [], {}, object()])
def test_only_literal_true_acceptance_can_report_browser_request_success(browser_runtime, action, param, returned):
    f = browser_runtime
    f.state.result = returned
    result = f.registry.dispatch(action, param, source="rule")
    assert result.success is False and result.error_code
    assert not result.verification or result.verification.verified is False
    assert len(f.events) == 1
    assert "打开了" not in (result.speech_override or "") and "搜好了" not in (result.speech_override or "")


@pytest.mark.parametrize("action,param", [("open_url", "example.com"), ("web_search", "测试搜索")])
def test_browser_exception_is_uncertain_failure_without_retry_or_completion(browser_runtime, action, param):
    f = browser_runtime
    f.state.failure = OSError("synthetic failure after possible launch")
    actor, context, result = f.invoke(action, param)
    assert not result.success and result.error_code == "BROWSER_REQUEST_ERROR"
    assert len(f.events) == 1 and not actor.verify(context).passed
    assert "未核实" in result.message
    assert all(word not in result.message + (result.speech_override or "") for word in ("回滚", "未执行", "没有执行", "已打开", "搜好了"))


@pytest.mark.parametrize("action,param,speech,target", [
    ("open_url", "example.com", "已向浏览器发送打开请求。", "https://example.com"),
    ("web_search", "中文 / A+B&C#D?", "已向浏览器发送搜索请求。",
     "https://www.baidu.com/s?wd=%E4%B8%AD%E6%96%87%20/%20A%2BB%26C%23D%3F"),
])
def test_true_is_request_acceptance_with_no_page_or_search_result_verification(browser_runtime, action, param, speech, target):
    f = browser_runtime
    result = f.registry.dispatch(action, param, source="rule")
    assert result.success and result.speech_override == speech
    assert result.verification.passed is True and result.verification.verified is False
    assert result.verification.gate_name == "BROWSER_REQUEST_ACCEPTED"
    assert "未核实" in result.verification.reason
    assert result.detail["target"] == target
    assert result.detail["request_accepted"] is True and result.detail["page_verified"] is False
    assert f.events == [("browser", target)]


@pytest.mark.parametrize("alias,param", [(" BROWSE ", "example.com"), ("open_website", "example.com"),
                                       (" SEARCH_WEB ", "中文"), ("baidu", "中文")])
def test_registered_alias_dispatch_accepts_fresh_request_without_page_proof(browser_runtime, alias, param):
    f = browser_runtime
    result = f.registry.dispatch(alias, param, source="rule")
    assert result.success and result.verification.passed is True
    assert result.verification.verified is False and len(f.events) == 1


@pytest.mark.parametrize("action,param", [("open_url", "example.com"), ("web_search", "树莓派")])
@pytest.mark.parametrize("fault", [False, None, "exception"])
def test_failed_browser_step_stops_actual_full_scheduler_tail(browser_runtime, action, param, fault):
    f = browser_runtime
    if fault == "exception":
        f.state.failure = OSError("synthetic post-request exception")
    else:
        f.state.result = fault
    steps = [{"action": "_r49_marker", "param": "before"}, {"action": action, "param": param},
             {"action": "_r49_marker", "param": "after"}, {"action": "web_search", "param": "unrequested fallback"}]
    result = f.executor.execute("multi_action", json.dumps(steps), source="rule")
    assert result["success"] is False and result["failed_action"] == action
    assert result["failed_step"] == 2 and result["completed_steps"] == 1 and result["skipped_steps"] == 2
    assert [event[0] for event in f.events] == ["marker", "browser"]
    assert f.events[0] == ("marker", "before") and "其后 2 步未执行" in result["message"]
    assert "都办好了" not in (result.get("speech_override") or "")


@pytest.mark.parametrize("param,target", [
    ("example.com:8080/path;params?x=1&x=2#Part", "https://example.com:8080/path;params?x=1&x=2#Part"),
    (' "https://EXAMPLE.com:443/a%2Fb;v=2?Q=%2B+%20&x=One#Part%2F" ',
     "https://EXAMPLE.com:443/a%2Fb;v=2?Q=%2B+%20&x=One#Part%2F"),
    ("http://user:pass@example.com/a?x=1#f", "http://user:pass@example.com/a?x=1#f"),
    ("https://example.com/中文?q=测试#片段", "https://example.com/中文?q=测试#片段"),
])
@pytest.mark.parametrize("representation", ["scalar", "value", "json_value"])
def test_valid_url_normalization_preserves_exact_authority_path_params_query_fragment(browser_runtime, param, target, representation):
    f = browser_runtime
    if representation == "value":
        param = {"value": param}
    elif representation == "json_value":
        param = json.dumps({"value": param}, ensure_ascii=False)
    result = f.registry.dispatch("open_url", param, source="rule")
    assert result.success and result.verification.passed and not result.verification.verified
    assert f.events == [("browser", target)]


@pytest.mark.parametrize("url", ["https://[example.com/path", "https://example.com]/path",
                                 "https://example.com:notaport/x", "https://example.com:65536/x",
                                 "https://[::1]/x", "https://example.com\n.evil.test/x",
                                 "https://example.com/pa\tth", "javascript:alert(1)",
                                 "file:///C:/Windows", "data:text/html,<h1>hello</h1>", "", "https://"])
def test_malformed_or_non_web_url_is_invalid_before_any_browser_request(browser_runtime, url):
    f = browser_runtime
    result = f.registry.dispatch("open_url", url, source="rule")
    assert not result.success and result.error_code == "INVALID_URL"
    assert not f.events


def test_url_dict_field_is_not_silently_added_as_a_second_parameter_contract(browser_runtime):
    result = browser_runtime.registry.dispatch("open_url", {"url": "https://example.com"}, source="rule")
    assert not result.success and result.error_code == "INVALID_URL" and not browser_runtime.events


@pytest.mark.parametrize("action,param", [("open_url", "example.com"), ("web_search", "树莓派")])
@pytest.mark.parametrize("mutation", ["trace", "action", "target", "copied_context"])
def test_request_proof_is_bound_to_current_trace_action_and_normalized_target(browser_runtime, action, param, mutation):
    f = browser_runtime
    actor, context, result = f.invoke(action, param)
    assert result.success and actor.verify(context).passed and not actor.verify(context).verified
    if mutation == "trace":
        context.trace_id = "fresh-other-trace"
    elif mutation == "action":
        context.action_name = "web_search" if action == "open_url" else "open_url"
    elif mutation == "target":
        context.raw_param = "other.example.com" if action == "open_url" else "另一个查询"
    else:
        context = f.models.ActionContext(action, raw_param=param, metadata=context.metadata)
    proof = actor.verify(context)
    assert proof.passed is False and proof.verified is False


@pytest.mark.parametrize("action,param,invalid", [("open_url", "example.com", "https://[broken"),
                                                ("web_search", "树莓派", "")])
def test_invalid_second_attempt_clears_prior_acceptance_metadata(browser_runtime, action, param, invalid):
    f = browser_runtime
    actor, context, result = f.invoke(action, param)
    assert result.success and actor.verify(context).passed
    context.raw_param = invalid
    failed = actor.execute(context)
    assert not failed.success and not actor.verify(context).passed
    context.raw_param = param
    assert not actor.verify(context).passed and len(f.events) == 1


@pytest.mark.parametrize("action,param", [("open_url", "example.com"), ("web_search", "树莓派")])
@pytest.mark.parametrize("failure", [False, None, "exception"])
def test_failed_second_attempt_clears_old_success_even_if_same_trace_and_target(browser_runtime, action, param, failure):
    f = browser_runtime
    actor, context, result = f.invoke(action, param)
    assert result.success and actor.verify(context).passed
    if failure == "exception":
        f.state.failure = OSError("synthetic post-request error")
    else:
        f.state.result = failure
    failed = actor.execute(context)
    assert not failed.success and not actor.verify(context).passed
    assert len(f.events) == 2


@pytest.mark.parametrize("observation", [None, [], "accepted", {"accepted": True}])
def test_unexecuted_context_never_inherits_browser_acceptance(browser_runtime, observation):
    f = browser_runtime
    actor = f.registry.get_action("open_url")
    context = f.models.ActionContext("open_url", raw_param="example.com",
                                    metadata={"_browser_request_observation": observation})
    proof = actor.verify(context)
    assert proof.passed is False and proof.verified is False and not f.events
