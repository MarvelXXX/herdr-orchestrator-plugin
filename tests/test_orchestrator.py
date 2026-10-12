import json
import multiprocessing
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.runtime import (ENTRY, HerdrError, dispatch_task, identity, poll_once,
                                        require_coordinator, require_worker)
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
        self.markers = {}

    def agent(self, target):
        record = self.agents.get(target) or next(
            (item for item in self.agents.values() if item["pane_id"] == target), None)
        if record is None:
            raise HerdrError("missing agent", "agent_not_found")
        return record.copy()

    def caller(self):
        return self.agent(self.caller_name)

    def call(self, *args):
        if args == ("agent", "list"):
            return {"agents": [item.copy() for item in self.agents.values()]}
        raise HerdrError("unsupported test call")

    def request(self, method, params):
        if method == "pane.report_metadata":
            if params.get("clear_display_agent"):
                self.markers.pop(params["pane_id"], None)
            else:
                self.markers[params["pane_id"]] = params["display_agent"]
            return {"ok": True}
        raise HerdrError("unsupported test request")

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

    def present(self, event, summary="verified result"):
        self.store.next(SOCKET)
        return self.store.present(SOCKET, event["seq"], summary)

    def test_only_head_can_be_presented_or_acknowledged_and_next_resumes_it(self):
        first = self.report(key="first")
        second = self.report(self.dispatch("other"), "second")
        self.assertEqual(self.store.next(SOCKET)["seq"], first["seq"])
        self.assertEqual(self.store.next(SOCKET)["seq"], first["seq"])
        with self.assertRaises(Conflict):
            self.store.present(SOCKET, second["seq"], "summary")
        with self.assertRaises(Conflict):
            self.store.ack(SOCKET, first["seq"], "这条处理完了")
        presented = self.present(first)
        self.assertEqual(self.store.next(SOCKET), presented)
        with self.assertRaises(Conflict):
            self.store.ack(SOCKET, second["seq"], "继续下一条")
        self.assertEqual(self.store.head(SOCKET)["seq"], first["seq"])
        self.store.ack(SOCKET, first["seq"], "这条处理完了")
        self.assertEqual(self.store.next(SOCKET)["seq"], second["seq"])

    def test_followup_receipt_releases_head_before_worker_finishes(self):
        event = self.report()
        self.present(event)
        followup = self.dispatch(parent=event["seq"], accepted=False)
        confirmed = self.store.ack(SOCKET, event["seq"], "这条处理完了，继续下一条")
        self.assertEqual(confirmed["state"], "awaiting_user")
        self.assertIsNotNone(confirmed["user_acked_at"])
        self.store.accept(followup["id"])
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
        self.store.close()
        self.store = Store(self.directory)
        self.assertEqual(self.store.next(SOCKET)["seq"], event["seq"])
        self.present(event)
        followup = self.dispatch(parent=event["seq"])
        self.assertIsNotNone(self.store.dispatch(followup["id"])["accepted_at"])
        self.assertEqual(self.store.head(SOCKET)["state"], "awaiting_user")
        self.store.ack(SOCKET, event["seq"], "这条处理完了")

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
            self.present(item)
            self.store.ack(SOCKET, item["seq"], "这条处理完了")
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
        second = self.report(self.dispatch("other"), "second")
        self.present(event)
        followup = self.dispatch(accepted=False, parent=event["seq"])
        self.store.ack(SOCKET, event["seq"], "继续下一条")
        self.assertEqual(poll_once(self.store, self.herdr, 100), "waiting_acceptance")
        self.store.accept(followup["id"])
        self.assertEqual(poll_once(self.store, self.herdr, 105), "notified")
        self.assertEqual(self.store.head(SOCKET)["seq"], second["seq"])

    def test_unconfirmed_head_stays_silent_but_confirmed_receipt_blocker_wakes_it(self):
        event = self.report()
        second = self.report(self.dispatch("other"), "second")
        self.present(event)
        followup = self.dispatch(accepted=False, parent=event["seq"])
        self.herdr.agents["worker"]["agent_status"] = "blocked"
        self.assertEqual(poll_once(self.store, self.herdr, 100), "awaiting_user")
        self.assertEqual(self.store.head(SOCKET)["seq"], event["seq"])
        self.store.ack(SOCKET, event["seq"], "这条处理完了")
        self.assertEqual(poll_once(self.store, self.herdr, 165), "notified")
        self.assertEqual(poll_once(self.store, self.herdr, 170), "already_notified")
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertIsNone(self.store.dispatch(followup["id"])["accepted_at"])
        self.assertEqual(self.store.head(SOCKET)["seq"], event["seq"])
        self.assertEqual(self.store.head(SOCKET)["state"], "awaiting_user")
        self.assertIsNotNone(self.store.head(SOCKET)["user_acked_at"])
        pending = self.store.status(SOCKET)["pending"]
        self.assertEqual(pending[1]["seq"], second["seq"])
        self.assertEqual(pending[1]["state"], "pending")
        self.assertEqual(pending[2]["kind"], "ui_blocked")

    def test_two_arrivals_present_only_first_until_user_ack_then_poll_wakes_second(self):
        first = self.report(key="first")
        second = self.report(self.dispatch("other"), "second")
        self.assertEqual(poll_once(self.store, self.herdr, 100), "notified")
        displayed = self.present(first)
        self.assertIsNotNone(displayed["presented_at"])
        for now in (105, 161, 1000):
            self.assertEqual(poll_once(self.store, self.herdr, now), "awaiting_user")
            self.assertEqual(self.store.next(SOCKET), displayed)
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(self.store.status(SOCKET)["pending"][1]["state"], "pending")
        self.store.ack(SOCKET, first["seq"], "这条处理完了，继续下一条")
        self.assertEqual(len(self.herdr.prompts), 1)
        self.assertEqual(poll_once(self.store, self.herdr, 1005), "notified")
        self.assertIn(f"#{second['seq']}", self.herdr.prompts[1][1])
        self.assertEqual(self.store.head(SOCKET)["state"], "pending")

    def test_acceptance_alone_is_not_user_acknowledgment(self):
        event = self.report()
        self.present(event)
        self.dispatch(parent=event["seq"])
        self.assertIsNone(self.store.head(SOCKET)["user_acked_at"])
        self.assertEqual(poll_once(self.store, self.herdr, 100), "awaiting_user")
        self.assertEqual(self.store.head(SOCKET)["seq"], event["seq"])

    def test_ack_before_accept_survives_pause_restart_and_needs_no_second_user_reply(self):
        event = self.report()
        second = self.report(self.dispatch("other"), "second")
        self.present(event)
        followup = self.dispatch(parent=event["seq"], accepted=False)
        reply = "这条处理完了，继续下一条\n"
        confirmed = self.store.ack(SOCKET, event["seq"], reply)
        self.store.update_session(SOCKET, enabled=0)
        self.store.close()
        self.store = Store(self.directory)
        self.assertEqual(self.store.next(SOCKET), confirmed)
        self.assertEqual(poll_once(self.store, self.herdr, 100), "paused")
        self.store.bind(SOCKET, "coordinator", identity(agent("coordinator")))
        self.assertEqual(poll_once(self.store, self.herdr, 200), "waiting_acceptance")
        self.store.accept(followup["id"])
        finished = self.store.one("SELECT * FROM events WHERE seq=?", (event["seq"],))
        self.assertEqual(finished["state"], "done")
        self.assertEqual(finished["user_reply"], reply)
        self.assertEqual(finished["user_acked_at"], confirmed["user_acked_at"])
        self.assertEqual(poll_once(self.store, self.herdr, 205), "notified")
        self.assertEqual(self.store.head(SOCKET)["seq"], second["seq"])
        self.assertIsNone(self.store.dispatch(followup["id"])["completed_at"])

    def test_duplicate_and_conflicting_ack_do_not_modify_or_release_another_head(self):
        event = self.report()
        second = self.report(self.dispatch("other"), "second")
        self.present(event)
        followup = self.dispatch(parent=event["seq"], accepted=False)
        confirmed = self.store.ack(SOCKET, event["seq"], "继续下一条")
        self.assertEqual(self.store.ack(SOCKET, event["seq"], "继续下一条"), confirmed)
        with self.assertRaises(Conflict):
            self.store.ack(SOCKET, event["seq"], "different reply")
        with self.assertRaises(Conflict):
            self.dispatch(parent=event["seq"])
        self.store.accept(followup["id"])
        finished = self.store.ack(SOCKET, event["seq"], "继续下一条")
        self.assertEqual(finished["state"], "done")
        self.assertEqual(finished["user_acked_at"], confirmed["user_acked_at"])
        self.assertEqual(self.store.head(SOCKET)["seq"], second["seq"])

    def test_present_is_idempotent_but_cannot_replace_displayed_summary(self):
        event = self.report()
        presented = self.present(event)
        self.assertEqual(self.store.present(SOCKET, event["seq"], "verified result"), presented)
        with self.assertRaises(Conflict):
            self.store.present(SOCKET, event["seq"], "different summary")
        with self.assertRaises(Conflict):
            self.store.ack(SOCKET, event["seq"], " \n")
        self.assertEqual(self.store.head(SOCKET), presented)

    def test_followups_allow_multiple_specialist_workers_before_presentation(self):
        event = self.report()
        first = self.dispatch(parent=event["seq"])
        second = self.dispatch("other", parent=event["seq"], accepted=False)
        self.present(event)
        self.store.ack(SOCKET, event["seq"], "继续下一条")
        self.assertEqual(self.store.head(SOCKET)["state"], "awaiting_user")
        self.store.accept(second["id"])
        self.assertIsNone(self.store.head(SOCKET))
        self.assertIsNone(self.store.dispatch(first["id"])["completed_at"])
        self.assertIsNone(self.store.dispatch(second["id"])["completed_at"])

    def test_abandonment_releases_acknowledged_head_but_does_not_confirm_for_user(self):
        event = self.report()
        self.present(event)
        followup = self.dispatch(parent=event["seq"], accepted=False)
        self.store.abandon(followup["id"], "confirmed cancelled")
        self.assertEqual(self.store.head(SOCKET)["state"], "awaiting_user")
        followup = self.dispatch(parent=event["seq"], accepted=False)
        self.store.ack(SOCKET, event["seq"], "继续下一条")
        self.store.abandon(followup["id"], "confirmed cancelled")
        self.assertIsNone(self.store.head(SOCKET))

    def test_worker_or_other_session_cannot_record_user_confirmation(self):
        event = self.report()
        self.present(event)
        self.herdr.caller_name = "worker"
        with self.assertRaises(Conflict):
            require_coordinator(self.store, self.herdr)
        other_socket = "/tmp/other-orchestrator.sock"
        self.store.bind(other_socket, "coordinator", identity(agent("coordinator")))
        with self.assertRaises(Conflict):
            self.store.ack(other_socket, event["seq"], "继续下一条")
        self.assertEqual(self.store.head(SOCKET)["state"], "awaiting_user")

    def test_paused_reopened_waiting_user_is_not_redisplayed(self):
        event = self.report()
        self.report(self.dispatch("other"), "second")
        presented = self.present(event)
        self.store.update_session(SOCKET, enabled=0)
        self.store.close()
        self.store = Store(self.directory)
        self.store.bind(SOCKET, "coordinator", identity(agent("coordinator")))
        self.assertEqual(self.store.next(SOCKET), presented)
        self.assertEqual(poll_once(self.store, self.herdr, 1000), "awaiting_user")
        self.assertEqual(self.herdr.prompts, [])

    def test_explicit_rebind_activates_new_coordinator_without_redisplaying_waiting_head(self):
        event = self.report()
        presented = self.present(event)
        self.store.update_session(SOCKET, enabled=0)
        self.herdr.agents["coordinator"]["agent_session"]["value"] = "new-coordinator-session"
        self.store.rebind(SOCKET, "coordinator", identity(self.herdr.agent("coordinator")))
        self.assertEqual(poll_once(self.store, self.herdr, 100), "notified")
        self.assertFalse(self.store.session(SOCKET)["activated"])
        self.assertEqual(self.store.head(SOCKET), presented)
        self.store.acknowledge_activation(SOCKET)
        self.assertEqual(poll_once(self.store, self.herdr, 200), "awaiting_user")
        self.assertEqual(len(self.herdr.prompts), 1)

    def test_missing_or_replaced_followup_wakes_only_confirmed_head(self):
        for state in ("missing", "replaced"):
            with self.subTest(state=state):
                event = self.report(key="report-" + state)
                self.present(event)
                followup = self.dispatch(parent=event["seq"], accepted=False)
                if state == "missing":
                    del self.herdr.agents["worker"]
                else:
                    self.herdr.agents["worker"]["agent_session"]["value"] = "replacement"
                self.assertEqual(poll_once(self.store, self.herdr, 100), "awaiting_user")
                self.store.ack(SOCKET, event["seq"], "这条处理完了")
                self.assertEqual(poll_once(self.store, self.herdr, 200), "notified")
                self.assertEqual(self.store.dispatch(followup["id"])["ui_status"], state)
                self.assertEqual(self.store.next(SOCKET)["state"], "awaiting_user")
                self.assertIsNone(self.store.head(SOCKET)["done_at"])
                self.assertIsNotNone(self.store.head(SOCKET)["user_acked_at"])
                self.store.abandon(followup["id"], "inspected and cancelled")
                self.herdr.agents["worker"] = agent("worker")
                ui_event = self.store.next(SOCKET)
                self.present(ui_event)
                self.store.ack(SOCKET, ui_event["seq"], "这条处理完了")

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
            first = store.head(SOCKET)
            other = store.create_dispatch(SOCKET, "other-task", "other", identity(agent("other")), SPEC)
            store.accept(other["id"])
            second = store.report(other["id"], "completion-2", "completed", {"result": "second"})
            prefix = [sys.executable, str(ENTRY), "--state-dir", str(root / "state"), "--socket", SOCKET]
            env["HERDR_PANE_ID"] = "w1:coordinator"

            def run(*args, expected=0):
                result = subprocess.run([*prefix, *args], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr)
                return json.loads(result.stdout)["data"] if expected == 0 else None

            summary = root / "summary.txt"
            summary.write_text("实际结果、证据及限制", encoding="utf-8")
            reply = root / "reply.txt"
            reply.write_text("这条处理完了，继续下一条\n", encoding="utf-8")
            run("finish", "--event", str(first["seq"]), "--summary-file", str(summary), expected=2)
            run("ack", "--event", str(first["seq"]), "--reply-file", str(reply), expected=1)
            run("next")
            presented = run("present", "--event", str(first["seq"]), "--summary-file", str(summary))
            self.assertEqual(presented["state"], "awaiting_user")
            self.assertEqual(run("next")["state"], "awaiting_user")
            run("ack", "--event", str(second["seq"]), "--reply-file", str(reply), expected=1)
            run("ack", "--event", str(first["seq"]), "--reply-file", "relative.txt", expected=1)
            task_file = root / "task.json"
            task_file.write_text(json.dumps(SPEC), encoding="utf-8")
            followup = run("dispatch", "--target", "worker", "--task-file", str(task_file),
                           "--task-id", "test-task", "--parent-event", str(first["seq"]))
            env["HERDR_PANE_ID"] = "w1:worker"
            run("ack", "--event", str(first["seq"]), "--reply-file", str(reply), expected=1)
            env["HERDR_PANE_ID"] = "w1:coordinator"
            confirmed = run("ack", "--event", str(first["seq"]), "--reply-file", str(reply))
            self.assertEqual(confirmed["state"], "awaiting_user")
            self.assertEqual(confirmed["user_reply"], reply.read_text(encoding="utf-8"))
            self.assertEqual(confirmed["user_reply_file"], str(reply.resolve()))
            self.assertEqual(run("ack", "--event", str(first["seq"]), "--reply-file", str(reply)), confirmed)
            run("dispatch", "--target", "worker", "--task-file", str(task_file),
                "--parent-event", str(first["seq"]), expected=1)
            env["HERDR_PANE_ID"] = "w1:worker"
            run("accept", "--dispatch", followup["id"])
            env["HERDR_PANE_ID"] = "w1:coordinator"
            finished = run("ack", "--event", str(first["seq"]), "--reply-file", str(reply))
            self.assertEqual(finished["state"], "done")
            self.assertEqual(finished["user_acked_at"], confirmed["user_acked_at"])
            reply.write_text("冲突确认", encoding="utf-8")
            run("ack", "--event", str(first["seq"]), "--reply-file", str(reply), expected=1)
            summary.unlink()
            reply.unlink()
            persisted = store.one("SELECT * FROM events WHERE seq=?", (first["seq"],))
            self.assertEqual(persisted["summary"], "实际结果、证据及限制")
            self.assertEqual(persisted["user_reply"], "这条处理完了，继续下一条\n")
            self.assertEqual(store.head(SOCKET)["seq"], second["seq"])
            store.close()


