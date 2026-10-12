import curses
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import test_orchestrator as fixtures
from herdr_orchestrator import PLUGIN_ID
from herdr_orchestrator.panel import Panel, agent_label, board, cells, snapshot, task_detail, wrap
from herdr_orchestrator.runtime import Herdr, identity, perform_action
from herdr_orchestrator.shortcuts import install_shortcuts
from herdr_orchestrator.store import Conflict, Store


class Screen:
    def __init__(self, height, width):
        self.height, self.width = height, width
        self.rows = {}

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.rows.clear()

    def addstr(self, y, x, text, *args):
        assert 0 <= y < self.height and x + cells(text) < self.width
        self.rows[y] = text

    def refresh(self):
        pass


class PanelTests(unittest.TestCase):
    dispatch = fixtures.OrchestratorTests.dispatch
    report = fixtures.OrchestratorTests.report
    present = fixtures.OrchestratorTests.present

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orchestrator-panel-")
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.herdr = fixtures.FakeHerdr()
        self.store.bind(fixtures.SOCKET, "coordinator", identity(self.herdr.agent("coordinator")))
        self.herdr.env = {"HERDR_PLUGIN_ID": PLUGIN_ID, "HERDR_PLUGIN_ENTRYPOINT_ID": "status",
                          "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"focused_pane_id": "w1:coordinator"})}
        self.herdr.invoke_action = Mock(return_value={"log": {"log_id": "test-log"}})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def click(self, panel, target, buttons=curses.BUTTON1_CLICKED):
        panel.draw(Screen(30, 100))
        row, left, right, _ = next(hit for hit in panel.hitboxes if hit[3] == target)
        return panel.mouse((left + right) // 2, row, buttons)

    def test_mouse_select_double_click_detail_and_toolbar_are_read_only(self):
        first = self.present(self.report())
        other = self.report(self.dispatch("other"), "second")
        before = self.store.status(fixtures.SOCKET)
        panel = Panel(self.store, self.herdr)
        self.click(panel, ("task", other["dispatch_id"]))
        self.assertEqual(panel.data["tasks"][panel.selected]["id"], other["dispatch_id"])
        self.assertEqual(panel.view, "home")
        self.click(panel, ("task", other["dispatch_id"]), curses.BUTTON1_DOUBLE_CLICKED)
        self.assertEqual(panel.view, "detail")
        self.click(panel, ("key", ord("q")))
        self.click(panel, ("key", ord("h")))
        self.assertTrue(panel.show_history)
        self.click(panel, ("key", ord("r")))
        self.assertTrue(self.click(panel, ("key", ord("q"))))
        self.assertEqual(self.store.status(fixtures.SOCKET), before)
        self.assertEqual(self.store.head(fixtures.SOCKET), first)
        self.herdr.invoke_action.assert_not_called()

    def test_mouse_menu_click_submits_simple_action_once(self):
        for key, name in (("1", "start"), ("2", "focus"), ("4", "stop")):
            with self.subTest(name=name):
                panel = Panel(self.store, self.herdr)
                self.herdr.invoke_action.reset_mock()
                self.click(panel, ("key", ord("m")))
                panel.draw(Screen(30, 100))
                row, left, right, _ = next(hit for hit in panel.hitboxes if hit[3] == ("key", ord(key)))
                panel.mouse(left, row, curses.BUTTON1_CLICKED)
                panel.mouse(left, row, curses.BUTTON1_DOUBLE_CLICKED)
                self.assertEqual(self.herdr.invoke_action.call_count, 1)
                self.assertEqual(self.herdr.invoke_action.call_args.args[0], name)

    def test_mouse_confirmation_keeps_original_head_after_next_is_presented(self):
        first = self.present(self.report())
        second = self.report(self.dispatch("other"), "second")
        panel = Panel(self.store, self.herdr)
        self.click(panel, ("key", ord("m")))
        self.click(panel, ("key", ord("5")))
        self.assertEqual(panel.view, "confirm")
        self.herdr.invoke_action.assert_not_called()
        self.store.ack(fixtures.SOCKET, first["seq"], "这条处理完了")
        self.present(second)
        self.click(panel, ("key", ord("y")))
        name, context = self.herdr.invoke_action.call_args.args
        self.assertEqual(json.loads(context["correlation_id"])["event"], first["seq"])
        self.herdr.env = {"HERDR_PLUGIN_ID": PLUGIN_ID, "HERDR_PLUGIN_ACTION_ID": name,
                          "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context)}
        self.assertEqual(perform_action(self.store, self.herdr, name)["seq"], first["seq"])
        self.assertIsNone(self.store.head(fixtures.SOCKET)["user_acked_at"])

    def test_mouse_archive_preview_cancel_and_fixed_dispatch(self):
        first = self.report()
        panel = Panel(self.store, self.herdr)
        self.click(panel, ("key", ord("m")))
        self.click(panel, ("key", ord("6")))
        self.assertEqual(panel.confirm[1]["dispatch"], first["dispatch_id"])
        self.click(panel, ("key", 27))
        self.herdr.invoke_action.assert_not_called()
        self.click(panel, ("key", ord("m")))
        self.click(panel, ("key", ord("6")))
        other = self.dispatch("other")
        archived = self.store.archive(fixtures.SOCKET, first["dispatch_id"])
        self.click(panel, ("key", ord("y")))
        name, context = self.herdr.invoke_action.call_args.args
        self.assertEqual(json.loads(context["correlation_id"])["dispatch"], first["dispatch_id"])
        self.herdr.env = {"HERDR_PLUGIN_ID": PLUGIN_ID, "HERDR_PLUGIN_ACTION_ID": name,
                          "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context)}
        self.assertEqual(perform_action(self.store, self.herdr, name), archived)
        self.assertIsNone(self.store.dispatch(other["id"])["archived_at"])

    def test_mouse_press_drag_release_and_empty_space_do_not_execute(self):
        self.present(self.report())
        panel = Panel(self.store, self.herdr)
        panel.prepare("continue")
        panel.draw(Screen(30, 100))
        row, left, right, _ = next(hit for hit in panel.hitboxes if hit[3] == ("key", ord("y")))
        panel.mouse(left, row, curses.BUTTON1_RELEASED)
        panel.mouse(left, row, curses.BUTTON1_PRESSED)
        panel.mouse(99, 0, curses.BUTTON1_RELEASED)
        panel.mouse(-1, -1, curses.BUTTON1_CLICKED)
        panel.mouse(99, 0, curses.BUTTON1_CLICKED)
        self.herdr.invoke_action.assert_not_called()
        self.assertIsNone(self.store.head(fixtures.SOCKET)["user_acked_at"])
        panel.mouse(left, row, curses.BUTTON1_PRESSED)
        panel.mouse(right - 1, row, curses.BUTTON1_RELEASED)
        self.herdr.invoke_action.assert_not_called()
        panel.mouse(left, row, curses.BUTTON1_PRESSED)
        panel.mouse(left, row, curses.BUTTON1_PRESSED | curses.REPORT_MOUSE_POSITION)
        panel.mouse(left, row, curses.BUTTON1_RELEASED)
        self.herdr.invoke_action.assert_not_called()
        panel.mouse(left, row, curses.BUTTON1_PRESSED)
        panel.mouse(left, row, curses.BUTTON1_RELEASED)
        self.assertEqual(self.herdr.invoke_action.call_count, 1)

    def test_mouse_stale_task_hit_does_not_select_replacement_row(self):
        first = self.report()
        panel = Panel(self.store, self.herdr)
        panel.draw(Screen(30, 100))
        row, left, _, _ = next(hit for hit in panel.hitboxes if hit[3] == ("task", first["dispatch_id"]))
        self.store.archive(fixtures.SOCKET, first["dispatch_id"])
        other = self.dispatch("other")
        panel.refresh()
        with self.assertRaises(Conflict):
            panel.mouse(left, row, curses.BUTTON1_CLICKED)
        self.assertEqual(panel.data["tasks"][panel.selected]["id"], other["id"])
        self.herdr.invoke_action.assert_not_called()

    def test_mouse_wheel_scroll_and_resize_clear_old_coordinates(self):
        self.present(self.report())
        panel = Panel(self.store, self.herdr)
        panel.key(10)
        panel.draw(Screen(10, 40))
        panel.mouse(5, 5, curses.BUTTON5_PRESSED)
        self.assertEqual(panel.offset, 3)
        panel.mouse(5, 5, curses.BUTTON4_PRESSED)
        self.assertEqual(panel.offset, 0)
        panel.key(curses.KEY_RESIZE)
        self.assertEqual(panel.hitboxes, [])
        self.assertFalse(panel.mouse(0, 0, curses.BUTTON1_CLICKED))
        panel.draw(Screen(16, 32))
        for row, left, right, _ in panel.hitboxes:
            self.assertTrue(0 <= row < 16 and 0 <= left < right < 32)
        self.herdr.invoke_action.assert_not_called()

    def test_mouse_mask_is_restored_and_keyboard_works_if_unavailable(self):
        panel = Panel(self.store, self.herdr)
        screen = Screen(24, 80)
        screen.keypad = Mock()
        screen.timeout = Mock()
        screen.getch = Mock(return_value=ord("q"))
        with patch("curses.mousemask", return_value=(0, 7)) as mask, patch("curses.mouseinterval"):
            panel.run(screen)
        self.assertEqual(mask.call_args_list[-1].args, (7,))
        with patch("curses.mousemask", side_effect=curses.error("unavailable")):
            panel.run(screen)
        self.herdr.invoke_action.assert_not_called()

    def test_mouse_event_dispatch_and_disabled_empty_task_controls(self):
        panel = Panel(self.store, self.herdr)
        panel.draw(Screen(30, 100))
        self.assertNotIn(("key", 10), [hit[3] for hit in panel.hitboxes])
        row, left, _, _ = next(hit for hit in panel.hitboxes if hit[3] == ("key", ord("m")))
        with patch("curses.getmouse", return_value=(0, left, row, 0, curses.BUTTON1_CLICKED)):
            panel.key(curses.KEY_MOUSE)
        self.assertEqual(panel.view, "menu")
        panel.draw(Screen(30, 100))
        self.assertNotIn(("key", ord("5")), [hit[3] for hit in panel.hitboxes])
        self.assertNotIn(("key", ord("6")), [hit[3] for hit in panel.hitboxes])
        self.herdr.invoke_action.assert_not_called()

    def test_open_refresh_detail_enter_and_exit_do_not_change_fifo(self):
        first = self.present(self.report())
        second = self.report(self.dispatch("other"), "second")
        before = self.store.status(fixtures.SOCKET)
        panel = Panel(self.store, self.herdr)
        panel.key(10)
        self.assertEqual(panel.view, "detail")
        panel.key(ord("q"))
        panel.key(ord("r"))
        panel.key(ord("m"))
        panel.key(10)
        self.assertEqual(panel.view, "menu")
        panel.key(ord("q"))
        self.assertTrue(panel.key(ord("q")))
        self.assertEqual(self.store.status(fixtures.SOCKET), before)
        self.assertEqual(before["head"], first)
        self.assertEqual(before["pending"][1], second)
        self.herdr.invoke_action.assert_not_called()

    def test_static_cli_without_binding_exits_once_without_transport_or_actions(self):
        with tempfile.TemporaryDirectory(prefix="orchestrator-unbound-") as directory:
            command = [sys.executable, str(fixtures.ENTRY), "--state-dir", directory,
                       "--socket", fixtures.SOCKET, "board"]
            result = subprocess.run(command, env=dict(os.environ, HERDR_ENV="1"),
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.count("总控任务面板"), 1)
            reopened = Store(directory)
            self.assertEqual(reopened.rows("SELECT * FROM sessions"), [])
            reopened.close()

    def test_static_task_view_does_not_expose_protocol_ids_and_keeps_state(self):
        self.present(self.report())
        before = snapshot(self.store, fixtures.SOCKET)
        output = io.StringIO()
        with patch("sys.stdout.isatty", return_value=False), redirect_stdout(output):
            board(self.store, self.herdr)
        self.assertIn("Agent：worker", output.getvalue())
        self.assertNotIn(before["tasks"][0]["id"], output.getvalue())
        self.assertNotIn(before["tasks"][0]["identity"], output.getvalue())
        self.assertEqual(snapshot(self.store, fixtures.SOCKET), before)

    def test_detail_uses_full_persisted_report_even_if_original_file_is_missing(self):
        dispatch = self.dispatch()
        body = "具体结果：数据 42\n" + "完整中文正文\n" * 90 + "最后一行证据"
        self.store.report(dispatch["id"], "full-result", "completed",
                          {"result": body, "result_file": str(self.directory / "missing.txt")})
        data = snapshot(self.store, fixtures.SOCKET)
        self.assertIn(body, task_detail(data["tasks"][0], data))
        self.assertIsNone(self.store.head(fixtures.SOCKET)["presented_at"])

    def test_unnamed_native_agent_has_clear_label_and_is_selectable(self):
        agent = self.herdr.agents["coordinator"]
        agent.pop("name")
        agent["terminal_title_stripped"] = "整理任务 · Codex"
        panel = Panel(self.store, self.herdr)
        panel.key(ord("m"))
        self.assertIn("整理任务 · Codex", "\n".join(panel.lines()))
        panel.prepare("switch")
        self.assertIn("整理任务 · Codex", "\n".join(panel.confirm[2]))
        agent.pop("terminal_title_stripped")
        self.assertEqual(agent_label(agent), "Codex · w1:coordinator")

    def test_refresh_preserves_selected_task_after_new_dispatch_arrives(self):
        self.report()
        panel = Panel(self.store, self.herdr)
        selected = panel.data["tasks"][panel.selected]["id"]
        self.dispatch("other")
        panel.refresh()
        self.assertEqual(len(panel.data["tasks"]), 2)
        self.assertEqual(panel.data["tasks"][panel.selected]["id"], selected)

    def test_completed_task_stays_visible_until_ack_and_all_followups_accept(self):
        event = self.report()
        dispatch_id = event["dispatch_id"]
        self.assertEqual(snapshot(self.store, fixtures.SOCKET)["tasks"][0]["id"], dispatch_id)
        self.store.next(fixtures.SOCKET)
        self.assertEqual(snapshot(self.store, fixtures.SOCKET)["tasks"][0]["id"], dispatch_id)
        self.present(event)
        followup = self.dispatch(accepted=False, parent=event["seq"])
        self.store.ack(fixtures.SOCKET, event["seq"], "这条处理完了")
        waiting = snapshot(self.store, fixtures.SOCKET)
        self.assertIn(dispatch_id, [task["id"] for task in waiting["tasks"]])
        self.assertEqual(waiting["head"]["state"], "awaiting_user")
        self.assertEqual(waiting["history_count"], 0)
        self.store.accept(followup["id"])
        active = snapshot(self.store, fixtures.SOCKET)
        self.assertEqual([task["id"] for task in active["tasks"]], [followup["id"]])
        self.assertEqual(active["history_count"], 1)
        history = snapshot(self.store, fixtures.SOCKET, show_history=True)
        completed = next(task for task in history["tasks"] if task["id"] == dispatch_id)
        self.assertIn("evidence", task_detail(completed, history))
        self.store.close()
        self.store = Store(self.directory)
        panel = Panel(self.store, self.herdr)
        self.assertEqual([task["id"] for task in panel.data["tasks"]], [followup["id"]])
        panel.key(ord("h"))
        self.assertIn(dispatch_id, [task["id"] for task in panel.data["tasks"]])

    def test_abandoned_task_is_hidden_only_after_its_reports_are_processed(self):
        abandoned = self.dispatch()
        self.store.abandon(abandoned["id"], "取消未回报任务")
        self.assertEqual(snapshot(self.store, fixtures.SOCKET)["tasks"], [])
        report = self.report(self.dispatch("other"))
        self.store.abandon(report["dispatch_id"], "停止后续回报")
        self.assertEqual(snapshot(self.store, fixtures.SOCKET)["tasks"][0]["id"], report["dispatch_id"])
        self.present(report)
        self.store.ack(fixtures.SOCKET, report["seq"], "这条处理完了")
        blocked = self.dispatch()
        event = self.store.report(blocked["id"], "blocked", "blocked", {"result": "等待输入"})
        self.present(event)
        self.store.ack(fixtures.SOCKET, event["seq"], "这条处理完了")
        data = snapshot(self.store, fixtures.SOCKET)
        self.assertEqual([task["id"] for task in data["tasks"]], [blocked["id"]])
        self.assertEqual(data["history_count"], 2)
        self.assertIsNone(data["head"])

    def test_snapshot_keeps_captured_head_visible_if_ack_arrives_during_read(self):
        event = self.present(self.report())
        rows = self.store.rows

        def acknowledge_during_read(sql, args=()):
            if sql.startswith("SELECT * FROM events"):
                self.store.ack(fixtures.SOCKET, event["seq"], "这条处理完了")
            return rows(sql, args)

        with patch.object(self.store, "rows", side_effect=acknowledge_during_read):
            data = snapshot(self.store, fixtures.SOCKET)
        self.assertEqual(data["head"]["seq"], event["seq"])
        self.assertEqual(data["tasks"][0]["id"], event["dispatch_id"])
        self.assertEqual(snapshot(self.store, fixtures.SOCKET)["tasks"], [])

    def test_history_toggle_preserves_selection_and_frozen_views_without_actions(self):
        done = self.present(self.report())
        self.store.ack(fixtures.SOCKET, done["seq"], "这条处理完了")
        live = self.dispatch("other")
        before = self.store.status(fixtures.SOCKET)
        panel = Panel(self.store, self.herdr)
        panel.key(ord("h"))
        self.assertEqual(panel.data["tasks"][panel.selected]["id"], live["id"])
        panel.key(ord("j"))
        self.assertEqual(panel.data["tasks"][panel.selected]["id"], done["dispatch_id"])
        panel.key(10)
        detail = panel.lines()
        panel.key(ord("h"))
        self.assertEqual(panel.lines(), detail)
        panel.key(ord("q"))
        panel.key(ord("h"))
        self.assertFalse(panel.show_history)
        self.assertEqual(panel.data["tasks"][panel.selected]["id"], live["id"])
        panel.key(ord("m"))
        panel.key(ord("h"))
        self.assertEqual(panel.view, "menu")
        self.assertFalse(panel.show_history)
        self.assertEqual(self.store.status(fixtures.SOCKET), before)
        self.herdr.invoke_action.assert_not_called()

    def test_empty_active_list_and_static_cli_preserve_completed_history(self):
        done = self.present(self.report())
        self.store.ack(fixtures.SOCKET, done["seq"], "这条处理完了")
        before = self.store.status(fixtures.SOCKET)
        panel = Panel(self.store, self.herdr)
        self.assertEqual(panel.data["tasks"], [])
        self.assertTrue(panel.lines())
        panel.key(10)
        self.assertEqual(panel.view, "home")
        screen = Screen(24, 80)
        panel.draw(screen)
        command = [sys.executable, str(fixtures.ENTRY), "--state-dir", str(self.directory),
                   "--socket", fixtures.SOCKET, "board"]
        result = subprocess.run(command, env=dict(os.environ, HERDR_ENV="1"),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("总控任务面板"), 1)
        self.assertNotIn("目标：test", result.stdout)
        self.assertNotIn(done["dispatch_id"], result.stdout)
        self.assertEqual(self.store.status(fixtures.SOCKET), before)
        panel.key(ord("h"))
        panel.key(10)
        self.assertIn("evidence", panel.lines())
        self.herdr.invoke_action.assert_not_called()

    def test_periodic_main_refresh_does_not_refresh_frozen_detail(self):
        self.report()
        panel = Panel(self.store, self.herdr)
        screen = Screen(24, 60)
        screen.keypad = Mock()
        screen.timeout = Mock()
        keys = iter([10, -1, ord("q"), ord("q")])
        screen.getch = lambda: next(keys)
        with patch.object(panel, "refresh", wraps=panel.refresh) as refresh:
            with patch("herdr_orchestrator.panel.time.monotonic", side_effect=range(0, 100, 3)):
                panel.run(screen)
            # Detail stays frozen; the refresh occurs only once after returning home.
            self.assertEqual(refresh.call_count, 1)

    def test_stale_confirmation_requests_only_displayed_event_and_never_new_head(self):
        first = self.present(self.report())
        second = self.report(self.dispatch("other"), "second")
        panel = Panel(self.store, self.herdr)
        panel.key(ord("m"))
        self.store.ack(fixtures.SOCKET, first["seq"], "这条处理完了")
        self.present(second)
        panel.key(ord("5"))
        panel.key(10)
        self.herdr.invoke_action.assert_not_called()
        panel.key(ord("y"))
        name, context = self.herdr.invoke_action.call_args.args
        self.assertEqual(json.loads(context["correlation_id"])["event"], first["seq"])
        self.herdr.env = {"HERDR_PLUGIN_ID": PLUGIN_ID, "HERDR_PLUGIN_ACTION_ID": name,
                          "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context)}
        result = perform_action(self.store, self.herdr, name)
        self.assertEqual(result["seq"], first["seq"])
        self.assertEqual(self.store.head(fixtures.SOCKET)["seq"], second["seq"])
        self.assertIsNone(self.store.head(fixtures.SOCKET)["user_acked_at"])

    def test_action_callback_keeps_frozen_menu_head_and_does_not_confirm_next_result(self):
        first = self.present(self.report())
        second = self.report(self.dispatch("other"), "second")
        panel = Panel(self.store, self.herdr)
        panel.prepare("continue")
        panel.submit()
        panel.key(ord("m"))
        self.store.ack_action(fixtures.SOCKET, first["seq"], {"invocation_source": "panel"})
        self.present(second)
        self.herdr.call = Mock(side_effect=lambda *args: {"agents": list(self.herdr.agents.values())}
                               if args == ("agent", "list") else {"logs": [{"log_id": "test-log", "status": "succeeded", "exit_code": 0}]})
        self.assertFalse(panel.check_action())
        self.assertEqual(panel.data["head"]["seq"], first["seq"])
        panel.key(ord("5"))
        panel.key(ord("y"))
        name, context = self.herdr.invoke_action.call_args.args
        self.assertEqual(json.loads(context["correlation_id"])["event"], first["seq"])
        self.herdr.env = {"HERDR_PLUGIN_ID": PLUGIN_ID, "HERDR_PLUGIN_ACTION_ID": name,
                          "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context)}
        self.assertEqual(perform_action(self.store, self.herdr, name)["seq"], first["seq"])
        self.assertIsNone(self.store.head(fixtures.SOCKET)["user_acked_at"])

    def test_menu_keeps_opened_pane_and_cannot_focus_then_confirm_implicitly(self):
        self.present(self.report())
        self.herdr.env["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps({"focused_pane_id": "w1:other"})
        panel = Panel(self.store, self.herdr)
        panel.prepare("continue")
        panel.submit()
        name, context = self.herdr.invoke_action.call_args.args
        self.assertEqual(context["focused_pane_id"], "w1:other")
        self.herdr.env = {"HERDR_PLUGIN_ID": PLUGIN_ID, "HERDR_PLUGIN_ACTION_ID": name,
                          "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context)}
        with self.assertRaises(Conflict):
            perform_action(self.store, self.herdr, name)
        self.assertIsNone(self.store.head(fixtures.SOCKET)["user_acked_at"])

    def test_direct_tty_board_cannot_submit_without_plugin_pane_context(self):
        panel = Panel(self.store, self.herdr)
        self.herdr.env.pop("HERDR_PLUGIN_ENTRYPOINT_ID")
        panel.prepare("stop")
        with self.assertRaises(Conflict):
            panel.submit()
        self.herdr.invoke_action.assert_not_called()
        self.assertTrue(self.store.session(fixtures.SOCKET)["enabled"])

    def test_panel_ack_source_and_followup_receipt_gate(self):
        first = self.present(self.report())
        followup = self.dispatch(accepted=False, parent=first["seq"])
        panel = Panel(self.store, self.herdr)
        panel.prepare("continue")
        panel.submit()
        name, context = self.herdr.invoke_action.call_args.args
        self.herdr.env = {"HERDR_PLUGIN_ID": PLUGIN_ID, "HERDR_PLUGIN_ACTION_ID": name,
                          "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context)}
        result = perform_action(self.store, self.herdr, name)
        self.assertEqual(result["state"], "awaiting_user")
        self.assertEqual(json.loads(result["user_ack_context"])["invocation_source"], "panel")
        self.assertIsNone(result["user_reply"])
        self.assertEqual(perform_action(self.store, self.herdr, name), result)
        self.store.accept(followup["id"])
        self.assertIsNone(self.store.head(fixtures.SOCKET))

    def test_panel_target_identity_change_is_rejected_before_pausing(self):
        panel = Panel(self.store, self.herdr)
        panel.prepare("switch")
        panel.submit()
        name, context = self.herdr.invoke_action.call_args.args
        self.herdr.env = {"HERDR_PLUGIN_ID": PLUGIN_ID, "HERDR_PLUGIN_ACTION_ID": name,
                          "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context)}
        self.herdr.agents["coordinator"]["agent_session"]["value"] = "replacement"
        before = self.store.session(fixtures.SOCKET)
        with self.assertRaises(Conflict):
            perform_action(self.store, self.herdr, name)
        self.assertEqual(self.store.session(fixtures.SOCKET), before)

    def test_simple_menu_choices_submit_once_without_extra_confirmation(self):
        for key, name in (("1", "start"), ("2", "focus"), ("4", "stop")):
            with self.subTest(name=name):
                panel = Panel(self.store, self.herdr)
                self.herdr.invoke_action.reset_mock()
                panel.key(ord("m"))
                panel.key(ord(key))
                self.assertEqual(self.herdr.invoke_action.call_args.args[0], name)
                self.assertEqual(self.herdr.invoke_action.call_count, 1)

    @unittest.skipUnless(shutil.which("herdr"), "Herdr CLI is required for config validation")
    def test_installer_adds_only_panel_key_and_preserves_legacy_bindings(self):
        actions = {"prefix+i": "status", "prefix+a": "start", "prefix+shift+a": "switch",
                   "prefix+shift+i": "focus", "prefix+shift+s": "stop", "prefix+enter": "continue"}
        path = self.directory / "config.toml"
        original = "# existing bindings\n" + "".join(
            f'[[keys.command]]\nkey = "{key}"\ntype = "plugin_action"\ncommand = "{PLUGIN_ID}.{name}"\n'
            for key, name in actions.items())
        path.write_text(original)
        from types import SimpleNamespace
        result = install_shortcuts(SimpleNamespace(env=dict(os.environ, HERDR_CONFIG_PATH=str(path)), binary="herdr"))
        self.assertEqual(result["installed"], ["prefix+shift+o"])
        self.assertTrue(path.read_text().startswith(original))

    def test_chinese_layout_and_resize_without_color_fit_terminal_cells(self):
        self.present(self.report())
        panel = Panel(self.store, self.herdr)
        for view in ("home", "menu", "detail", "confirm"):
            panel.view = view
            if view == "confirm":
                panel.prepare("continue")
            for height, width in ((24, 80), (16, 32), (7, 20), (4, 8)):
                with self.subTest(view=view, height=height, width=width):
                    screen = Screen(height, width)
                    panel.draw(screen)
                    self.assertTrue(screen.rows)
        self.assertEqual("".join(wrap("中文e\u0301正文", 5)), "中文e\u0301正文")


class NativeInvokeTests(unittest.TestCase):
    def test_native_ndjson_action_request_preserves_context_without_action_env_spoofing(self):
        with tempfile.TemporaryDirectory(prefix="panel-api-") as directory:
            path = str(Path(directory) / "herdr.sock")
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(path)
                listener.listen(1)
                received = []

                def serve():
                    with listener.accept()[0] as connection:
                        received.append(json.loads(connection.makefile("r").readline()))
                        connection.sendall(b'{"id":"orchestrator-panel","result":{"log":{"log_id":"receipt"}}}\n')

                worker = threading.Thread(target=serve)
                worker.start()
                herdr = Herdr(path, {"HERDR_ENV": "1"})
                context = {"invocation_source": "panel", "focused_pane_id": "example-pane",
                           "correlation_id": json.dumps({"action": "continue", "event": 1})}
                receipt = herdr.invoke_action("continue", context)
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(receipt["log"]["log_id"], "receipt")
                self.assertEqual(received[0]["method"], "plugin.action.invoke")
                self.assertEqual(received[0]["params"]["context"], context)
                self.assertNotIn("HERDR_PLUGIN_ACTION_ID", herdr.env)
