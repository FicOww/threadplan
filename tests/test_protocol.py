"""The model grammar describes the same field types the operation boundary needs."""

from copy import deepcopy
import unittest

from threadplan.codec import _schema_errors
from threadplan.protocol import model_schema


class ProtocolChecks(unittest.TestCase):
    def item(self, operation):
        return {"items": [{"decision":"PROPOSE", "reason":"构造协议检查", "evidence_message_ids":["m1"], "operation":operation}]}

    def test_unused_event_fields_are_not_required_for_cancel_or_partial_update(self):
        for operation in ({"kind":"CANCEL", "event_ref":"c1"},
                          {"kind":"UPDATE", "event_ref":"c1", "changes":{"description":"新备注"}},
                          {"kind":"CREATE", "fields":{"title":"安排", "start_at":"2026-11-20T14:00:00+08:00"}}):
            self.assertFalse(_schema_errors(self.item(operation), model_schema(), "response"))

    def test_participants_are_string_references_not_undeclared_object_variants(self):
        operation={"kind":"CREATE", "fields":{"participants":["me","未解析的人物引用"]}}
        self.assertFalse(_schema_errors(self.item(operation), model_schema(), "response"))
        malformed=deepcopy(operation)
        malformed["fields"]["participants"]=[{"description":"同事"},{"actor_id":"me"}]
        self.assertTrue(_schema_errors(self.item(malformed), model_schema(), "response"))

    def test_numeric_time_operations_reject_booleans_and_nonfinite_values(self):
        for value in (True, float("nan"), float("inf"), "30"):
            self.assertTrue(_schema_errors(self.item({"kind":"UPDATE","event_ref":"c1","shift_minutes":value}),model_schema(),"response"))
        self.assertFalse(_schema_errors(self.item({"kind":"UPDATE","event_ref":"c1","shift_minutes":-30}),model_schema(),"response"))

    def test_unknown_write_capability_cannot_hide_in_an_untyped_field_object(self):
        self.assertTrue(_schema_errors(self.item({"kind":"CREATE","fields":{"reminders":[10]}}),model_schema(),"response"))
