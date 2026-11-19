from pathlib import Path
from datetime import datetime, timezone
import tempfile
import unittest

from demo import sandbox
from demo.workbench import Workbench
from threadplan import InputError


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.clock = 1000
        self.path = Path(directory.name)
        self.app = Workbench(self.path, use_archive=False, clock=lambda: self.clock)
        self.owner = self.app.principal(self.app.create_session())

    def command_for(self, scenario):
        task = self.app.create_task(self.owner, scenario)
        activity = next(item for item in task["items"] if "operation" in item)
        command = self.app.prepare(self.owner, task["id"], activity["activity_ref"])
        return task["id"], command["id"]

    def mutations(self, task_id):
        _, calendar = self.app._runtime(self.app._task(self.owner, task_id))
        return calendar.mutations()

    def test_prepare_is_read_only_and_confirm_persists_original_receipt(self):
        task_id, command = self.command_for("reschedule")
        self.assertEqual(self.mutations(task_id), 0)
        self.assertEqual(self.app.command(self.owner, task_id, command, "confirm")["state"], "SUCCEEDED")
        reopened = Workbench(self.path, use_archive=False, clock=lambda: self.clock)
        self.assertEqual(reopened.command(self.owner, task_id, command, "confirm")["state"], "SUCCEEDED")
        calendar = reopened.task_view(self.owner, task_id)["calendar"]
        self.assertEqual(calendar[0]["fields"]["location"], "会议室 A")
        self.assertEqual(calendar[0]["fields"]["start_at"], "2026-11-20T07:30:00+00:00")
        self.assertEqual(self.mutations(task_id), 1)

    def test_calendar_snapshots_include_real_local_changes_only_after_confirmation(self):
        for scenario, expected_before, expected_after in (
            ("create", [], ["2026-11-20T08:00:00+00:00"]),
            ("reschedule", ["2026-11-20T06:00:00+00:00"], ["2026-11-20T07:30:00+00:00"]),
            ("cancel", ["2026-11-20T06:00:00+00:00"], []),
        ):
            with self.subTest(scenario=scenario):
                task, command = self.command_for(scenario)
                prepared = self.app.task_view(self.owner, task)
                self.assertEqual(prepared["initial_calendar"], prepared["calendar"])
                self.assertEqual(self.app.command(self.owner, task, command, "confirm")["state"], "SUCCEEDED")
                committed = self.app.task_view(self.owner, task)
                dates = lambda snapshot: [event["fields"]["start_at"] for event in snapshot]
                self.assertEqual(dates(committed["initial_calendar"]), expected_before)
                self.assertEqual(dates(committed["calendar"]), expected_after)
                if expected_before and expected_after:
                    self.assertEqual(committed["initial_calendar"][0]["id"], committed["calendar"][0]["id"])

    def test_same_scenario_command_can_be_confirmed_in_two_separate_tasks(self):
        first, command = self.command_for("reschedule")
        second, repeated = self.command_for("reschedule")
        self.assertEqual(command, repeated)
        self.assertEqual(self.app.command(self.owner, first, command, "confirm")["state"], "SUCCEEDED")
        self.assertEqual(self.mutations(first), 1)
        self.assertEqual(self.mutations(second), 0)
        self.assertEqual(self.app.command(self.owner, second, repeated, "confirm")["state"], "SUCCEEDED")
        self.assertEqual(self.mutations(second), 1)

    def test_unknown_and_pending_use_original_read_only_recovery(self):
        for case, state, outcome in (
            ("lost-ack", "OUTCOME_UNKNOWN", "SUCCEEDED"),
            ("pending-verification", "VERIFY_PENDING", "SUCCEEDED"),
            ("unknown-create", "OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN"),
        ):
            with self.subTest(case=case):
                task, command = self.command_for(case)
                self.assertEqual(self.app.command(self.owner, task, command, "confirm")["state"], state)
                self.assertEqual(self.app.command(self.owner, task, command, "confirm")["state"], state)
                recovered = self.app.command(self.owner, task, command, "recover")
                self.assertEqual(recovered["state"], outcome)
                if case == "lost-ack":
                    self.assertIs(recovered["acknowledged"], False)
                self.assertEqual(self.mutations(task), 1)

    def test_clarification_has_new_message_and_exact_selected_target(self):
        task = self.app.create_task(self.owner, "choose")
        identifier = task["id"]
        self.assertEqual([item["decision"] for item in task["items"]], ["WAIT"])
        self.assertEqual(task["choices"], ["会议室 A", "会议室 B"])
        old = self.app._task(self.owner, identifier)["request"]
        complete = self.app.clarify(self.owner, identifier, "会议室 B")
        self.assertNotEqual(old, self.app._task(self.owner, identifier)["request"])
        self.assertEqual(complete["messages"][:-1], task["messages"])
        self.assertEqual(complete["items"][0]["operation"]["event_ref"], "c2")
        command = self.app.prepare(self.owner, identifier, complete["items"][0]["activity_ref"])
        self.assertEqual(self.app.command(self.owner, identifier, command["id"], "confirm")["state"], "SUCCEEDED")
        self.assertEqual([event["fields"]["start_at"] for event in self.app.task_view(self.owner, identifier)["calendar"]],
                         ["2026-11-20T06:00:00+00:00", "2026-11-20T07:30:00+00:00"])

    def test_partial_and_failed_reads_do_not_create_commands(self):
        for case in ("partial", "failed-read", "unsupported"):
            with self.subTest(case=case):
                task = self.app.create_task(self.owner, case)
                self.assertTrue(task["items"])
                self.assertFalse(any("operation" in item for item in task["items"]))
                self.assertEqual(self.mutations(task["id"]), 0)

    def test_other_sessions_reset_and_expiry_cannot_confirm(self):
        task, command = self.command_for("reschedule")
        other = self.app.principal(self.app.create_session())
        with self.assertRaisesRegex(LookupError, "task_not_found"):
            self.app.command(other, task, command, "confirm")
        self.clock += 1801
        with self.assertRaisesRegex(ValueError, "approval_expired"):
            self.app.command(self.owner, task, command, "confirm")
        new = self.app.restart(self.owner, task)
        self.assertNotEqual(new["id"], task)
        with self.assertRaisesRegex(ValueError, "approval_source_changed"):
            self.app.command(self.owner, task, command, "confirm")
        self.assertEqual(self.mutations(task), 0)

    def test_replay_server_cannot_submit_a_live_model_job(self):
        with self.assertRaisesRegex(ValueError, "sandbox_disabled"):
            self.app.start_sandbox(self.owner, {"constructed": True, "note": "合成安排", "events": []})

    def test_sandbox_requires_consent_and_valid_source_metadata(self):
        now = datetime(2026, 11, 19, 2, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "constructed_input_confirmation_required"):
            sandbox.create_input({"note": "合成安排"}, now)
        data = {"constructed": True, "note": "合成安排", "events": []}
        draft = sandbox.create_input(data, now)
        updated = sandbox.followup(draft | {}, "会议室 B", now)
        self.assertEqual(draft["intake"]["messages"], updated["intake"]["messages"][:-1])
        self.assertEqual(draft["intake"]["source"], updated["intake"]["source"])
        self.assertNotEqual(draft["intake"]["request_id"], updated["intake"]["request_id"])
        invalid = {**draft["intake"], "messages": [{"text": "只有正文"}]}
        with self.assertRaises(InputError):
            sandbox.create_input({**data, "intake": invalid}, now)
        data["events"] = [{"title": "合成评审", "start_at": "2026-11-20T14:00:00", "end_at": "2026-11-20T15:00:00"}]
        with self.assertRaisesRegex(ValueError, "invalid_simulated_event_time"):
            sandbox.create_input(data, now)
