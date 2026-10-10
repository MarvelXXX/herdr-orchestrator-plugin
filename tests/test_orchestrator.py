import json
import multiprocessing
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.runtime import (ENTRY, HerdrError, dispatch_task, identity, poll_once,
                                        require_worker)
from herdr_orchestrator.store import Conflict, Store

SOCKET = "/tmp/orchestrator-test.sock"
SPEC = {"goal": "test", "scope": "temporary files", "authorization": "read-only",
        "acceptance_criteria": "report actual evidence"}


def agent(name, state="idle"):
    return {"name": name, "pane_id": f"w1:{name}", "agent": "codex", "agent_status": state,
            "agent_session": {"kind": "id", "value": f"native-{name}"}}


class FakeHerdr:
    socket = SOCKET

    def __init__(self):
        self.agents = {name: agent(name) for name in ("coordinator", "worker", "other")}
        self.caller_name = "coordinator"
        self.prompts = []
        self.enabled = True
        self.prompt_error = None

    def agent(self, target):
        if target not in self.agents:
            raise HerdrError("missing agent", "agent_not_found")
        return self.agents[target].copy()

    def caller(self):
        return self.agent(self.caller_name)

    def plugin_enabled(self):
        return self.enabled

    def prompt(self, target, text):
        if self.prompt_error:
            raise self.prompt_error
        self.prompts.append((target, text))
        return {"submitted": True}


