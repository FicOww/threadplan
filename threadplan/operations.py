"""Deterministic operation compilation; no model prompts or coordination."""

from copy import deepcopy
from .contracts import Proposal
from .codec import _aware_time
from .intake import text
from .timeops import add_minutes, elapsed_minutes, instant, minutes

FIELDS = {"title", "start_at", "end_at", "location", "description"}


def resolve_operation(item, records, *, default_duration_minutes=30, policy_ref=None,
                      duration_policy_source="PRODUCT_DEFAULT"):
    """Check a minimal operation and derive only explicit keep-duration arithmetic.

    No lexical entailment, exact-title match or guessed default duration is used.
    The returned object is derived execution data; the model answer is untouched.
    """
    op = deepcopy(item["operation"])
    kind = op.get("kind")
    if type(kind) is not str or kind not in {"CREATE", "UPDATE", "CANCEL"}:
        raise ValueError("invalid_operation_kind")
    if op.get("changes" if kind == "CREATE" else "fields") not in (None, {}):
        raise ValueError("invalid_mutation_container")
    target = None
    if kind != "CREATE":
        if not text(op.get("event_ref")) or op["event_ref"] not in records:
            raise ValueError("unknown_event_reference")
        target = records[op["event_ref"]]
        if target.get("read_verified") is False:
            raise ValueError("target_not_verified_by_read")
        if target.get("status") == "cancelled":
            raise ValueError("target_already_cancelled")
    elif op.get("event_ref") is not None:
        raise ValueError("create_has_target")
    source_fields = op.get("fields" if kind == "CREATE" else "changes", {})
    if source_fields is None:
        source_fields = {}
    if type(source_fields) is not dict:
        raise ValueError("invalid_mutation_fields")
    # Null is explicitly unknown, not a field clear. Preserve the original raw
    # response; compile only known mutations and retain interpretation metadata.
    known = {key: value for key, value in source_fields.items() if value is not None}
    if kind == "CANCEL" and known:
        raise ValueError("cancel_has_changes")
    metadata = {key: known[key] for key in ("timezone", "duration_minutes", "participants") if key in known}
    required = set(item.get("required_write_fields") or [])
    if kind == "UPDATE" and "description" in known:
        required.add("description")
    unsupported_required = required - FIELDS - {'duration_minutes'}
    if unsupported_required:
        raise ValueError("unsupported_required_write_field:" + ",".join(sorted(unsupported_required)))
    if required:
        metadata['required_write_fields'] = sorted(required)
    if "participants" in metadata:
        metadata["not_written_fields"] = ["participants"]
    unsupported = set(known) - FIELDS - set(metadata)
    if unsupported:
        raise ValueError("unsupported_mutation_field:" + ",".join(sorted(unsupported)))
    changes = {key: value for key, value in known.items() if key in FIELDS}
    if kind == "CANCEL" and changes:
        raise ValueError("cancel_has_changes")
    for name in ("title", "location", "description"):
        if name in changes and not text(changes[name]):
            raise ValueError("invalid_" + name)
    if "participants" in metadata and (type(metadata["participants"]) is not list or
            any(not text(v) for v in metadata["participants"])):
        raise ValueError("invalid_participants")
    for name in ("start_at", "end_at"):
        if name in changes:
            try:
                changes[name] = instant(changes[name], metadata.get("timezone")).isoformat()
            except ValueError as error:
                raise ValueError("invalid_" + name + ":" + str(error)) from error
    derived = []
    if op.get("shift_minutes") is not None:
        if kind != "UPDATE" or "start_at" in changes:
            raise ValueError("shift_requires_update_without_explicit_start")
        changes["start_at"] = add_minutes(target["fields"].get("start_at"),
            minutes(op["shift_minutes"], signed=True)).isoformat()
        metadata["shift_minutes"] = op["shift_minutes"]
        derived.append("start_at:SHIFT_MINUTES")
    preserve_duration = op.get('preserve_duration')
    if preserve_duration is not None and type(preserve_duration) is not bool:
        raise ValueError('invalid_preserve_duration')
    if preserve_duration and kind != 'UPDATE':
        raise ValueError('preserve_duration_requires_update')
    if kind == "UPDATE" and "start_at" in changes and not {"end_at", "duration_minutes"} & known.keys():
        if preserve_duration is False:
            raise ValueError("duration_change_requires_end_or_duration")
        preserve_duration = True
    if kind in {'CREATE','UPDATE'} and 'duration_minutes' in metadata:
        duration = metadata["duration_minutes"]
        try:
            minutes(duration)
        except ValueError:
            raise ValueError("invalid_explicit_duration")
        start = _aware_time(changes.get('start_at') or (target['fields'].get('start_at') if target else None))
        if start is None:
            raise ValueError('explicit_duration_requires_start')
        expected_end = add_minutes(start.isoformat(), duration)
        if 'end_at' in changes and _aware_time(changes['end_at']) != expected_end:
            raise ValueError('explicit_duration_contradiction')
        if 'end_at' not in changes:
            changes['end_at'] = expected_end.isoformat()
            derived.append('end_at:EXPLICIT_DURATION')
    if (kind == 'CREATE' and 'start_at' in changes and 'end_at' not in changes
            and default_duration_minutes is not None):
        if type(default_duration_minutes) is not int or not 0 < default_duration_minutes <= 525600:
            raise ValueError('invalid_default_duration')
        changes['end_at'] = add_minutes(changes['start_at'], default_duration_minutes).isoformat()
        assumption = {'field':'end_at','basis':duration_policy_source,
                      'duration_minutes':default_duration_minutes}
        if policy_ref is not None:
            assumption['policy_ref'] = policy_ref
        metadata['policy_assumptions'] = [assumption]
        derived.append('end_at:' + duration_policy_source)
    if kind == "CREATE" and not {"title", "start_at", "end_at"} <= changes.keys():
        raise ValueError("incomplete_create")
    if kind == 'UPDATE' and preserve_duration:
        old_start, old_end = (_aware_time(target['fields'].get(name)) for name in ('start_at','end_at'))
        if old_start is None or old_end is None or old_end <= old_start:
            raise ValueError('original_duration_unknown')
        expected = add_minutes(changes.get('start_at') or target['fields']['start_at'],
                               elapsed_minutes(old_start.isoformat(), old_end.isoformat()))
        if 'end_at' in changes and _aware_time(changes['end_at']) != expected:
            raise ValueError('preserve_duration_contradiction')
        if 'start_at' in changes and 'end_at' not in changes:
            changes['end_at'] = expected.isoformat()
            derived.append('end_at:PRESERVE_DURATION')
        metadata['preserve_duration'] = True
    if kind != 'CANCEL' and not changes:
        raise ValueError('empty_changes')
    effective = {} if target is None else deepcopy(target["fields"])
    effective.update(changes)
    missing_required = required - {key for key, value in effective.items() if value is not None}
    if "duration_minutes" in missing_required and "duration_minutes" in metadata and "end_at" in effective:
        missing_required.remove("duration_minutes")
    if missing_required:
        raise ValueError("missing_required_write_field:" + ",".join(sorted(missing_required)))
    if kind != "CANCEL":
        # Arithmetic can cross an offset transition; normalize derived endpoints
        # into the explicitly supplied named zone before returning them.
        if metadata.get("timezone") is not None:
            from zoneinfo import ZoneInfo
            for name in ("start_at", "end_at"):
                if name in changes:
                    changes[name] = _aware_time(changes[name]).astimezone(ZoneInfo(metadata["timezone"])).isoformat()
                    effective[name] = changes[name]
        start, end = (_aware_time(effective.get(name)) for name in ("start_at", "end_at"))
        if start is None or end is None or end <= start:
            raise ValueError("invalid_or_incomplete_interval")
    return {"kind": kind, "event_ref": op.get("event_ref"), "changes": changes,
            "evidence_message_ids": list(item["evidence_message_ids"]), "derived": derived, "metadata": metadata}


def as_proposal(operation, native_id=None):
    return Proposal(operation["kind"], "cancelled" if operation["kind"] == "CANCEL" else "confirmed",
                    tuple(operation["evidence_message_ids"]), deepcopy(operation["changes"]), native_id)
