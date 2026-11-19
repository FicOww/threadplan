"""Constructed transport/contract tests only. No network calls or live inference."""

from copy import deepcopy
import json
import subprocess
import unittest
from unittest.mock import patch

from threadplan.adapters.feishu import (FeishuApprovalRequired, FeishuCalendar,
    FeishuError, LarkCLI, MANAGED_MARKER, TEST_PREFIX, event_description, event_record, _managed_description)
from scripts.feishu_test_support import create_private_test_calendar
from threadplan.contracts import Proposal


CALENDAR = {"calendar_id": "test-calendar", "type": "shared", "summary": "ThreadPlan Test",
            "permissions": "private", "role": "owner"}


def proposal(operation="CREATE", changes=None, target=None):
    return Proposal(operation, "cancelled" if operation == "CANCEL" else "confirmed", ("m1",),
        changes if changes is not None else {"title": "Constructed test", "start_at": "2026-11-07T18:00:00+08:00",
                                           "end_at": "2026-11-07T19:00:00+08:00"}, target)


class FakeTransport:
    """Explicit in-memory fake used to inspect requests; it is not Feishu."""
    def __init__(self):
        self.calendar = deepcopy(CALENDAR)
        self.event = None
        self.calls = []

    def invoke(self, resource, method, *, params=None, data=None, approved=False):
        assert approved is True
        self.calls.append((resource, method, deepcopy(params), deepcopy(data)))
        if resource == "calendars":
            return {"calendar": deepcopy(self.calendar)} if method == "create" else deepcopy(self.calendar)
        if method == "create":
            self.event = dict(deepcopy(data), event_id="fake-event-id")
        if method == "patch":
            self.event.update(deepcopy(data))
        if method == "delete":
            self.event["status"] = "cancelled"
            return {}
        return {"event": deepcopy(self.event)}


class FeishuAdapterTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTransport()
        self.adapter = FeishuCalendar("test-calendar", cli=self.fake)

    def create(self):
        return self.adapter.create_event(proposal(), "approval-1", approved=True)

    def test_approval_is_not_inferred_for_every_external_entrypoint(self):
        calls = [lambda: self.adapter.get_calendar(), lambda: self.adapter.get_event("e"),
                 lambda: self.adapter.create_event(proposal(), "key"),
                 lambda: self.adapter.update_event(proposal("UPDATE", {"title": "x"}, "e"), {}, "key"),
                 lambda: self.adapter.cancel_event(proposal("CANCEL", {}, "e"), "key"),
                 lambda: create_private_test_calendar(cli=self.fake)]
        for call in calls:
            with self.assertRaises(FeishuApprovalRequired):
                call()
        self.assertEqual(self.fake.calls, [])

    def test_calendar_creation_and_safety_gate(self):
        self.assertEqual(create_private_test_calendar(cli=self.fake, approved=True), CALENDAR)
        self.assertEqual(self.fake.calls[-1][3]["permissions"], "private")
        with self.assertRaises(FeishuError):
            FeishuCalendar("primary", cli=self.fake)
        for key, value in (("is_deleted", True), ("is_third_party", True),
                           ("role", "reader"), ("calendar_id", "wrong")):
            self.fake.calendar = dict(CALENDAR, **{key: value})
            with self.subTest(key=key), self.assertRaises(FeishuError):
                self.create()
        self.assertFalse(any(method == "create" and resource == "events" for resource, method, _, _ in self.fake.calls))

    def test_create_request_no_notifications_meetings_or_guests(self):
        result = self.create()
        self.assertEqual(result["event_id"], "fake-event-id")
        _, _, params, body = self.fake.calls[-1]
        self.assertEqual(body["start_time"], {"timestamp": "1794045600", "timezone": "UTC"})
        self.assertEqual(body["summary"], "Constructed test")
        self.assertEqual(body["description"], "")
        self.assertEqual(body["vchat"], {"vc_type": "no_meeting"})
        self.assertEqual(body["visibility"], "private")
        self.assertEqual(body["free_busy_status"], "free")
        self.assertEqual(body["reminders"], [])
        self.assertIs(body["need_notification"], False)
        self.assertNotIn("attendees", body)
        self.assertIn("idempotency_key", params)
        self.create()
        self.assertEqual(params, self.fake.calls[-1][2])
        self.adapter.create_event(proposal(), "other", approved=True)
        self.assertNotEqual(params["idempotency_key"], self.fake.calls[-1][2]["idempotency_key"])

    def test_description_create_and_patch_preserve_user_text_without_touching_time(self):
        value=proposal(changes=dict(proposal().changes,description='带身份证原件。'))
        raw=self.adapter.create_event(value,'notes-create',approved=True)
        self.assertEqual(event_description(raw),'带身份证原件。')
        self.assertEqual(raw['description'],'带身份证原件。')
        changed=self.adapter.update_event(proposal('UPDATE',{'description':'改为带护照。'},raw['event_id']),
                                          raw,'notes-update',approved=True)
        body=self.fake.calls[-1][3]
        self.assertNotIn('start_time',body)
        self.assertNotIn('end_time',body)
        self.assertEqual(event_description(changed),'改为带护照。')
        self.assertEqual(changed['description'],'改为带护照。')
        self.adapter.managed_event_ids.clear()
        self.adapter.authorized_event_ids.add(raw['event_id'])
        self.fake.event['description']='上游原备注'
        baseline=deepcopy(self.fake.event)
        result=self.adapter.update_event(proposal('UPDATE',{'description':'上游新备注'},raw['event_id']),
                                         baseline,'external-notes-update',approved=True)
        self.assertEqual(result['description'],'上游新备注')

    def test_partial_calendar_creation_fetches_details_and_preserves_id_on_failure(self):
        with patch.object(self.fake, "invoke", side_effect=[
            {"calendar": {"calendar_id": "test-calendar"}}, deepcopy(CALENDAR)]) as invoke:
            result = create_private_test_calendar(cli=self.fake, approved=True)
            self.assertEqual(result, CALENDAR)
            self.assertEqual(invoke.call_count, 2)
        with patch.object(self.fake, "invoke", side_effect=[
            {"calendar": {"calendar_id": "test-calendar"}}, FeishuError("read unavailable")]):
            with self.assertRaises(FeishuError) as caught:
                create_private_test_calendar(cli=self.fake, approved=True)
            self.assertEqual(caught.exception.details["created_calendar_id"], "test-calendar")
            self.assertTrue(caught.exception.details["outcome_known_created"])

    def test_arbitrary_named_calendar_and_read_role_are_supported(self):
        self.fake.calendar.update(summary="My Projects", type="primary", permissions="public")
        raw = self.create()
        self.assertEqual(raw["summary"], "Constructed test")
        self.fake.calendar["role"] = "reader"
        self.adapter.get_calendar(approved=True)
        before = len([c for c in self.fake.calls if c[0:2] == ("events", "create")])
        with self.assertRaises(FeishuError):
            self.create()
        self.assertEqual(before, len([c for c in self.fake.calls if c[0:2] == ("events", "create")]))

    def test_legacy_marker_is_data_encoding_not_authorization(self):
        raw = self.create()
        self.fake.event.update(summary=TEST_PREFIX+"Old title", description=_managed_description("old notes"))
        baseline = deepcopy(self.fake.event)
        self.assertEqual(event_record(baseline)["fields"]["title"], "Old title")
        changed = self.adapter.update_event(proposal("UPDATE", {"title":"Changed", "description":"new notes"}, raw["event_id"]),
            baseline, "legacy-update", approved=True)
        self.assertEqual(changed["summary"], TEST_PREFIX+"Changed")
        self.assertEqual(event_description(changed), "new notes")
        literal = dict(changed, description="ordinary notes")
        self.assertEqual(event_record(literal)["fields"]["title"], TEST_PREFIX+"Changed")

    def test_partial_event_creation_keeps_returned_id_in_ledger(self):
        with patch.object(self.fake, "invoke", side_effect=[deepcopy(CALENDAR), {"event": {"event_id": "returned-id"}}]):
            result = self.create()
        self.assertEqual(result, {"event_id": "returned-id"})
        self.assertIn("returned-id", self.adapter.managed_event_ids)

    def test_create_rejects_incomplete_invalid_time_and_guests(self):
        base = proposal().changes
        invalid = [{"end_at": None}, {"start_at": "2026-11-07T18:00:00"},
                   {"start_at": "2026-11-07T18:00:00+00:60"},
                   {"start_at": "2026-11-07T18:00:00.100+08:00"},
                   {"end_at": base["start_at"]}, {"participants": ["someone"]}, {"title": " "}]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(FeishuError):
                self.adapter.create_event(proposal(changes=dict(base, **changes)), "key", approved=True)
        self.assertEqual(self.fake.calls, [])

    def test_update_merges_only_requested_time_and_preserves_other_fields(self):
        self.create()
        baseline = self.adapter.get_event("fake-event-id", approved=True)
        self.adapter.update_event(proposal("UPDATE", {"start_at": "2026-11-07T18:30:00+08:00"}, "fake-event-id"),
                                  baseline, "key", approved=True)
        _, method, params, body = self.fake.calls[-1]
        self.assertEqual(method, "patch")
        self.assertEqual(params["event_id"], "fake-event-id")
        self.assertEqual(body["end_time"], baseline["end_time"])
        self.assertEqual(set(body), {"start_time", "end_time", "need_notification"})

    def test_update_rejects_stale_baseline_and_impossible_merged_interval(self):
        baseline = self.create()
        change = proposal("UPDATE", {"title": "change"}, "fake-event-id")
        self.fake.event["summary"] += " edited"
        with self.assertRaises(FeishuError):
            self.adapter.update_event(change, baseline, "key", approved=True)
        baseline = deepcopy(self.fake.event)
        with self.assertRaises(FeishuError):
            self.adapter.update_event(proposal("UPDATE", {"start_at": "2026-11-08T18:00:00+08:00"}, "fake-event-id"),
                                      baseline, "key", approved=True)
        self.assertFalse(any(call[1] == "patch" for call in self.fake.calls))

    def test_title_update_does_not_resend_time(self):
        baseline = self.create()
        self.adapter.update_event(proposal("UPDATE", {"title": "new"}, "fake-event-id"), baseline, "key", approved=True)
        self.assertEqual(self.fake.calls[-1][3], {"summary": "new", "need_notification": False})

    def test_cancel_requires_trusted_ledger_not_title_markers_and_does_not_notify(self):
        self.create()
        cancel = proposal("CANCEL", {}, "fake-event-id")
        unmanaged = FeishuCalendar("test-calendar", cli=self.fake)
        with self.assertRaises(FeishuError):
            unmanaged.cancel_event(cancel, "key", approved=True)
        self.fake.event["description"] = "ordinary user notes"
        self.assertEqual(self.adapter.cancel_event(cancel, "key", approved=True), {})
        self.assertEqual(self.fake.calls[-1][2]["need_notification"], "false")


