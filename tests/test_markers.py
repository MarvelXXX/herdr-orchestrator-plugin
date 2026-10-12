import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_orchestrator as fixtures
from herdr_orchestrator import PLUGIN_ID
from herdr_orchestrator.cli import execute, parser
from herdr_orchestrator.runtime import (HerdrError, coordinator_marker, identity,
                                        perform_action, poll_once, refresh_coordinator_marker)
from herdr_orchestrator.store import Store


class MarkerTests(unittest.TestCase):
    dispatch = fixtures.OrchestratorTests.dispatch
    report = fixtures.OrchestratorTests.report
    present = fixtures.OrchestratorTests.present

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orchestrator-marker-")
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.herdr = fixtures.FakeHerdr()
        self.store.bind(fixtures.SOCKET, "coordinator", identity(self.herdr.agent("coordinator")))
        self.store.acknowledge_activation(fixtures.SOCKET)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def action(self, name, target="coordinator"):
        self.herdr.env = {"HERDR_PLUGIN_ID": PLUGIN_ID, "HERDR_PLUGIN_ACTION_ID": name,
                          "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(
                              {"focused_pane_id": self.herdr.agent(target)["pane_id"]})}
        with patch("herdr_orchestrator.runtime.spawn_daemon", return_value={"running_pid": 1}):
            return perform_action(self.store, self.herdr, name)

    def cli(self, *command):
        args = parser().parse_args(["--state-dir", str(self.directory), "--socket", fixtures.SOCKET, *command])
        with patch("herdr_orchestrator.cli.Herdr", return_value=self.herdr), patch(
                "herdr_orchestrator.cli.spawn_daemon", return_value={"running_pid": 1}):
            return execute(args)

    def test_marker_is_display_only_guarded_and_owned_by_plugin(self):
        agent = self.herdr.agents["coordinator"]
        agent["agent_session"]["source"] = "herdr:codex"
        original = json.loads(json.dumps(agent))
        self.herdr.request = Mock(wraps=self.herdr.request)
        coordinator_marker(self.herdr, agent)
        method, params = self.herdr.request.call_args.args
        self.assertEqual(method, "pane.report_metadata")
        self.assertEqual(params["pane_id"], agent["pane_id"])
        self.assertEqual(params["source"], PLUGIN_ID)
        self.assertEqual(params["agent"], "codex")
        self.assertGreater(params["ttl_ms"], 5000)
        self.assertEqual(agent, original)
        self.assertEqual(self.herdr.prompts, [])

    def test_waiting_head_and_busy_coordinator_still_get_marker_without_wake(self):
        event = self.present(self.report())
        self.herdr.agents["coordinator"]["agent_status"] = "working"
        before = self.store.status(fixtures.SOCKET)
        self.assertEqual(poll_once(self.store, self.herdr), "awaiting_user")
        self.assertIn("w1:coordinator", self.herdr.markers)
        self.assertEqual(self.store.status(fixtures.SOCKET), before)
        self.assertEqual(self.store.head(fixtures.SOCKET), event)
        self.assertEqual(self.herdr.prompts, [])

    def test_native_session_source_without_lifecycle_authority_still_renders_marker(self):
        event = self.present(self.report())
        self.herdr.agents["coordinator"]["agent_session"]["source"] = "herdr:codex"
        request = self.herdr.request

        def detect_only_metadata(method, params):
            # Herdr accepts but hides presentation guarded by an absent lifecycle authority.
            if params.get("applies_to_source") is not None:
                return {"ok": True}
            return request(method, params)

        self.herdr.request = detect_only_metadata
        self.assertEqual(poll_once(self.store, self.herdr), "awaiting_user")
        self.assertIn("w1:coordinator", self.herdr.markers)
        self.assertEqual(self.store.head(fixtures.SOCKET), event)
        self.assertEqual(self.herdr.prompts, [])

    def test_switch_clears_old_marker_and_marks_new_without_changing_queue(self):
        event = self.present(self.report())
        coordinator_marker(self.herdr, self.herdr.agent("coordinator"))
        self.action("switch", "other")
        self.assertEqual(set(self.herdr.markers), {"w1:other"})
        self.assertEqual(self.store.session(fixtures.SOCKET)["coordinator"], "other")
        self.assertEqual(self.store.head(fixtures.SOCKET), event)
        self.action("switch", "other")
        self.assertEqual(set(self.herdr.markers), {"w1:other"})

    def test_pause_preserves_binding_and_marker_without_expiry_and_can_resume(self):
        event = self.present(self.report())
        self.herdr.request = Mock(wraps=self.herdr.request)
        self.action("stop")
        self.assertNotIn("ttl_ms", self.herdr.request.call_args.args[1])
        self.assertIn("w1:coordinator", self.herdr.markers)
        self.assertFalse(self.store.session(fixtures.SOCKET)["enabled"])
        self.assertEqual(self.store.head(fixtures.SOCKET), event)
        self.action("start", "other")
        self.assertIn("ttl_ms", self.herdr.request.call_args.args[1])
        self.assertEqual(self.store.session(fixtures.SOCKET)["coordinator"], "coordinator")
        self.assertEqual(self.store.head(fixtures.SOCKET), event)

    def test_identity_change_removes_marker_and_pauses_without_confirming(self):
        event = self.present(self.report())
        coordinator_marker(self.herdr, self.herdr.agent("coordinator"))
        self.herdr.agents["coordinator"]["agent_session"]["value"] = "replacement"
        self.assertEqual(poll_once(self.store, self.herdr), "identity_changed")
        self.assertEqual(self.herdr.markers, {})
        self.assertFalse(self.store.session(fixtures.SOCKET)["enabled"])
        self.assertEqual(self.store.head(fixtures.SOCKET), event)

    def test_cli_bind_stop_rebind_and_paused_startup_sync_marker(self):
        self.cli("bind", "--target", "coordinator")
        self.assertEqual(set(self.herdr.markers), {"w1:coordinator"})
        self.cli("stop")
        self.herdr.markers.clear()
        self.assertEqual(self.cli("startup"), {"resumed": False})
        self.assertEqual(set(self.herdr.markers), {"w1:coordinator"})
        self.cli("bind", "--target", "other", "--rebind")
        self.assertEqual(set(self.herdr.markers), {"w1:other"})

    def test_missing_agent_skips_marker_and_other_errors_propagate(self):
        before = self.store.status(fixtures.SOCKET)
        for code in ("agent_not_found", "pane_not_found", "transport_uncertain"):
            with self.subTest(code=code):
                self.herdr.agent = Mock(side_effect=HerdrError("agent lookup failed", code))
                if code == "transport_uncertain":
                    with self.assertRaises(HerdrError):
                        refresh_coordinator_marker(self.store, self.herdr)
                else:
                    refresh_coordinator_marker(self.store, self.herdr)
                self.assertEqual(self.store.status(fixtures.SOCKET), before)

    def test_pause_without_native_session_clears_stale_marker_and_keeps_reports(self):
        event = self.present(self.report())
        coordinator_marker(self.herdr, self.herdr.agent("coordinator"))
        self.herdr.agents["coordinator"].pop("agent_session")
        self.action("stop")
        self.assertEqual(self.herdr.markers, {})
        self.assertFalse(self.store.session(fixtures.SOCKET)["enabled"])
        self.assertEqual(self.store.head(fixtures.SOCKET), event)
