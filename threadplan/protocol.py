"""Shared typed decision carrier, decoding and source-reference validation."""

import json
from pathlib import Path
from .codec import canonical_json, _reject_constant, _unique_object
from .intake import text

DECISION_POLICY = Path(__file__).with_name("decision_policy.md").read_text(encoding="utf-8")
def model_schema():
    texts = {"type": "array", "items": {"type": "string"}}
    nullable_text = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    fields = {"type": "object", "properties": {
        **{name: nullable_text for name in ("title", "description", "location", "start_at", "end_at", "timezone")},
        "duration_minutes": {"anyOf": [{"type": "number"}, {"type": "null"}],
                             "description": "Only a duration stated in original messages. Omit for the trusted default policy; engineering applies and records that default."},
        "participants": {"anyOf": [texts, {"type": "null"}]}}, "additionalProperties": False}
    operation = {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["CREATE", "UPDATE", "CANCEL"]},
        "event_ref": nullable_text,
        "fields": {"anyOf": [fields, {"type": "null"}]},
        "changes": {"anyOf": [fields, {"type": "null"}]},
        "preserve_duration": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
        "shift_minutes": {"anyOf": [{"type": "number"}, {"type": "null"}]}},
        "required": ["kind"], "additionalProperties": False}
    query = {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["find_events", "get_events", "get_messages"]},
        "range": {"type": "object", "properties": {"start_at": {"type": "string"}, "end_at": {"type": "string"}},
                  "required": ["start_at", "end_at"], "additionalProperties": False},
        "hints": {"type": "object", "properties": {"title": {"type": "string"}, "location": {"type": "string"}, "participants": texts},
                  "additionalProperties": False},
        "page_token": {"type": "string"}, "event_refs": texts, "message_ids": texts, "fields": texts},
        "required": ["kind"], "additionalProperties": False}
    item = {"type": "object", "properties": {
        "activity_ref": {"type": "string"},
        "decision": {"type": "string", "enum": ["PROPOSE", "CREATE", "UPDATE", "CANCEL", "NEED_MORE", "NO_ACTION", "WAIT", "UNSUPPORTED"]},
        "reason": {"type": "string"}, "evidence_message_ids": texts,
        "operation": {"anyOf": [operation, {"type": "null"}]},
        "next_read": {"anyOf": [query, {"type": "null"}]},
        "clarification_question": {"type": "string"}, "missing_facts": texts,
        "candidate_refs": texts, "attempted_read_refs": texts,
        "required_write_fields": texts}, "required": ["decision", "reason", "evidence_message_ids"],
        "additionalProperties": True}
    return {"type": "object", "properties": {"items": {"type": "array", "items": item, "minItems": 1}},
            "required": ["items"], "additionalProperties": False}


def decode(response):
    value = (json.loads(response, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
             if type(response) is str else json.loads(canonical_json(response)))
    if type(value) is dict and type(value.get("items")) is list:
        for item in value["items"]:
            if type(item) is dict and type(item.get("decision")) is str and item["decision"] in {"CREATE", "UPDATE", "CANCEL"}:
                if type(item.get("operation")) is dict and item["operation"].get("kind") == item["decision"]:
                    item["decision"] = "PROPOSE"
    return value


def check_result(value, messages):
    if type(value) is not dict or type(value.get("items")) is not list or not value["items"]:
        return ["result.items:expected_nonempty_array"]
    ids = {message["id"] for message in messages}
    errors = []
    activities = set()
    for index, item in enumerate(value["items"]):
        prefix = f"items[{index}]"
        if type(item) is not dict:
            errors.append(prefix + ":expected_object")
            continue
        state = item.get("decision")
        if type(state) is not str or state not in {"PROPOSE", "NEED_MORE", "NO_ACTION", "WAIT", "UNSUPPORTED"}:
            errors.append(prefix + ":invalid_decision")
        if not text(item.get("reason")):
            errors.append(prefix + ":missing_reason")
        evidence = item.get("evidence_message_ids")
        if (type(evidence) is not list or not evidence or any(not text(i) for i in evidence)
                or not set(evidence) <= ids or len(set(evidence)) != len(evidence)):
            errors.append(prefix + ":invalid_evidence")
        for field in ("required_write_fields", "missing_facts", "candidate_refs", "attempted_read_refs"):
            if field in item and (type(item[field]) is not list or any(not text(v) for v in item[field])):
                errors.append(prefix + ":invalid_" + field)
        if "activity_ref" in item and not text(item["activity_ref"]):
            errors.append(prefix + ":invalid_activity_ref")
        elif text(item.get("activity_ref")):
            if item["activity_ref"] in activities:
                errors.append(prefix + ":duplicate_activity_ref")
            activities.add(item["activity_ref"])
        if state == "WAIT" and (not text(item.get("clarification_question")) or not item.get("missing_facts")):
            errors.append(prefix + ":wait_requires_question_and_missing_facts")
        if state == "PROPOSE" and type(item.get("operation")) is not dict:
            errors.append(prefix + ":missing_operation")
        if state == "NEED_MORE" and type(item.get("next_read")) is not dict:
            errors.append(prefix + ":missing_next_read")
        if state != "PROPOSE" and item.get("operation") is not None:
            errors.append(prefix + ":operation_for_nonproposal")
    return errors


