"""Explicit-calendar Feishu adapter; no credentials or automatic login.

The core never invokes this module. The host must approve each external call.
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import math
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any
import uuid

from threadplan.contracts import Proposal
from threadplan.codec import _aware_time
from threadplan.codec import fingerprint

TEST_PREFIX = "[ThreadPlan TEST] "
MANAGED_MARKER = "managed-by:threadplan-feishu-test-v1"
TEST_DESCRIPTION_FOOTER = TEST_PREFIX.strip() + "\n" + MANAGED_MARKER
BASELINE_FIELDS = ("summary", "start_time", "end_time", "location", "description",
                   "description_rich", "status", "attendees", "has_more_attendee", "recurrence",
                   "free_busy_status", "reminders", "vchat", "visibility", "recurring_event_id", "is_exception")
_CLI_CALLS = ContextVar("threadplan_native_cli_calls", default=None)


@contextmanager
def capture_cli_calls():
    """Task/thread-local native invocations, never guessed underlying HTTP counts."""
    calls = []
    token = _CLI_CALLS.set(calls)
    try:
        yield calls
    finally:
        _CLI_CALLS.reset(token)


def is_legacy_test_event(raw: dict) -> bool:
    """Decode the explicit historical format; not a calendar authorization rule."""
    value = raw.get("description_rich") or raw.get("description") or ""
    return (isinstance(raw.get("summary"), str) and raw["summary"].startswith(TEST_PREFIX)
            and isinstance(value, str) and value.endswith(TEST_DESCRIPTION_FOOTER))


def event_description(raw: dict) -> str:
    """Business view only; the original provider record remains unchanged."""
    value = raw.get("description_rich") or raw.get("description") or ""
    if is_legacy_test_event(raw):
        return value[:-len(TEST_DESCRIPTION_FOOTER)].removesuffix("\n\n")
    return value


def check_event_scope(raw: dict, *, allow_reminders=False) -> None:
    """Supported write capabilities, independent of title, owner or calendar name."""
    if (raw.get("attendees") or raw.get("has_more_attendee") or raw.get("recurrence")
            or raw.get("recurring_event_id") or raw.get("is_exception")
            or (raw.get("reminders") and not allow_reminders)
            or (raw.get("vchat") or {}).get("vc_type") not in {None, "no_meeting"}
            or any((raw.get(key) or {}).get("date") for key in ("start_time", "end_time"))):
        raise FeishuError("Attendees, recurrence, reminders, all-day and meeting operations are unsupported")


def _managed_description(value=None):
    return ((_text(value, "description") + "\n\n") if value is not None else "") + TEST_DESCRIPTION_FOOTER


class FeishuError(RuntimeError):
    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


class FeishuApprovalRequired(FeishuError):
    """Host approval or a CLI high-risk confirmation is required; never auto-retry."""


def event_record(raw: dict) -> dict:
    """One lossless provider-to-business mapping for discovery and execution."""
    identifier = raw.get("event_id")
    if not isinstance(identifier, str) or not identifier:
        raise FeishuError("Read record has no real provider event ID")
    title = raw.get("summary")
    fields = {"title": title.removeprefix(TEST_PREFIX) if is_legacy_test_event(raw) else title}
    for name, source in (("start_at", "start_time"), ("end_at", "end_time")):
        value = raw.get(source)
        if not isinstance(value, dict):
            continue
        if value.get("date"):
            fields[name.replace("_at", "_date")] = value["date"]
            fields["all_day"] = True
        elif "timestamp" in value:
            try:
                seconds = value["timestamp"]
                if type(seconds) is bool or str(int(seconds)) != str(seconds):
                    raise ValueError("not integer seconds")
                fields[name] = datetime.fromtimestamp(int(seconds), timezone.utc).isoformat()
            except (ValueError, TypeError, OverflowError, OSError) as error:
                raise FeishuError("Invalid source timestamp") from error
    if "location" in raw:
        fields["location"] = (raw.get("location") or {}).get("name")
    if "description_rich" in raw or "description" in raw:
        fields["description"] = event_description(raw)
    for field in ("free_busy_status", "recurrence"):
        if field in raw:
            fields[field] = raw[field]
    if "attendees" in raw and raw.get("has_more_attendee") is False:
        identifiers = [item.get("attendee_id") for item in raw["attendees"]]
        if all(isinstance(value, str) for value in identifiers):
            fields["participants"] = identifiers
    return {"id": identifier, "fields": fields, "status": raw.get("status", "unknown"),
            "version": fingerprint(raw), "raw": deepcopy(raw)}


def _approved(value: bool) -> None:
    if value is not True:
        raise FeishuApprovalRequired("Explicit approved=True is required before external execution")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FeishuError(f"{name} must be a nonempty string")
    return value


def discover_cli() -> str:
    """Locate an existing executable without running wrappers that can install it."""
    direct = shutil.which("lark-cli.exe")
    if direct:
        return direct
    wrapper = shutil.which("lark-cli") or shutil.which("lark-cli.cmd")
    if wrapper:
        path = Path(wrapper)
        binary = path.parent / "node_modules" / "@larksuite" / "cli" / "bin" / "lark-cli.exe"
        if binary.is_file():
            return str(binary)
        if path.suffix.lower() not in {".cmd", ".bat", ".ps1"}:
            return str(path)
    raise FeishuError("Existing lark-cli executable not found; installation is not automatic")


class LarkCLI:
    """JSON body travels on stdin; argv never passes through cmd.exe or a shell."""

    def __init__(self, executable: str | None = None, *, timeout: float = 60, profile: str | None = None):
        if executable and Path(executable).suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise FeishuError("Pass the native lark-cli executable, not a shell wrapper")
        self._executable = executable
        self.profile = profile
        self.timeout = timeout

    @property
    def executable(self):
        if self._executable is None:
            self._executable = discover_cli()
        return self._executable

    def command(self, *arguments):
        profile = ["--profile", self.profile] if self.profile is not None else []
        return [self.executable, *profile, *arguments]

    def invoke(self, resource: str, method: str, *, params: dict | None = None,
               data: dict | None = None, approved: bool = False) -> dict:
        _approved(approved)
        allowed = {("calendars", "create"), ("calendars", "get"),
                   ("events", "create"), ("events", "get"),
                   ("events", "patch"), ("events", "delete"), ("events", "instance_view")}
        if (resource, method) not in allowed:
            raise FeishuError("Unsupported calendar operation")
        argv = self.command("calendar", resource, method, "--as", "user", "--format", "json")
        for key, value in (params or {}).items():
            if key not in {"calendar_id", "event_id", "idempotency_key", "need_notification", "need_attendee", "start_time", "end_time"}:
                raise FeishuError(f"Unsupported parameter: {key}")
            argv.extend(["--" + key.replace("_", "-"), str(value)])
        body = None
        if data is not None:
            argv.extend(["--data", "-"])
            body = json.dumps(data, ensure_ascii=False, allow_nan=False)
        env = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
        attempt = {"resource": resource, "method": method, "return_code": None}
        observed = _CLI_CALLS.get()
        if observed is not None:
            observed.append(attempt)
        started = time.perf_counter()
        try:
            result = subprocess.run(argv, input=body, capture_output=True, text=True,
                                    encoding="utf-8", timeout=self.timeout, env=env, shell=False)
            attempt["return_code"] = result.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FeishuError("CLI execution failed; write outcome may be unknown, do not blindly retry",
                              details={"type": type(exc).__name__}) from exc
        finally:
            attempt["elapsed_seconds"] = time.perf_counter() - started
        stream = result.stdout if result.returncode == 0 else result.stderr
        try:
            envelope = json.loads(stream)
        except (ValueError, TypeError) as exc:
            raise FeishuError("CLI returned invalid JSON; write outcome may be unknown",
                              details={"exit_code": result.returncode}) from exc
        if not isinstance(envelope, dict):
            raise FeishuError("CLI response must be a JSON object")
        if result.returncode == 10:
            raise FeishuApprovalRequired("CLI requires explicit high-risk confirmation",
                                         details=envelope.get("error", {}))
        if result.returncode != 0 or envelope.get("ok") is not True:
            raise FeishuError("Feishu request failed", details=envelope.get("error", {"exit_code": result.returncode}))
        if envelope.get("identity") != "user":
            raise FeishuError("CLI response identity is not user")
        payload = envelope.get("data")
        if not isinstance(payload, dict):
            raise FeishuError("CLI success response is missing object data")
        return payload


def _time(value: str) -> dict:
    _text(value, "timestamp")
    try:
        parsed = _aware_time(value)
        if parsed is None:
            raise ValueError("missing offset")
        timestamp = parsed.timestamp()
        if not timestamp.is_integer():
            raise ValueError("subsecond times are unsupported")
    except (ValueError, OverflowError, OSError) as exc:
        raise FeishuError("Time must be ISO 8601 with explicit offset and whole seconds") from exc
    return {"timestamp": str(int(timestamp)), "timezone": "UTC"}


def _proposal(proposal: Proposal, operation: str, approval_key: str, *, direct=False) -> dict:
    _text(approval_key, "approval_key")
    if not isinstance(proposal, Proposal) or proposal.operation != operation:
        raise FeishuError(f"Expected {operation} Proposal")
    expected = "cancelled" if operation == "CANCEL" else "confirmed"
    if proposal.commitment_state != expected or proposal.unresolved_fields:
        raise FeishuError("Proposal is not ready for host approval")
    if not direct and not proposal.evidence_message_ids:
        raise FeishuError("Proposal requires message evidence")
    if not isinstance(proposal.changes, dict):
        raise FeishuError("Proposal changes must be an object")
    fields = {"title", "start_at", "end_at", "location", "description", "participants"}
    if direct:
        fields.update(("reminder_minutes", "location_address"))
    if set(proposal.changes) - fields:
        raise FeishuError("Unsupported proposal fields")
    changes = {key: value for key, value in proposal.changes.items() if value is not None}
    if changes.get("participants"):
        raise FeishuError("Attendee synchronization is outside this adapter; explicitly project host-approved fields")
    changes.pop("participants", None)
    if operation == "CREATE" and proposal.target_event_id is not None:
        raise FeishuError("CREATE cannot target an existing event")
    if operation != "CREATE":
        _text(proposal.target_event_id, "target_event_id")
    if operation == "CANCEL" and changes:
        raise FeishuError("CANCEL cannot change event fields")
    if operation != "CANCEL" and not changes:
        raise FeishuError("Empty event changes")
    return changes


def reminder_minutes(raw):
    reminders = raw.get("reminders")
    if reminders is None:
        return []
    if type(reminders) is not list or any(type(item) is not dict or type(item.get("minutes")) is not int
                                         for item in reminders):
        raise FeishuError("Provider returned invalid personal reminders")
    return sorted(item["minutes"] for item in reminders)


def _extra_event_fields(body, changes):
    if "location_address" in changes:
        body["location"]["address"] = changes["location_address"]
    if "reminder_minutes" in changes:
        body["reminders"] = [{"minutes": value} for value in changes["reminder_minutes"]]


def _pair(start: dict, end: dict) -> None:
    try:
        if int(end["timestamp"]) <= int(start["timestamp"]):
            raise ValueError("end <= start")
    except (KeyError, TypeError, ValueError) as exc:
        raise FeishuError("Both effective times must be timestamps and end must follow start") from exc


def _title(value: str) -> str:
    return _text(value, "title")


class FeishuCalendar:
    direct_entry = False

    def __init__(self, calendar_id: str, *, cli: LarkCLI | None = None,
                 managed_event_ids: set[str] | None = None):
        self.calendar_id = _text(calendar_id, "calendar_id")
        if calendar_id.strip().lower() == "primary":
            raise FeishuError("An explicit canonical calendar ID is required; implicit primary aliases are not accepted")
        self.cli = cli or LarkCLI()
        # Only restore this allowlist from the host's trusted execution ledger.
        self.managed_event_ids = set(managed_event_ids or ())
        # Exact existing targets admitted by the trusted host's approval ledger.
        self.authorized_event_ids = set()

    @staticmethod
    def _check_calendar(calendar: dict, *, writable=False) -> None:
        if calendar.get("is_deleted") is True or calendar.get("is_third_party") is True:
            raise FeishuError("Calendar is deleted or cannot be managed through this provider")
        roles = {"owner", "writer"} if writable else {"owner", "writer", "reader", "free_busy_reader"}
        if calendar.get("role") not in roles:
            raise FeishuError("Calendar does not grant the required read/write role")

    def get_calendar(self, *, approved: bool = False, writable=False) -> dict:
        _approved(approved)
        calendar = self.cli.invoke("calendars", "get", params={"calendar_id": self.calendar_id}, approved=True)
        if calendar.get("calendar_id") != self.calendar_id:
            raise FeishuError("Calendar response ID mismatch")
        self._check_calendar(calendar, writable=writable)
        return calendar

    def get_event(self, event_id: str, *, approved: bool = False, writable=False) -> dict:
        _approved(approved)
        self.get_calendar(approved=True, writable=writable)
        response = self.cli.invoke("events", "get", params={"calendar_id": self.calendar_id,
            "event_id": _text(event_id, "event_id"), "need_attendee": "true"}, approved=True)
        event = response.get("event")
        if not isinstance(event, dict) or event.get("event_id") != event_id:
            raise FeishuError("Event response ID missing or mismatched")
        return event

    def find_events(self, range: dict, *, approved: bool = False) -> dict:
        """Read the whole bounded view, without an ownership or title filter."""
        _approved(approved)
        start, end = (_aware_time(range.get(name)) for name in ("start_at", "end_at"))
        if start is None or end is None or not 0 < (end-start).total_seconds() < 40*86400:
            raise FeishuError("Invalid bounded calendar view range")
        self.get_calendar(approved=True)
        result = self.cli.invoke("events", "instance_view", params={
            "calendar_id": self.calendar_id, "start_time": str(math.floor(start.timestamp())),
            "end_time": str(math.ceil(end.timestamp()))}, approved=True)
        # The native successful empty response is {} (items is optional in the
        # provider schema). Absence/null means an empty list, not a read failure.
        # Explicit malformed collections and pagination remain distinguishable.
        if result.get("items") is not None and type(result["items"]) is not list:
            raise FeishuError("Event view returned an invalid items array")
        return result

    def _managed(self, event: dict) -> None:
        if event.get("event_id") not in self.managed_event_ids | self.authorized_event_ids:
            raise FeishuError("Event is not in the trusted execution ledger")
        check_event_scope(event, allow_reminders=self.direct_entry)

    def prepare_write(self, proposal, baseline, approval_key, *, approved=False, authorize_target=None):
        """Fresh checks and encoding only; return a send callable.

        The journaled host calls it immediately and records dispatch at that
        boundary. Never persist or reuse this callable across requests.
        """
        _approved(approved)
        if proposal.operation == "CREATE":
            return self._prepare_create(proposal, approval_key, approved=True)
        if proposal.operation == "UPDATE":
            return self._prepare_update(proposal, baseline, approval_key, approved=True,
                                        authorize_target=authorize_target)
        if proposal.operation == "CANCEL":
            return self._prepare_cancel(proposal, baseline, approval_key, approved=True,
                                        authorize_target=authorize_target)
        raise FeishuError("Unsupported operation")

    def create_event(self, proposal, approval_key, *, approved=False):
        return self.prepare_write(proposal, None, approval_key, approved=approved)()

    def update_event(self, proposal, baseline_event, approval_key, *, approved=False):
        return self.prepare_write(proposal, baseline_event, approval_key, approved=approved)()

    def cancel_event(self, proposal, approval_key, *, approved=False):
        return self.prepare_write(proposal, None, approval_key, approved=approved)()

    def _prepare_create(self, proposal: Proposal, approval_key: str, *, approved: bool = False):
        _approved(approved)
        changes = _proposal(proposal, "CREATE", approval_key, direct=self.direct_entry)
        body = {"summary": _title(changes.get("title")),
                "start_time": _time(changes.get("start_at")), "end_time": _time(changes.get("end_at")),
                "description": changes.get("description", ""),
                "visibility": "private", "free_busy_status": "free", "need_notification": False,
                "reminders": [], "vchat": {"vc_type": "no_meeting"}, "attendee_ability": "none"}
        _pair(body["start_time"], body["end_time"])
        if "location" in changes:
            body["location"] = {"name": _text(changes["location"], "location")}
        _extra_event_fields(body, changes)
        self.get_calendar(approved=True, writable=True)
        def send():
            response = self.cli.invoke("events", "create", params={"calendar_id": self.calendar_id,
                "idempotency_key": str(uuid.uuid5(uuid.NAMESPACE_URL, f"threadplan:{self.calendar_id}:{approval_key}"))},
                data=body, approved=True)
            event = response.get("event")
            if not isinstance(event, dict):
                raise FeishuError("Creation returned no event; retain approval key for safe recovery",
                                  details={"create_response": response, "approval_key": approval_key})
            try:
                self.managed_event_ids.add(_text(event.get("event_id"), "returned event_id"))
            except FeishuError as exc:
                exc.details = {"created_event": event, "approval_key": approval_key}
                raise
            return event
        return send

    def _prepare_update(self, proposal: Proposal, baseline_event: dict, approval_key: str,
                        *, approved: bool = False, authorize_target=None):
        _approved(approved)
        changes = _proposal(proposal, "UPDATE", approval_key, direct=self.direct_entry)
        if not isinstance(baseline_event, dict) or baseline_event.get("event_id") != proposal.target_event_id:
            raise FeishuError("UPDATE requires the exact target baseline event")
        current = self.get_event(proposal.target_event_id, approved=True, writable=True)
        for key in BASELINE_FIELDS:
            if current.get(key) != baseline_event.get(key):
                raise FeishuError("Event changed since approval baseline; obtain fresh approval")
        (authorize_target or self._managed)(current)
        body = {"need_notification": False}
        if "title" in changes:
            body["summary"] = (TEST_PREFIX + _title(changes["title"]) if is_legacy_test_event(current) else _title(changes["title"]))
        if "location" in changes:
            body["location"] = {"name": _text(changes["location"], "location")}
        _extra_event_fields(body, changes)
        if "description" in changes:
            body["description"] = (_managed_description(changes["description"])
                if is_legacy_test_event(current) else _text(changes["description"], "description"))
        if "start_at" in changes or "end_at" in changes:
            body["start_time"] = (_time(changes["start_at"]) if "start_at" in changes else deepcopy(current.get("start_time")))
            body["end_time"] = (_time(changes["end_at"]) if "end_at" in changes else deepcopy(current.get("end_time")))
            _pair(body["start_time"], body["end_time"])
        def send():
            response = self.cli.invoke("events", "patch", params={"calendar_id": self.calendar_id,
                "event_id": proposal.target_event_id}, data=body, approved=True)
            event = response.get("event")
            if not isinstance(event, dict) or event.get("event_id") != proposal.target_event_id:
                raise FeishuError("Update response event ID missing or mismatched")
            return event
        return send

    def _prepare_cancel(self, proposal: Proposal, baseline, approval_key: str,
                        *, approved: bool = False, authorize_target=None):
        _approved(approved)
        _proposal(proposal, "CANCEL", approval_key)
        event = self.get_event(proposal.target_event_id, approved=True, writable=True)
        if baseline is not None and (baseline.get("event_id") != proposal.target_event_id
                or any(event.get(key) != baseline.get(key) for key in BASELINE_FIELDS)):
            raise FeishuError("Event changed since approval baseline; obtain fresh approval")
        (authorize_target or self._managed)(event)
        return lambda: self.cli.invoke("events", "delete", params={"calendar_id": self.calendar_id,
            "event_id": proposal.target_event_id, "need_notification": "false"}, approved=True)

    def verify_cancelled(self, event_id: str, *, approved: bool = False) -> dict:
        """Read-only verification, distinguishing deletion from authentication failure.

        The live API returned code 193003/'event is deleted' after our test
        deletion. This is verification evidence, not a fabricated event object.
        """
        _approved(approved)
        if event_id not in self.managed_event_ids | self.authorized_event_ids:
            raise FeishuError("Cancellation verification requires a trusted managed event ID")
        try:
            event = self.get_event(event_id, approved=True)
        except FeishuError as error:
            detail = error.details
            if (detail.get("type") == "api" and detail.get("code") == 193003
                    and str(detail.get("message", "")).strip().lower() == "event is deleted"):
                return {"verified": True, "evidence": "event_get_error_193003_deleted",
                        "provider_error_code": 193003}
            raise
        return {"verified": event.get("status") == "cancelled", "evidence": "event_get_status",
                "provider_status": event.get("status")}
