"""Herdr transport and the plugin-owned polling process."""
import fcntl
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from contextlib import contextmanager

from . import PLUGIN_ID
from .store import Conflict

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "orchestrator.py"
POLL_SECONDS = 5
WAKE_RETRY_SECONDS = 60
DAEMON_TIMEOUT = 30


class HerdrError(RuntimeError):
    def __init__(self, message, code="herdr_error"):
        super().__init__(message)
        self.code = code


def identity(agent):
    session = agent.get("agent_session") or {}
    if not agent.get("agent") or not session.get("value"):
        raise Conflict("agent has no native session identity; check its Herdr integration first")
    return json.dumps({"agent": agent["agent"], "kind": session["kind"],
                       "session": session["value"]}, sort_keys=True)


class Herdr:
    def __init__(self, socket, env=None):
        self.env = dict(os.environ if env is None else env)
        if self.env.get("HERDR_ENV") != "1" or not socket:
            raise Conflict("run inside Herdr with HERDR_ENV=1 and an explicit session socket")
        self.env["HERDR_SOCKET_PATH"] = socket
        self.socket = socket
        self.binary = self.env.get("HERDR_BIN_PATH") or "herdr"

    def call(self, *args):
        try:
            result = subprocess.run([self.binary, *args], env=self.env, capture_output=True,
                                    text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HerdrError(str(error), "transport_uncertain") from error
        raw = result.stdout if result.returncode == 0 else result.stderr
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise HerdrError(f"Herdr returned non-JSON ({result.returncode}): {raw.strip()}",
                             "transport_uncertain") from error
        if result.returncode or "error" in data:
            detail = data.get("error") or {}
            raise HerdrError(detail.get("message", raw.strip()), detail.get("code", "herdr_error"))
        return data.get("result", data)

    def agent(self, target):
        result = self.call("agent", "get", target)
        agent = result.get("agent")
        if not isinstance(agent, dict):
            raise HerdrError("agent.get returned no agent record")
        return agent

    def prompt(self, target, text):
        return self.call("agent", "prompt", target, text)

    def plugin_enabled(self):
        result = self.call("plugin", "list", "--plugin", PLUGIN_ID, "--json")
        return any(plugin.get("plugin_id") == PLUGIN_ID and plugin.get("enabled")
                   for plugin in result["plugins"])

    def caller(self):
        pane = self.env.get("HERDR_PANE_ID")
        if not pane:
            raise Conflict("this command requires the calling agent's HERDR_PANE_ID")
        return self.agent(pane)

    def invoke_action(self, name, context):
        request = {"id": "orchestrator-panel", "method": "plugin.action.invoke",
                   "params": {"plugin_id": PLUGIN_ID, "action_id": name, "context": context}}
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(20)
                connection.connect(self.socket)
                connection.sendall((json.dumps(request) + "\n").encode())
                with connection.makefile("r", encoding="utf-8") as response:
                    result = json.loads(response.readline())
        except (OSError, ValueError) as error:
            raise HerdrError(str(error), "transport_uncertain") from error
        if "error" in result:
            detail = result["error"]
            raise HerdrError(detail["message"], detail["code"])
        return result["result"]


def command_prefix(store, socket):
    return shlex.join([sys.executable, str(ENTRY), "--state-dir", str(store.directory),
                       "--socket", socket])


@contextmanager
def control_lock(store):
    with (store.directory / "control.lock").open("a") as lock:
        deadline = time.monotonic() + DAEMON_TIMEOUT
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("another coordinator action is still running")
                time.sleep(0.05)
        yield


def pause_daemon(store, socket):
    session = store.session(socket)
    store.update_session(socket, enabled=0)
    with (store.directory / f"session-{session['id']}.lock").open("a") as lock:
        deadline = time.monotonic() + DAEMON_TIMEOUT
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("coordination paused, but the old daemon has not released its lock")
                time.sleep(0.05)
        store.update_session(socket, pid=None)


def action_context(herdr, name):
    if (herdr.env.get("HERDR_PLUGIN_ID") != PLUGIN_ID
            or herdr.env.get("HERDR_PLUGIN_ACTION_ID") != name):
        raise Conflict("invoke this action through Herdr with the matching plugin and action ID")
    context = json.loads(herdr.env.get("HERDR_PLUGIN_CONTEXT_JSON", "null"))
    if not isinstance(context, dict):
        raise Conflict("action requires Herdr plugin context")
    source = context.get("invocation_source")
    if source is not None and (not isinstance(source, str) or not source):
        raise Conflict("invalid action invocation source")
    return context


def perform_action(store, herdr, name):
    context = action_context(herdr, name)
    panel = None
    if context.get("invocation_source") == "panel":
        correlation = context.get("correlation_id")
        if not isinstance(correlation, str):
            raise Conflict("面板请求缺少结果关联，请重新打开面板")
        panel = json.loads(correlation)
        if not isinstance(panel, dict) or panel.get("action") != name:
            raise Conflict("面板请求已失效，请刷新后重新选择操作")
    # Capture the event before transport calls or lock waits; never acknowledge a later head.
    event = store.head(herdr.socket) if name == "continue" else None
    if name == "continue" and panel is not None:
        head = event
        expected = panel.get("event")
        if type(expected) is not int or expected <= 0:
            raise Conflict("面板没有可确认的结果，请刷新并查看当前结果")
        event = store.one("SELECT * FROM events WHERE socket=? AND seq=?", (herdr.socket, expected))
        if event and event["user_acked_at"] is None and (not head or head["seq"] != expected):
            raise Conflict("当前结果已变化，请刷新；没有确认下一条")
    if name == "continue" and (not event or event["presented_at"] is None
                               or (event["state"] != "awaiting_user" and event["user_acked_at"] is None)):
        raise Conflict("there is no presented FIFO head to confirm")
    if name == "archive" and (panel is None or not isinstance(panel.get("dispatch"), str)
                              or not panel["dispatch"]):
        raise Conflict("请在面板中选择要归档的任务，再预览并执行")
    with control_lock(store):
        if name == "status":
            return herdr.call("plugin", "pane", "open", "--plugin", PLUGIN_ID,
                              "--entrypoint", "status")
        if name == "stop":
            if panel is not None and store.session(herdr.socket)["identity"] != panel.get("identity"):
                raise Conflict("总控已变化，请刷新后再操作")
            pause_daemon(store, herdr.socket)
            return {"paused": True, "workers_kept": True, "reports_kept": True}
        if name == "install-shortcuts":
            from .shortcuts import install_shortcuts
            return install_shortcuts(herdr)
        current = store.one("SELECT * FROM sessions WHERE socket=?", (herdr.socket,))
        if name in ("start", "switch"):
            target = current["coordinator"] if name == "start" and current else context.get("focused_pane_id")
            if not target:
                raise Conflict("select an agent pane before binding a coordinator")
            agent = herdr.agent(target)
            native = identity(agent)
            if panel is not None and native != panel.get("identity"):
                raise Conflict("选中的 Agent 身份已变化，请关闭面板后重新打开")
            bound_target = agent.get("name") or agent["pane_id"]
            if name == "switch" and agent["agent_status"] not in ("idle", "done"):
                raise Conflict("the selected coordinator must be idle before switching")
            if current and current["identity"] != native:
                if name != "switch":
                    raise Conflict("coordinator identity changed; use the explicit switch action")
                try:
                    old = herdr.agent(current["coordinator"])
                except HerdrError as error:
                    if error.code not in ("agent_not_found", "pane_not_found"):
                        raise
                    old = None
                if old and old["agent_status"] not in ("idle", "done"):
                    raise Conflict("the current coordinator is busy; wait before switching")
                pause_daemon(store, herdr.socket)
                selected = herdr.agent(target)
                if identity(selected) != native or selected["agent_status"] not in ("idle", "done"):
                    raise Conflict("selected coordinator changed while pausing; coordination remains paused")
                session = store.rebind(herdr.socket, bound_target, native)
            else:
                session = store.bind(herdr.socket, bound_target, native)
            return {"session": session, "daemon": spawn_daemon(store, herdr.socket)}
        session = store.session(herdr.socket)
        coordinator = herdr.agent(session["coordinator"])
        if identity(coordinator) != session["identity"]:
            raise Conflict("coordinator identity changed; switch explicitly")
        if panel is not None and session["identity"] != panel.get("identity"):
            raise Conflict("总控已变化，请刷新后再操作")
        if name == "focus":
            return herdr.call("agent", "focus", coordinator["pane_id"])
        if name in ("continue", "archive"):
            if context.get("focused_pane_id") != coordinator["pane_id"]:
                raise Conflict(f"{name} must be invoked in the bound coordinator pane")
            if coordinator["agent_status"] not in ("idle", "done"):
                raise Conflict("wait for the coordinator to become idle before this operation")
            if name == "archive":
                return store.archive(herdr.socket, panel["dispatch"])
            return store.ack_action(herdr.socket, event["seq"],
                                    {**context, "plugin_id": PLUGIN_ID, "action_id": name})
        raise ValueError(f"unknown action: {name}")


def require_coordinator(store, herdr):
    session = store.session(herdr.socket)
    caller = herdr.caller()
    bound = herdr.agent(session["coordinator"])
    if (identity(caller) != session["identity"] or caller["pane_id"] != bound["pane_id"]):
        raise Conflict("only the explicitly bound coordinator may run this command")
    return session


def require_worker(store, herdr, dispatch_id):
    dispatch = store.dispatch(dispatch_id)
    caller = herdr.caller()
    bound = herdr.agent(dispatch["target"])
    if (dispatch["socket"] != herdr.socket or identity(caller) != dispatch["identity"]
            or caller["pane_id"] != bound["pane_id"]):
        raise Conflict("this report does not belong to the calling worker/session")
    return dispatch


def task_spec(file):
    spec = json.loads(Path(file).read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        raise ValueError("task file must contain a JSON object")
    for field in ("goal", "scope", "authorization", "acceptance_criteria"):
        if not isinstance(spec.get(field), str) or not spec[field].strip():
            raise ValueError(f"task file requires non-empty {field}")
    return spec


def dispatch_task(store, herdr, target, spec, task_id, parent_seq=None):
    require_coordinator(store, herdr)
    worker = herdr.agent(target)
    if worker["agent_status"] not in ("idle", "done"):
        raise Conflict(f"worker is {worker['agent_status']}; inspect or choose another idle worker")
    if identity(worker) == store.session(herdr.socket)["identity"]:
        raise Conflict("the coordinator cannot dispatch business work to itself")
    target = worker.get("name") or worker["pane_id"]
    dispatch = store.create_dispatch(herdr.socket, task_id, target, identity(worker), spec, parent_seq)
    prefix = command_prefix(store, herdr.socket)
    did = dispatch["id"]
    prompt = "\n".join([
        f"[Herdr Orchestrator worker] task_id={task_id} dispatch_id={did}",
        "你是执行代理，只执行下述任务；不要接管总控，也不要扩大授权。",
        f"第一步先执行接收确认：{prefix} accept --dispatch {did}",
        "确认成功后执行任务；完成或遇到业务阻塞时，将完整回报写入绝对路径 UTF-8 文件。",
        "按任务复杂度说明结论、实际做了什么、重要具体结果或数据、关键依据与结果文件、"
        "验证方式及重要输出和验证层级、未解决项或限制及下一步。",
        "完成回报不能只有「成功/已完成」或一个路径；简单问候保留完整回复原文即可，"
        "无需凑字段或固定模板，不暴露推理过程或全部工具日志。",
        f"完成后执行：{prefix} report --dispatch {did} --message-id {did}:completed "
        "--status completed --result-file <绝对结果路径>",
        f"业务阻塞时执行：{prefix} report --dispatch {did} --message-id {did}:blocked:1 "
        "--status blocked --result-file <绝对结果路径>；后续不同阻塞使用新的 message-id。",
        "相同消息重试使用原 message-id 和原内容；不能把终端回显当作回报。",
        "工具审批 UI 卡住时可能无法执行回报，不代答未授权审批。完成后保留代理等待后续任务。",
        "任务：", json.dumps(spec, ensure_ascii=False, indent=2),
    ])
    # The terminal write and SQLite cannot share a transaction. Never replay an uncertain send.
    try:
        herdr.prompt(target, prompt)
    except HerdrError as error:
        raise HerdrError(f"dispatch {did} delivery uncertain ({error.code}); inspect receipt and "
                         f"worker before retrying: {error}", "delivery_uncertain") from error
    store.delivered(did)
    return store.dispatch(did)


def poll_once(store, herdr, now=None):
    now = time.time() if now is None else now
    session = store.session(herdr.socket)
    if not session["enabled"]:
        return "paused"
    if not herdr.plugin_enabled():
        return "plugin_disabled"
    coordinator = herdr.agent(session["coordinator"])
    if identity(coordinator) != session["identity"]:
        store.update_session(herdr.socket, enabled=0, error="coordinator identity changed; rebind explicitly")
        return "identity_changed"
    for dispatch in store.rows("""SELECT * FROM dispatches WHERE socket=?
            AND completed_at IS NULL AND abandoned_at IS NULL AND archived_at IS NULL""", (herdr.socket,)):
        try:
            worker = herdr.agent(dispatch["target"])
        except HerdrError as error:
            if error.code not in ("agent_not_found", "pane_not_found"):
                raise
            worker = None
        state = worker["agent_status"] if worker else "missing"
        if worker and identity(worker) != dispatch["identity"]:
            state = "replaced"
        if state in ("blocked", "missing", "replaced") and state != dispatch["ui_status"]:
            store.report(dispatch["id"], f"{dispatch['id']}:ui:{state}:{now}", "ui_blocked",
                         {"agent_state": state, "target": dispatch["target"],
                          "note": "Inspect agent get/read; no business outcome or approval answer is inferred."})
        with store.transaction():
            store.db.execute("UPDATE dispatches SET ui_status=? WHERE id=?", (state, dispatch["id"]))

    head = store.head(herdr.socket)
    if not session["activated"]:
        wake_key = "activation"
        prefix = command_prefix(store, herdr.socket)
        text = ("[Herdr Orchestrator] 用户已通过插件 start action 明确指定本会话为总控。\n"
                f"请读取 {ROOT / 'skills/herdr-orchestrator/SKILL.md'}，"
                f"入口命令为 {prefix}。读取后执行 {prefix} ready。"
                "只编排、派发、回读与汇总；保持已有任务与授权范围。")
    elif head and head["state"] == "awaiting_user" and head["user_acked_at"] is None:
        return "awaiting_user"
    elif head:
        followups = store.rows("""SELECT id,accepted_at,ui_status FROM dispatches
            WHERE parent_seq=? AND abandoned_at IS NULL AND archived_at IS NULL""", (head["seq"],))
        waiting = [item for item in followups if item["accepted_at"] is None]
        attention = [item for item in waiting if item["ui_status"] in ("blocked", "missing", "replaced")]
        if waiting and not attention:
            return "waiting_acceptance"
        receipts = ",".join(f"{item['id']}:{bool(item['accepted_at'])}:{item['ui_status']}"
                            for item in followups)
        wake_key = f"head:{head['seq']}:{receipts}"
        prefix = command_prefix(store, herdr.socket)
        if head["user_acked_at"] is not None:
            text = (f"[Herdr Orchestrator] FIFO 队首 #{head['seq']} 的用户确认已保存。\n"
                    f"请执行 {prefix} next 检查后续接收阻塞；只检查当前条，"
                    "不重复展示已确认的结果，不要求用户重复确认，不读取下一条。"
                    "后续全部接收后自动出队，不等待执行完成，不重新提交已有派发。")
        else:
            text = (f"[Herdr Orchestrator] FIFO 队首 #{head['seq']} 待处理。\n"
                    f"请执行 {prefix} next，只处理这一条。完整读取 worker 回报正文，按任务复杂度保留"
                    "判断和后续处理所需的具体结果、关键依据、验证输出与层级、限制和下一步。"
                    "区分 worker 自报与总控实际核验，不以摘要代替核验；长报告先展示充分内容，再提供全文入口。"
                    "简单问候展示完整回复原文即可。将准备展示的正文写入文件，先执行 present 持久登记，"
                    "再向用户展示同一份内容并等待确认；"
                    "只有当前条的实际用户确认及后续派发接收记录齐全才出队，不等待执行完成。"
                    "本回合不继续 next；下一条由轮询唤醒。已有派发不重新提交。")
        if attention:
            text += "\n后续 worker 尚未接收且需要检查：" + json.dumps(attention, ensure_ascii=False)
    else:
        return "empty"
    if coordinator["agent_status"] not in ("idle", "done"):
        return "coordinator_busy"
    if session["wake_key"] == wake_key and now - (session["wake_at"] or 0) < WAKE_RETRY_SECONDS:
        return "already_notified"
    # Herdr has no conditional idle-only prompt. Recheck, and enforce FIFO in the store.
    coordinator = herdr.agent(session["coordinator"])
    if identity(coordinator) != session["identity"] or coordinator["agent_status"] not in ("idle", "done"):
        return "coordinator_changed"
    herdr.prompt(session["coordinator"], text)
    store.update_session(herdr.socket, wake_key=wake_key, wake_at=now, error=None)
    return "notified"


def spawn_daemon(store, socket):
    session = store.session(socket)
    log = store.directory / f"session-{session['id']}.log"
    with (store.directory / f"session-{session['id']}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"running_pid": session["pid"], "log": str(log)}
    with log.open("a") as output:
        process = subprocess.Popen([sys.executable, str(ENTRY), "--state-dir", str(store.directory),
                                    "--socket", socket, "daemon"],
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                   start_new_session=True)
    deadline = time.monotonic() + DAEMON_TIMEOUT
    while store.session(socket)["pid"] != process.pid:
        if process.poll() is not None:
            store.update_session(socket, enabled=0)
            raise Conflict(f"daemon exited before acquiring its lock; inspect {log}")
        if time.monotonic() >= deadline:
            store.update_session(socket, enabled=0)
            raise TimeoutError(f"daemon startup timed out; coordination paused, inspect {log}")
        time.sleep(0.05)
    return {"started_pid": process.pid, "log": str(log)}


def run_daemon(store, herdr):
    session = store.session(herdr.socket)
    with (store.directory / f"session-{session['id']}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        store.update_session(herdr.socket, pid=os.getpid())
        try:
            while store.session(herdr.socket)["enabled"]:
                try:
                    state = poll_once(store, herdr)
                    store.update_session(herdr.socket, tick_at=time.time())
                    if state in ("paused", "plugin_disabled", "identity_changed"):
                        break
                except (HerdrError, Conflict) as error:
                    store.update_session(herdr.socket, error=str(error))
                    print(json.dumps({"time": time.time(), "error": str(error)}, ensure_ascii=False), flush=True)
                    if isinstance(error, HerdrError) and error.code == "transport_uncertain":
                        break
                time.sleep(POLL_SECONDS)
        finally:
            store.update_session(herdr.socket, pid=None)
