"""Admission for locally constructed inputs submitted to the fixed free route."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import secrets

from threadplan import Intake, UpstreamContext
from threadplan.codec import _aware_time

from .scenarios import Phase, Scenario


SOURCE_LIVE = "live_openrouter_sandbox"


def _text(value, *, max_length):
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError("invalid_sandbox_text")
    return value.strip()


def _calendar(value):
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("invalid_simulated_calendar")
    events = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) - {"title", "start_at", "end_at", "location", "description"}:
            raise ValueError("invalid_simulated_event")
        fields = {"title": _text(entry.get("title"), max_length=140)}
        start, end = (_aware_time(entry.get(key)) for key in ("start_at", "end_at"))
        if start is None or end is None or end <= start or start.microsecond or end.microsecond:
            raise ValueError("invalid_simulated_event_time")
        fields.update(start_at=start.isoformat(), end_at=end.isoformat())
        for key in ("location", "description"):
            if entry.get(key):
                fields[key] = _text(entry[key], max_length=500)
        events.append({"id": "demo-event-" + secrets.token_hex(8), "status": "confirmed", "fields": fields})
    return {"events": events, "read_status": "COMPLETE"}


def create_input(data, now):
    if data.get("constructed") is not True:
        raise ValueError("constructed_input_confirmation_required")
    zone = _text(data.get("timezone", "Asia/Shanghai"), max_length=80)
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("invalid_timezone") from error
    identifier = "sandbox-" + secrets.token_hex(12)
    if data.get("intake") is not None:
        intake = Intake(data["intake"]).to_dict()
        actor = _text(data.get("actor"), max_length=80)
        if actor not in {message["sender_id"] for message in intake["messages"]}:
            raise ValueError("actor_not_in_conversation")
    else:
        actor = "我"
        intake = Intake({
            "schema_version": "0.3", "request_id": identifier + "-input",
            "source": {"kind": "personal_note", "provider": "threadplan_sandbox", "conversation_id": identifier},
            "messages": [{"id": identifier + "-1", "sender_id": actor, "sent_at": now.isoformat(),
                          "text": _text(data.get("note"), max_length=4500)}],
        }).to_dict()
    if len(intake["messages"]) > 40:
        raise ValueError("sandbox_message_limit")
    context = UpstreamContext(user_id=actor, default_timezone=zone,
                              connection_ref="demo-sandbox", scope_ref="simulation",
                              namespace=identifier, policy_ref="sandbox-free-v1").to_dict()
    return {"id": identifier, "title": intake["messages"][0]["text"][:42],
            "intake": intake, "context": context, "fixture": _calendar(data.get("events", [])),
            "anchor": now.isoformat()}


def scenario(data):
    return Scenario(data["id"], data["title"], "自由试用", data["intake"], data["context"],
                    data["fixture"], _aware_time(data["anchor"]),
                    (Phase(data["intake"]["messages"], []),), SOURCE_LIVE)


def followup(data, value, now):
    old = data["intake"]
    if len(old["messages"]) >= 40:
        raise ValueError("sandbox_message_limit")
    message = {"id": data["id"] + "-" + secrets.token_hex(8), "sender_id": data["context"]["user_id"],
               "sent_at": now.astimezone(timezone.utc).isoformat(), "text": _text(value, max_length=1200)}
    source = dict(old["source"])
    if source["kind"] == "personal_note":
        source.setdefault("conversation_id", old["messages"][0]["id"])
    data["intake"] = Intake(dict(old, request_id=data["id"] + "-" + secrets.token_hex(8),
                                 source=source, messages=old["messages"] + [message])).to_dict()
    return data
