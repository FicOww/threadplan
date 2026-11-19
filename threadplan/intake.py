"""Strict v0.3 source intake. No inferred identities, dates or calendar data."""

from dataclasses import dataclass
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .codec import canonical_json, fingerprint
from .codec import _reject_constant, _unique_object
from .codec import _aware_time


class InputError(ValueError):
    def __init__(self, code, path=""):
        self.code, self.path = code, path
        super().__init__(code + (":" + path if path else ""))


def text(value):
    return type(value) is str and bool(value.strip())


@dataclass(frozen=True)
class UpstreamContext:
    user_id: str
    default_timezone: str
    connection_ref: str
    scope_ref: str
    namespace: str = "default"
    policy_ref: str = "explicit-approval-v1"
    default_duration_minutes: int | None = None
    scope_mode: str = "self_related"

    def __post_init__(self):
        if any(not text(value) for key,value in vars(self).items() if key != 'default_duration_minutes'):
            raise InputError("INVALID_UPSTREAM_CONTEXT")
        # Omitted/null legacy configuration now resolves to the product policy.
        # Keep the origin for operation provenance, not as a second configuration.
        duration_source = "PRODUCT_DEFAULT" if self.default_duration_minutes is None else "UPSTREAM_DEFAULT_DURATION"
        if self.default_duration_minutes is None:
            object.__setattr__(self, "default_duration_minutes", 30)
        if (type(self.default_duration_minutes) is not int
                or not 0 < self.default_duration_minutes <= 525600):
            raise InputError('INVALID_DEFAULT_DURATION')
        if self.scope_mode not in {"self_related", "team"}:
            raise InputError("INVALID_SCOPE_MODE")
        try:
            ZoneInfo(self.default_timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise InputError("INVALID_DEFAULT_TIMEZONE") from error
        object.__setattr__(self, "_duration_source", duration_source)

    @property
    def duration_policy_source(self):
        return self._duration_source

    def to_dict(self):
        return {key: value for key, value in vars(self).items() if not key.startswith("_")}


@dataclass(frozen=True, init=False)
class Intake:
    _json: str

    def __init__(self, data):
        if type(data) is not dict:
            raise InputError("INVALID_REQUEST")
        required = {"schema_version", "request_id", "source", "messages"}
        if set(data) != required or data["schema_version"] != "0.3" or not text(data["request_id"]):
            raise InputError("INVALID_REQUEST")
        source = data["source"]
        if (type(source) is not dict or not {"kind", "provider"} <= set(source)
                or set(source) - {"kind", "provider", "conversation_id"}
                or source["kind"] not in {"conversation", "personal_note"} or not text(source["provider"])):
            raise InputError("INVALID_SOURCE")
        if source["kind"] == "conversation" and not text(source.get("conversation_id")):
            raise InputError("MISSING_CONVERSATION_ID")
        if "conversation_id" in source and not text(source["conversation_id"]):
            raise InputError("INVALID_CONVERSATION_ID")
        if type(data["messages"]) is not list or not data["messages"]:
            raise InputError("MISSING_MESSAGES")
        ids = set()
        for index, message in enumerate(data["messages"]):
            path = f"messages[{index}]"
            if type(message) is not dict or set(message) - {
                    "id", "sender_id", "sent_at", "text", "reply_to", "updated_at", "mentions"}:
                raise InputError("INVALID_MESSAGE", path)
            for key, code in (("id", "MISSING_MESSAGE_ID"), ("sender_id", "MISSING_SENDER_ID"),
                              ("text", "MISSING_MESSAGE_TEXT")):
                if not text(message.get(key)):
                    raise InputError(code, path + "." + key)
            if message["id"] in ids:
                raise InputError("DUPLICATE_MESSAGE_ID", path + ".id")
            ids.add(message["id"])
            if message.get("sent_at") is None or message.get("sent_at") == "":
                raise InputError("MISSING_SENT_AT", path + ".sent_at")
            if _aware_time(message["sent_at"]) is None:
                raise InputError("INVALID_SENT_AT", path + ".sent_at")
            if message.get("reply_to") is not None and not text(message["reply_to"]):
                raise InputError("INVALID_REPLY", path + ".reply_to")
            if message.get("updated_at") is not None:
                updated = _aware_time(message["updated_at"])
                if updated is None or updated < _aware_time(message["sent_at"]):
                    raise InputError("INVALID_UPDATED_AT", path + ".updated_at")
            if "mentions" in message and (type(message["mentions"]) is not list or any(
                    type(item) is not dict or set(item) != {"token", "actor_id"}
                    or not text(item["token"]) or not text(item["actor_id"]) for item in message["mentions"])):
                raise InputError("INVALID_MENTIONS", path + ".mentions")
        try:
            encoded = canonical_json(data)
        except (TypeError, ValueError) as error:
            raise InputError("NON_JSON_INPUT") from error
        object.__setattr__(self, "_json", encoded)

    @classmethod
    def from_json(cls, value):
        try:
            return cls(json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_constant))
        except InputError:
            raise
        except (TypeError, ValueError, RecursionError) as error:
            raise InputError("INVALID_JSON") from error

    def to_dict(self):
        return json.loads(self._json)

    @property
    def fingerprint(self):
        return fingerprint(self.to_dict())

    def conversation_key(self, context):
        data = self.to_dict()
        source = data["source"]
        # A delivery ID changes on redelivery/edit and cannot identify a note.
        # Unscoped notes use their stable primary source-message ID; a host can
        # provide conversation_id as the stable document/note scope explicitly.
        origin = source.get("conversation_id", {"personal_note": data["messages"][0]["id"]})
        return fingerprint({"namespace": context.namespace, "owner": context.user_id,
                            "connection": context.connection_ref, "scope": context.scope_ref,
                            "provider": source["provider"],
                            "conversation": origin})
