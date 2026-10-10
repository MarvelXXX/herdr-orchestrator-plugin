import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from . import PLUGIN_ID, VERSION
from .runtime import (Herdr, HerdrError, dispatch_task, identity, require_coordinator,
                      require_worker, run_daemon, spawn_daemon, task_spec, control_lock,
                      pause_daemon, perform_action)
from .store import Conflict, Store, new_id


def parser():
    result = argparse.ArgumentParser(description="Herdr Orchestrator plugin runtime")
    result.add_argument("--version", action="version", version=VERSION)
    result.add_argument("--state-dir", default=os.environ.get("HERDR_PLUGIN_STATE_DIR"))
    result.add_argument("--socket", default=os.environ.get("HERDR_SOCKET_PATH"))
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("check-runtime", help="check plugin prerequisites at installation")
    commands.add_parser("startup", help="resume a previously enabled coordinator")
    commands.add_parser("daemon", help="internal polling process")
    commands.add_parser("status", help="read task and FIFO state as JSON")
    commands.add_parser("board", help="read-only status popup")
    commands.add_parser("stop", help="pause wake-ups, keeping workers and queued reports")
    bind = commands.add_parser("bind", help="explicitly bind a coordinator")
    bind.add_argument("--target", required=True)
    bind.add_argument("--rebind", action="store_true", help="replace a paused coordinator binding")
    action = commands.add_parser("action", help="Herdr action entrypoint")
    action.add_argument("name", choices=("start", "switch", "focus", "continue", "stop", "status",
                                       "install-shortcuts"))
    commands.add_parser("ready", help="coordinator confirms reading the bundled skill")
    commands.add_parser("next", help="read or resume the current FIFO head")
    dispatch = commands.add_parser("dispatch", help="submit a task to an existing idle worker")
    dispatch.add_argument("--target", required=True)
    dispatch.add_argument("--task-file", required=True)
    dispatch.add_argument("--task-id", help="reuse the task ID for supplements and follow-ups")
    dispatch.add_argument("--parent-event", type=int, help="FIFO head that caused this follow-up")
    accept = commands.add_parser("accept", help="worker explicitly accepts a dispatch")
    accept.add_argument("--dispatch", required=True)
    wait = commands.add_parser("wait-accepted", help="bounded receipt wait; does not wait for completion")
    wait.add_argument("--dispatch", required=True)
    wait.add_argument("--timeout", type=float, default=10, help="seconds, at most 60")
    report = commands.add_parser("report", help="worker appends a durable completion/blocker")
    report.add_argument("--dispatch", required=True)
    report.add_argument("--message-id", required=True, help="stable ID to reuse for the same message retry")
    report.add_argument("--status", choices=("completed", "blocked"), required=True)
    report.add_argument("--result-file", required=True)
    present = commands.add_parser("present", help="persist the displayed head summary and wait for the user")
    present.add_argument("--event", type=int, required=True)
    present.add_argument("--summary-file", required=True)
    ack = commands.add_parser("ack", help="record actual user confirmation; dequeue after follow-up receipts")
    ack.add_argument("--event", type=int, required=True)
    ack.add_argument("--reply-file", required=True, help="absolute UTF-8 file containing the user's exact reply")
    abandon = commands.add_parser("abandon", help="revoke an inspected attempt; does not stop its agent")
    abandon.add_argument("--dispatch", required=True)
    abandon.add_argument("--reason", required=True)
    return result


def state_directory(args):
    if not args.state_dir:
        raise Conflict("use the Herdr plugin action, or copy its command with --state-dir")
    return Path(args.state_dir).expanduser().resolve()


def board(store, socket):
    while True:
        data = store.status(socket)
        session = data["session"]
        if sys.stdout.isatty():
            print("\033[2J\033[H", end="")
        print(f"Orchestrator {VERSION} | {'运行中' if session['enabled'] else '已暂停'}")
        print(f"总控: {session['coordinator']} | 待处理: {len(data['pending'])}")
        print(f"后台 PID: {session['pid']} | 最近轮询: {session['tick_at']}")
        if session["error"]:
            print("错误:", session["error"])
        print("\n任务 / 派发 / 代理 / 接收确认 / 状态")
        for dispatch in data["dispatches"]:
            state = ("已撤销" if dispatch["abandoned_at"] else
                     "已回报完成" if dispatch["completed_at"] else dispatch["delivery"])
            print(dispatch["task_id"], dispatch["id"], dispatch["target"],
                  "已接收" if dispatch["accepted_at"] else "待接收", state,
                  dispatch["ui_status"] or "", sep=" | ")
        print("\nFIFO 序号 / 类型 / 处理状态 / 回报位置")
        for event in data["pending"]:
            payload = json.loads(event["payload"])
            print(event["seq"], event["kind"], event["state"],
                  payload.get("result_file", payload.get("note", "")), sep=" | ")
        print("\n关闭此窗口只关闭状态视图；暂停请使用插件 stop action。", flush=True)
        if not sys.stdout.isatty():
            return
        time.sleep(2)


