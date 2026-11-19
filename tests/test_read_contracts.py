"""Read coverage, pagination and independent failure contracts; no live I/O."""

import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from threadplan import UpstreamContext
from threadplan_host import ConversationPlanner, PlanningLimits, PlanningStore
from threadplan_host.planning_reads import duplicate_search_complete, merge_calendar_reply, overlaps
from test_planning_v3 import ConstructedReader, ScriptedModel, decision, read_request, request


class ReadContractChecks(unittest.TestCase):
    def setUp(self):
        self.context = UpstreamContext("me", "Asia/Shanghai", "c", "s")
        self.query = read_request()["items"][0]["next_read"]
        self.work = {"records": {}, "reads": [], "messages": request()["messages"]}
        self.fields = {"start_at": "2026-11-11T15:00:00+08:00", "end_at": "2026-11-11T15:30:00+08:00"}

    def merge(self, query=None, **reply):
        query = query or self.query
        value = {"read_status": "COMPLETE", "events": [],
                 "searched": {"range": query.get("range"), "applied_filters": {}}, **reply}
        result = merge_calendar_reply(self.work, query, value, self.context, max_candidates=50)
        self.work["reads"].append(result)
        return result

    def test_unreported_coverage_or_applied_filters_never_prove_absence(self):
        read = self.merge(searched=None)
        self.assertEqual(read["read_status"], "PARTIAL")
        self.assertIsNone(read["searched"]["applied_filters"])
        self.assertFalse(duplicate_search_complete(self.work["reads"], self.fields))
        self.merge(searched={"range": self.query["range"], "applied_filters": {"title": "评审"}})
        self.assertFalse(duplicate_search_complete(self.work["reads"], self.fields))

    def test_last_page_without_the_first_is_not_complete_coverage(self):
        page = dict(self.query, page_token="next")
        self.merge(page)
        self.assertFalse(duplicate_search_complete(self.work["reads"], self.fields))

    def test_linked_complete_pages_can_establish_bounded_coverage(self):
        first = self.merge(has_more=True, next_page_token="next")
        self.assertEqual(first["read_status"], "PARTIAL")
        self.assertFalse(duplicate_search_complete(self.work["reads"], self.fields))
        self.merge(dict(self.query, page_token="next"))
        self.assertTrue(duplicate_search_complete(self.work["reads"], self.fields))

    def test_failed_or_incomplete_page_does_not_establish_a_complete_chain(self):
        self.merge(read_status="PARTIAL", next_page_token="next")
        self.merge(dict(self.query, page_token="next"))
        self.assertFalse(duplicate_search_complete(self.work["reads"], self.fields))

    def test_missing_detail_invalidates_old_target_and_old_fields_are_not_new_facts(self):
        event = {"id": "real-provider-id", "status": "confirmed", "fields": dict(self.fields, title="旧标题", description="旧说明")}
        self.merge(events=[event])
        changed = deepcopy(event)
        changed["fields"].pop("description")
        self.merge(events=[changed])
        self.assertNotIn("description", self.work["records"]["c1"]["fields"])
        self.assertEqual(self.work["records"]["c1"]["sources"][0]["fields"]["description"], "旧说明")
        read = self.merge({"kind": "get_events", "event_refs": ["c1"]})
        self.assertEqual(read["read_status"], "PARTIAL")
        self.assertFalse(self.work["records"]["c1"]["read_verified"])

    def test_unsupported_calendar_interval_is_not_reported_as_known_no_overlap(self):
        self.merge(events=[{"id":"all-day", "status":"confirmed", "fields": {
            "title":"全天安排", "all_day":True, "start_date":"2026-11-11", "end_date":"2026-11-12"}}])
        report = overlaps({"kind":"CREATE", "changes":self.fields}, self.work)
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual(report["unknown_interval_refs"], ["c1"])


class IndependentActivityChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = PlanningStore(Path(temporary.name) / "state.sqlite")
        self.context = UpstreamContext("me", "Asia/Shanghai", "c", "s")
        self.reader = ConstructedReader()

    async def test_failed_activity_does_not_block_an_independent_successful_one(self):
        async def reader(*, query, context):
            if query["range"]["start_at"].startswith("2026-11-10"):
                return {"read_status": "FAILED", "reason": "constructed unavailable window", "events": []}
            return await self.reader(query=query, context=context)
        first = read_request()
        first["items"][0]["activity_ref"] = "failed-activity"
        first["items"][0]["next_read"]["range"] = {"start_at": "2026-11-10T00:00:00+08:00", "end_at": "2026-11-11T00:00:00+08:00"}
        good = read_request()["items"][0]
        good["activity_ref"] = "good-activity"
        first["items"].append(good)
        final = decision()
        final["items"][0]["activity_ref"] = "good-activity"
        final["items"].append({"activity_ref": "failed-activity", "decision": "NO_ACTION", "reason": "未取得日历", "evidence_message_ids": ["m1"]})
        row = await ConversationPlanner(self.store, context=self.context, calendar_reader=reader).analyze(request(), ScriptedModel(first, final))
        self.assertEqual([i["decision"] for i in row["result"]["items"]], ["PROPOSE", "FAILED"])
        self.assertEqual(len(row["result"]["operations"]), 1)

    async def test_model_budget_stop_keeps_a_valid_independent_proposal(self):
        second = decision()
        second["items"][0]["activity_ref"] = "a1"
        more = read_request()["items"][0]
        more["activity_ref"] = "a2"
        more["next_read"]["range"] = {"start_at": "2026-11-12T00:00:00+08:00", "end_at": "2026-11-13T00:00:00+08:00"}
        more.update(clarification_question="哪一天？", missing_facts=["日期"])
        second["items"].append(more)
        model = ScriptedModel(read_request(), second)
        row = await ConversationPlanner(self.store, context=self.context, calendar_reader=self.reader,
            limits=PlanningLimits(max_model_calls=2)).analyze(request(), model)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual([i["decision"] for i in row["result"]["items"]], ["PROPOSE", "FAILED"])
        self.assertIn("model_budget_exhausted", row["result"]["item_errors"][0]["code"])

    async def test_read_timeout_is_explicit_and_no_late_fact_is_committed(self):
        async def slow(**kwargs):
            await asyncio.sleep(2)
            return {"read_status": "COMPLETE", "events": []}
        planner = ConversationPlanner(self.store, context=self.context, calendar_reader=slow,
                                      limits=PlanningLimits(read_timeout_seconds=1))
        final = {"items": [{"decision": "NO_ACTION", "reason": "没有取得日历", "evidence_message_ids": ["m1"]}]}
        row = await planner.analyze(request(), ScriptedModel(read_request(), final))
        self.assertEqual(row["result"]["items"][0]["decision"], "FAILED")
        self.assertFalse(row["result"]["records"])
        read = next(t for t in self.store.traces(row["id"]) if t["kind"] == "READ")
        self.assertEqual(read["error"]["type"], "TimeoutError")
        self.assertGreaterEqual(read["metadata"]["elapsed_seconds"], 1)

    async def test_invalid_later_envelope_does_not_erase_known_independent_proposal(self):
        first = decision("CREATE")
        first["items"][0]["activity_ref"] = "ready"
        more = read_request()["items"][0]
        more["activity_ref"] = "pending"
        more["next_read"]["range"] = {"start_at":"2026-11-12T00:00:00+08:00", "end_at":"2026-11-13T00:00:00+08:00"}
        first["items"].append(more)
        # The CREATE needs prior coverage; retain its independent confirmed plan
        # after a later model response fails its envelope, rather than losing it.
        model = ScriptedModel(read_request(), first, {"broken": True}, {"broken": True})
        row = await ConversationPlanner(self.store, context=self.context, calendar_reader=self.reader).analyze(request(), model)
        self.assertEqual(len(row["result"]["operations"]), 1)
        self.assertEqual(row["result"]["items"][0]["decision"], "PROPOSE")
        self.assertEqual(len(model.calls), 4)
