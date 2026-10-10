"""Herdr transport and the plugin-owned polling process."""
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import PLUGIN_ID
from .store import Conflict

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "orchestrator.py"
POLL_SECONDS = 5
WAKE_RETRY_SECONDS = 60


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


def command_prefix(store, socket):
    return shlex.join([sys.executable, str(ENTRY), "--state-dir", str(store.directory),
                       "--socket", socket])


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
        "确认成功后执行任务；完成或遇到业务阻塞时，将实际结果、证据、验证层级及未解决项写入文件。",
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
            AND completed_at IS NULL AND abandoned_at IS NULL""", (herdr.socket,)):
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
            WHERE parent_seq=? AND abandoned_at IS NULL""", (head["seq"],))
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
                    f"请执行 {prefix} next，只处理这一条。向用户展示结果后执行 present 持久等待用户；"
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
    with log.open("a") as output:
        process = subprocess.Popen([sys.executable, str(ENTRY), "--state-dir", str(store.directory),
                                    "--socket", socket, "daemon"],
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                   start_new_session=True)
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
