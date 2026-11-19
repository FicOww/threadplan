"""Scoped read validation, stable record references and lossless source storage."""

from copy import deepcopy
from datetime import timedelta

from threadplan.codec import fingerprint
from threadplan.intake import text
from threadplan.codec import _aware_time


def validate_query(query, records, messages, *, max_range_days=31):
    if type(query) is not dict:
        raise ValueError("read_request_must_be_object")
    kind = query.get("kind")
    if kind == "find_events":
        if set(query) - {"kind", "range", "hints", "page_token"} or type(query.get("range")) is not dict:
            raise ValueError("invalid_find_request")
        if "page_token" in query and not text(query["page_token"]):
            raise ValueError("invalid_page_token")
        bounds = query["range"]
        if set(bounds) != {"start_at", "end_at"}:
            raise ValueError("invalid_read_range")
        start, end = (_aware_time(bounds[name]) for name in ("start_at", "end_at"))
        if start is None or end is None or end <= start or end - start > timedelta(days=max_range_days):
            raise ValueError("invalid_or_oversized_read_range")
        hints = query.get("hints", {})
        if type(hints) is not dict or set(hints) - {"title", "location", "participants"}:
            raise ValueError("invalid_read_hints")
        if any(not text(value) for name, value in hints.items() if name != "participants"):
            raise ValueError("invalid_read_hints")
        if "participants" in hints and (type(hints["participants"]) is not list or any(
                not text(value) for value in hints["participants"])):
            raise ValueError("invalid_read_hints")
    elif kind == "get_events":
        refs = query.get("event_refs")
        if (set(query) - {"kind", "event_refs", "fields"} or type(refs) is not list or not refs
                or any(type(ref) is not str or ref not in records for ref in refs)):
            raise ValueError("invalid_detail_reference")
        if "fields" in query and (type(query["fields"]) is not list or any(not text(field) for field in query["fields"])):
            raise ValueError("invalid_detail_fields")
    elif kind == "get_messages":
        ids = query.get("message_ids")
        # IDs may be outside the window only when referenced by the actual source.
        known = {m["id"] for m in messages} | {m.get("reply_to") for m in messages}
        if (set(query) != {"kind", "message_ids"} or type(ids) is not list or not ids
                or any(type(identifier) is not str or identifier not in known for identifier in ids)):
            raise ValueError("invalid_history_reference")
    else:
        raise ValueError("unsupported_read_capability")
    return deepcopy(query)


