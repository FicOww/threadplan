"""Real SQLite + real FeishuHost logic over explicit FakeCalendar, not live I/O."""

from copy import deepcopy
from datetime import datetime, timezone
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from threadplan import UpstreamContext, Proposal
from threadplan.adapters.feishu import FeishuError
from threadplan_host import ConversationPlanner, PlanningExecutor, PlanningStore
from threadplan_host.feishu import FeishuHost
from test_feishu_host import FakeCalendar
from test_feishu_adapter import FakeTransport
from threadplan.adapters.feishu import FeishuCalendar
from test_planning_v3 import ScriptedModel, decision, request, read_request


class SnapshotReader:
    """Fixture read adapter; its calendar service is explicitly fake."""
    def __init__(self, backend):
        self.backend = backend

    def __call__(self, *, query, context):
        events, raw = self.backend.snapshot(**({"event_ids": query["event_ids"]}
            if query["kind"] == "get_events" else {"range": query["range"]}))
        return {"read_status": "COMPLETE", "events": [{"id": event.id, "fields": event.fields,
                "status": raw[event.id]["status"], "raw": raw[event.id]} for event in events],
                "searched": {"range": deepcopy(query.get("range")), "applied_filters": {}}}


class NativeTransport(FakeTransport):
    """Count actual adapter invocations locally; no network or timing claim."""
    def invoke(self, resource, method, **kwargs):
        if method == "instance_view":
            self.calls.append((resource, method, deepcopy(kwargs.get("params")), None))
            return {"items": [deepcopy(self.event)] if self.event else []}
        result = super().invoke(resource, method, **kwargs)
        if method == "create":
            self.event["status"] = "confirmed"
            result = {"event": deepcopy(self.event)}
        return result


