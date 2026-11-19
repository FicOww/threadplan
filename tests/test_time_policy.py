"""V1 deterministic time/field contracts; constructed operations, no inference."""

from copy import deepcopy
import unittest

from threadplan import InputError, UpstreamContext
from threadplan.operations import resolve_operation
from threadplan.timeops import add_minutes, instant


class TimePolicyChecks(unittest.TestCase):
    def setUp(self):
        self.records = {"c1": {"status": "confirmed", "read_verified": True, "fields": {
            "title": "原安排", "start_at": "2026-11-11T14:00:00+08:00",
            "end_at": "2026-11-11T15:00:00+08:00", "description": "保留备注", "location": "306"}}}

    def compile(self, kind="CREATE", fields=None, **operation):
        op = {"kind": kind, **operation}
        op["fields" if kind == "CREATE" else "changes"] = fields or {}
        if kind != "CREATE":
            op["event_ref"] = "c1"
        return resolve_operation({"operation": op, "evidence_message_ids": ["m1"]}, self.records)

    def test_default_configuration_resolves_to_product_thirty_minutes(self):
        context = UpstreamContext("me", "Asia/Shanghai", "c", "s")
        self.assertEqual(context.default_duration_minutes, 30)
        self.assertEqual(context.duration_policy_source, "PRODUCT_DEFAULT")
        self.assertEqual(context.to_dict()["default_duration_minutes"], 30)
        explicit = UpstreamContext("me", "Asia/Shanghai", "c", "s", default_duration_minutes=45)
        self.assertEqual(explicit.duration_policy_source, "UPSTREAM_DEFAULT_DURATION")
        self.assertEqual(UpstreamContext("me", "Asia/Shanghai", "c", "s", scope_mode="team").scope_mode, "team")
        with self.assertRaises(InputError):
            UpstreamContext("me", "Asia/Shanghai", "c", "s", scope_mode="anything")

    def test_default_duration_crosses_midnight_without_inventing_start(self):
        result = self.compile(fields={"title": "安排", "start_at": "2026-11-11T23:50:00+08:00"})
        self.assertEqual(result["changes"]["end_at"], "2026-11-12T00:20:00+08:00")
        self.assertEqual(result["metadata"]["policy_assumptions"][0]["basis"], "PRODUCT_DEFAULT")
        with self.assertRaisesRegex(ValueError, "incomplete_create"):
            self.compile(fields={"title": "缺开始"})

    def test_start_only_update_keeps_actual_duration_and_unmentioned_fields(self):
        result = self.compile("UPDATE", {"start_at": "2026-11-11T14:30:00+08:00"})
        self.assertEqual(result["changes"], {"start_at": "2026-11-11T14:30:00+08:00",
                                            "end_at": "2026-11-11T15:30:00+08:00"})
        self.assertTrue(result["metadata"]["preserve_duration"])
        self.assertEqual(self.records["c1"]["fields"]["description"], "保留备注")

    def test_explicit_duration_or_end_can_change_duration(self):
        for fields in ({"start_at": "2026-11-11T14:30:00+08:00", "duration_minutes": 20},
                       {"start_at": "2026-11-11T14:30:00+08:00", "end_at": "2026-11-11T14:50:00+08:00"}):
            self.assertEqual(self.compile("UPDATE", fields)["changes"]["end_at"], "2026-11-11T14:50:00+08:00")
        with self.assertRaisesRegex(ValueError, "duration_change_requires_end_or_duration"):
            self.compile("UPDATE", {"start_at": "2026-11-11T14:30:00+08:00"}, preserve_duration=False)

    def test_shift_is_elapsed_arithmetic_and_does_not_rewrite_notes(self):
        shifted = self.compile("UPDATE", shift_minutes=-30)
        self.assertEqual(shifted["changes"], {"start_at": "2026-11-11T13:30:00+08:00",
                                             "end_at": "2026-11-11T14:30:00+08:00"})
        self.assertEqual(shifted["metadata"]["shift_minutes"], -30)
        with self.assertRaisesRegex(ValueError, "shift_requires"):
            self.compile("UPDATE", {"start_at": "2026-11-11T15:00:00+08:00"}, shift_minutes=30)

    def test_required_field_must_be_present_in_effective_written_event(self):
        item = {"required_write_fields": ["description"], "evidence_message_ids": ["m1"],
                "operation": {"kind": "CREATE", "fields": {"title": "安排", "start_at": "2026-11-11T14:00:00+08:00"}}}
        original = deepcopy(item)
        with self.assertRaisesRegex(ValueError, "missing_required_write_field:description"):
            resolve_operation(item, {})
        self.assertEqual(item, original)
        item["operation"] = {"kind": "UPDATE", "event_ref": "c1", "changes": {"title": "改标题"}}
        self.assertEqual(resolve_operation(item, self.records)["changes"], {"title": "改标题"})

    def test_nonexistent_time_or_incorrect_named_zone_offset_is_rejected(self):
        for start in ("2026-03-08T02:30:00-05:00", "2026-07-01T10:00:00-05:00"):
            with self.assertRaisesRegex(ValueError, "timezone_offset_mismatch"):
                self.compile(fields={"title": "DST", "start_at": start, "timezone": "America/New_York"})
        with self.assertRaisesRegex(ValueError, "invalid_event_timezone"):
            self.compile(fields={"title": "DST", "start_at": "2026-11-11T14:00:00+08:00", "timezone": "Invalid/Zone"})

    def test_elapsed_duration_across_dst_produces_correct_local_offset(self):
        result = self.compile(fields={"title": "DST", "start_at": "2026-03-08T01:45:00-05:00",
                                      "duration_minutes": 30, "timezone": "America/New_York"})
        self.assertEqual(result["changes"]["end_at"], "2026-03-08T03:15:00-04:00")
        shifted = add_minutes("2026-11-01T01:45:00-04:00", 30, "America/New_York")
        self.assertEqual(shifted.isoformat(), "2026-11-01T01:15:00-05:00")
        # Explicit offsets disambiguate repeated wall times; naive times do not.
        self.assertNotEqual(instant("2026-11-01T01:15:00-04:00", "America/New_York").timestamp(),
                            instant("2026-11-01T01:15:00-05:00", "America/New_York").timestamp())
        with self.assertRaisesRegex(ValueError, "invalid_aware_time"):
            instant("2026-11-01T01:15:00", "America/New_York")

    def test_nonfinite_and_boolean_durations_and_shifts_are_rejected(self):
        for value in (float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                self.compile("UPDATE", shift_minutes=value)
            with self.assertRaises(ValueError):
                self.compile(fields={"title": "安排", "start_at": "2026-11-11T14:00:00+08:00", "duration_minutes": value})

    def test_misplaced_mutation_fields_cannot_silently_turn_into_cancellation(self):
        item = {"operation":{"kind":"CANCEL","event_ref":"c1","fields":{"title":"应当改标题"}},
                "evidence_message_ids":["m1"]}
        with self.assertRaisesRegex(ValueError, "invalid_mutation_container"):
            resolve_operation(item, self.records)
        item["operation"] = {"kind":"CREATE", "changes":{"title":"错误容器"}}
        with self.assertRaisesRegex(ValueError, "invalid_mutation_container"):
            resolve_operation(item, {})
