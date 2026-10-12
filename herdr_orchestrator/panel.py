"""Chinese task popup; reads are passive, menu requests use Herdr's native action API."""
import curses
import json
import locale
import sys
import time
import unicodedata
from datetime import datetime

from . import PLUGIN_ID, VERSION
from .runtime import HerdrError, identity
from .store import Conflict


def cells(text):
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def wrap(text, width):
    lines = []
    for paragraph in str(text).expandtabs(4).split("\n"):
        line = ""
        for char in paragraph:
            if unicodedata.category(char) == "Cc":
                char = "?"
            if line and cells(line + char) > max(2, width):
                lines.append(line)
                line = ""
            line += char
        lines.append(line)
    return lines


def clip(text, width):
    if width <= 0:
        return ""
    result = ""
    for char in text:
        if unicodedata.category(char) == "Cc":
            char = " "
        if cells(result + char) > width:
            break
        result += char
    return result


def timestamp(value):
    return datetime.fromtimestamp(value).strftime("%m-%d %H:%M") if value else "尚无记录"


def snapshot(store, socket):
    session = store.one("SELECT rowid AS id,* FROM sessions WHERE socket=?", (socket,))
    return {"session": session, "head": store.head(socket),
            "tasks": store.rows("SELECT * FROM dispatches WHERE socket=? ORDER BY created_at DESC", (socket,)),
            "events": store.rows("SELECT * FROM events WHERE socket=? ORDER BY seq", (socket,))}


def goal(task):
    return json.loads(task["spec"])["goal"]


def agent_label(agent):
    return (agent.get("name") or agent.get("terminal_title_stripped")
            or f"{agent['agent'].capitalize()} · {agent['pane_id']}")


def task_label(task, data):
    fallback = task["target"] if ":" not in task["target"] else json.loads(task["identity"])["agent"].capitalize() + " Agent"
    return data.get("agent_labels", {}).get(task["target"], fallback)


def task_state(task, events):
    reports = [item for item in events if item["dispatch_id"] == task["id"]]
    if task["abandoned_at"]:
        return "已撤销"
    if reports:
        latest = reports[-1]
        if latest["state"] == "awaiting_user":
            return "已确认，等待后续接收" if latest["user_acked_at"] else "等待用户处理"
        if latest["state"] == "handling":
            return "总控正在处理回报"
        if latest["state"] == "pending":
            return "回报排队中"
        if task["completed_at"]:
            return "回报已处理"
    if task["ui_status"] in ("blocked", "missing", "replaced"):
        return {"blocked": "等待审批或输入", "missing": "Agent 不在当前会话", "replaced": "Agent 身份已变化"}[task["ui_status"]]
    if not task["accepted_at"]:
        return "等待 Agent 接收"
    return "正在执行" if task["ui_status"] == "working" else "已接收，等待回报"


def overview(data):
    session, head = data["session"], data["head"]
    if not session:
        lines = ["尚未绑定总控：在一个空闲 Agent 窗格打开面板，按 m 选择启动。"]
    else:
        state = "自动提醒已暂停" if not session["enabled"] else "自动提醒已开启"
        if session["enabled"] and not session["activated"]:
            state += " · 等待总控准备就绪"
        fallback = session["coordinator"] if ":" not in session["coordinator"] else json.loads(session["identity"])["agent"].capitalize() + " 总控"
        label = data.get("coordinator_label") or fallback
        lines = [f"总控：{label}", "状态：" + state,
                 f"最近检查：{timestamp(session['tick_at'])}"]
        if session["error"]:
            lines.append("自动提醒异常：" + session["error"] + "；查看详情和插件日志，按已有授权处理。")
    if not head:
        lines.append("当前没有等待的结果；Agent 可以继续工作。")
    else:
        task = next(item for item in data["tasks"] if item["id"] == head["dispatch_id"])
        state = {"pending": "等待总控展示", "handling": "总控正在准备展示",
                 "awaiting_user": "已确认，等待后续 Agent 接收" if head["user_acked_at"] else "等待你处理"}[head["state"]]
        lines.extend(["当前结果：" + goal(task), state + "；Enter 可查看全文，查看本身不会确认。"])
    return lines