def concurrent_report(directory, dispatch_id, number):
    store = Store(directory)
    try:
        store.report(dispatch_id, f"message-{number}", "completed", {"result": str(number)})
    finally:
        store.close()


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orch test ")
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.herdr = FakeHerdr()
        self.store.bind(SOCKET, "coordinator", identity(self.herdr.agent("coordinator")))
        self.store.acknowledge_activation(SOCKET)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def dispatch(self, name="worker", accepted=True, parent=None):
        dispatch = self.store.create_dispatch(SOCKET, "task-" + name, name,
                                             identity(agent(name)), SPEC, parent)
        if accepted:
            self.store.accept(dispatch["id"])
        return dispatch

    def report(self, dispatch=None, key="message"):
        dispatch = dispatch or self.dispatch()
        return self.store.report(dispatch["id"], key, "completed", {"result": "evidence"})

    def test_only_head_can_finish_and_next_resumes_it(self):
        first = self.report(key="first")
        second = self.report(self.dispatch("other"), "second")
        self.assertEqual(self.store.next(SOCKET)["seq"], first["seq"])
        self.assertEqual(self.store.next(SOCKET)["seq"], first["seq"])
        with self.assertRaises(Conflict):
            self.store.finish(SOCKET, second["seq"], "summary")
        self.store.finish(SOCKET, first["seq"], "verified result")
        self.assertEqual(self.store.next(SOCKET)["seq"], second["seq"])

    def test_followup_receipt_releases_head_before_worker_finishes(self):
        event = self.report()
        self.store.next(SOCKET)
        followup = self.dispatch(parent=event["seq"], accepted=False)
        with self.assertRaises(Conflict):
            self.store.finish(SOCKET, event["seq"], "follow-up sent")
        self.store.accept(followup["id"])
        self.store.finish(SOCKET, event["seq"], "follow-up explicitly accepted")
        self.assertIsNone(self.store.head(SOCKET))
        self.assertIsNone(self.store.dispatch(followup["id"])["completed_at"])

    def test_parent_dispatch_must_reference_head(self):
        event = self.report()
        with self.assertRaises(Conflict):
            self.dispatch("other", parent=event["seq"] + 1)

    def test_reports_require_explicit_receipt(self):
        dispatch = self.dispatch(accepted=False)
        with self.assertRaises(Conflict):
            self.report(dispatch)
        self.assertIsNone(self.store.head(SOCKET))

    def test_retried_report_is_idempotent_and_conflicting_reuse_fails(self):
        dispatch = self.dispatch()
        event = self.report(dispatch)
        self.assertEqual(self.report(dispatch)["seq"], event["seq"])
        with self.assertRaises(Conflict):
            self.store.report(dispatch["id"], "message", "completed", {"result": "changed"})
        self.assertEqual(len(self.store.status(SOCKET)["pending"]), 1)

    def test_blocked_then_completed_is_two_ordered_reports(self):
        dispatch = self.dispatch()
        blocked = self.store.report(dispatch["id"], "block-1", "blocked", {"result": "need input"})
        completed = self.report(dispatch, "completed")
        self.assertLess(blocked["seq"], completed["seq"])

    def test_restart_keeps_handling_head_receipts_and_followups(self):
        event = self.report()
        self.store.next(SOCKET)
        followup = self.dispatch("other", parent=event["seq"])
        self.store.close()
        self.store = Store(self.directory)
        self.assertEqual(self.store.next(SOCKET)["seq"], event["seq"])
        self.assertIsNotNone(self.store.dispatch(followup["id"])["accepted_at"])
        self.store.finish(SOCKET, event["seq"], "recovered without redispatch")

    def test_concurrent_producers_have_no_skipped_or_invisible_reports(self):
        dispatches = [self.dispatch(f"worker-{number}") for number in range(8)]
        context = multiprocessing.get_context("spawn")
        processes = [context.Process(target=concurrent_report,
                     args=(str(self.directory), item["id"], number))
                     for number, item in enumerate(dispatches)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        pending = self.store.status(SOCKET)["pending"]
        self.assertEqual([item["seq"] for item in pending], list(range(1, 9)))
        self.assertEqual({json.loads(item["payload"])["result"] for item in pending},
                         {str(number) for number in range(8)})
        for item in pending:
            self.assertEqual(self.store.next(SOCKET)["seq"], item["seq"])
            self.store.finish(SOCKET, item["seq"], "processed")
        self.assertIsNone(self.store.head(SOCKET))

    def test_busy_blocked_and_unknown_coordinator_receive_no_prompt(self):
        self.report()
        for state in ("working", "blocked", "unknown"):
            self.herdr.agents["coordinator"]["agent_status"] = state
            self.assertEqual(poll_once(self.store, self.herdr), "coordinator_busy")
        self.assertEqual(self.herdr.prompts, [])

    def test_idle_coordinator_wakes_and_unchanged_head_is_not_spammed(self):
        self.report()
        self.assertEqual(poll_once(self.store, self.herdr, 100), "notified")
        self.assertEqual(poll_once(self.store, self.herdr, 105), "already_notified")
        self.assertEqual(len(self.herdr.prompts), 1)

    def test_receipt_wait_does_not_keep_waking_until_accepted(self):
        event = self.report()
        followup = self.dispatch("other", accepted=False, parent=event["seq"])
        self.assertEqual(poll_once(self.store, self.herdr, 100), "waiting_acceptance")
        self.store.accept(followup["id"])
        self.assertEqual(poll_once(self.store, self.herdr, 105), "notified")

    def test_blocked_followup_wakes_current_head_even_without_receipt(self):
        event = self.report()
        self.store.next(SOCKET)
        self.dispatch("other", accepted=False, parent=event["seq"])
        self.herdr.agents["other"]["agent_status"] = "blocked"
        self.assertEqual(poll_once(self.store, self.herdr, 100), "notified")
        self.assertIn("尚未接收且需要检查", self.herdr.prompts[0][1])
        self.assertEqual(self.store.head(SOCKET)["seq"], event["seq"])
        with self.assertRaises(Conflict):
            self.store.finish(SOCKET, event["seq"], "cannot skip a blocked receipt")
        self.assertEqual(poll_once(self.store, self.herdr, 105), "already_notified")

    def test_ui_blocker_is_reported_once_without_business_completion(self):
        dispatch = self.dispatch(accepted=False)
        self.herdr.agents["worker"]["agent_status"] = "blocked"
        poll_once(self.store, self.herdr, 100)
        poll_once(self.store, self.herdr, 105)
        pending = self.store.status(SOCKET)["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "ui_blocked")
        self.assertIsNone(self.store.dispatch(dispatch["id"])["completed_at"])

    def test_paused_or_disabled_plugin_sends_nothing(self):
        self.report()
        self.herdr.enabled = False
        self.assertEqual(poll_once(self.store, self.herdr), "plugin_disabled")
        self.store.update_session(SOCKET, enabled=0)
        self.assertEqual(poll_once(self.store, self.herdr), "paused")
        self.assertEqual(self.herdr.prompts, [])

    def test_replacement_coordinator_pauses_instead_of_guessing(self):
        self.report()
        self.herdr.agents["coordinator"]["agent_session"]["value"] = "different-session"
        self.assertEqual(poll_once(self.store, self.herdr), "identity_changed")
        self.assertFalse(self.store.session(SOCKET)["enabled"])
        self.assertEqual(self.herdr.prompts, [])

    def test_wrong_worker_cannot_acknowledge_another_dispatch(self):
        dispatch = self.dispatch(accepted=False)
        self.herdr.caller_name = "other"
        with self.assertRaises(Conflict):
            require_worker(self.store, self.herdr, dispatch["id"])

    def test_uncertain_submission_is_recorded_and_not_replayed(self):
        self.herdr.prompt_error = HerdrError("timeout", "transport_uncertain")
        with self.assertRaises(HerdrError):
            dispatch_task(self.store, self.herdr, "worker", SPEC, "task-1")
        with self.assertRaises(Conflict):
            dispatch_task(self.store, self.herdr, "worker", SPEC, "task-1")
        dispatches = self.store.status(SOCKET)["dispatches"]
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(dispatches[0]["delivery"], "uncertain")

    def test_existing_worker_is_reused_without_starting_an_agent(self):
        self.report()
        dispatch = dispatch_task(self.store, self.herdr, "worker", SPEC, "same-task")
        self.assertEqual(dispatch["target"], "worker")
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertIsNone(dispatch["accepted_at"])

    def test_abandoned_attempt_cannot_report_and_does_not_block_reuse(self):
        dispatch = self.dispatch()
        self.store.abandon(dispatch["id"], "confirmed not executing; cancellation requested")
        with self.assertRaises(Conflict):
            self.report(dispatch)
        self.dispatch()


class CliTests(unittest.TestCase):
    def test_injected_worker_commands_run_and_result_survives_source_file_removal(self):
        with tempfile.TemporaryDirectory(prefix="orch cli ") as directory:
            root = Path(directory)
            fake = root / "fake-herdr"
            fake.write_text("#!/usr/bin/env python3\n" + r'''
import json, os, sys
args=sys.argv[1:]
if args[:2] == ['agent','get']:
    name=args[2].replace('w1:','')
    result={'agent':{'name':name,'pane_id':'w1:'+name,'agent':'codex','agent_status':'idle',
        'agent_session':{'kind':'id','value':'native-'+name}}}
else:
    result={'submitted':True}
print(json.dumps({'result':result}))
''', encoding="utf-8")
            fake.chmod(0o755)
            env = {key: value for key, value in os.environ.items() if not key.startswith("HERDR_")}
            env.update(HERDR_ENV="1", HERDR_PANE_ID="w1:worker", HERDR_SOCKET_PATH=SOCKET,
                       HERDR_BIN_PATH=str(fake))
            store = Store(root / "state")
            store.bind(SOCKET, "coordinator", identity(agent("coordinator")))
            herdr = FakeHerdr()
            dispatch = dispatch_task(store, herdr, "worker", SPEC, "test-task")
            prompt = herdr.prompts[0][1]
            accept_command = prompt.splitlines()[2].split("：", 1)[1]
            result = subprocess.run(shlex.split(accept_command), env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            file = root / "result.md"
            file.write_text("已验证的结果", encoding="utf-8")
            command = [sys.executable, str(ENTRY), "--state-dir", str(root / "state"),
                       "--socket", SOCKET, "report", "--dispatch", dispatch["id"],
                       "--message-id", "completion-1", "--status", "completed",
                       "--result-file", str(file)]
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            file.unlink()
            self.assertEqual(json.loads(store.head(SOCKET)["payload"])["result"], "已验证的结果")
            store.close()


if __name__ == "__main__":
    unittest.main()