def execute(args):
    if args.command == "check-runtime":
        if sys.version_info < (3, 11):
            raise Conflict("Python 3.11 or newer is required")
        if sys.platform not in ("linux", "darwin"):
            raise Conflict("this release supports Linux and macOS")
        return {"version": VERSION, "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version}

    directory = state_directory(args)
    if not args.socket:
        raise Conflict("HERDR_SOCKET_PATH or --socket is required")
    if args.command == "startup" and not (directory / "state.sqlite3").exists():
        return {"resumed": False, "reason": "no binding yet"}
    store = Store(directory)
    try:
        herdr = Herdr(args.socket)
        command = args.command
        if command == "action":
            return perform_action(store, herdr, args.name)

        if command == "bind":
            agent = herdr.agent(args.target)
            native = identity(agent)
            with control_lock(store):
                if args.rebind:
                    if store.session(args.socket)["enabled"]:
                        raise Conflict("pause coordination before rebinding")
                    pause_daemon(store, args.socket)
                operation = store.rebind if args.rebind else store.bind
                session = operation(args.socket, agent.get("name") or agent["pane_id"], native)
                return {"session": session, "daemon": spawn_daemon(store, args.socket)}
        if command == "startup":
            with control_lock(store):
                registered = store.one("SELECT * FROM sessions WHERE socket=?", (args.socket,))
                if registered and registered["enabled"]:
                    return spawn_daemon(store, args.socket)
                return {"resumed": False}
        if command == "daemon":
            run_daemon(store, herdr)
            return None
        if command == "stop":
            with control_lock(store):
                pause_daemon(store, args.socket)
            return {"paused": True, "workers_kept": True, "reports_kept": True}
        if command == "status":
            return store.status(args.socket)
        if command == "board":
            board(store, args.socket)
            return None
        if command == "accept":
            require_worker(store, herdr, args.dispatch)
            return store.accept(args.dispatch)
        if command == "report":
            require_worker(store, herdr, args.dispatch)
            path = Path(args.result_file).expanduser()
            if not path.is_absolute() or not path.is_file():
                raise Conflict("result-file must be an existing absolute file path")
            return store.report(args.dispatch, args.message_id, args.status,
                                {"result_file": str(path.resolve()),
                                 "result": path.read_text(encoding="utf-8")})

        require_coordinator(store, herdr)
        if command == "ready":
            store.acknowledge_activation(args.socket)
            return {"ready": True}
        if command == "next":
            event = store.next(args.socket)
            if event:
                event["payload"] = json.loads(event["payload"])
                event["dispatch"] = store.dispatch(event["dispatch_id"])
                event["followups"] = store.rows("SELECT * FROM dispatches WHERE parent_seq=?", (event["seq"],))
            return event
        if command == "dispatch":
            return dispatch_task(store, herdr, args.target, task_spec(args.task_file),
                                 args.task_id or new_id("task"), args.parent_event)
        if command == "wait-accepted":
            if not 0 < args.timeout <= 60:
                raise ValueError("timeout must be greater than zero and at most 60 seconds")
            dispatch = store.dispatch(args.dispatch)
            if dispatch["socket"] != args.socket:
                raise Conflict("dispatch belongs to a different Herdr session")
            deadline = time.monotonic() + args.timeout
            while True:
                dispatch = store.dispatch(args.dispatch)
                if dispatch["accepted_at"]:
                    return {"accepted": True, "dispatch": dispatch}
                if dispatch["abandoned_at"]:
                    raise Conflict("dispatch was abandoned")
                if time.monotonic() >= deadline:
                    raise TimeoutError("no explicit worker receipt yet; inspect before retrying dispatch")
                time.sleep(min(0.2, deadline - time.monotonic()))
        if command in ("present", "ack"):
            path = Path(args.summary_file if command == "present" else args.reply_file).expanduser()
            if not path.is_absolute() or not path.is_file():
                raise Conflict(f"{command} requires an existing absolute UTF-8 file path")
            content = path.read_text(encoding="utf-8")
            if command == "present":
                return store.present(args.socket, args.event, content)
            return store.ack(args.socket, args.event, content, str(path.resolve()))
        if command == "abandon":
            dispatch = store.dispatch(args.dispatch)
            if dispatch["socket"] != args.socket:
                raise Conflict("dispatch belongs to a different Herdr session")
            return store.abandon(args.dispatch, args.reason)
        raise ValueError(f"unknown command: {command}")
    finally:
        store.close()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        data = execute(args)
        if data is not None:
            print(json.dumps({"ok": True, "data": data}, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        return 130
    except (Conflict, HerdrError, OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"ok": False, "error": str(error),
                          "code": getattr(error, "code", type(error).__name__)}, ensure_ascii=False),
              file=sys.stderr)
        return 4 if isinstance(error, TimeoutError) else 1