def task_detail(task, data):
    spec = json.loads(task["spec"])
    lines = ["任务目标", spec["goal"], "", "执行 Agent：" + task_label(task, data),
             "状态：" + task_state(task, data["events"]), "派发时间：" + timestamp(task["created_at"])]
    for key, label in (("scope", "范围"), ("authorization", "授权"), ("acceptance_criteria", "完成标准")):
        lines.extend(["", label, spec[key]])
    for event in data["events"]:
        if event["dispatch_id"] != task["id"]:
            continue
        payload = json.loads(event["payload"])
        lines.extend(["", "Agent 回报（面板未自动核验业务结果） · " + timestamp(event["created_at"]),
                      payload.get("result", payload.get("note", "没有正文"))])
        if payload.get("result_file"):
            lines.append("原始文件：" + payload["result_file"])
        if event["summary"]:
            lines.extend(["", "总控已登记的展示正文", event["summary"]])
        if event["user_acked_at"]:
            source = "实际消息" if event["user_ack_source"] == "message" else "插件操作" if event["user_ack_source"] == "plugin_action" else "历史来源未记录"
            lines.append("处理完成确认：" + source + " · " + timestamp(event["user_acked_at"]))
            if event["user_reply"]:
                lines.append(event["user_reply"])
    if task["abandon_reason"]:
        lines.extend(["", "撤销原因", task["abandon_reason"]])
    return lines


def readable_error(message):
    translations = {
        "continue must be invoked": "请在已绑定总控的窗格打开面板；定位后关闭并重新打开。",
        "wait for the coordinator": "总控正在忙，请等它空闲后再确认。",
        "current coordinator is busy": "原总控正在忙，不能中途更换。",
        "selected coordinator must be idle": "候选 Agent 尚未空闲，请稍后重开面板。",
        "no presented FIFO head": "没有已展示、等待用户处理的结果，不能跳过队列。",
        "selected coordinator changed": "等待期间候选 Agent 已变化，自动提醒保持暂停。",
        "agent has no native session": "Agent 尚无原生会话身份，请检查 integration 和第一条消息。",
    }
    return next((text for key, text in translations.items() if key in message), "操作未完成：" + message)


