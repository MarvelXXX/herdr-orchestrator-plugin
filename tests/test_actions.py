import fcntl
import json
import multiprocessing
import os
import shutil
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from herdr_orchestrator import PLUGIN_ID
from herdr_orchestrator.cli import execute, parser
from herdr_orchestrator.runtime import HerdrError, perform_action, identity, pause_daemon
from herdr_orchestrator.shortcuts import SHORTCUTS, install_shortcuts
from herdr_orchestrator.store import Conflict, Store
import test_orchestrator as fixtures
from test_orchestrator import FakeHerdr, SOCKET, agent


def retiring_daemon(directory, ready):
    store = Store(directory)
    session = store.session(SOCKET)
    with (store.directory / f"session-{session['id']}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        store.update_session(SOCKET, pid=os.getpid())
        ready.set()
        while store.session(SOCKET)["enabled"]:
            time.sleep(.01)
        store.update_session(SOCKET, pid=None)
        time.sleep(.15)  # PID cleanup happens before the daemon releases its lock.
    store.close()


class ActionTests(unittest.TestCase):
    dispatch = fixtures.OrchestratorTests.dispatch
    report = fixtures.OrchestratorTests.report
    present = fixtures.OrchestratorTests.present

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orchestrator-actions-")
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.herdr = FakeHerdr()
        self.store.bind(SOCKET, "coordinator", identity(agent("coordinator")))
        self.store.acknowledge_activation(SOCKET)
        self.spawn = patch("herdr_orchestrator.runtime.spawn_daemon", return_value={"running_pid": 1})
        self.spawn_mock = self.spawn.start()

    def tearDown(self):
        self.spawn.stop()
        self.store.close()
        self.temp.cleanup()

    def action(self, name, target="coordinator", source="keybinding"):
        self.herdr.env = {"HERDR_PLUGIN_ID": PLUGIN_ID, "HERDR_PLUGIN_ACTION_ID": name,
                          "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(
                              {"focused_pane_id": agent(target)["pane_id"], "invocation_source": source})}
        return perform_action(self.store, self.herdr, name)

    def test_start_resumes_binding_while_focus_is_elsewhere(self):
        event = self.present(self.report())
        self.store.update_session(SOCKET, enabled=0)
        result = self.action("start", "other")
        self.assertEqual(result["session"]["coordinator"], "coordinator")
        self.assertTrue(result["session"]["activated"])
        self.assertTrue(result["session"]["enabled"])
        self.assertEqual(self.store.head(SOCKET), event)

    def test_first_start_and_switch_bind_selected_native_agent(self):
        for name in ("start", "switch"):
            with self.subTest(name=name):
                self.store.db.execute("DELETE FROM sessions")
                self.store.db.commit()
                result = self.action(name, "other")
                self.assertEqual(result["session"]["identity"], identity(agent("other")))
                self.assertFalse(result["session"]["activated"])

    def test_switch_same_target_preserves_activation_and_waiting_head(self):
        event = self.present(self.report())
        original = self.store.session(SOCKET)
        result = self.action("switch")
        self.assertEqual(result["session"], original)
        self.assertEqual(self.store.head(SOCKET), event)

    def test_switch_recovers_closed_old_pane_but_propagates_other_transport_errors(self):
        original = self.store.session(SOCKET)
        original_agent = self.herdr.agent
        for code in ("transport_uncertain", "agent_not_found", "pane_not_found"):
            with self.subTest(code=code):
                def lookup(target):
                    if target == "coordinator":
                        raise HerdrError("old agent unavailable", code)
                    return original_agent(target)
                self.herdr.agent = lookup
                if code == "transport_uncertain":
                    with self.assertRaises(HerdrError):
                        self.action("switch", "other")
                    self.assertEqual(self.store.session(SOCKET), original)
                else:
                    result = self.action("switch", "other")
                    self.assertEqual(result["session"]["coordinator"], "other")
                    self.store.update_session(SOCKET, enabled=0)
                    self.store.rebind(SOCKET, "coordinator", identity(agent("coordinator")))

    def test_switch_invalid_or_busy_agents_does_not_pause_old_coordinator(self):
        original = self.store.session(SOCKET)
        for failure in ("missing_identity", "working", "blocked", "old_busy"):
            with self.subTest(failure=failure):
                self.herdr.agents["other"] = agent("other")
                self.herdr.agents["coordinator"] = agent("coordinator")
                if failure == "missing_identity":
                    self.herdr.agents["other"]["agent_session"] = None
                elif failure == "old_busy":
                    self.herdr.agents["coordinator"]["agent_status"] = "working"
                else:
                    self.herdr.agents["other"]["agent_status"] = failure
                with self.assertRaises(Conflict):
                    self.action("switch", "other")
                self.assertEqual(self.store.session(SOCKET), original)
                self.spawn_mock.assert_not_called()

    def test_switch_waits_for_lock_release_even_after_old_pid_is_cleared(self):
        event = self.present(self.report())
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        process = context.Process(target=retiring_daemon, args=(self.directory, ready))
        process.start()
        self.assertTrue(ready.wait(5))

        def start_after_release(store, socket):
            with (store.directory / f"session-{store.session(socket)['id']}.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return {"started_pid": 2}

        self.spawn_mock.side_effect = start_after_release
        try:
            result = self.action("switch", "other")
            self.assertEqual(result["session"]["coordinator"], "other")
            self.assertFalse(result["session"]["activated"])
            self.assertEqual(self.store.head(SOCKET), event)
        finally:
            process.join(5)
        self.assertEqual(process.exitcode, 0)

    def test_daemon_exit_timeout_leaves_binding_paused_and_reports_failure(self):
        session = self.store.session(SOCKET)
        with (self.directory / f"session-{session['id']}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch("herdr_orchestrator.runtime.DAEMON_TIMEOUT", 0):
                with self.assertRaises(TimeoutError):
                    self.action("switch", "other")
        self.assertEqual(self.store.session(SOCKET)["coordinator"], "coordinator")
        self.assertFalse(self.store.session(SOCKET)["enabled"])
        self.spawn_mock.assert_not_called()

    def test_switch_rechecks_target_after_wait_and_keeps_old_binding_on_change(self):
        def pause_then_replace(store, socket):
            pause_daemon(store, socket)
            self.herdr.agents["other"]["agent_session"]["value"] = "replacement"

        with patch("herdr_orchestrator.runtime.pause_daemon", side_effect=pause_then_replace):
            with self.assertRaises(Conflict):
                self.action("switch", "other")
        self.assertEqual(self.store.session(SOCKET)["coordinator"], "coordinator")
        self.assertFalse(self.store.session(SOCKET)["enabled"])
        self.spawn_mock.assert_not_called()

    def test_continue_rejects_wrong_plugin_action_context_focus_and_busy(self):
        original = self.present(self.report())
        for change in ("plugin", "action", "context", "focus", "busy", "identity"):
            with self.subTest(change=change):
                self.herdr.agents["coordinator"] = agent("coordinator")
                self.herdr.env = {"HERDR_PLUGIN_ID": PLUGIN_ID, "HERDR_PLUGIN_ACTION_ID": "continue",
                                  "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(
                                      {"focused_pane_id": agent("coordinator")["pane_id"]})}
                if change == "plugin":
                    self.herdr.env["HERDR_PLUGIN_ID"] = "another-plugin"
                elif change == "action":
                    self.herdr.env["HERDR_PLUGIN_ACTION_ID"] = "start"
                elif change == "context":
                    self.herdr.env["HERDR_PLUGIN_CONTEXT_JSON"] = "null"
                elif change == "focus":
                    self.herdr.env["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps(
                        {"focused_pane_id": agent("other")["pane_id"]})
                elif change == "busy":
                    self.herdr.agents["coordinator"]["agent_status"] = "working"
                else:
                    self.herdr.agents["coordinator"]["agent_session"]["value"] = "replacement"
                with self.assertRaises(Conflict):
                    perform_action(self.store, self.herdr, "continue")
                self.assertEqual(self.store.head(SOCKET), original)

    def test_continue_rejects_empty_pending_and_handling_heads(self):
        with self.assertRaises(Conflict):
            self.action("continue")
        event = self.report()
        with self.assertRaises(Conflict):
            self.action("continue")
        self.store.next(SOCKET)
        with self.assertRaises(Conflict):
            self.action("continue")
        self.assertIsNone(self.store.head(SOCKET)["user_acked_at"])
        self.assertEqual(self.store.head(SOCKET)["seq"], event["seq"])

    def test_click_before_presentation_cannot_confirm_a_head_presented_during_lookup(self):
        event = self.report()
        original_agent = self.herdr.agent

        def present_during_lookup(target):
            self.present(event)
            return original_agent(target)

        self.herdr.agent = present_during_lookup
        with self.assertRaises(Conflict):
            self.action("continue")
        self.assertEqual(self.store.head(SOCKET)["state"], "pending")
        self.assertIsNone(self.store.head(SOCKET)["user_acked_at"])

    def test_continue_confirms_one_head_without_inventing_user_reply(self):
        first = self.present(self.report())
        second = self.report(self.dispatch("other"), "second")
        confirmed = self.action("continue")
        self.assertEqual(confirmed["seq"], first["seq"])
        self.assertEqual(confirmed["state"], "done")
        self.assertEqual(confirmed["user_ack_source"], "plugin_action")
        self.assertIsNone(confirmed["user_reply"])
        self.assertIsNone(confirmed["user_reply_file"])
        self.assertEqual(json.loads(confirmed["user_ack_context"])["invocation_source"], "keybinding")
        with self.assertRaises(Conflict):
            self.action("continue")
        self.assertEqual(self.store.head(SOCKET)["seq"], second["seq"])
        self.assertEqual(self.store.head(SOCKET)["state"], "pending")
        self.assertEqual(self.herdr.prompts, [])

    def test_continue_retry_keeps_source_and_waits_until_last_accept(self):
        first = self.present(self.report())
        followup = self.dispatch(accepted=False, parent=first["seq"])
        other = self.dispatch("other", accepted=False, parent=first["seq"])
        confirmed = self.action("continue", source="cli")
        self.assertEqual(confirmed["state"], "awaiting_user")
        self.assertEqual(self.action("continue", source="api"), confirmed)
        self.store.accept(followup["id"])
        self.assertEqual(self.store.head(SOCKET), confirmed)
        self.store.accept(other["id"])
        self.assertIsNone(self.store.head(SOCKET))
        finished = self.store.one("SELECT * FROM events WHERE seq=?", (first["seq"],))
        self.assertEqual(finished["user_ack_context"], confirmed["user_ack_context"])
        self.assertIsNone(self.store.dispatch(other["id"])["completed_at"])

    def test_action_retry_does_not_overwrite_actual_message_confirmation(self):
        event = self.present(self.report())
        self.dispatch(accepted=False, parent=event["seq"])
        confirmed = self.store.ack(SOCKET, event["seq"], "继续下一条", "/tmp/user-reply.txt")
        self.assertEqual(self.action("continue"), confirmed)
        self.assertEqual(confirmed["user_ack_source"], "message")
        self.assertEqual(confirmed["user_reply"], "继续下一条")
        self.assertIsNone(confirmed["user_ack_context"])

    def test_captured_event_does_not_confirm_head_changed_during_transport(self):
        first = self.present(self.report())
        second = self.report(self.dispatch("other"), "second")
        original_agent = self.herdr.agent

        def advance_during_lookup(target):
            self.store.ack_action(SOCKET, first["seq"], {"invocation_source": "cli"})
            self.present(second)
            return original_agent(target)

        self.herdr.agent = advance_during_lookup
        confirmed = self.action("continue")
        self.assertEqual(confirmed["seq"], first["seq"])
        self.assertEqual(self.store.head(SOCKET)["seq"], second["seq"])
        self.assertIsNone(self.store.head(SOCKET)["user_acked_at"])

    def test_focus_targets_binding_without_reading_or_acknowledging_head(self):
        event = self.present(self.report())
        self.herdr.call = unittest.mock.Mock(return_value={"focused": True})
        self.action("focus", "other")
        self.herdr.call.assert_called_once_with("agent", "focus", agent("coordinator")["pane_id"])
        self.assertEqual(self.store.head(SOCKET), event)

    def test_cli_action_entrypoint_executes_with_actual_action_environment(self):
        event = self.present(self.report())
        env = {"HERDR_PLUGIN_ID": PLUGIN_ID, "HERDR_PLUGIN_ACTION_ID": "continue",
               "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(
                   {"focused_pane_id": agent("coordinator")["pane_id"], "invocation_source": "cli"})}
        self.herdr.env = env
        args = parser().parse_args(["--state-dir", str(self.directory), "--socket", SOCKET,
                                    "action", "continue"])
        with patch("herdr_orchestrator.cli.Herdr", return_value=self.herdr):
            result = execute(args)
        self.assertEqual(result["seq"], event["seq"])
        self.assertEqual(result["user_ack_source"], "plugin_action")