def merge_calendar_reply(work, query, response, context, *, max_candidates):
    if type(response) is not dict or response.get("read_status") not in {"COMPLETE", "PARTIAL", "FAILED"}:
        raise ValueError("invalid_calendar_read_response")
    if response.get("connection_ref", context.connection_ref) != context.connection_ref:
        raise ValueError("calendar_read_scope_mismatch")
    if response.get("scope_ref", context.scope_ref) != context.scope_ref:
        raise ValueError("calendar_read_scope_mismatch")
    if response["read_status"] == "FAILED":
        return {"query": deepcopy(query), "read_status": "FAILED", "reason": response.get("reason", "read_failed"), "events": []}
    events = response.get("events")
    if type(events) is not list:
        raise ValueError("calendar_events_must_be_array")
    view = []
    for raw in events:
        if type(raw) is not dict or not text(raw.get("id")) or type(raw.get("fields")) is not dict:
            raise ValueError("invalid_calendar_event")
        key = fingerprint({"connection": context.connection_ref, "id": raw["id"]})
        ref = next((ref for ref, record in work["records"].items() if record["key"] == key), None)
        if ref is None:
            if len(work["records"]) >= max_candidates:
                return {"query": deepcopy(query), "read_status": "PARTIAL", "reason": "candidate_budget_exhausted", "events": view}
            ref = "c" + str(len(work["records"]) + 1)
            work["records"][ref] = {"key": key, "id": raw["id"], "fields": {}, "sources": []}
        record = work["records"][ref]
        record["fields"] = deepcopy(raw["fields"])
        record["read_verified"] = True
        record["status"] = raw.get("status", "unknown")
        record["version"] = raw.get("version", fingerprint(raw["fields"]))
        record["sources"].append(deepcopy(raw))
        if query["kind"] == "get_events" and ref not in query["event_refs"]:
            raise ValueError("unexpected_detail_event")
        view.append({"event_ref": ref, "fields": deepcopy(record["fields"]), "status": record["status"]})
    status = response["read_status"]
    page_complete = status == "COMPLETE"
    if query["kind"] == "get_events":
        missing = set(query["event_refs"]) - {event["event_ref"] for event in view}
        for ref in missing:
            work["records"][ref]["read_verified"] = False
        if missing:
            status = "PARTIAL"
            page_complete = False
    # A next page or provider-side restrictive filters cannot prove full absence.
    if response.get("has_more") or response.get("next_page_token") or response.get("page_token"):
        status = "PARTIAL"
    searched = deepcopy(response.get("searched"))
    if query["kind"] == "find_events" and (type(searched) is not dict or "applied_filters" not in searched):
        status, page_complete = "PARTIAL", False
        searched = {"range": deepcopy(query["range"]), "applied_filters": None}
    if searched is None:
        searched = {}
    if query["kind"] == "find_events" and searched.get("range") != query["range"]:
        raise ValueError("read_range_mismatch")
    return {"query": deepcopy(query), "read_status": status, "searched": searched,
            "page_complete": page_complete, "next_page_token": response.get("next_page_token") or response.get("page_token"),
            "reason": response.get("reason"), "events": view}


def duplicate_search_complete(reads, fields):
    start, end = (_aware_time(fields.get(name)) for name in ("start_at", "end_at"))
    if start is None or end is None:
        return False
    reachable = set()
    for read in reads:
        if read["query"]["kind"] != "find_events":
            continue
        searched = read.get("searched", {})
        if searched.get("applied_filters") != {}:
            continue
        bounds = searched.get("range", {})
        low, high = (_aware_time(bounds.get(name)) for name in ("start_at", "end_at"))
        if low is None or high is None or low > start or high < end:
            continue
        base = fingerprint({key: value for key, value in read["query"].items() if key != "page_token"})
        token = read["query"].get("page_token")
        if token is not None and (base, token) not in reachable:
            continue
        if not read.get("page_complete", read["read_status"] == "COMPLETE"):
            continue
        next_token = read.get("next_page_token")
        if next_token:
            reachable.add((base, next_token))
        elif read["read_status"] == "COMPLETE":
            return True
    return False


def overlaps(operation, work):
    fields = deepcopy(work["records"].get(operation.get("event_ref"), {}).get("fields", {}))
    fields.update(operation["changes"])
    if operation["kind"] == "CANCEL" or (operation["kind"] == "UPDATE" and not {"start_at", "end_at"} & operation["changes"].keys()):
        return {"status": "NOT_APPLICABLE", "overlaps": []}
    start, end = (_aware_time(fields.get(name)) for name in ("start_at", "end_at"))
    items, unknown = [], []
    for ref, record in work["records"].items():
        if ref == operation.get("event_ref") or record["status"] == "cancelled" or record["fields"].get("free_busy_status") == "free":
            continue
        other_start, other_end = (_aware_time(record["fields"].get(name)) for name in ("start_at", "end_at"))
        if record.get("read_verified", True) and (other_start is None or other_end is None):
            unknown.append(ref)
        if other_start is not None and other_end is not None and start < other_end and other_start < end:
            items.append({"event_ref": ref, "title": record["fields"].get("title"),
                          "start_at": record["fields"]["start_at"], "end_at": record["fields"]["end_at"],
                          "busy_status": record["fields"].get("free_busy_status", "unknown")})
    return {"status": "COMPLETE" if not unknown and duplicate_search_complete(work["reads"], fields) else "PARTIAL",
            "overlaps": items, "unknown_interval_refs": unknown}
