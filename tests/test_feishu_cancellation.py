"""Contract checks based on the live deletion response; no external calls."""

import unittest
from unittest.mock import patch
from threadplan.adapters.feishu import FeishuCalendar, FeishuError


class CancellationChecks(unittest.TestCase):
    def setUp(self):
        self.calendar = FeishuCalendar("test-calendar", cli=object(), managed_event_ids={"test-event"})

    def test_live_observed_deleted_code_is_evidence_not_a_fake_event(self):
        error = FeishuError("request failed", details={"type": "api", "code": 193003, "message": "event is deleted"})
        with patch.object(self.calendar, "get_event", side_effect=error):
            result = self.calendar.verify_cancelled("test-event", approved=True)
        self.assertTrue(result["verified"])
        self.assertEqual(result["provider_error_code"], 193003)
        self.assertNotIn("event", result)

    def test_auth_or_unrelated_error_never_counts_as_deletion(self):
        for details in ({"type": "authentication", "code": 193003, "message": "event is deleted"},
                        {"type": "api", "code": 99991679, "message": "missing scope"}):
            with self.subTest(details=details), patch.object(self.calendar, "get_event", side_effect=FeishuError("error", details=details)):
                with self.assertRaises(FeishuError):
                    self.calendar.verify_cancelled("test-event", approved=True)

    def test_explicit_cancelled_status_and_still_confirmed_status(self):
        for status, verified in (("cancelled", True), ("confirmed", False)):
            with patch.object(self.calendar, "get_event", return_value={"status": status}):
                result = self.calendar.verify_cancelled("test-event", approved=True)
                self.assertEqual(result["verified"], verified)

    def test_unknown_managed_id_does_not_trigger_a_read(self):
        with patch.object(self.calendar, "get_event") as read:
            with self.assertRaises(FeishuError):
                self.calendar.verify_cancelled("unmanaged", approved=True)
            read.assert_not_called()
