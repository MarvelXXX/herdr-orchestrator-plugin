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


def snapshot(store, socket, show_history=False):
    session = store.one("SELECT rowid AS id,* FROM sessions WHERE socket=?", (socket,))
    head = store.head(socket)
    events = store.rows("SELECT * FROM events WHERE socket=? ORDER BY seq", (socket,))
    tasks = store.rows("SELECT * FROM dispatches WHERE socket=? ORDER BY created_at DESC", (socket,))
    unsettled = {event["dispatch_id"] for event in events if event["state"] != "done"}
    active = [task for task in tasks if (head and task["id"] == head["dispatch_id"])
              or (task["archived_at"] is None and (task["id"] in unsettled
                  or (task["completed_at"] is None and task["abandoned_at"] is None)))]
    return {"session": session, "head": head,
            "tasks": tasks if show_history else active, "events": events,
            "history_count": len(tasks) - len(active)}


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
    if task["archived_at"] is not None:
        return "已归档"
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
            lines.append("自动提醒异常：" + readable_error(session["error"]))
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
    if task["archived_at"] is not None:
        lines.append("归档时间：" + timestamp(task["archived_at"]))
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
        "coordinator identity changed": "总控已换会话。回到总控，重开后按 r，再选操作 3。",
        "选中的 Agent 身份已变化": "候选 Agent 已换会话；按 r 刷新，再选择菜单 3 核对新目标。",
        "continue must be invoked": "请在已绑定总控的窗格打开面板；定位后关闭并重新打开。",
        "wait for the coordinator": "总控正在忙，请等它空闲后再操作。",
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
        self.show_history = False
        self.data = snapshot(store, herdr.socket)
        self.candidate = None
        self.message = "仅查看不会确认结果；按 m 打开操作菜单。"
        self.view, self.selected, self.offset = "home", 0, 0
        if self.data["head"]:
            self.selected = next(i for i, task in enumerate(self.data["tasks"])
                                 if task["id"] == self.data["head"]["dispatch_id"])
        self.confirm = None
        self.pending = None
        self.hitboxes = []
        self.frame = None
        self.mouse_down = None
        self.refresh()

    def refresh(self):
        selected = self.data["tasks"][self.selected]["id"] if self.data["tasks"] else None
        self.data = snapshot(self.store, self.herdr.socket, self.show_history)
        fallback = self.data["head"]["dispatch_id"] if self.data["head"] else None
        self.selected = next((i for i, item in enumerate(self.data["tasks"]) if item["id"] == selected), None)
        if self.selected is None:
            self.selected = next((i for i, item in enumerate(self.data["tasks"]) if item["id"] == fallback), 0)
        try:
            agents = self.herdr.call("agent", "list")["agents"]
            self.candidate = next((agent for agent in agents
                                   if agent["pane_id"] == self.context.get("focused_pane_id")), None)
            if self.candidate:
                try:
                    identity(self.candidate)
                except Conflict:
                    self.candidate = None
                    self.message = "当前 Agent 尚无会话身份；先发送一条普通消息，再按 r 刷新。"
            self.data["agent_labels"] = {key: agent_label(agent) for agent in agents
                                         for key in (agent.get("name"), agent["pane_id"]) if key}
            if self.data["session"]:
                target = self.data["session"]["coordinator"]
                self.data["coordinator_label"] = self.data["agent_labels"].get(target)
        except HerdrError as error:
            self.candidate = None
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
            "switch": f"更换 / 重新指定总控为：{label}\n原总控和候选均须空闲；保留任务、回报和等待状态。",
            "stop": f"暂停自动提醒：{label}\n停止自动提醒，正在工作的 Agent 继续工作，结果仍可回报。",
            "continue": "当前结果已处理完\n只确认这一条已处理完；后续 Agent 接收齐全后再提醒下一条。",
            "archive": "归档所选任务\n插件脚本直接执行，无需总控回复或空闲。\n退出待处理列表和提醒队列，保留历史；Agent 工作继续。\n只归档所选任务，其他后续任务保留；归档不代表业务完成。",
        }
        lines = [descriptions[name]]
        if name == "continue":
            if not head or head["state"] != "awaiting_user" or head["presented_at"] is None:
                raise Conflict("没有已展示、等待用户处理的结果；不能跳过当前队首")
            expected["event"] = head["seq"]
            task = next(item for item in self.data["tasks"] if item["id"] == head["dispatch_id"])
            lines.extend(["", "结果目标：" + goal(task), head["summary"]])
        if name == "archive":
            if not self.data["tasks"]:
                raise Conflict("没有可归档的任务")
            task = self.data["tasks"][self.selected]
            expected["dispatch"] = task["id"]
            lines.extend(["", "任务目标：" + goal(task), "执行 Agent：" + task_label(task, self.data),
                          "当前状态：" + task_state(task, self.data["events"])])
        self.confirm = (name, expected, lines)
        self.view, self.offset = "confirm", 0
        self.message = "请核对上方目标；取消不会修改任务，确认才执行。"

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
                self.message = "操作失败：" + readable_error(str(error))
                return False
            self.message = {"start": "已启动 / 恢复，等待总控准备就绪。", "switch": "已更换总控，等待总控准备就绪。",
                            "stop": "已暂停自动提醒，Agent 工作继续。", "continue": "这条已记为处理完成；接收齐全后再提醒下一条。",
                            "focus": "已定位总控，请重新打开面板。", "archive": "任务已归档，历史保留；Agent 工作继续。"}[name]
            if self.view == "home":
                self.refresh()
            return name == "focus"
        if time.monotonic() - started > 45:
            self.message = "动作尚未结束；查看插件日志，不重复提交。可退出面板。"
        return False

    def key(self, key):
        if key == curses.KEY_MOUSE:
            _, x, y, _, buttons = curses.getmouse()
            return self.mouse(x, y, buttons)
        if key == curses.KEY_RESIZE:
            self.hitboxes = []
            self.frame = None
            self.mouse_down = None
        elif key in (ord("q"), 27):
            if self.view == "home":
                return True
            self.view, self.offset = "home", 0
        elif key == ord("r"):
            self.refresh()
            self.view, self.offset = "home", 0
            self.message = "已刷新，仅查看；没有确认结果。"
        elif key == ord("m") and self.view == "home":
            self.view, self.offset = "menu", 0
        elif key == ord("h") and self.view == "home":
            self.show_history = not self.show_history
            self.refresh()
            self.offset = 0
            self.message = "显示全部历史；查看不会确认结果。" if self.show_history else "仅显示待处理任务；历史记录保留。"
        elif key == ord("f") and self.view == "home":
            if self.pending:
                raise Conflict("已有动作等待完成，请先查看日志")
            self.prepare("focus")
            self.submit()
        elif self.view == "menu" and key in map(ord, "123456"):
            if self.pending:
                raise Conflict("已有动作等待完成，请先查看日志")
            name = ("start", "focus", "switch", "stop", "continue", "archive")[key - ord("1")]
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

    def mouse(self, x, y, buttons):
        if not self.frame or self.frame[0] != self.view:
            self.mouse_down = None
            return False
        _, height, width = self.frame
        if not (0 <= x < width and 0 <= y < height):
            self.mouse_down = None
            return False
        if buttons & getattr(curses, "REPORT_MOUSE_POSITION", 0):
            self.mouse_down = None
            return False
        if buttons & (curses.BUTTON4_PRESSED | getattr(curses, "BUTTON5_PRESSED", 0)):
            self.mouse_down = None
            key = curses.KEY_UP if buttons & curses.BUTTON4_PRESSED else curses.KEY_DOWN
            for _ in range(3):
                self.key(key)
            return False
        target = next((target for row, left, right, target in self.hitboxes
                       if row == y and left <= x < right), None)
        if buttons & curses.BUTTON1_PRESSED:
            self.mouse_down = (self.frame, target, x, y)
            return False
        if buttons & curses.BUTTON1_RELEASED:
            pressed = self.mouse_down
            self.mouse_down = None
            if pressed != (self.frame, target, x, y):
                return False
        elif not buttons & (curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED):
            return False
        self.mouse_down = None
        if target is None:
            return False
        kind, value = target
        if kind == "task":
            selected = next((i for i, task in enumerate(self.data["tasks"]) if task["id"] == value), None)
            if selected is None:
                raise Conflict("任务列表已变化，请刷新后重新选择")
            self.selected = selected
            if buttons & curses.BUTTON1_DOUBLE_CLICKED:
                self.key(10)
            return False
        if buttons & curses.BUTTON1_DOUBLE_CLICKED:
            # Do not activate a button on the page opened by the first click.
            return False
        return self.key(value)

    def lines(self):
        if self.view == "menu":
            candidate = agent_label(self.candidate) if self.candidate else "没有可用的 Agent"
            coordinator = self.data.get("coordinator_label") or (self.data["session"] or {}).get("coordinator") or candidate
            selected = goal(self.data["tasks"][self.selected]) if self.data["tasks"] else "没有可归档的任务"
            return ["1 启动 / 恢复：" + coordinator,
                    "  已有绑定只恢复原总控", "2 定位总控：" + coordinator, "  关闭面板后在总控处重新打开", "3 更换 / 重新指定总控为当前窗格的 Agent：" + candidate,
                    "  新旧总控均须空闲，任务与等待状态保留", "4 暂停自动提醒：Agent 工作继续，不丢回报",
                    "5 当前结果已处理完：" + ("仅确认面板显示的这一条" if self.data["head"] and self.data["head"]["state"] == "awaiting_user" else "暂无已展示的结果"),
                    "6 归档所选任务：" + selected, "  脚本直接执行，无需总控空闲；保留历史，Agent 继续"]
        if self.view == "confirm":
            return self.confirm[2] + ["", "按 y 明确执行；Esc / q 返回。Enter 不执行。"]
        if self.view == "detail":
            return task_detail(self.data["tasks"][self.selected], self.data)
        lines = []
        if not self.data["tasks"]:
            lines.append("暂无待处理任务；按 h 查看已处理记录。" if self.data["history_count"]
                         else "暂无任务；向总控发送任务，由 worker 接收和回报。")
        for number, task in enumerate(self.data["tasks"]):
            lines.extend([("> " if number == self.selected else "  ") + goal(task).replace("\n", " "),
                          f"  {task_state(task, self.data['events'])} | {task_label(task, self.data)} | {timestamp(task['created_at'])}"])
        return lines

    def toolbar(self, width):
        if self.view == "home":
            buttons = [("回到总控 f", ord("f")), ("详情 Enter", 10), ("操作 m", ord("m")),
                       ("待处理 h" if self.show_history else "历史 h", ord("h")),
                       ("刷新 r", ord("r")), ("退出 q", ord("q"))]
        elif self.view == "confirm":
            label = {"archive": "确认归档", "continue": "确认处理完", "switch": "确认切换"}[self.confirm[0]]
            buttons = [(label + " y", ord("y")), ("取消 Esc", 27), ("刷新 r", ord("r"))]
        else:
            buttons = [("返回 q", ord("q")), ("刷新 r", ord("r"))]
        row, x = 0, 0
        result = []
        for label, key in buttons:
            text = clip("[ " + label + " ]", width - 1)
            if x and x + cells(text) >= width:
                row, x = row + 1, 0
            result.append((row, x, text, key))
            x += cells(text) + 1
        return result

    def draw(self, screen):
        height, width = screen.getmaxyx()
        frame = (self.view, height, width)
        if self.frame != frame:
            self.mouse_down = None
        self.frame = frame
        self.hitboxes = []
        screen.erase()
        toolbar = self.toolbar(width) if width > 1 else []
        footer_rows = toolbar[-1][0] + 3 if toolbar else 3
        if height < footer_rows + 2 or width < 12:
            if width > 1 and height > 1:
                screen.addstr(0, 0, clip("请放大窗口", width - 1))
                text = clip("[ 返回 q ]" if self.view != "home" else "[ 退出 q ]", width - 1)
                screen.addstr(height - 1, 0, text, curses.A_REVERSE)
                self.hitboxes.append((height - 1, 0, cells(text), ("key", ord("q"))))
            screen.refresh()
            return
        header = [f"总控任务面板 · {VERSION}"]
        if self.view == "home":
            header += overview(self.data)
            if width < 48:
                header = [line for line in header if not line.startswith("最近检查")]
            label = "全部任务" if self.show_history else "待处理任务"
            count = f"{len(self.data['tasks'])} 项"
            if not self.show_history and self.data["history_count"]:
                count += f"，历史 {self.data['history_count']} 项"
            header.append(f"── {label}（{count}） " + "─" * width)
        else:
            header.append({"menu": "操作菜单 · 单击选择；目标已固定，r 刷新",
                           "detail": "任务详情 · 查看不会确认或归档",
                           "confirm": "操作预览 · 核对目标后明确执行"}[self.view])
            header.append("─" * width)
        header = header[:max(1, height - footer_rows - 1)]
        body = []
        menu_key = None
        for line in self.lines():
            target = None
            attribute = 0
            if self.view == "home" and self.data["tasks"]:
                index = len(body) // 2
                target = ("task", self.data["tasks"][index]["id"])
                if index == self.selected:
                    attribute = curses.A_REVERSE
            elif self.view == "menu":
                if len(line) > 1 and line[0] in "123456" and line[1] == " ":
                    menu_key = ord(line[0])
                    line = "[" + line[0] + "] " + line[2:]
                    attribute = curses.A_BOLD
                disabled = (menu_key == ord("6") and not self.data["tasks"]) or (
                    menu_key == ord("5") and (not self.data["head"] or self.data["head"]["state"] != "awaiting_user"))
                if disabled:
                    attribute = curses.A_DIM
                elif menu_key and line.strip():
                    target = ("key", menu_key)
            wrapped = [clip(line, width - 1)] if self.view == "home" else wrap(line, width - 1)
            body.extend((text, target, attribute) for text in wrapped)
        available = max(1, height - len(header) - footer_rows)
        if self.view == "home" and self.data["tasks"]:
            selected_line = self.selected * 2
            if selected_line < self.offset:
                self.offset = selected_line
            elif selected_line + 1 >= self.offset + available:
                self.offset = selected_line + 2 - available
        self.offset = min(self.offset, max(0, len(body) - available))
        for number, line in enumerate(header):
            screen.addstr(number, 0, clip(line, width - 1), curses.A_BOLD)
        for number, (line, target, attribute) in enumerate(body[self.offset:self.offset + available], len(header)):
            if target and target[0] == "task" and attribute == curses.A_REVERSE:
                line = clip(line + " " * width, width - 1)
            screen.addstr(number, 0, clip(line, width - 1), attribute)
            if target and line.strip():
                self.hitboxes.append((number, 0, width - 1 if target[0] == "task" else cells(line), target))
        footer = height - footer_rows
        screen.addstr(footer, 0, clip(self.message, width - 1), curses.A_BOLD)
        for row, x, text, key in toolbar:
            disabled = self.view == "home" and ((key == 10 and not self.data["tasks"])
                                               or (key == ord("f") and not self.data["session"]))
            screen.addstr(footer + 1 + row, x, text, curses.A_DIM if disabled else curses.A_REVERSE)
            if not disabled:
                self.hitboxes.append((footer + 1 + row, x, x + cells(text), ("key", key)))
        hint = {"home": "单击选择 · 双击全文 · 滚轮滚动",
                "confirm": "预览不执行 · 确认才执行 · 滚轮 / ↑↓"}.get(self.view, "滚轮 / ↑↓ 滚动 · 查看不执行")
        screen.addstr(height - 1, 0, clip(f"{hint} · {self.offset + 1}/{max(1, len(body))}", width - 1))
        screen.refresh()

    def run(self, screen):
        screen.keypad(True)
        screen.timeout(200)
        old_mask = None
        try:
            available, old_mask = curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(150)
            if not available:
                self.message = "终端未启用鼠标；键盘操作仍可用。"
        except curses.error:
            self.message = "终端未启用鼠标；键盘操作仍可用。"
        next_check = 0
        next_refresh = time.monotonic() + 2
        try:
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
                except (HerdrError, Conflict, OSError, ValueError, curses.error) as error:
                    self.message = readable_error(str(error))
        finally:
            if old_mask is not None:
                curses.mousemask(old_mask)


def board(store, herdr):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        data = snapshot(store, herdr.socket)
        print(f"总控任务面板 · {VERSION}")
        print("\n".join(overview(data)))
        print(f"待处理任务（{len(data['tasks'])} 项）；历史 {data['history_count']} 项。")
        if not data["tasks"]:
            print("暂无待处理任务。")
        for task in data["tasks"]:
            print(f"目标：{goal(task)}\nAgent：{task_label(task, data)} | {task_state(task, data['events'])} | {timestamp(task['created_at'])}")
        print("静态只读视图；请在 Herdr 打开面板操作，按 h 查看全部历史。")
        return
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(Panel(store, herdr).run)
