"""V1 loop contracts over scripted models/calendars; never semantic inference."""

from copy import deepcopy
import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from threadplan import Intake, InputError, UpstreamContext
from threadplan_host import ConversationPlanner, PlanningLimits, PlanningStore
from threadplan.codec import _aware_time


def request(request_id="r1", *, note=False):
    return {"schema_version": "0.3", "request_id": request_id,
            "source": {"kind": "personal_note" if note else "conversation", "provider": "constructed",
                       **({} if note else {"conversation_id": "chat"})},
            "messages": [
                {"id": "m1", "sender_id": "alex", "sent_at": "2026-11-10T10:00:00+08:00",
                 "text": "明天和小李的方案评审从两点改到下午三点，时长不变。"},
                {"id": "m2", "sender_id": "me", "sent_at": "2026-11-10T10:01:00+08:00", "text": "同意。", "reply_to": "m1"}]}


def read_request():
    return {"items": [{"activity_ref": "a1", "decision": "NEED_MORE", "reason": "读取原安排日期", "evidence_message_ids": ["m1", "m2"],
        "next_read": {"kind": "find_events", "range": {"start_at": "2026-11-11T00:00:00+08:00", "end_at": "2026-11-12T00:00:00+08:00"}}}]}


def decision(kind="UPDATE", *, ref="c1", end=False):
    operation = {"kind": kind, "event_ref": ref}
    if kind == "UPDATE":
        operation['preserve_duration'] = True
        operation["changes"] = {"start_at": "2026-11-11T15:00:00+08:00"}
        if end:
            operation["changes"]["end_at"] = "2026-11-11T15:30:00+08:00"
    if kind == "CREATE":
        operation = {"kind": kind, "fields": {"title": "方案评审", "start_at": "2026-11-11T15:00:00+08:00",
                                              "end_at": "2026-11-11T15:30:00+08:00"}}
    return {"items": [{"decision": "PROPOSE", "operation": operation, "reason": "Constructed decision.",
                        "evidence_message_ids": ["m1", "m2"]}]}