class CLIContractTests(unittest.TestCase):
    def invoke(self, result):
        with patch("threadplan.adapters.feishu.subprocess.run", return_value=result) as run:
            payload = LarkCLI("native.exe").invoke("events", "create", params={"calendar_id": "id"},
                data={"summary": "Text `literal` $(literal) & 中文"}, approved=True)
            return payload, run.call_args

    def test_top_level_ok_user_and_stdin_contract(self):
        payload, call = self.invoke(subprocess.CompletedProcess([], 0, '{"ok":true,"identity":"user","data":{"event":{"event_id":"real-response-id"}}}', ""))
        self.assertEqual(payload["event"]["event_id"], "real-response-id")
        argv = call.args[0]
        self.assertEqual(argv[argv.index("--as") + 1], "user")
        self.assertEqual(argv[-2:], ["--data", "-"])
        self.assertNotIn("--yes", argv)
        self.assertFalse(call.kwargs["shell"])
        self.assertIn("中文", json.loads(call.kwargs["input"])["summary"])

    def test_legacy_code_zero_is_not_success_and_bot_is_rejected(self):
        for output in ('{"code":0,"data":{}}', '{"ok":true,"identity":"bot","data":{}}', 'not json'):
            with self.subTest(output=output), self.assertRaises(FeishuError):
                self.invoke(subprocess.CompletedProcess([], 0, output, ""))

    def test_auth_error_and_exit10_remain_structured_without_retry(self):
        for code, error, exception in ((1, {"type": "authentication", "subtype": "missing_user_auth"}, FeishuError),
                                      (10, {"type": "confirmation", "action": "calendar events delete", "risk": "high-risk-write"}, FeishuApprovalRequired)):
            with patch("threadplan.adapters.feishu.subprocess.run", return_value=subprocess.CompletedProcess([], code, "", json.dumps({"ok": False, "error": error}))) as run:
                with self.assertRaises(exception) as caught:
                    LarkCLI("native.exe").invoke("events", "delete", approved=True)
                self.assertEqual(caught.exception.details, error)
                self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
