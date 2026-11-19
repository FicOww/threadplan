"""Agent tool behavior over real SQLite and explicit constructed calendars."""

from concurrent.futures import ThreadPoolExecutor
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from threadplan import UpstreamContext, Proposal, InputError
from threadplan_host.agent import AgentSession
from threadplan_host.feishu import FeishuHost
from threadplan_host.planning_store import PlanningStore
from test_feishu_host import FakeCalendar
from test_planning_execution_v3 import SnapshotReader
from test_planning_v3 import decision, request


class AgentToolChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.clock = lambda: datetime(2026, 11, 10, 3, tzinfo=timezone.utc)
        self.context = UpstreamContext("me", "Asia/Shanghai", "fixture-connection", "private-test")
        self.store = PlanningStore(self.path / "agent.sqlite", clock=self.clock)
        self.calendar = FakeCalendar()
        self.backend = FeishuHost(self.calendar, self.path / "provider.sqlite")
        self.agent = self.runtime()
        self.query = {"kind": "find_events", "range": {
            "start_at": "2026-11-11T00:00:00+08:00", "end_at": "2026-11-12T00:00:00+08:00"}}

    def runtime(self, allowed=()):
        return AgentSession(self.store, context=self.context, calendar_reader=SnapshotReader(self.backend),
                            backend=self.backend, allowed_operations=allowed)

    def read(self, *args, **kwargs):
        return asyncio.run(self.agent.read(*args, **kwargs))

    def seed(self, title="旧安排", start="2026-11-11T14:00:00+08:00", end="2026-11-11T14:30:00+08:00"):
        return self.backend.execute(Proposal("CREATE", "confirmed", ("fixture",), {
            "title": title, "start_at": start, "end_at": end}), "fixture-" + title, {}, approved=True)["event_id"]

    def writes(self):
        return [call for call in self.calendar.calls if call[0] in {"create", "update", "cancel"}]

    def planned(self, kind="CREATE"):
        key = self.agent.open(request())["request_key"]
        self.read(key, self.query)
        result = self.agent.propose(key, decision(kind))
        prepared = self.agent.prepare(key, result["plan"]["plan_fingerprint"])
        return key, prepared["commands"][0]["id"]

    def test_source_metadata_is_required_before_any_calendar_read(self):
        for field in ("sender_id", "sent_at"):
            source = request()
            del source["messages"][0][field]
            with self.assertRaises(InputError):
                self.agent.open(source)
        self.assertFalse(self.calendar.calls)

    def test_model_approval_and_raw_native_ids_do_not_grant_execution(self):
        key = self.agent.open(request())["request_key"]
        self.read(key, self.query)
        raw = dict(decision("CREATE"), approved=True, allowed_operations=["CREATE"])
        result = self.agent.propose(key, raw)
        command = self.agent.prepare(key, result["plan"]["plan_fingerprint"])["commands"][0]
        with self.assertRaisesRegex(ValueError, "trusted_execution_policy_required"):
            self.agent.execute(command["id"])
        self.assertFalse(self.writes())
        # The approved plan stays immutable, even when the agent resubmits it.
        changed = deepcopy(raw)
        changed["items"][0]["operation"]["fields"]["title"] = "changed"
        with self.assertRaisesRegex(ValueError, "frozen_plan"):
            self.agent.propose(key, changed)

    def test_complete_read_and_known_refs_are_required(self):
        key = self.agent.open(request())["request_key"]
        result = self.agent.propose(key, decision("CREATE"))
        self.assertEqual(result["plan"]["items"][0]["decision"], "FAILED")
        key = self.agent.open(request("r2"))["request_key"]
        self.read(key, self.query)
        result = self.agent.propose(key, decision("UPDATE", ref="invented-provider-id"))
        self.assertIn("unknown_event_reference", result["plan"]["item_errors"][0]["code"])
        self.assertFalse(self.writes())

    def test_preauthorized_execution_reopens_reuses_receipt_and_retains_raw_decision(self):
        key, command = self.planned()
        runtime = self.runtime(("CREATE",))
        created = runtime.execute(command)
        self.assertEqual(created["state"], "SUCCEEDED")
        count = len(self.writes())
        reopened = self.runtime(("CREATE",))
        self.assertTrue(reopened.execute(command)["already_applied"])
        self.assertEqual(len(self.writes()), count)
        self.assertEqual(reopened.status(command_id=command)["state"], "SUCCEEDED")
        self.assertEqual(next(t for t in self.store.traces(key) if t["kind"] == "DECISION")["input"], decision("CREATE"))

    def test_new_source_invalidates_old_unissued_plan(self):
        _, command = self.planned()
        newer = request("new-source")
        newer["messages"].append({"id": "m3", "sender_id": "me", "sent_at": "2026-11-10T10:02:00+08:00",
                                  "text": "先别创建。"})
        self.agent.open(newer)
        with self.assertRaisesRegex(ValueError, "stale_request"):
            self.runtime(("CREATE",)).execute(command)
        self.assertFalse(self.writes())

    def test_concurrent_reads_keep_refs_bound_to_their_original_provider_ids(self):
        one = self.seed()
        two = self.seed("另一天", "2026-11-12T14:00:00+08:00", "2026-11-12T14:30:00+08:00")
        key = self.agent.open(request())["request_key"]
        other = deepcopy(self.query)
        other["range"] = {"start_at": "2026-11-12T00:00:00+08:00", "end_at": "2026-11-13T00:00:00+08:00"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda query: self.read(key, query), (self.query, other)))
        records = self.store.get(key)["work"]["records"]
        self.assertEqual({record["id"] for record in records.values()}, {one, two})
        for result in results:
            for record in result["calendar_records"]:
                expected_id = {"旧安排": one, "另一天": two}[record["fields"]["title"]]
                self.assertEqual(records[record["event_ref"]]["id"], expected_id)
        self.assertEqual(len([t for t in self.store.traces(key) if t["kind"] == "READ"]), 2)

    def test_wait_and_no_action_are_completed_without_any_write_or_model_call(self):
        for state in ("WAIT", "NO_ACTION"):
            raw = request("note-" + state, note=True)
            key = self.agent.open(raw)["request_key"]
            result = self.agent.propose(key, {"items": [{"decision": state, "reason": "Constructed test reason",
                                                "evidence_message_ids": ["m1"], **({"missing_facts": ["确认"], "clarification_question": "是否确认？"} if state == "WAIT" else {})}]})
            self.assertEqual(result["plan"]["operations"], [])
            self.assertEqual(self.agent.prepare(key, result["plan"]["plan_fingerprint"])["commands"], [])
            self.assertFalse([t for t in self.store.traces(key) if t["kind"] == "READ"])
        self.assertFalse(self.writes())

    def test_keep_duration_is_derived_from_the_real_target_interval(self):
        identifier = self.seed()
        key = self.agent.open(request())["request_key"]
        self.read(key, self.query)
        result = self.agent.propose(key, decision("UPDATE"))
        op = result["plan"]["operations"][0]
        self.assertEqual(op["changes"]["end_at"], "2026-11-11T15:30:00+08:00")
        self.assertEqual(self.store.get(key)["result"]["records"][op["event_ref"]]["id"], identifier)
        self.assertIn("end_at:PRESERVE_DURATION", op["derived"])

    def test_failed_malformed_read_cannot_admit_partially_merged_targets(self):
        key = self.agent.open(request())["request_key"]
        response = {"read_status": "COMPLETE", "events": [
            {"id": "invalid-response-candidate", "fields": {"title": "partial"}}, {"fields": {}}]}
        with patch.object(self.agent, "calendar_reader", return_value=response):
            result = self.read(key, self.query)
            self.assertEqual(result["latest_read"]["read_status"], "FAILED")
        work = self.store.get(key)["work"]
        self.assertFalse(work["records"])
        self.assertEqual(work["reads"][0]["read_status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
