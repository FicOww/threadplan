"""Constructed host/ledger checks with an explicit FakeCalendar, never live Feishu."""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from threadplan_host.feishu import FeishuHost, normalize_event
from threadplan.adapters.feishu import BASELINE_FIELDS, FeishuApprovalRequired, FeishuError, MANAGED_MARKER, TEST_PREFIX, _managed_description
from threadplan.contracts import Proposal


def proposal(operation="CREATE", changes=None, target=None):
    return Proposal(operation, "cancelled" if operation == "CANCEL" else "confirmed", ("m1",),
                    changes if changes is not None else {"title": "Constructed dinner",
                        "start_at": "2026-11-13T19:00:00+08:00", "end_at": "2026-11-13T20:00:00+08:00"}, target)


def provider_time(value):
    return {"timestamp": str(int(datetime.fromisoformat(value).timestamp())), "timezone": "Asia/Shanghai"}


class FakeCalendar:
    """Explicit fake provider records request contracts; it does not call Feishu."""
    def __init__(self):
        self.calendar_id = "fake-test-calendar"
        self.managed_event_ids = {"unrelated-old-cli-test"}
        self.raw = {}
        self.calls = []
        self.read_error = None
        self.write_error = None
        self.fail_readback = False

    def get_calendar(self, *, approved=False):
        assert approved is True
        self.calls.append(("calendar_get",))
        if self.read_error:
            raise self.read_error
        return {"calendar_id": self.calendar_id, "summary": "ThreadPlan Test", "type": "shared",
                "permissions": "private", "role": "owner"}

    def get_event(self, identifier, *, approved=False):
        assert approved is True
        self.get_calendar(approved=True)
        self.calls.append(("get", identifier))
        if self.read_error or self.fail_readback:
            raise self.read_error or FeishuError("readback unavailable")
        return deepcopy(self.raw[identifier])

    def prepare_write(self, proposal, baseline, approval_key, *, approved=False, authorize_target=None):
        assert approved is True
        if proposal.operation == "CREATE":
            self.get_calendar(approved=True)
            return lambda: self.create_event(proposal, approval_key, approved=True)
        current = self.get_event(proposal.target_event_id, approved=True)
        if any(current.get(key) != baseline.get(key) for key in BASELINE_FIELDS):
            raise FeishuError("Event changed since approval baseline; obtain fresh approval")
        authorize_target(current)
        if proposal.operation == "UPDATE":
            return lambda: self.update_event(proposal, baseline, approval_key, approved=True)
        return lambda: self.cancel_event(proposal, approval_key, approved=True)

    def find_events(self, range, *, approved=False):
        assert approved is True
        self.get_calendar(approved=True)
        low, high = (datetime.fromisoformat(range[name]).timestamp() for name in ("start_at", "end_at"))
        return {"items": [deepcopy(raw) for raw in self.raw.values()
                          if int(raw["start_time"]["timestamp"]) < high
                          and low < int(raw["end_time"]["timestamp"])]}

    def create_event(self, proposal, approval_key, *, approved=False):
        assert approved is True
        self.calls.append(("create", approval_key, deepcopy(proposal.changes)))
        if self.write_error:
            raise self.write_error
        identifier = "fake-created-" + str(len(self.raw))
        self.raw[identifier] = {"event_id": identifier, "summary": TEST_PREFIX + proposal.changes["title"],
            "description": _managed_description(proposal.changes.get("description")), "start_time": provider_time(proposal.changes["start_at"]),
            "end_time": provider_time(proposal.changes["end_at"]), "status": "confirmed",
            "visibility": "private", "vchat": {"vc_type": "no_meeting"}, "reminders": []}
        self.managed_event_ids.add(identifier)
        return deepcopy(self.raw[identifier])

    def update_event(self, proposal, baseline, approval_key, *, approved=False):
        assert approved is True
        self.calls.append(("update", approval_key, deepcopy(proposal.changes), deepcopy(baseline)))
        if self.write_error:
            raise self.write_error
        raw = self.raw[proposal.target_event_id]
        for key, value in proposal.changes.items():
            if key in {"start_at", "end_at"}:
                raw[{"start_at": "start_time", "end_at": "end_time"}[key]] = provider_time(value)
            elif key == "title":
                raw["summary"] = TEST_PREFIX + value
            elif key == "location":
                raw["location"] = {"name": value}
            elif key == "description":
                raw['description']=_managed_description(value) if proposal.target_event_id in self.managed_event_ids else value
        return deepcopy(raw)

    def cancel_event(self, proposal, approval_key, *, approved=False):
        assert approved is True
        self.calls.append(("cancel", approval_key))
        if self.write_error:
            raise self.write_error
        self.raw[proposal.target_event_id]["status"] = "cancelled"
        return {}

    def verify_cancelled(self, identifier, *, approved=False):
        assert approved is True
        self.calls.append(("verify_cancelled", identifier))
        return {"verified": self.raw[identifier]["status"] == "cancelled", "evidence": "fake_provider_status"}


class FeishuHostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "ledger.sqlite"
        self.fake = FakeCalendar()
        self.host = FeishuHost(self.fake, self.path)

    def create(self):
        return self.host.execute(proposal(), "create-key", {}, approved=True)

    def writes(self):
        return [call for call in self.fake.calls if call[0] in {"create", "update", "cancel"}]

    def test_approval_and_explicit_participant_exclusion(self):
        with self.assertRaises(FeishuApprovalRequired):
            self.host.execute(proposal(), "key", {})
        participant_proposal = proposal(changes=dict(proposal().changes, participants=["alex"]))
        with self.assertRaises(FeishuError):
            self.host.execute(participant_proposal, "key", {}, approved=True)
        self.assertEqual(self.fake.calls, [])
        result = self.host.execute(participant_proposal, "key", {}, approved=True, exclude_attendees=True)
        self.assertEqual(result["excluded_participants"], ["alex"])
        self.assertNotIn("participants", self.writes()[0][2])
        self.assertFalse(result["attendee_sync"])
        self.assertFalse(result["reminders"])

    def test_snapshot_reads_only_host_created_ids_and_normalizes_utc(self):
        self.assertEqual(self.fake.managed_event_ids, set())
        events, baselines = self.host.snapshot()
        self.assertEqual((events, baselines), ([], {}))
        self.assertFalse(self.writes())
        result = self.create()
        self.fake.calls.clear()
        events, baselines = self.host.snapshot()
        self.assertEqual(events[0].id, result["event_id"])
        self.assertEqual(events[0].fields["title"], "Constructed dinner")
        self.assertEqual(events[0].fields["start_at"], "2026-11-13T11:00:00+00:00")
        self.assertEqual([call for call in self.fake.calls if call[0] == "get"], [("get", result["event_id"])])
        self.assertTrue(baselines[result["event_id"]]["summary"].startswith(TEST_PREFIX))
        self.assertTrue(self.host.status["connected"])

    def test_successful_create_receipt_survives_restart_and_blocks_changed_request(self):
        original = self.create()
        restarted = FeishuHost(self.fake, self.path)
        self.fake.calls.clear()
        retry = restarted.execute(proposal(), "create-key", {}, approved=True)
        self.assertTrue(retry["already_applied"])
        self.assertEqual(retry["event_id"], original["event_id"])
        self.assertEqual(self.fake.calls, [])
        with self.assertRaises(FeishuError):
            restarted.execute(proposal(changes=dict(proposal().changes, title="Changed")), "create-key", {}, approved=True)

    def test_unknown_write_outcome_is_durable_and_never_automatically_retried(self):
        self.fake.write_error = FeishuError("timeout; outcome unknown")
        with self.assertRaises(FeishuError):
            self.create()
        self.fake.write_error = None
        restarted = FeishuHost(self.fake, self.path)
        with self.assertRaises(FeishuError) as caught:
            restarted.execute(proposal(), "create-key", {}, approved=True)
        self.assertEqual(caught.exception.details["state"], "outcome_unknown")
        with self.assertRaises(FeishuError):
            restarted.recover("create-key", approved=True)
        self.assertEqual(len(self.writes()), 1)

    def test_unresolved_target_blocks_different_approval_key(self):
        identifier = self.create()["event_id"]
        _, baselines = self.host.snapshot()
        update = proposal("UPDATE", {"title": "Changed"}, identifier)
        self.fake.write_error = FeishuError("timeout; outcome unknown")
        with self.assertRaises(FeishuError):
            self.host.execute(update, "first-update", baselines, approved=True)
        self.fake.write_error = None
        with self.assertRaises(FeishuError):
            self.host.execute(update, "second-update", baselines, approved=True)
        self.assertEqual(len(self.writes()), 2)

    def test_readback_mismatch_does_not_report_success(self):
        identifier = self.create()["event_id"]
        _, baselines = self.host.snapshot()
        update = proposal("UPDATE", {"title": "Requested new title"}, identifier)
        with patch.object(self.fake, "update_event", return_value=deepcopy(self.fake.raw[identifier])):
            with self.assertRaises(FeishuError) as caught:
                self.host.execute(update, "ignored-update", baselines, approved=True)
        self.assertEqual(caught.exception.details["field"], "title")
        with self.host.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM receipts WHERE approval_key='ignored-update'").fetchone()[0], "verify_pending")
        with self.assertRaises(FeishuError):
            self.host.recover("ignored-update", approved=True)
        self.assertEqual(len(self.writes()), 1)

    def test_returned_create_id_survives_readback_failure(self):
        self.fake.fail_readback = True
        with self.assertRaises(FeishuError):
            self.create()
        identifier = next(iter(self.fake.raw))
        restarted = FeishuHost(self.fake, self.path)
        self.assertIn(identifier, restarted.calendar.managed_event_ids)
        self.fake.fail_readback = False
        events, _ = restarted.snapshot()
        self.assertEqual(events[0].id, identifier)
        recovered = restarted.execute(proposal(), "create-key", {}, approved=True)
        self.assertTrue(recovered["recovered_verification"])
        self.assertEqual(recovered["event_id"], identifier)
        self.assertEqual(len(self.writes()), 1)

    def test_cancel_acknowledgment_is_reverified_without_resending_delete(self):
        identifier = self.create()["event_id"]
        _, baselines = self.host.snapshot()
        with patch.object(self.fake, "verify_cancelled", side_effect=FeishuError("verification temporarily unavailable")):
            with self.assertRaises(FeishuError):
                self.host.execute(proposal("CANCEL", {}, identifier), "cancel-read-failure", baselines, approved=True)
        restarted = FeishuHost(self.fake, self.path)
        self.assertTrue(restarted.recover("cancel-read-failure", approved=True)["cancellation_verification"]["verified"])
        self.assertEqual(len([call for call in self.writes() if call[0] == "cancel"]), 1)
        self.assertEqual(restarted.snapshot(), ([], {}))

    def test_definite_prewrite_failure_can_be_retried_under_same_frozen_approval(self):
        self.fake.read_error = FeishuError("read failed before any write")
        with self.assertRaises(FeishuError):
            self.create()
        self.assertEqual(self.writes(), [])
        with self.host.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM receipts WHERE approval_key='create-key'").fetchone()[0], "failed_before_write")
        self.fake.read_error = None
        with self.assertRaises(FeishuError):
            self.host.execute(proposal(changes=dict(proposal().changes, title="Different request")),
                              "create-key", {}, approved=True)
        self.assertEqual(self.writes(), [])
        result = self.create()
        self.assertTrue(result["event_id"])
        self.assertEqual(len(self.writes()), 1)

    def test_recovery_requires_approval_and_never_creates_an_unknown_receipt(self):
        with self.assertRaises(FeishuApprovalRequired):
            self.host.recover("not-present")
        with self.assertRaises(FeishuError) as caught:
            self.host.recover("not-present", approved=True)
        self.assertEqual(caught.exception.details["state"], "not_started")
        self.assertEqual(self.fake.calls, [])

    def test_unknown_update_can_be_read_reconciled_without_claiming_an_acknowledgment(self):
        identifier = self.create()["event_id"]
        _, baselines = self.host.snapshot()
        update = proposal("UPDATE", {"title": "Applied before response was lost"}, identifier)
        original = self.fake.update_event
        def update_then_timeout(*args, **kwargs):
            original(*args, **kwargs)
            raise FeishuError("constructed lost update response")
        with patch.object(self.fake, "update_event", side_effect=update_then_timeout):
            with self.assertRaises(FeishuError):
                self.host.execute(update, "unknown-applied-update", baselines, approved=True)
        result = FeishuHost(self.fake, self.path).recover("unknown-applied-update", approved=True)
        self.assertFalse(result["provider_acknowledged"])
        self.assertEqual(result["event"]["fields"]["title"], update.changes["title"])
        self.assertEqual(len(self.writes()), 2)

    def test_unknown_cancel_can_verify_deletion_without_a_second_delete(self):
        identifier = self.create()["event_id"]
        _, baselines = self.host.snapshot()
        original = self.fake.cancel_event
        def cancel_then_timeout(*args, **kwargs):
            original(*args, **kwargs)
            raise FeishuError("constructed lost delete response")
        with patch.object(self.fake, "cancel_event", side_effect=cancel_then_timeout):
            with self.assertRaises(FeishuError):
                self.host.execute(proposal("CANCEL", {}, identifier), "unknown-applied-cancel", baselines, approved=True)
        result = FeishuHost(self.fake, self.path).recover("unknown-applied-cancel", approved=True)
        self.assertFalse(result["provider_acknowledged"])
        self.assertTrue(result["cancellation_verification"]["verified"])
        self.assertEqual(len([call for call in self.writes() if call[0] == "cancel"]), 1)

    def test_update_and_cancel_require_analysis_baseline_and_reject_stale(self):
        identifier = self.create()["event_id"]
        _, baselines = self.host.snapshot()
        update = proposal("UPDATE", {"start_at": "2026-11-13T19:30:00+08:00"}, identifier)
        with self.assertRaises(FeishuError):
            self.host.execute(update, "missing", {}, approved=True)
        self.fake.raw[identifier]["location"] = {"name": "External edit"}
        for operation in (update, proposal("CANCEL", {}, identifier)):
            with self.assertRaises(FeishuError):
                self.host.execute(operation, operation.operation, baselines, approved=True)
        self.assertEqual(len(self.writes()), 1)

    def test_update_reads_back_real_provider_event_and_retry_does_not_write(self):
        identifier = self.create()["event_id"]
        _, baselines = self.host.snapshot()
        update = proposal("UPDATE", {"start_at": "2026-11-13T19:30:00+08:00"}, identifier)
        result = self.host.execute(update, "update-key", baselines, approved=True)
        self.assertEqual(result["event"]["fields"]["start_at"], "2026-11-13T11:30:00+00:00")
        retry = self.host.execute(update, "update-key", baselines, approved=True)
        self.assertTrue(retry["already_applied"])
        self.assertEqual(len(self.writes()), 2)

    def test_cancel_is_verified_and_successful_receipt_can_be_replayed_after_restart(self):
        identifier = self.create()["event_id"]
        _, baselines = self.host.snapshot()
        cancel = proposal("CANCEL", {}, identifier)
        result = self.host.execute(cancel, "cancel-key", baselines, approved=True)
        self.assertTrue(result["cancellation_verification"]["verified"])
        self.assertEqual(result["provider_response"], {})
        restarted = FeishuHost(self.fake, self.path)
        self.fake.calls.clear()
        retry = restarted.execute(cancel, "cancel-key", baselines, approved=True)
        self.assertTrue(retry["already_applied"])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(restarted.snapshot(), ([], {}))

    def test_provider_read_error_is_not_masked_by_cached_status_or_empty_snapshot(self):
        self.create()
        self.fake.read_error = FeishuError("auth expired", details={"type": "authorization"})
        with self.assertRaises(FeishuError):
            self.host.snapshot()
        self.assertFalse(self.host.status["connected"])
        self.assertEqual(self.host.status["error"], "auth expired")

    def test_deleted_error_for_active_event_is_not_silently_treated_as_cancelled(self):
        identifier = self.create()["event_id"]
        with patch.object(self.fake, "get_event", side_effect=FeishuError("event is deleted", details={"code": 193003})):
            with self.assertRaises(FeishuError):
                self.host.snapshot()
        with self.host.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM managed_events WHERE event_id=?", (identifier,)).fetchone()[0], "active")

    def test_ledger_cannot_be_reused_for_another_calendar(self):
        other = FakeCalendar()
        other.calendar_id = "another-calendar"
        with self.assertRaises(FeishuError):
            FeishuHost(other, self.path)

    def test_connection_file_does_not_adopt_cleanup_ids(self):
        connection = self.path.parent / "connection.json"
        connection.write_text(json.dumps({"provider": "feishu", "identity": "user", "summary": "ThreadPlan Test",
            "calendar_id": self.fake.calendar_id, "event_id": "unrelated-old-cli-test"}), encoding="utf-8")
        with patch("threadplan_host.feishu.FeishuCalendar", return_value=self.fake):
            host = FeishuHost.from_connection(connection, self.path)
        self.assertEqual(host.calendar.managed_event_ids, set())
        self.assertEqual(self.fake.calls, [])

    def test_normalization_rejects_all_day_and_fractional_seconds(self):
        identifier = self.create()["event_id"]
        for start in ({"date": "2026-11-13"}, {"timestamp": "1794567600.1"}, {"timestamp": True}):
            raw = dict(self.fake.raw[identifier], start_time=start)
            with self.assertRaises(FeishuError):
                normalize_event(raw)


if __name__ == "__main__":
    unittest.main()