class Panel:
    def __init__(self, store, herdr):
        self.store, self.herdr = store, herdr
        self.context = json.loads(herdr.env.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
        if not isinstance(self.context, dict):
            raise Conflict("面板上下文无效，请通过 Herdr 重新打开")
        self.data = snapshot(store, herdr.socket)
        self.candidate = None
        self.message = "仅查看不会确认结果；按 m 打开操作菜单。"
        pane = self.context.get("focused_pane_id")
        if pane:
            try:
                self.candidate = herdr.agent(pane)
                identity(self.candidate)
            except (HerdrError, Conflict) as error:
                self.candidate = None
                self.message = "当前窗格不能作为总控：" + str(error)
        self.view, self.selected, self.offset = "home", 0, 0
        if self.data["head"]:
            self.selected = next(i for i, task in enumerate(self.data["tasks"])
                                 if task["id"] == self.data["head"]["dispatch_id"])
        self.confirm = None
        self.pending = None
        self.refresh()

    def refresh(self):
        selected = self.data["tasks"][self.selected]["id"] if self.data["tasks"] else None
        self.data = snapshot(self.store, self.herdr.socket)
        self.selected = next((i for i, item in enumerate(self.data["tasks"]) if item["id"] == selected), 0)
        try:
            agents = self.herdr.call("agent", "list")["agents"]
            self.data["agent_labels"] = {key: agent_label(agent) for agent in agents
                                         for key in (agent.get("name"), agent["pane_id"]) if key}
            if self.data["session"]:
                target = self.data["session"]["coordinator"]
                self.data["coordinator_label"] = self.data["agent_labels"].get(target)
        except HerdrError as error:
            self.message = "Agent 状态查询失败：" + str(error)

    def prepare(self, name):
        session = self.data["session"]
        head = self.data["head"]
        target = self.candidate if name == "switch" or (name == "start" and not session) else session
        if not target:
            raise Conflict("没有明确目标；请先在空闲 Agent 窗格打开面板")
        expected = {"action": name, "identity": identity(target) if target is self.candidate else target["identity"]}
        label = agent_label(target) if target is self.candidate else self.data.get("coordinator_label", target["coordinator"])
        descriptions = {
            "start": f"启动 / 恢复总控：{label}\n已有绑定只恢复原总控；未绑定时使用打开面板的 Agent。",
            "focus": f"定位总控：{label}\n仅定位，成功后关闭面板；需重新打开才能在新窗格确认结果。",
            "switch": f"更换总控为：{label}\n原总控和候选均须空闲；保留任务、回报和等待状态。",
            "stop": f"暂停自动提醒：{label}\n停止自动提醒，正在工作的 Agent 继续工作，结果仍可回报。",
            "continue": "当前结果已处理完\n只确认这一条已处理完；后续 Agent 接收齐全后再提醒下一条。",
        }
        lines = [descriptions[name]]
        if name == "continue":
            if not head or head["state"] != "awaiting_user" or head["presented_at"] is None:
                raise Conflict("没有已展示、等待用户处理的结果；不能跳过当前队首")
            expected["event"] = head["seq"]
            task = next(item for item in self.data["tasks"] if item["id"] == head["dispatch_id"])
            lines.extend(["", "结果目标：" + goal(task), head["summary"]])
        self.confirm = (name, expected, lines)
        self.view, self.offset = "confirm", 0

    def submit(self):
        if (self.herdr.env.get("HERDR_PLUGIN_ID") != PLUGIN_ID
                or self.herdr.env.get("HERDR_PLUGIN_ENTRYPOINT_ID") != "status"):
            raise Conflict("操作需从 Herdr 的总控任务面板入口发起；直接 CLI board 只可查看")
        name, expected, _ = self.confirm
        context = dict(self.context, invocation_source="panel", correlation_id=json.dumps(expected))
        receipt = self.herdr.invoke_action(name, context)
        self.pending = (receipt["log"]["log_id"], name, time.monotonic())
        self.message = "请求已提交，等待动作日志完成；不会自动重试。"
        self.view, self.offset = "home", 0

    def check_action(self):
        log_id, name, started = self.pending
        logs = self.herdr.call("plugin", "log", "list", "--plugin", PLUGIN_ID, "--limit", "100")["logs"]
        log = next((item for item in logs if item["log_id"] == log_id), None)
        if log and log["status"] != "running":
            self.pending = None
            if log.get("exit_code") != 0:
                raw = log["stderr"] or log["stdout"]
                try:
                    error = json.loads(raw).get("error", raw)
                except json.JSONDecodeError:
                    error = raw
                self.message = readable_error(str(error))
                return False
            self.message = {"start": "已启动 / 恢复，等待总控准备就绪。", "switch": "已更换总控，等待总控准备就绪。",
                            "stop": "已暂停自动提醒，Agent 工作继续。", "continue": "这条已记为处理完成；接收齐全后再提醒下一条。",
                            "focus": "已定位总控，请重新打开面板。"}[name]
            if self.view == "home":
                self.refresh()
            return name == "focus"
        if time.monotonic() - started > 45:
            self.message = "动作尚未结束；查看插件日志，不重复提交。可退出面板。"
        return False

    def key(self, key):
        if key in (ord("q"), 27):
            if self.view == "home":
                return True
            self.view, self.offset = "home", 0
        elif key == ord("r"):
            self.refresh()
            self.view, self.offset = "home", 0
            self.message = "已刷新，仅查看；没有确认结果。"
        elif key == ord("m") and self.view == "home":
            self.view, self.offset = "menu", 0
        elif self.view == "menu" and key in map(ord, "12345"):
            if self.pending:
                raise Conflict("已有动作等待完成，请先查看日志")
            name = ("start", "focus", "switch", "stop", "continue")[key - ord("1")]
            self.prepare(name)
            if name in ("start", "focus", "stop"):
                self.submit()
        elif self.view == "confirm" and key == ord("y"):
            if self.pending:
                raise Conflict("已有动作等待完成，请先查看日志")
            self.submit()
        elif self.view == "home" and key in (10, 13, curses.KEY_ENTER):
            if self.data["tasks"]:
                self.view, self.offset = "detail", 0
        elif key in (curses.KEY_UP, ord("k"), curses.KEY_DOWN, ord("j"), curses.KEY_PPAGE, curses.KEY_NPAGE):
            direction = -1 if key in (curses.KEY_UP, ord("k"), curses.KEY_PPAGE) else 1
            if self.view == "home":
                self.selected = min(max(0, self.selected + direction), max(0, len(self.data["tasks"]) - 1))
            else:
                self.offset = max(0, self.offset + direction * (8 if key in (curses.KEY_PPAGE, curses.KEY_NPAGE) else 1))
        return False

    def lines(self):
        if self.view == "menu":
            candidate = agent_label(self.candidate) if self.candidate else "没有可用的 Agent"
            coordinator = self.data.get("coordinator_label") or (self.data["session"] or {}).get("coordinator") or candidate
            return ["操作菜单（数字明确选择；更换 / 确认先预览）", "此页对象已固定；r 返回主列表刷新，Esc 返回。", "", "1 启动 / 恢复：" + coordinator,
                    "  已有绑定只恢复原总控", "2 定位总控：" + coordinator, "  关闭面板后在总控处重新打开", "3 更换总控为打开面板的 Agent：" + candidate,
                    "  新旧总控均须空闲，任务与等待状态保留", "4 暂停自动提醒：Agent 工作继续，不丢回报",
                    "5 当前结果已处理完：仅确认面板显示的这一条"]
        if self.view == "confirm":
            return self.confirm[2] + ["", "按 y 明确执行；Esc / q 返回。Enter 不执行。"]
        if self.view == "detail":
            return task_detail(self.data["tasks"][self.selected], self.data)
        lines = []
        if not self.data["tasks"]:
            lines.append("暂无任务；向总控发送任务，由 worker 接收和回报。")
        for number, task in enumerate(self.data["tasks"]):
            lines.extend([("> " if number == self.selected else "  ") + goal(task).replace("\n", " "),
                          f"  {task_state(task, self.data['events'])} | {task_label(task, self.data)} | {timestamp(task['created_at'])}"])
        return lines

    def draw(self, screen):
        height, width = screen.getmaxyx()
        screen.erase()
        if height < 6 or width < 12:
            if width > 1 and height > 1:
                screen.addstr(0, 0, clip("请放大窗口", width - 1))
            screen.refresh()
            return
        header = [f"总控任务面板 · {VERSION}"]
        if self.view == "home":
            header += overview(self.data)
            if width < 48:
                header = [line for line in header if not line.startswith("最近检查")]
            header.append(f"── 任务列表（{len(self.data['tasks'])} 项） " + "─" * width)
        header = header[:max(1, height - 5)]
        body = []
        for line in self.lines():
            # Task rows remain compact; detail and confirmation views contain the full text.
            body.extend([clip(line, max(1, width - 1))] if self.view == "home" else wrap(line, width - 1))
        available = max(1, height - len(header) - 3)
        if self.view == "home" and self.data["tasks"]:
            selected_line = self.selected * 2
            if selected_line < self.offset:
                self.offset = selected_line
            elif selected_line + 1 >= self.offset + available:
                self.offset = selected_line + 2 - available
        self.offset = min(self.offset, max(0, len(body) - available))
        rows = header + body[self.offset:self.offset + available]
        for number, line in enumerate(rows):
            if number < height - 3:
                attribute = (curses.A_REVERSE if line.startswith("> ") else curses.A_BOLD
                             if number == 0 or line.startswith(("当前结果", "等待", "任务列表")) else 0)
                screen.addstr(number, 0, clip(line, max(1, width - 1)), attribute)
        footer = {"home": "q退出 Enter详情 m操作 r刷新", "detail": "q/Esc返回 r刷新 ↑↓/PgUpDn滚动",
                  "menu": "q/Esc返回 数字选择 r刷新", "confirm": "q/Esc取消 y执行 r刷新"}[self.view]
        if height >= 3 and width >= 2:
            screen.addstr(height - 3, 0, clip(self.message, width - 1), curses.A_BOLD)
            screen.addstr(height - 2, 0, clip(footer, width - 1), curses.A_REVERSE)
            screen.addstr(height - 1, 0, clip(f"↑↓滚动 · {self.offset + 1}/{max(1, len(body))} 行 · 查看不确认", width - 1))
        screen.refresh()

    def run(self, screen):
        screen.keypad(True)
        screen.timeout(200)
        next_check = 0
        next_refresh = time.monotonic() + 2
        while True:
            self.draw(screen)
            key = screen.getch()
            try:
                if self.key(key):
                    return
                if self.view == "home" and time.monotonic() >= next_refresh:
                    self.refresh()
                    next_refresh = time.monotonic() + 2
                if self.pending and time.monotonic() >= next_check:
                    next_check = time.monotonic() + 1
                    if self.check_action():
                        return
            except (HerdrError, Conflict, OSError, ValueError) as error:
                self.message = readable_error(str(error))


def board(store, herdr):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        data = snapshot(store, herdr.socket)
        print(f"总控任务面板 · {VERSION}")
        print("\n".join(overview(data)))
        for task in data["tasks"]:
            print(f"目标：{goal(task)}\nAgent：{task_label(task, data)} | {task_state(task, data['events'])} | {timestamp(task['created_at'])}")
        print("静态只读视图；请在 Herdr 打开面板操作。")
        return
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(Panel(store, herdr).run)
