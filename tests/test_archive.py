import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_orchestrator as fixtures
from herdr_orchestrator import PLUGIN_ID
from herdr_orchestrator.cli import execute, parser
from herdr_orchestrator.panel import Panel, snapshot, task_detail
from herdr_orchestrator.runtime import identity, perform_action, poll_once
from herdr_orchestrator.store import Conflict, Store


class ArchiveTests(unittest.TestCase):
    dispatch = fixtures.OrchestratorTests.dispatch
    report = fixtures.OrchestratorTests.report
    present = fixtures.OrchestratorTests.present

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orchestrator-archive-")
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.herdr = fixtures.FakeHerdr()
        self.store.bind(fixtures.SOCKET, "coordinator", identity(self.herdr.agent("coordinator")))
        self.store.acknowledge_activation(fixtures.SOCKET)
        self.herdr.env = {"HERDR_PLUGIN_ID": PLUGIN_ID, "HERDR_PLUGIN_ENTRYPOINT_ID": "status",
                          "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"focused_pane_id": "w1:coordinator"})}
        self.herdr.invoke_action = Mock(return_value={"log": {"log_id": "archive-log"}})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def context(self, dispatch_id):
        return {"invocation_source": "panel", "focused_pane_id": "w1:coordinator",
                "correlation_id": json.dumps({"action": "archive", "dispatch": dispatch_id,
                                               "identity": self.store.session(fixtures.SOCKET)["identity"]})}

    def action(self, context):
        self.herdr.env = {"HERDR_PLUGIN_ID": PLUGIN_ID, "HERDR_PLUGIN_ACTION_ID": "archive",
                          "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context)}
        return perform_action(self.store, self.herdr, "archive")

    def test_archive_head_preserves_report_and_confirmation_then_poll_wakes_next(self):
        first = self.present(self.report())
        second = self.report(self.dispatch("other"), "second")
        archived = self.store.archive(fixtures.SOCKET, first["dispatch_id"])
        self.assertIsNotNone(archived["archived_at"])
        self.assertEqual(self.store.one("SELECT * FROM events WHERE seq=?", (first["seq"],)), first)
        self.assertIsNone(first["user_acked_at"])
        self.assertEqual(self.store.status(fixtures.SOCKET)["pending"], [second])
        self.assertEqual(self.store.head(fixtures.SOCKET), second)
        self.assertEqual(poll_once(self.store, self.herdr, 100), "notified")
        self.assertEqual(self.store.head(fixtures.SOCKET)["state"], "pending")
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertIn(f"#{second['seq']}", self.herdr.prompts[0][1])
        with self.assertRaises(Conflict):
            self.store.ack(fixtures.SOCKET, first["seq"], "这条处理完了")

    def test_archiving_non_head_and_retry_do_not_change_current_wait_or_wake(self):
        first = self.present(self.report())
        second = self.report(self.dispatch("other"), "second")
        self.store.update_session(fixtures.SOCKET, wake_key="existing", wake_at=50)
        archived = self.store.archive(fixtures.SOCKET, second["dispatch_id"])
        before = self.store.status(fixtures.SOCKET)
        self.assertEqual(self.store.archive(fixtures.SOCKET, second["dispatch_id"]), archived)
        self.assertEqual(self.store.status(fixtures.SOCKET), before)
        self.assertEqual(self.store.head(fixtures.SOCKET), first)
        self.assertEqual(before["session"]["wake_key"], "existing")
        self.assertEqual(poll_once(self.store, self.herdr, 100), "awaiting_user")
        self.assertEqual(self.herdr.prompts, [])

    def test_late_accept_and_reports_remain_in_history_without_notifications(self):
        task = self.dispatch(accepted=False)
        self.herdr.agents["worker"]["agent_status"] = "blocked"
        archived = self.store.archive(fixtures.SOCKET, task["id"])
        self.assertIsNone(archived["completed_at"])
        self.assertIsNone(archived["abandoned_at"])
        self.assertEqual(poll_once(self.store, self.herdr, 100), "empty")
        self.assertEqual(self.store.rows("SELECT * FROM events"), [])
        self.store.accept(task["id"])
        blocked = self.store.report(task["id"], "blocked", "blocked", {"result": "仍在等待输入"})
        completed = self.store.report(task["id"], "completed", "completed", {"result": "归档后的完整结果"})
        self.assertEqual(self.store.report(task["id"], "completed", "completed",
                                         {"result": "归档后的完整结果"}), completed)
        self.assertIsNone(self.store.head(fixtures.SOCKET))
        self.assertEqual(self.store.status(fixtures.SOCKET)["pending"], [])
        self.assertEqual(poll_once(self.store, self.herdr, 105), "empty")
        history = snapshot(self.store, fixtures.SOCKET, show_history=True)
        self.assertEqual(len(history["events"]), 2)
        self.assertIn("归档后的完整结果", task_detail(history["tasks"][0], history))
        self.assertIsNone(blocked["user_acked_at"])
        self.assertEqual(self.herdr.agents["worker"]["agent_status"], "blocked")
        self.assertEqual(self.herdr.prompts, [])

    def test_archive_last_unaccepted_followup_releases_only_already_confirmed_parent(self):
        first = self.present(self.report())
        followup = self.dispatch(accepted=False, parent=first["seq"])
        second = self.report(self.dispatch("other"), "second")
        confirmed = self.store.ack(fixtures.SOCKET, first["seq"], "这条处理完了")
        self.store.archive(fixtures.SOCKET, followup["id"])
        finished = self.store.one("SELECT * FROM events WHERE seq=?", (first["seq"],))
        self.assertEqual(finished["state"], "done")
        self.assertEqual(finished["user_acked_at"], confirmed["user_acked_at"])
        self.assertEqual(self.store.head(fixtures.SOCKET), second)
        self.store.accept(followup["id"])
        self.assertEqual(self.store.head(fixtures.SOCKET), second)
        self.assertIsNone(second["user_acked_at"])

    def test_archiving_followup_does_not_confirm_parent_or_archive_siblings(self):
        first = self.present(self.report())
        followup = self.dispatch(accepted=False, parent=first["seq"])
        sibling = self.dispatch("other", accepted=False, parent=first["seq"])
        self.store.archive(fixtures.SOCKET, followup["id"])
        self.assertEqual(self.store.head(fixtures.SOCKET), first)
        self.assertIsNone(first["user_acked_at"])
        self.assertIsNone(self.store.dispatch(first["dispatch_id"])["archived_at"])
        self.store.archive(fixtures.SOCKET, first["dispatch_id"])
        self.assertIsNone(self.store.dispatch(sibling["id"])["archived_at"])
        new_task = self.dispatch()
        self.assertIsNone(self.store.dispatch(new_task["id"])["archived_at"])

    def test_panel_archive_keeps_selected_dispatch_when_new_tasks_arrive(self):
        first = self.report()
        panel = Panel(self.store, self.herdr)
        panel.key(ord("m"))
        other = self.dispatch("other")
        panel.key(ord("6"))
        panel.key(10)
        self.herdr.invoke_action.assert_not_called()
        panel.key(ord("y"))
        name, context = self.herdr.invoke_action.call_args.args
        self.assertEqual(name, "archive")
        self.assertEqual(json.loads(context["correlation_id"])["dispatch"], first["dispatch_id"])
        self.action(context)
        self.assertIsNotNone(self.store.dispatch(first["dispatch_id"])["archived_at"])
        self.assertIsNone(self.store.dispatch(other["id"])["archived_at"])
        panel.refresh()
        self.assertEqual([task["id"] for task in panel.data["tasks"]], [other["id"]])
        panel.key(ord("h"))
        self.assertEqual(len(panel.data["tasks"]), 2)

    def test_archive_action_rejects_invalid_context_focus_busy_and_identity(self):
        task = self.dispatch()
        before = self.store.status(fixtures.SOCKET)
        for change in ("plugin", "action", "no_selection", "focus", "busy", "identity", "other_session"):
            with self.subTest(change=change):
                self.herdr.agents["coordinator"] = fixtures.agent("coordinator")
                context = self.context(task["id"])
                self.herdr.env = {"HERDR_PLUGIN_ID": PLUGIN_ID, "HERDR_PLUGIN_ACTION_ID": "archive"}
                if change == "plugin":
                    self.herdr.env["HERDR_PLUGIN_ID"] = "other-plugin"
                elif change == "action":
                    self.herdr.env["HERDR_PLUGIN_ACTION_ID"] = "continue"
                elif change == "no_selection":
                    context.pop("correlation_id")
                elif change == "focus":
                    context["focused_pane_id"] = "w1:other"
                elif change == "busy":
                    self.herdr.agents["coordinator"]["agent_status"] = "working"
                elif change == "identity":
                    self.herdr.agents["coordinator"]["agent_session"]["value"] = "replacement"
                else:
                    self.store.bind("/tmp/other-archive-session.sock", "coordinator", identity(fixtures.agent("coordinator")))
                    other = self.store.create_dispatch("/tmp/other-archive-session.sock", "other-task", "other",
                                                       identity(fixtures.agent("other")), fixtures.SPEC)
                    context = self.context(other["id"])
                self.herdr.env["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps(context)
                with self.assertRaises(Conflict):
                    perform_action(self.store, self.herdr, "archive")
                self.assertEqual(self.store.status(fixtures.SOCKET), before)

    def test_cli_archive_is_explicit_and_only_bound_coordinator_can_run_it(self):
        task = self.dispatch()
        args = parser().parse_args(["--state-dir", str(self.directory), "--socket", fixtures.SOCKET,
                                    "archive", "--dispatch", task["id"]])
        self.herdr.caller_name = "worker"
        with patch("herdr_orchestrator.cli.Herdr", return_value=self.herdr):
            with self.assertRaises(Conflict):
                execute(args)
            self.herdr.caller_name = "coordinator"
            result = execute(args)
        self.assertIsNotNone(result["archived_at"])
        self.assertIsNone(result["completed_at"])

    def test_snapshot_keeps_head_task_when_archive_arrives_during_read(self):
        event = self.present(self.report())
        rows = self.store.rows

        def archive_during_read(sql, args=()):
            if sql.startswith("SELECT * FROM events"):
                self.store.archive(fixtures.SOCKET, event["dispatch_id"])
            return rows(sql, args)

        with patch.object(self.store, "rows", side_effect=archive_during_read):
            data = snapshot(self.store, fixtures.SOCKET)
        self.assertEqual(data["head"]["seq"], event["seq"])
        self.assertEqual(data["tasks"][0]["id"], event["dispatch_id"])
        self.assertEqual(snapshot(self.store, fixtures.SOCKET)["tasks"], [])

    def test_v3_migration_preserves_all_fields_and_archival_survives_reopen(self):
        first = self.present(self.report())
        second = self.report(self.dispatch("other"), "second")
        self.store.db.execute("ALTER TABLE dispatches DROP COLUMN archived_at")
        self.store.db.execute("PRAGMA user_version=3")
        self.store.db.commit()
        original = {table: self.store.rows(f"SELECT * FROM {table}") for table in ("sessions", "dispatches", "events")}
        self.store.close()
        self.store = Store(self.directory)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 4)
        for table, records in original.items():
            migrated = self.store.rows(f"SELECT * FROM {table}")
            self.assertEqual([{key: row[key] for key in records[0]} for row in migrated], records)
        self.assertTrue(all(task["archived_at"] is None for task in self.store.rows("SELECT * FROM dispatches")))
        self.assertEqual(self.store.head(fixtures.SOCKET), first)
        archived = self.store.archive(fixtures.SOCKET, first["dispatch_id"])
        self.store.close()
        self.store = Store(self.directory)
        self.assertEqual(self.store.dispatch(first["dispatch_id"]), archived)
        self.assertEqual(self.store.head(fixtures.SOCKET), second)
        self.assertEqual(snapshot(self.store, fixtures.SOCKET)["history_count"], 1)
        self.assertIsNone(self.store.one("SELECT * FROM events WHERE seq=?", (first["seq"],))["user_acked_at"])
