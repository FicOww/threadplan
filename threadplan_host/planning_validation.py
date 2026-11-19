"""One per-activity admission boundary for autonomous and host-driven decisions."""

from copy import deepcopy
from threadplan.codec import fingerprint
from threadplan.protocol import check_result
from threadplan.operations import resolve_operation
from .planning_reads import duplicate_search_complete


def resolve_candidate(candidate, work, context):
    if type(candidate) is not dict or type(candidate.get("items")) is not list or not candidate["items"]:
        raise ValueError("result.items:expected_nonempty_array")
    operations, items, failures = [], [], []
    claimed_targets, claimed_activities = {}, {}
    for index, item in enumerate(candidate["items"]):
        op = item.get("operation") if type(item) is dict else None
        activity = item.get("activity_ref", "a" + str(index + 1)) if type(item) is dict else None
        if type(activity) is str:
            claimed_activities.setdefault(activity, []).append(index)
        if type(op) is dict and type(op.get("event_ref")) is str:
            claimed_targets.setdefault(op["event_ref"], []).append(index)
    identities = {context.user_id} | {message["sender_id"] for message in work["messages"]}
    identities.update(mention["actor_id"] for message in work["messages"] for mention in message.get("mentions", []))
    participant_ids = identities | {actor for record in work["records"].values()
        for actor in record["fields"].get("participants") or [] if isinstance(actor, str)}
    for index, item in enumerate(candidate["items"]):
        value = deepcopy(item) if type(item) is dict else {}
        value.setdefault("activity_ref", "a" + str(index + 1))
        try:
            if type(value["activity_ref"]) is str and len(claimed_activities[value["activity_ref"]]) > 1:
                raise ValueError("conflicting_activity_reference")
            if value.get("decision") == "STOPPED":
                raise ValueError("runtime_stopped:" + value.get("reason", "unknown"))
            errors = check_result({"items": [item]}, work["messages"])
            if errors:
                raise ValueError(";".join(error.replace("items[0]:", "") for error in errors))
            reads = [read for read in work["reads"] if
                     read.get("activity_ref") in {None, value["activity_ref"]}]
            actual_refs = {read["read_ref"] for read in work["reads"] if "read_ref" in read}
            if not set(item.get("attempted_read_refs", [])) <= actual_refs:
                raise ValueError("unknown_read_reference")
            if not set(item.get("candidate_refs", [])) <= work["records"].keys():
                raise ValueError("unknown_candidate_reference")
            latest = {}
            for read in reads:
                latest[fingerprint(read["query"])] = read
            failed = [read for read in latest.values() if read["read_status"] == "FAILED"]
            if failed:
                raise ValueError("calendar_or_history_read_failed:" + ",".join(read.get("read_ref", "unknown") for read in failed))
            if item["decision"] != "PROPOSE":
                items.append(value)
                continue
            target = item["operation"].get("event_ref")
            if type(target) is str and len(claimed_targets.get(target, [])) > 1:
                raise ValueError("conflicting_operations_for_target")
            operation = resolve_operation(item, work["records"],
                default_duration_minutes=context.default_duration_minutes, policy_ref=context.policy_ref,
                duration_policy_source=context.duration_policy_source)
            location = operation["changes"].get("location")
            participants = operation["metadata"].get("participants", [])
            unresolved = [actor for actor in participants if actor not in participant_ids]
            if unresolved:
                operation["metadata"]["participants"] = [actor for actor in participants if actor in participant_ids]
                operation["metadata"]["unresolved_participant_references"] = unresolved
                operation["derived"].append("participants:UNMAPPED_REFERENCES_SEPARATED")
            if (location in identities
                    and not any(location in message["text"] for message in work["messages"])
                    and not any(record["fields"].get("location") == location for record in work["records"].values())):
                if operation["kind"] != "CREATE" or "location" in operation["metadata"].get("required_write_fields", []):
                    raise ValueError("identity_reference_is_not_a_source_location")
                operation["changes"].pop("location")
                operation["metadata"]["discarded_fields"] = {"location": {
                    "value": location, "reason": "identity_reference_without_source_venue"}}
                operation["derived"].append("location:UNKNOWN_AFTER_IDENTITY_TYPE_CHECK")
            if operation["kind"] == "CREATE" and not duplicate_search_complete(work["reads"], operation["changes"]):
                raise ValueError("duplicate_search_incomplete")
            operation["item_index"] = index
            operation["activity_ref"] = value["activity_ref"]
            operations.append(operation)
            items.append(value)
        except ValueError as error:
            details = {"item_index": index, "activity_ref": value["activity_ref"], "code": str(error)}
            failures.append(details)
            items.append({"activity_ref": value["activity_ref"], "decision": "FAILED",
                          "evidence_message_ids": value.get("evidence_message_ids", []),
                          "reason": "操作未通过确定性校验或所需读取失败。", "error": details})
    return {"items": items, "operations": operations, "item_errors": failures}