class PlanningExecutionV3Checks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.context = UpstreamContext("me", "Asia/Shanghai", "fixture-connection", "private-test")
        self.clock = lambda: datetime(2026, 11, 10, 3, tzinfo=timezone.utc)
        self.store = PlanningStore(self.path / "planning.sqlite", clock=self.clock)
        self.calendar = FakeCalendar()
        self.backend = FeishuHost(self.calendar, self.path / "provider.sqlite")
        self.reader = SnapshotReader(self.backend)
        self.planner = ConversationPlanner(self.store, context=self.context, calendar_reader=self.reader)
        self.executor = PlanningExecutor(self.store, context=self.context, backend=self.backend)

    def writes(self):
        return [call for call in self.calendar.calls if call[0] in {"create", "update", "cancel"}]

    def seed(self):
        value = Proposal("CREATE", "confirmed", ("fixture-seed",), {"title": "聊一下",
            "start_at": "2026-11-11T14:00:00+08:00", "end_at": "2026-11-11T14:30:00+08:00"})
        return self.backend.execute(value, "explicit-fixture-seed", {}, approved=True)["event_id"]

    async def analyze(self, *, kind="UPDATE", raw=None):
        row = await self.planner.analyze(raw or request(), ScriptedModel(read_request(), decision(kind)))
        self.assertEqual(row["state"], "COMPLETED", row.get("error"))
        return row

    def prepare(self, row):
        return self.executor.prepare(row["id"], 0, expected_fingerprint=row["result"]["plan_fingerprint"], approved=True)

    async def test_native_managed_update_call_baseline(self):
        transport = NativeTransport()
        self.calendar = FeishuCalendar("test-calendar", cli=transport)
        self.backend = FeishuHost(self.calendar, self.path / "native.sqlite")
        self.executor.backend = self.backend
        self.reader.backend = self.backend
        self.seed()
        row = await self.analyze()
        transport.calls.clear()
        command = self.prepare(row)
        self.assertEqual(len(transport.calls), 2)
        done = self.executor.execute(command["id"], approved=True)
        self.assertEqual(done["state"], "SUCCEEDED")
        self.assertEqual(len(transport.calls), 7)
        self.assertEqual(sum(method == "patch" for _, method, _, _ in transport.calls), 1)
        count = len(transport.calls)
        self.assertTrue(self.executor.execute(command["id"], approved=True)["already_applied"])
        self.assertEqual(len(transport.calls), count)

    async def test_native_encoding_and_changed_scope_fail_before_dispatch(self):
        transport = NativeTransport()
        calendar = FeishuCalendar("test-calendar", cli=transport)
        backend = FeishuHost(calendar, self.path / "native.sqlite")
        invalid = Proposal("CREATE", "confirmed", ("m1",), {"title": "",
            "start_at": "2026-11-11T14:00:00+08:00", "end_at": "2026-11-11T14:30:00+08:00"})
        with self.assertRaises(FeishuError):
            backend.execute(invalid, "bad-encoding", {}, approved=True)
        self.assertEqual(backend.receipt("bad-encoding")["state"], "failed_before_write")
        valid = Proposal("CREATE", "confirmed", ("m1",), dict(invalid.changes, title="valid"))
        transport.calendar["role"] = "reader"
        with self.assertRaises(FeishuError):
            backend.execute(valid, "changed-scope", {}, approved=True)
        self.assertEqual(backend.receipt("changed-scope")["state"], "failed_before_write")
        self.assertFalse(any(method in {"create", "patch", "delete"} and resource == "events"
                             for resource, method, _, _ in transport.calls))

    async def test_recovery_harness_faults_at_native_boundary_offline(self):
        from scripts import verify_calendar_recovery as harness
        transport = NativeTransport()
        backend = FeishuHost(FeishuCalendar("test-calendar", cli=transport), self.path / "native.sqlite")
        class FixtureClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 11, 12, tzinfo=timezone.utc)
        # Run the real harness with an explicit constructed transport, no login,
        # network, production ledger or durable real-calendar evidence.
        with patch.object(harness, "ROOT", self.path), patch.object(harness, "datetime", FixtureClock), \
                patch.object(harness.FeishuHost, "from_connection", return_value=backend), redirect_stdout(io.StringIO()):
            self.assertEqual(harness.run(), 0)
        receipt = json.loads(next((self.path / "runs").glob("*/receipt.json")).read_text(encoding="utf-8"))
        self.assertEqual(len(receipt["steps"]), 3)
        self.assertEqual(receipt["native_mutation_invocations"], 3)
        self.assertTrue(all(step["recovery_mutation_invocations"] == 0 for step in receipt["steps"]))

    async def test_prepare_is_read_only_and_requires_actual_upstream_approval_and_fingerprint(self):
        self.seed();row = await self.analyze()
        before = len(self.writes())
        with self.assertRaisesRegex(ValueError, "approval_required"):
            self.executor.prepare(row["id"], 0, expected_fingerprint=row["result"]["plan_fingerprint"])
        with self.assertRaisesRegex(ValueError, "fingerprint_mismatch"):
            self.executor.prepare(row["id"], 0, expected_fingerprint="wrong", approved=True)
        command = self.prepare(row)
        self.assertEqual(command["state"], "PREPARED")
        self.assertEqual(len(self.writes()), before)
        result = self.executor.execute(command["id"], approved=True)
        self.assertEqual(result["state"], "SUCCEEDED")
        self.assertEqual(len(self.writes()), before + 1)
        repeated = self.executor.execute(command["id"], approved=True)
        self.assertTrue(repeated["already_applied"])
        self.assertEqual(len(self.writes()), before + 1)
        self.assertTrue(self.store.bindings(row["conversation"]))

    async def test_existing_marked_event_can_update_and_cancel_without_claiming_creation(self):
        identifier = self.seed()
        self.calendar.raw[identifier]["description"] = "人手记的方案评审，请带预算表。"
        with self.backend.connect() as db:
            db.execute("DELETE FROM managed_events")
        self.backend = FeishuHost(self.calendar, self.backend.path)
        self.executor.backend = self.backend
        self.reader.backend = self.backend
        row = await self.analyze()
        self.assertFalse(self.calendar.authorized_event_ids, "Reads must not grant edits")
        done = self.executor.execute(self.prepare(row)["id"], approved=True)
        self.assertEqual(done["state"], "SUCCEEDED")
        self.assertEqual(done["result"]["event_id"], identifier)
        self.assertEqual(self.calendar.raw[identifier]["description"], "人手记的方案评审，请带预算表。")
        restarted = FeishuHost(self.calendar, self.backend.path)
        self.assertNotIn(identifier, self.calendar.managed_event_ids)
        self.assertIn(identifier, self.calendar.authorized_event_ids)
        with restarted.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM managed_events").fetchone()[0], 0)
        _, baselines = restarted.snapshot(event_ids=[identifier])
        cancelled = restarted.execute(Proposal("CANCEL", "cancelled", ("m1",), {}, identifier),
                                     "cancel-existing", baselines, approved=True)
        self.assertTrue(cancelled["cancellation_verification"]["verified"])

    async def test_create_reports_overlap_with_unmanaged_record_in_complete_snapshot(self):
        identifier = self.seed()
        self.calendar.raw[identifier]["summary"] = "其他个人安排"
        self.calendar.raw[identifier]["start_time"]["timestamp"] = str(int(datetime.fromisoformat("2026-11-11T15:00:00+08:00").timestamp()))
        self.calendar.raw[identifier]["end_time"]["timestamp"] = str(int(datetime.fromisoformat("2026-11-11T16:00:00+08:00").timestamp()))
        with self.backend.connect() as db:
            db.execute("DELETE FROM managed_events")
        self.backend = FeishuHost(self.calendar, self.backend.path)
        self.executor.backend = self.backend
        self.reader.backend = self.backend
        row = await self.analyze(kind="CREATE")
        self.assertTrue(row["result"]["conflict_reports"][0]["overlaps"])
        done = self.executor.execute(self.prepare(row)["id"], approved=True)
        self.assertEqual(done["state"], "SUCCEEDED")
        self.assertEqual(self.calendar.raw[identifier]["summary"], "其他个人安排")
        self.assertFalse(self.calendar.authorized_event_ids)

    async def test_existing_reminder_event_is_readable_but_cannot_be_changed(self):
        identifier = self.seed()
        self.calendar.raw[identifier]["summary"] = "含提醒的日程"
        self.calendar.raw[identifier]["reminders"] = [{"minutes":10}]
        with self.backend.connect() as db:
            db.execute("DELETE FROM managed_events")
        self.backend = FeishuHost(self.calendar, self.backend.path)
        self.executor.backend = self.backend
        self.reader.backend = self.backend
        row = await self.analyze()
        command = self.prepare(row)
        before = len(self.writes())
        with self.assertRaisesRegex(FeishuError, "reminders"):
            self.executor.execute(command["id"], approved=True)
        self.assertEqual(len(self.writes()), before)
        self.assertFalse(self.calendar.authorized_event_ids)
        self.assertEqual(self.executor.command(command["id"])["state"], "FAILED_BEFORE_WRITE")

    async def test_notes_are_persisted_read_back_and_replayed_without_time_changes(self):
        identifier=self.seed()
        before=deepcopy(self.calendar.raw[identifier])
        final=decision('UPDATE')
        final['items'][0]['operation']={'kind':'UPDATE','event_ref':'c1','changes':{'description':'带预算表和旧方案。'}}
        row=await self.planner.analyze(request(),ScriptedModel(read_request(),final))
        self.assertEqual(row['state'],'COMPLETED',row.get('error'))
        command=self.prepare(row)
        done=self.executor.execute(command['id'],approved=True)
        self.assertEqual(done['result']['event']['fields']['description'],'带预算表和旧方案。')
        self.assertEqual(self.calendar.raw[identifier]['start_time'],before['start_time'])
        self.assertEqual(self.calendar.raw[identifier]['end_time'],before['end_time'])
        writes=len(self.writes())
        self.assertTrue(self.executor.execute(command['id'],approved=True)['already_applied'])
        self.assertEqual(len(self.writes()),writes)

    async def test_partial_scope_snapshot_is_not_an_empty_calendar(self):
        row = await self.analyze(kind="CREATE")
        with patch.object(self.calendar, "find_events", return_value={"items": [], "has_more": True}):
            with self.assertRaisesRegex(FeishuError, "incomplete"):
                self.prepare(row)
        self.assertFalse(self.writes())

    async def test_disappeared_overlap_does_not_block_create_or_change_other_events(self):
        identifier = self.seed()
        self.calendar.raw[identifier]["start_time"]["timestamp"] = str(int(datetime.fromisoformat(
            "2026-11-11T15:00:00+08:00").timestamp()))
        self.calendar.raw[identifier]["end_time"]["timestamp"] = str(int(datetime.fromisoformat(
            "2026-11-11T15:30:00+08:00").timestamp()))
        row = await self.analyze(kind="CREATE")
        command = self.prepare(row)
        self.calendar.raw[identifier]["status"] = "cancelled"
        before = len(self.writes())
        self.assertEqual(self.executor.execute(command["id"], approved=True)["state"], "SUCCEEDED")
        self.assertEqual(len(self.writes()), before + 1)
        self.assertEqual(self.calendar.raw[identifier]["status"], "cancelled")

    async def test_new_create_candidate_requires_judgment_even_with_different_title(self):
        row = await self.analyze(kind="CREATE")
        command = self.prepare(row)
        self.backend.execute(Proposal("CREATE", "confirmed", ("fixture",), {
            "title": "另一种标题", "start_at": "2026-11-11T15:00:00+08:00",
            "end_at": "2026-11-11T15:30:00+08:00"}), "concurrent-fixture", {}, approved=True)
        before = len(self.writes())
        with self.assertRaisesRegex(ValueError, "new_calendar_candidates_reanalysis_required"):
            self.executor.execute(command["id"], approved=True)
        self.assertEqual(len(self.writes()), before)
        self.assertIsNone(self.backend.receipt(command["id"]))

    async def test_changed_known_create_candidate_requires_reanalysis(self):
        identifier = self.backend.execute(Proposal("CREATE", "confirmed", ("fixture",), {
            "title": "旧候选", "start_at": "2026-11-11T15:00:00+08:00",
            "end_at": "2026-11-11T15:30:00+08:00"}), "old-candidate", {}, approved=True)["event_id"]
        row = await self.analyze(kind="CREATE")
        command = self.prepare(row)
        self.calendar.raw[identifier]["description"] = "活动身份已由人工重新说明"
        before = len(self.writes())
        with self.assertRaisesRegex(ValueError, "baseline_changed"):
            self.executor.execute(command["id"], approved=True)
        self.assertEqual(len(self.writes()), before)

    async def test_create_reopen_update_and_cancel_keep_real_backend_assigned_id(self):
        created = await self.analyze(kind="CREATE")
        command = self.prepare(created)
        actual = self.executor.execute(command["id"], approved=True)
        identifier = actual["result"]["event_id"]
        self.assertEqual(actual["state"], "SUCCEEDED")
        reopened = PlanningExecutor(PlanningStore(self.store.path, clock=self.clock), context=self.context,
                                    backend=FeishuHost(self.calendar, self.backend.path))
        self.assertTrue(reopened.execute(command["id"], approved=True)["already_applied"])
        raw = request("cancel-r2")
        raw["messages"] += [{"id":"m3","sender_id":"alex","sent_at":"2026-11-10T10:02:00+08:00","text":"方案评审取消。"},
                            {"id":"m4","sender_id":"me","sent_at":"2026-11-10T10:03:00+08:00","text":"收到，取消。"}]
        first, final = read_request(), decision("CANCEL")
        first["items"][0]["evidence_message_ids"] = ["m3","m4"]
        final["items"][0]["evidence_message_ids"] = ["m3","m4"]
        cancelled = await self.planner.analyze(raw, ScriptedModel(first,final))
        self.assertEqual(cancelled["state"],"COMPLETED",cancelled.get("error"))
        done = reopened.execute(self.prepare(cancelled)["id"], approved=True)
        self.assertEqual(done["result"]["event_id"],identifier)
        self.assertTrue(done["result"]["cancellation_verification"]["verified"])
        self.assertTrue(reopened.execute(command["id"], approved=True)["already_applied"])
        self.assertEqual(self.store.bindings(created["conversation"])[0]["status"], "cancelled")

    async def test_acknowledged_write_read_failure_survives_restart_and_recovery_only_reads(self):
        self.seed()
        row = await self.analyze()
        command = self.prepare(row)
        update_event = self.calendar.update_event

        def write_then_lose_readback(*args, **kwargs):
            response = update_event(*args, **kwargs)
            self.calendar.fail_readback = True
            return response

        with patch.object(self.calendar, "update_event", side_effect=write_then_lose_readback):
            with self.assertRaisesRegex(FeishuError, "readback unavailable"):
                self.executor.execute(command["id"], approved=True)
        self.assertEqual(self.executor.command(command["id"])["state"], "VERIFY_PENDING")
        self.calendar.fail_readback = False
        count = len(self.writes())
        reopened = PlanningExecutor(PlanningStore(self.store.path, clock=self.clock), context=self.context,
                                    backend=FeishuHost(self.calendar, self.backend.path))
        self.assertEqual(reopened.recover(command["id"], approved=True)["state"], "SUCCEEDED")
        self.assertTrue(reopened.execute(command["id"], approved=True)["already_applied"])
        self.assertEqual(len(self.writes()), count)

    async def test_backend_receipt_is_authority_after_sync_crash_and_legacy_mirror(self):
        self.seed(); row = await self.analyze(); command = self.prepare(row)
        with patch.object(self.executor, "_sync", side_effect=RuntimeError("crash after backend success")):
            with self.assertRaises(RuntimeError):
                self.executor.execute(command["id"], approved=True)
        count = len(self.writes())
        with self.store.connect() as db:
            stored = db.execute("SELECT state,result FROM v3_commands WHERE id=?", (command["id"],)).fetchone()
            self.assertEqual(tuple(stored), ("DISPATCHING", None))
        reopened = PlanningExecutor(PlanningStore(self.store.path, clock=self.clock), context=self.context,
                                    backend=FeishuHost(self.calendar, self.backend.path))
        self.assertTrue(reopened.execute(command["id"], approved=True)["already_applied"])
        with self.store.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT state,result,error FROM v3_commands WHERE id=?",
                (command["id"],)).fetchone()), ("ISSUED", None, None))
            # A stale historical mirror cannot override an existing receipt.
            db.execute("UPDATE v3_commands SET state='OUTCOME_UNKNOWN',result='{}' WHERE id=?", (command["id"],))
        self.assertEqual(reopened.command(command["id"])["state"], "SUCCEEDED")
        self.assertEqual(len(self.writes()), count)

    async def test_missing_receipt_during_dispatch_does_not_enable_concurrent_retry(self):
        self.seed(); row = await self.analyze(); command = self.prepare(row)
        with self.store.connect() as db:
            db.execute("UPDATE v3_commands SET state='DISPATCHING' WHERE id=?", (command["id"],))
        count = len(self.writes())
        with self.assertRaisesRegex(FeishuError, "No execution receipt"):
            self.executor.recover(command["id"], approved=True)
        self.assertEqual(self.executor.command(command["id"])["state"], "IN_FLIGHT")
        with self.assertRaisesRegex(ValueError, "read_only_recovery"):
            self.executor.execute(command["id"], approved=True)
        self.assertEqual(len(self.writes()), count)

    async def test_unknown_update_read_reconciliation_does_not_resend(self):
        self.seed();row = await self.analyze();command = self.prepare(row)
        original = self.calendar.update_event
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise FeishuError("injected receiver response loss")
        with patch.object(self.calendar,"update_event",side_effect=lost):
            with self.assertRaises(FeishuError):self.executor.execute(command["id"],approved=True)
        self.assertEqual(self.executor.command(command["id"])["state"],"OUTCOME_UNKNOWN")
        count=len(self.writes())
        with self.assertRaisesRegex(ValueError,"read_only_recovery"):
            self.executor.execute(command["id"],approved=True)
        recovered=self.executor.recover(command["id"],approved=True)
        self.assertEqual(recovered["state"],"SUCCEEDED")
        self.assertFalse(recovered["result"]["provider_acknowledged"])
        self.assertEqual(len(self.writes()),count)

    async def test_source_supersession_blocks_unissued_command_but_not_issued_recovery(self):
        self.seed();row=await self.analyze();command=self.prepare(row)
        newer=request("new-window");newer["messages"].append({"id":"m3","sender_id":"me","text":"等一下。","sent_at":"2026-11-10T10:02:00+08:00"})
        from threadplan import Intake
        self.store.register(Intake(newer),self.context,duration_seconds=180)
        before=len(self.writes())
        with self.assertRaisesRegex(ValueError,"stale_request"):
            self.executor.execute(command["id"],approved=True)
        self.assertEqual(len(self.writes()),before)

    async def test_external_edit_cannot_be_silently_overwritten(self):
        identifier=self.seed();row=await self.analyze();command=self.prepare(row)
        self.calendar.raw[identifier]["summary"]="[ThreadPlan TEST] Changed elsewhere"
        before=len(self.writes())
        with self.assertRaisesRegex(FeishuError,"changed since approval baseline"):
            self.executor.execute(command["id"],approved=True)
        self.assertEqual(len(self.writes()),before)

    async def test_other_principal_cannot_reuse_successful_command_or_recover(self):
        self.seed();row=await self.analyze();command=self.prepare(row)
        self.executor.execute(command["id"],approved=True)
        other=PlanningExecutor(self.store,context=UpstreamContext("other","Asia/Shanghai","fixture-connection","private-test"),backend=self.backend)
        with self.assertRaisesRegex(ValueError,"scope_mismatch"):other.command(command['id'])
        with self.assertRaisesRegex(ValueError,"scope_mismatch"):other.execute(command["id"],approved=True)
        with self.assertRaisesRegex(ValueError,"scope_mismatch"):other.recover(command["id"],approved=True)

    async def test_overlapping_creates_partial_retry_does_not_replay_first_success(self):
        first, final = read_request(), decision("CREATE")
        final["items"].append(deepcopy(final["items"][0]))
        for index, item in enumerate(final["items"]):
            item["intent_ref"] = "i" + str(index + 1)
            item["operation"]["fields"]["title"] += str(index + 1)
        row = await self.planner.analyze(request(), ScriptedModel(first, final))
        self.assertEqual(row["state"], "COMPLETED", row.get("error"))
        one = self.prepare(row)
        self.executor.execute(one["id"], approved=True)
        two = self.executor.prepare(row["id"], 1,
            expected_fingerprint=row["result"]["plan_fingerprint"], approved=True)
        with patch.object(self.calendar, "prepare_write", side_effect=FeishuError("injected prewrite check failure")):
            with self.assertRaises(FeishuError):
                self.executor.execute(two["id"], approved=True)
        self.assertEqual(self.executor.command(two["id"])["state"], "FAILED_BEFORE_WRITE")
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.executor.execute(two["id"], approved=True)["state"], "SUCCEEDED")
        self.assertTrue(self.executor.execute(one["id"], approved=True)["already_applied"])
        self.assertEqual(len(self.writes()), 2)

    async def test_note_edit_with_new_delivery_id_invalidates_old_unissued_command(self):
        raw = request("note-original", note=True)
        original = await self.analyze(kind="CREATE", raw=raw)
        command = self.prepare(original)
        edited = deepcopy(raw)
        edited["request_id"] = "note-edited-delivery"
        edited["messages"][0].update(text="这条记录先不要写进日历。", updated_at="2026-11-10T10:02:00+08:00")
        self.planner.session.open(edited)
        with self.assertRaisesRegex(ValueError, "stale_request"):
            self.executor.execute(command["id"], approved=True)
        self.assertFalse(self.writes())

    async def test_note_redelivery_reuses_successful_receipt_across_request_ids(self):
        raw = request("note-first-delivery", note=True)
        first = await self.analyze(kind="CREATE", raw=raw)
        command = self.prepare(first)
        done = self.executor.execute(command["id"], approved=True)
        replay = deepcopy(raw)
        replay["request_id"] = "note-redelivery"
        second = await self.analyze(kind="CREATE", raw=replay)
        repeated = self.prepare(second)
        self.assertEqual(repeated["id"], command["id"])
        self.assertTrue(self.executor.execute(repeated["id"], approved=True)["already_applied"])
        self.assertEqual(done["state"], "SUCCEEDED")
        self.assertEqual(len(self.writes()), 1)