@unittest.skipUnless(shutil.which("herdr"), "Herdr CLI is required for config validation")
class ShortcutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orchestrator-config-")
        self.path = Path(self.temp.name) / "config.toml"
        self.herdr = SimpleNamespace(env={**os.environ, "HERDR_CONFIG_PATH": str(self.path)},
                                     binary=shutil.which("herdr"))

    def tearDown(self):
        self.temp.cleanup()

    def test_preserves_text_prefix_and_custom_keys_and_is_idempotent(self):
        original = '# user comment\n[keys]\nprefix = "ctrl+a"\n[[keys.command]]\nkey = "prefix+alt+g"\ntype = "shell"\ncommand = "true"\n'
        self.path.write_text(original)
        self.path.chmod(0o600)
        result = install_shortcuts(self.herdr)
        installed = self.path.read_bytes()
        self.assertTrue(installed.startswith(original.encode()))
        parsed = tomllib.loads(installed.decode())
        self.assertEqual(parsed["keys"]["prefix"], "ctrl+a")
        self.assertEqual(len(parsed["keys"]["command"]), 2)
        self.assertEqual(Path(result["backup"]).read_text(), original)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(result["backup"]).stat().st_mode & 0o777, 0o600)
        self.assertTrue(install_shortcuts(self.herdr)["already_installed"])
        self.assertEqual(self.path.read_bytes(), installed)
        self.assertEqual(Path(result["backup"]).read_text(), original)

    def test_custom_conflict_preserves_config_and_makes_no_partial_install(self):
        original = '[[keys.command]]\nkey = "prefix+shift+o"\ntype = "shell"\ncommand = "true"\n'
        self.path.write_text(original)
        with self.assertRaises(Conflict):
            install_shortcuts(self.herdr)
        self.assertEqual(self.path.read_text(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_backup_is_private_when_original_configuration_is_world_readable(self):
        self.path.write_text('# existing configuration\n')
        self.path.chmod(0o644)
        result = install_shortcuts(self.herdr)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(Path(result["backup"]).stat().st_mode & 0o777, 0o600)

    def test_native_conflict_is_rejected_by_real_herdr_check(self):
        original = '[keys]\nhelp = "prefix+shift+o"\n'
        self.path.write_text(original)
        with self.assertRaises(Conflict):
            install_shortcuts(self.herdr)
        self.assertEqual(self.path.read_text(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_xdg_path_without_existing_config_installs_exact_recommended_actions(self):
        self.herdr.env.pop("HERDR_CONFIG_PATH")
        self.herdr.env["XDG_CONFIG_HOME"] = self.temp.name
        result = install_shortcuts(self.herdr)
        path = Path(self.temp.name) / "herdr/config.toml"
        self.assertEqual(result["config"], str(path))
        commands = tomllib.loads(path.read_text())["keys"]["command"]
        self.assertEqual({row["key"]: row["command"] for row in commands},
                         {key: f"{PLUGIN_ID}.{action}" for key, action in SHORTCUTS.items()})


class MigrationV2Tests(unittest.TestCase):
    def test_v2_preserves_message_evidence_and_does_not_invent_legacy_ack_source(self):
        with tempfile.TemporaryDirectory(prefix="orchestrator-v2-") as directory:
            store = Store(directory)
            store.bind(SOCKET, "coordinator", identity(agent("coordinator")))
            helper = SimpleNamespace(store=store)
            helper.dispatch = lambda *args, **kwargs: fixtures.OrchestratorTests.dispatch(helper, *args, **kwargs)
            first = fixtures.OrchestratorTests.report(helper)
            store.next(SOCKET)
            store.present(SOCKET, first["seq"], "complete result")
            helper.dispatch(accepted=False, parent=first["seq"])
            store.ack(SOCKET, first["seq"], "继续下一条", "/tmp/user-reply.txt")
            second = fixtures.OrchestratorTests.report(helper, helper.dispatch("other"), "second")
            store.db.execute("UPDATE events SET state='done',done_at=30 WHERE seq=?", (second["seq"],))
            for column in ("user_ack_source", "user_ack_context"):
                store.db.execute(f"ALTER TABLE events DROP COLUMN {column}")
            store.db.execute("PRAGMA user_version=2")
            store.db.commit()
            original = store.rows("SELECT * FROM events ORDER BY seq")
            store.close()
            store = Store(directory)
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 3)
            migrated = store.rows("SELECT * FROM events ORDER BY seq")
            self.assertEqual([{key: row[key] for key in original[0]} for row in migrated], original)
            self.assertEqual(migrated[0]["user_ack_source"], "message")
            self.assertIsNone(migrated[0]["user_ack_context"])
            self.assertIsNone(migrated[1]["user_ack_source"])
            store.close()
            store = Store(directory)
            self.assertEqual(store.head(SOCKET)["state"], "awaiting_user")
            self.assertEqual(store.head(SOCKET)["user_reply"], "继续下一条")
            store.close()