class ScriptedModel:
    def __init__(self, *outputs):
        self.outputs, self.calls = list(outputs), []
        self.last_record = None

    async def __call__(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        value = self.outputs.pop(0)
        if callable(value):
            value = value(json.loads(kwargs["input_json"]))
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


class ConstructedReader:
    def __init__(self, *, status="COMPLETE"):
        self.status, self.calls = status, []
        self.events = [{"id": "provider-manual-1", "status": "confirmed", "fields": {
            "title": "聊一下", "start_at": "2026-11-11T14:00:00+08:00", "end_at": "2026-11-11T14:30:00+08:00",
            "description": "和小李讨论方案", "location": "二号会议室"}}]

    async def __call__(self, *, query, context):
        self.calls.append(deepcopy(query))
        values=deepcopy(self.events)
        if query["kind"]=="get_events":
            values=[item for item in values if item["id"] in query["event_ids"]]
        else:
            low,high=(_aware_time(query["range"][key]) for key in ("start_at","end_at"))
            values=[item for item in values if _aware_time(item["fields"]["start_at"])<high and low<_aware_time(item["fields"]["end_at"])]
        return {"read_status": self.status, "events": values, "connection_ref": context.connection_ref,
                "searched": {"range": deepcopy(query.get("range")), "applied_filters": {}}}


class PlanningV3Checks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.context = UpstreamContext("me", "Asia/Shanghai", "test-connection", "private-test")
        self.now = datetime(2026, 11, 10, 3, tzinfo=timezone.utc)
        self.store = PlanningStore(Path(directory.name) / "state.sqlite", clock=lambda: self.now)
        self.reader = ConstructedReader()
        self.planner = ConversationPlanner(self.store, context=self.context, calendar_reader=self.reader)

    async def test_complete_source_and_single_policy_without_forced_extraction(self):
        model = ScriptedModel(read_request(), decision())
        row = await self.planner.analyze(request(), model)
        self.assertEqual(row["state"], "COMPLETED", row.get("error"))
        self.assertEqual(len(row["result"]["operations"]), 1)
        first, final = [json.loads(call["input_json"]) for call in model.calls]
        self.assertEqual(first["messages"], request()["messages"])
        self.assertFalse(first["calendar_records"])
        self.assertEqual(first["time_policy"]["default_duration_minutes"], 30)
        self.assertEqual(model.calls[0]["system_prompt"], model.calls[1]["system_prompt"])
        self.assertNotIn("interpretation", final)
        self.assertNotIn("provider-manual-1", model.calls[1]["input_json"])
        self.assertEqual(final["reads"][0]["read_ref"], "r1")
        self.assertNotIn("events", final["reads"][0])
        self.assertEqual(row["result"]["operations"][0]["derived"], ["end_at:PRESERVE_DURATION"])

    async def test_no_action_and_wait_finish_in_one_call_without_read(self):
        for index, item in enumerate((
                {"decision": "NO_ACTION", "reason": "改期已拒绝", "evidence_message_ids": ["m1", "m2"]},
                {"decision": "WAIT", "reason": "未确认", "evidence_message_ids": ["m1"],
                 "missing_facts": ["是否同意"], "clarification_question": "是否同意这个时间？"})):
            model = ScriptedModel({"items": [item]})
            row = await self.planner.analyze(request(str(index)), model)
            self.assertEqual(row["result"]["items"][0]["decision"], item["decision"])
            self.assertFalse(row["result"]["operations"])
            self.assertEqual(len(model.calls), 1)
        self.assertFalse(self.reader.calls)

    async def test_source_admission_precedes_model_and_tool_calls(self):
        for field in ("sender_id", "sent_at"):
            raw = request()
            del raw["messages"][0][field]
            with self.assertRaises(InputError):
                await self.planner.analyze(raw, ScriptedModel())
        for raw in ('{"id":"1","id":"2"}', '{"n":NaN}'):
            with self.assertRaises(InputError):
                Intake.from_json(raw)
        self.assertFalse(self.reader.calls)

    async def test_reopen_reuses_analysis_and_conflicting_delivery_is_rejected(self):
        model = ScriptedModel(read_request(), decision())
        one = await self.planner.analyze(request(), model)
        again = await self.planner.analyze(request(), model)
        self.assertTrue(again["reused"])
        self.assertEqual(one["result"], again["result"])
        self.assertEqual(len(model.calls), 2)
        changed = request()
        changed["messages"][0]["text"] = "另一个内容"
        with self.assertRaisesRegex(InputError, "REQUEST_ID_CONFLICT"):
            await self.planner.analyze(changed, model)

    async def test_model_controls_old_anchor_then_new_range_and_details(self):
        first = read_request()
        first["items"][0]["next_read"]["range"] = {"start_at": "2026-11-10T00:00:00+08:00", "end_at": "2026-11-11T00:00:00+08:00"}
        model = ScriptedModel(first, read_request(),
            {"items": [{"activity_ref": "a1", "decision": "NEED_MORE", "reason": "核对备注", "evidence_message_ids": ["m1"],
                        "next_read": {"kind": "get_events", "event_refs": ["c1"], "fields": ["description"]}}]}, decision())
        row = await self.planner.analyze(request(), model)
        self.assertEqual(row["result"]["operations"][0]["kind"], "UPDATE")
        self.assertEqual(self.reader.calls[0]["range"], first["items"][0]["next_read"]["range"])
        self.assertEqual(self.reader.calls[-1]["event_ids"], ["provider-manual-1"])
        self.assertNotIn("event_refs", self.reader.calls[-1])

    async def test_partial_read_cannot_authorize_create(self):
        self.reader.status = "PARTIAL"
        row = await self.planner.analyze(request(), ScriptedModel(read_request(), decision("CREATE")))
        self.assertFalse(row["result"]["operations"])
        self.assertEqual(row["result"]["items"][0]["decision"], "FAILED")
        self.assertIn("duplicate_search_incomplete", row["result"]["item_errors"][0]["code"])

    async def test_failed_read_cannot_be_disguised_as_business_wait(self):
        self.reader.status = "FAILED"
        wait = {"items": [{"activity_ref": "a1", "decision": "WAIT", "reason": "没有取得日历", "evidence_message_ids": ["m1"],
                           "clarification_question": "原记录是哪条？", "missing_facts": ["目标记录"]}]}
        row = await self.planner.analyze(request(), ScriptedModel(read_request(), wait))
        self.assertEqual(row["result"]["items"][0]["decision"], "FAILED")
        self.assertIn("read_failed:r1", row["result"]["item_errors"][0]["code"])

    async def test_one_invalid_activity_preserves_valid_independent_operation(self):
        final = decision()
        final["items"].append(decision(ref="invented")["items"][0])
        row = await self.planner.analyze(request(), ScriptedModel(read_request(), final))
        self.assertEqual([item["decision"] for item in row["result"]["items"]], ["PROPOSE", "FAILED"])
        self.assertEqual(len(row["result"]["operations"]), 1)

    async def test_two_claims_for_same_target_block_both_but_keep_other_activity(self):
        final = decision()
        final["items"].append(deepcopy(final["items"][0]))
        final["items"].append({"decision": "NO_ACTION", "reason": "其他事项未确认", "evidence_message_ids": ["m2"]})
        row = await self.planner.analyze(request(), ScriptedModel(read_request(), final))
        self.assertEqual([item["decision"] for item in row["result"]["items"]], ["FAILED", "FAILED", "NO_ACTION"])
        self.assertFalse(row["result"]["operations"])

    async def test_repeat_query_stops_without_extra_read_or_invented_question(self):
        more = read_request()
        more["items"][0].update(clarification_question="请确认原会议是哪一场？", missing_facts=["会议身份"])
        row = await self.planner.analyze(request(), ScriptedModel(more, more))
        self.assertEqual(row["result"]["items"][0]["decision"], "WAIT")
        self.assertEqual(row["result"]["items"][0]["clarification_question"], "请确认原会议是哪一场？")
        self.assertEqual(len(self.reader.calls), 1)
        self.assertEqual(len([t for t in self.store.traces(row["id"]) if t["kind"] == "MODEL"]), 2)

    async def test_unavailable_history_is_explicit_and_does_not_invent_messages(self):
        raw = request()
        raw["messages"][0]["reply_to"] = "m0"
        more = read_request()
        more["items"][0]["next_read"] = {"kind": "get_messages", "message_ids": ["m0"]}
        more["items"][0].update(clarification_question="请提供被回复的原消息。", missing_facts=["原消息"])
        row = await self.planner.analyze(raw, ScriptedModel(more, more))
        self.assertEqual(row["result"]["items"][0]["decision"], "WAIT")
        self.assertEqual(row["result"]["reads"][0]["read_status"], "UNAVAILABLE")
        self.assertEqual(row["result"]["messages"], raw["messages"])
        self.assertFalse(self.reader.calls)

    async def test_history_keeps_real_order_without_sorting_timestamps(self):
        raw = request()
        raw["messages"][0]["reply_to"] = "m0"
        parent = {"id": "m0", "sender_id": "alex", "sent_at": "2026-11-10T09:59:00+08:00", "text": "原安排"}
        async def history(**kwargs):
            return {"read_status": "COMPLETE", "conversation_id": "chat", "messages": [parent]}
        planner = ConversationPlanner(self.store, context=self.context, calendar_reader=self.reader, history_reader=history)
        more = read_request()
        more["items"][0]["next_read"] = {"kind": "get_messages", "message_ids": ["m0"]}
        row = await planner.analyze(raw, ScriptedModel(more, read_request(), decision()))
        self.assertEqual([m["id"] for m in row["result"]["messages"]], ["m0", "m1", "m2"])
        self.assertEqual(row["request"]["messages"], raw["messages"])

    async def test_malformed_query_is_corrected_before_dispatch(self):
        bad = read_request()
        bad["items"][0]["next_read"]["hints"] = ["bad"]
        model = ScriptedModel(bad, read_request(), decision())
        row = await self.planner.analyze(request(), model)
        self.assertEqual(row["result"]["operations"][0]["kind"], "UPDATE")
        self.assertEqual(len(self.reader.calls), 1)
        self.assertIn("invalid_read_hints", model.calls[1]["input_json"])

    async def test_raw_protocol_error_and_json_correction_are_retained(self):
        model = ScriptedModel("{bad", read_request(), decision())
        row = await self.planner.analyze(request(), model)
        traces = [t for t in self.store.traces(row["id"]) if t["kind"] == "MODEL"]
        self.assertEqual(traces[0]["output"], "{bad")
        self.assertTrue(traces[0]["error"])
        self.assertEqual(len(row["result"]["operations"]), 1)

    async def test_source_edit_invalidates_old_plan_and_notes_stay_isolated(self):
        one = await self.planner.analyze(request(), ScriptedModel(read_request(), decision()))
        newer = request("new")
        newer["messages"][1].update(text="不同意", updated_at="2026-11-10T10:02:00+08:00")
        self.store.register(Intake(newer), self.context, duration_seconds=180)
        with self.assertRaisesRegex(ValueError, "stale_request"):
            self.store.current(one["id"])
        with self.assertRaises(InputError):
            self.store.register(Intake(request("old")), self.context, duration_seconds=180)
        a, _ = self.store.register(Intake(request("note1", note=True)), self.context, duration_seconds=180)
        unrelated = request("note2", note=True)
        unrelated["messages"][0]["id"] = "other-note-message"
        unrelated["messages"][1].update(id="other-note-ack", reply_to="other-note-message")
        self.store.register(Intake(unrelated), self.context, duration_seconds=180)
        self.assertEqual(self.store.current(a["id"])["state"], "RECEIVED")

    async def test_retry_after_keeps_frozen_clock_deadline_and_budget(self):
        from threadplan.openrouter_transport import OpenRouterError
        model = ScriptedModel(OpenRouterError("http_error", http_status=429, retry_after_seconds=4), read_request(), decision())
        first = await self.planner.analyze(request(), model)
        self.assertEqual(first["state"], "FAILED")
        await self.planner.analyze(request(), model, retry=True)
        self.assertEqual(len(model.calls), 1)
        self.now += timedelta(seconds=5)
        resumed = await self.planner.analyze(request(), model, retry=True)
        self.assertEqual(resumed["state"], "COMPLETED", resumed.get("error"))
        self.assertEqual(resumed["created_at"], first["created_at"])
        self.assertEqual(resumed["deadline_at"], first["deadline_at"])
        self.assertEqual(len(model.calls), 3)

    async def test_http400_requires_explicit_retry(self):
        from threadplan.openrouter_transport import OpenRouterError
        model = ScriptedModel(OpenRouterError("http_error", http_status=400), read_request(), decision())
        failed = await self.planner.analyze(request(), model)
        self.assertTrue(failed["error"]["manual_retry_allowed"])
        await self.planner.analyze(request(), model)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual((await self.planner.analyze(request(), model, retry=True))["state"], "COMPLETED")

    async def test_transport_metadata_belongs_to_each_actual_concurrent_task(self):
        class Model:
            record = ContextVar("v1_model_record", default=None)
            @property
            def last_record(self):
                return self.record.get()
            async def __call__(self, **kwargs):
                data = json.loads(kwargs["input_json"])
                self.record.set({"source": data["messages"][0]["text"]})
                await asyncio.sleep(0)
                return {"items": [{"decision": "NO_ACTION", "reason": "构造检查", "evidence_message_ids": ["m1"]}]}
        left, right = request("left"), request("right")
        left["source"]["conversation_id"], right["source"]["conversation_id"] = "left", "right"
        rows = await asyncio.gather(self.planner.analyze(left, Model()), self.planner.analyze(right, Model()))
        for row in rows:
            trace = next(t for t in self.store.traces(row["id"]) if t["kind"] == "MODEL")
            self.assertEqual(trace["metadata"]["source"], row["request"]["messages"][0]["text"])

    async def test_participants_and_optional_identity_location_do_not_become_writes(self):
        final = decision("CREATE")
        final["items"][0]["operation"]["fields"].update(participants=["me", "alex", "unknown-person"], location="me", description="带资料")
        row = await self.planner.analyze(request(), ScriptedModel(read_request(), final))
        op = row["result"]["operations"][0]
        self.assertEqual(op["metadata"]["participants"], ["me", "alex"])
        self.assertEqual(op["metadata"]["unresolved_participant_references"], ["unknown-person"])
        self.assertNotIn("location", op["changes"])
        self.assertEqual(op["changes"]["description"], "带资料")

    async def test_overlap_is_advisory_and_not_permission_to_change_other_event(self):
        self.reader.events.append({"id": "other", "status": "confirmed", "fields": {"title": "其他安排",
            "start_at": "2026-11-11T15:15:00+08:00", "end_at": "2026-11-11T16:00:00+08:00"}})
        row = await self.planner.analyze(request(), ScriptedModel(read_request(), decision()))
        self.assertEqual(len(row["result"]["operations"]), 1)
        self.assertEqual(row["result"]["conflict_reports"][0]["overlaps"][0]["event_ref"], "c2")

    async def test_default_duration_only_fills_confirmed_create_compiled_after_reads(self):
        final = decision("CREATE")
        final["items"][0]["operation"]["fields"].pop("end_at")
        row = await self.planner.analyze(request(), ScriptedModel(read_request(), final))
        self.assertEqual(row["result"]["operations"][0]["changes"]["end_at"], "2026-11-11T15:30:00+08:00")
        self.assertNotIn("end_at", row["result"]["candidate"]["items"][0]["operation"]["fields"])
