"""External task lifecycle over the existing action registry and OS execution lock."""
import json
import sqlite3
import threading
import time
import uuid
from contextlib import nullcontext
from pathlib import Path


class IntegrationTasks:
    def __init__(self, path, registry=None, executor=None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.message_db = str(Path(path).parent / "messages.db")
        if registry is None:
            from canaan_action_framework import ActionRegistry
            registry = ActionRegistry
        self.registry = registry
        self.default_executor = executor is None
        self.executor = executor or self._execute
        # Serialize schema migration across connections, including older databases.
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("""CREATE TABLE IF NOT EXISTS tasks(
                id TEXT PRIMARY KEY, action TEXT, param TEXT, state TEXT, result TEXT,
                approved INTEGER, created_at REAL, updated_at REAL)""")
            if "client_id" not in {r[1] for r in self.db.execute("PRAGMA table_info(tasks)")}:
                self.db.execute("ALTER TABLE tasks ADD COLUMN client_id TEXT")  # 谁提出的；外部客户端只看得到自己的任务详情
            self.db.execute("""CREATE TABLE IF NOT EXISTS task_requests(
                namespace TEXT NOT NULL, request_id TEXT NOT NULL,
                task_id TEXT NOT NULL REFERENCES tasks(id), payload TEXT NOT NULL,
                PRIMARY KEY(namespace, request_id))""")
            self.db.execute("UPDATE tasks SET state='unknown' WHERE state IN ('queued','running')")
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.db.close()
            raise

    def _execute(self, action, param, approved, on_step=None):
        if action == "daily_briefing":
            param = {**(param if isinstance(param, dict) else {}), "message_db": self.message_db}
        return self.registry.dispatch(action, param, source="api", confirmed=approved, on_step=on_step).to_dict()

    @staticmethod
    def _request_payload(action, param, require_confirmation, permission):
        """JSON types stay distinct; only object key order is insignificant."""
        def validate(value):
            if value is None or type(value) in (str, bool, int, float):
                return
            if type(value) is list:
                for item in value:
                    validate(item)
                return
            if type(value) is dict and all(type(key) is str for key in value):
                for item in value.values():
                    validate(item)
                return
            raise ValueError("带 request_id 的动作参数须为 JSON 类型，对象键须为字符串")

        try:
            validate(param)
            return json.dumps({"action": action, "param": param,
                               "require_confirmation": require_confirmation, "permission": permission},
                              ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, RecursionError) as ex:
            raise ValueError("带 request_id 的动作参数须为有限、无循环的 JSON 数据") from ex

    def submit(self, action, param=None, client_id=None, require_confirmation=False, request_id=None):
        """require_confirmation 让只读任务也等主人确认；request_id 按提出者持久去重。

        client_id 由可信调用方传入（API 使用已认证 principal.id）；None 是独立的主人命名空间。
        重试只返回当前任务，包括终态和 unknown；不会重新排队或执行。
        """
        if not isinstance(action, str) or len(action) > 80:
            raise ValueError("action 须为不超过 80 字符的动作名")
        act = self.registry.get_action(action)
        if act is None:
            raise ValueError("动作不存在")
        namespace = payload = None
        if request_id is not None:
            if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
                raise ValueError("request_id 须为 1–128 字符的非空字符串")
            if client_id is not None and (not isinstance(client_id, str) or not client_id.strip() or len(client_id) > 128):
                raise ValueError("client_id 须为 1–128 字符的非空字符串")
            if type(require_confirmation) is not bool:
                raise ValueError("require_confirmation 须为布尔值")
            namespace = "owner" if client_id is None else "client:" + client_id
            payload = self._request_payload(act.name, param, require_confirmation, act.permission_level.value)
        param_json = json.dumps(param, ensure_ascii=False)
        if len(param_json) > 16000:
            raise ValueError("动作参数过长")
        readonly = act.permission_level.value == "safe_readonly" and not require_confirmation
        task_id = uuid.uuid4().hex
        state = "queued" if readonly else "awaiting_confirmation"
        with self.lock:
            try:
                # The write lock covers lookup + both inserts, also across DB connections.
                self.db.execute("BEGIN IMMEDIATE")
                if request_id is not None:
                    existing = self.db.execute("SELECT task_id,payload FROM task_requests WHERE namespace=? AND request_id=?",
                                               (namespace, request_id)).fetchone()
                    if existing is not None:
                        if existing["payload"] != payload:
                            raise ValueError("request_id 冲突：动作、参数或确认策略与首次请求不同")
                        task = self.get(existing["task_id"])
                        self.db.commit()
                        return task
                self.db.execute("INSERT INTO tasks(id,action,param,state,result,approved,created_at,updated_at,client_id) VALUES(?,?,?,?,?,?,?,?,?)",
                                (task_id, act.name, param_json, state, "{}", 0, time.time(), time.time(), client_id))
                if request_id is not None:
                    self.db.execute("INSERT INTO task_requests(namespace,request_id,task_id,payload) VALUES(?,?,?,?)",
                                    (namespace, request_id, task_id, payload))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        if readonly:
            self._start(task_id)
        return self.get(task_id)

    def get(self, task_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError("任务不存在")
            result = dict(row)
            result["param"] = json.loads(result["param"])
            result["result"] = json.loads(result["result"])
            return result

    def list(self, action=None):
        with self.lock:
            rows = self.db.execute("SELECT id FROM tasks WHERE (? IS NULL OR action=?) ORDER BY created_at DESC LIMIT 100", (action, action))
            return [self.get(r[0]) for r in rows]

    def confirm(self, task_id):
        with self.lock:
            self.get(task_id)
            changed = self.db.execute("UPDATE tasks SET state='queued',approved=1 WHERE id=? AND state='awaiting_confirmation'", (task_id,)).rowcount
            self.db.commit()
        if changed:
            self._start(task_id)
        return self.get(task_id)

    def cancel(self, task_id):
        with self.lock:
            task = self.get(task_id)
            if task["state"] in ("queued", "awaiting_confirmation"):
                self.db.execute("UPDATE tasks SET state='cancelled',updated_at=? WHERE id=?", (time.time(), task_id))
                self.db.commit()
            elif task["state"] == "running" and task["action"] == "computer_use":
                from canaan_computer_use import CanaanComputerUse
                CanaanComputerUse.request_stop()
        return self.get(task_id)

    def _start(self, task_id):
        def run():
            task = self.get(task_id)
            execution_lock = nullcontext()
            if self.default_executor and task["action"] not in ("openmuse_task", "opendots_task", "multiagent_task", "knowledge_query", "opendots_memory", "daily_briefing"):
                from canaan_task_manager import GLOBAL_TASK_MANAGER
                execution_lock = GLOBAL_TASK_MANAGER.os_exec_lock
            # Keep waiting tasks queued so cancellation cannot stop a different active task.
            with execution_lock:
                with self.lock:
                    changed = self.db.execute("UPDATE tasks SET state='running',updated_at=? WHERE id=? AND state='queued'", (time.time(), task_id)).rowcount
                    self.db.commit()
                    task = self.get(task_id)
                if not changed:
                    return
                def on_step(current, total, description):
                    progress = {"progress": {"current": current, "total": total, "description": description}}
                    with self.lock:
                        self.db.execute("UPDATE tasks SET result=?,updated_at=? WHERE id=? AND state='running'",
                                        (json.dumps(progress, ensure_ascii=False), time.time(), task_id))
                        self.db.commit()
                try:
                    if self.default_executor:
                        result = self._execute(task["action"], task["param"], bool(task["approved"]), on_step)
                    else:
                        result = self.executor(task["action"], task["param"], bool(task["approved"]))
                except Exception as ex:
                    result = {"success": False, "message": f"任务执行异常：{type(ex).__name__}"}
            state = "succeeded" if result.get("success") else "failed"
            if result.get("error_code") == "CONFIRM_REQUIRED":
                state = "awaiting_confirmation"
            with self.lock:
                self.db.execute("UPDATE tasks SET state=?,result=?,updated_at=? WHERE id=?",
                                (state, json.dumps(result, ensure_ascii=False), time.time(), task_id))
                self.db.commit()
        threading.Thread(target=run, name=f"canaan-task-{task_id[:8]}", daemon=True).start()