class MigrationTests(unittest.TestCase):
    def test_v1_migration_preserves_history_unfinished_head_and_followup_receipts_on_reopen(self):
        with tempfile.TemporaryDirectory(prefix="orch v1 ") as directory:
            db = sqlite3.connect(Path(directory) / "state.sqlite3")
            db.row_factory = sqlite3.Row
            db.executescript("""
                CREATE TABLE sessions (
                    socket TEXT PRIMARY KEY, coordinator TEXT NOT NULL, identity TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, activated INTEGER NOT NULL DEFAULT 0,
                    wake_key TEXT, wake_at REAL, error TEXT, pid INTEGER, tick_at REAL
                );
                CREATE TABLE dispatches (
                    id TEXT PRIMARY KEY, socket TEXT NOT NULL REFERENCES sessions(socket),
                    task_id TEXT NOT NULL, target TEXT NOT NULL, identity TEXT NOT NULL,
                    spec TEXT NOT NULL, delivery TEXT NOT NULL DEFAULT 'uncertain',
                    accepted_at REAL, completed_at REAL, abandoned_at REAL, abandon_reason TEXT,
                    ui_status TEXT, parent_seq INTEGER REFERENCES events(seq), created_at REAL NOT NULL
                );
                CREATE TABLE events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, socket TEXT NOT NULL REFERENCES sessions(socket),
                    message_id TEXT NOT NULL, dispatch_id TEXT NOT NULL REFERENCES dispatches(id),
                    kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    summary TEXT, created_at REAL NOT NULL, done_at REAL, UNIQUE(socket, message_id)
                );
                CREATE INDEX events_pending ON events(socket, state, seq);
                PRAGMA user_version=1;
            """)
            db.execute("INSERT INTO sessions(socket,coordinator,identity,activated) VALUES(?,?,?,1)",
                       (SOCKET, "coordinator", identity(agent("coordinator"))))
            for number, state in enumerate(("done", "handling", "pending"), 1):
                db.execute("""INSERT INTO dispatches
                    (id,socket,task_id,target,identity,spec,accepted_at,completed_at,created_at)
                    VALUES(?,?,?,?,?,?,10,20,5)""", (str(number), SOCKET, "task-" + str(number), "worker",
                                                   identity(agent("worker")), json.dumps(SPEC)))
                db.execute("""INSERT INTO events
                    (socket,message_id,dispatch_id,kind,payload,state,summary,created_at,done_at)
                    VALUES(?,?,?,'completed',?,?,?,20,?)""", (SOCKET, "old-" + str(number), str(number),
                    json.dumps({"result": "old evidence " + str(number)}), state,
                    "历史摘要" if state == "done" else None, 30 if state == "done" else None))
            db.execute("""INSERT INTO dispatches
                (id,socket,task_id,target,identity,spec,parent_seq,created_at)
                VALUES('followup',?,?,?,?,?,2,25)""", (SOCKET, "task-2", "worker",
                                                      identity(agent("worker")), json.dumps(SPEC)))
            db.commit()
            before = {table: [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
                      for table in ("sessions", "dispatches", "events")}
            db.close()
            store = Store(directory)
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 4)
            for table, original_rows in before.items():
                after = store.rows(f"SELECT * FROM {table}")
                self.assertEqual([{key: row[key] for key in original_rows[0]} for row in after], original_rows)
            self.assertEqual(store.head(SOCKET)["state"], "handling")
            self.assertEqual(store.next(SOCKET)["seq"], 2)
            self.assertIsNone(store.head(SOCKET)["user_acked_at"])
            store.present(SOCKET, 2, "迁移后实际展示")
            confirmed = store.ack(SOCKET, 2, "这条处理完了")
            self.assertEqual(confirmed["state"], "awaiting_user")
            store.close()
            store = Store(directory)
            self.assertEqual(store.next(SOCKET), confirmed)
            herdr = FakeHerdr()
            self.assertEqual(poll_once(store, herdr, 100), "waiting_acceptance")
            store.accept("followup")
            self.assertEqual(store.head(SOCKET)["seq"], 3)
            self.assertEqual(store.head(SOCKET)["state"], "pending")
            self.assertIsNone(store.dispatch("followup")["completed_at"])
            historical = store.one("SELECT * FROM events WHERE seq=1")
            self.assertEqual(historical["summary"], "历史摘要")
            self.assertIsNone(historical["user_acked_at"])
            self.assertEqual(poll_once(store, herdr, 105), "notified")
            store.close()


if __name__ == "__main__":
    unittest.main()
