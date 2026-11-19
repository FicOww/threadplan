"""Persistent approvals for explicitly configured Feishu calendars.

Bounded calendar reads and journaled writes. Existing events require
approval for an exact baseline; read discovery alone never grants edit rights.
"""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from threadplan.adapters.feishu import (FeishuApprovalRequired, FeishuCalendar,
    FeishuError, _proposal, event_record, check_event_scope)
from threadplan.contracts import Event, Proposal
from threadplan.codec import _aware_time


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _now():
    return datetime.now(timezone.utc).isoformat()


def normalize_event(raw: dict) -> Event:
    """Convert actual timestamp seconds to UTC; never infer an all-day time."""
    record = event_record(raw)
    fields = record["fields"]
    start, end = (_aware_time(fields.get(name)) for name in ("start_at", "end_at"))
    if fields.get("all_day") or start is None or end is None:
        raise FeishuError("Provider event has unsupported or invalid timestamp seconds")
    if end <= start or not isinstance(fields.get("title"), str):
        raise FeishuError("Provider event interval or title is invalid")
    return Event(record["id"], {key: value for key, value in fields.items()
        if key in {"title", "start_at", "end_at", "location", "description"} and value is not None})



class FeishuHost:
    def __init__(self, calendar: FeishuCalendar, db_path: Path):
        self.calendar = calendar
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.status = {"provider": "feishu", "identity": "user", "calendar_id": calendar.calendar_id,
                       "connected": False, "verified_at": None,
                       "scope": "configured_feishu_calendar", "attendee_sync": False, "reminders": False}
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS connection (calendar_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS managed_events (
                    event_id TEXT PRIMARY KEY, state TEXT NOT NULL, created_by_key TEXT NOT NULL,
                    provider_event TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts (
                    approval_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, operation TEXT NOT NULL,
                    target_event_id TEXT, state TEXT NOT NULL, result TEXT, error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipt_commands (
                    approval_key TEXT PRIMARY KEY, proposal TEXT NOT NULL,
                    baseline TEXT, exclude_attendees INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS authorized_events (
                    event_id TEXT PRIMARY KEY, state TEXT NOT NULL, provider_event TEXT NOT NULL);
            """)
            rows = db.execute("SELECT calendar_id FROM connection").fetchall()
            if rows and {row["calendar_id"] for row in rows} != {calendar.calendar_id}:
                raise FeishuError("Ledger belongs to another calendar; use a separate ledger")
            db.execute("INSERT OR IGNORE INTO connection(calendar_id) VALUES(?)", (calendar.calendar_id,))
            identifiers = {row["event_id"] for row in db.execute("SELECT event_id FROM managed_events")}
            existing = {row["event_id"] for row in db.execute("SELECT event_id FROM authorized_events")}
        # Do not trust a broader allowlist supplied by an unrelated adapter client.
        self.calendar.managed_event_ids = identifiers
        self.calendar.authorized_event_ids = existing

    @classmethod
    def from_connection(cls, path: Path, db_path: Path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("provider") != "feishu" or data.get("identity") != "user":
            raise FeishuError("Connection must explicitly identify a user-authorized Feishu calendar")
        return cls(FeishuCalendar(data.get("calendar_id")), db_path)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _read_calendar(self):
        try:
            calendar = self.calendar.get_calendar(approved=True)
            FeishuCalendar._check_calendar(calendar)
            if calendar.get("calendar_id") != self.calendar.calendar_id:
                raise FeishuError("Provider calendar ID differs from connection")
        except Exception as exc:
            self._read_failed(exc)
            raise
        self.status.update(connected=True, verified_at=_now(), error=None)
        return calendar

    def _read_failed(self, exc):
        self.status.update(connected=False, error=str(exc), checked_at=_now())

    def _managed(self, raw):
        identifier = raw.get("event_id")
        with self.connect() as db:
            row = db.execute("SELECT state FROM managed_events WHERE event_id=?", (identifier,)).fetchone()
            existing = db.execute("SELECT state FROM authorized_events WHERE event_id=?", (identifier,)).fetchone()
        if not ((row is not None and row["state"] == "active") or (existing is not None and existing["state"] == "active")):
            raise FeishuError("Event is not active in the trusted execution ledger")
        check_event_scope(raw)

    def _authorize_existing(self, current):
        """Admission accompanies exact-target execution approval, never a read."""
        identifier = current["event_id"]
        if identifier in self.calendar.managed_event_ids | self.calendar.authorized_event_ids:
            self._managed(current)
            return
        check_event_scope(current)
        normalize_event(current)
        if current.get("status") == "cancelled":
            raise FeishuError("Target is cancelled")
        with self.connect() as db:
            db.execute("INSERT INTO authorized_events VALUES(?,'active',?) ON CONFLICT(event_id) "
                       "DO UPDATE SET state='active',provider_event=excluded.provider_event", (identifier, _json(current)))
        self.calendar.authorized_event_ids.add(identifier)

    @staticmethod
    def _verify_fields(event: Event, projected: Proposal, raw: dict):
        if raw.get("status") == "cancelled":
            raise FeishuError("Read-back returned a cancelled event after a create or update")
        for key, expected in projected.changes.items():
            if expected is None:
                continue
            actual = event.fields.get(key)
            if key in {"start_at", "end_at"}:
                expected_time, actual_time = _aware_time(expected), _aware_time(actual)
                matches = expected_time is not None and actual_time is not None and expected_time == actual_time
            else:
                matches = expected == actual
            if not matches:
                raise FeishuError("Provider read-back does not match the approved event change",
                                  details={"field": key, "event_id": event.id})

    def snapshot(self, *, range=None, event_ids=None) -> tuple[list[Event], dict[str, dict]]:
        """Explicit view/IDs are read scope; no args is the local receipt inventory."""
        if range is not None or event_ids is not None:
            if range is not None:
                view = self.calendar.find_events(range, approved=True)
                if view.get("has_more") or view.get("page_token"):
                    raise FeishuError("Calendar baseline view is incomplete")
                raws = view.get("items") or []
            else:
                raws = [self.calendar.get_event(identifier, approved=True) for identifier in event_ids]
            records = [event_record(raw) for raw in raws if raw.get("status") != "cancelled"]
            if range is not None or event_ids:
                self.status.update(connected=True, verified_at=_now(), error=None)
            return ([Event(r["id"], r["fields"]) for r in records],
                    {r["id"]: deepcopy(r["raw"]) for r in records})
        self._read_calendar()
        with self.connect() as db:
            identifiers = [row["event_id"] for row in db.execute(
                "SELECT event_id FROM managed_events WHERE state='active' UNION "
                "SELECT event_id FROM authorized_events WHERE state='active' ORDER BY event_id")]
        events, baselines = [], {}
        try:
            for identifier in identifiers:
                raw = self.calendar.get_event(identifier, approved=True)
                self._managed(raw)
                if raw.get("status") == "cancelled":
                    with self.connect() as db:
                        db.execute("UPDATE managed_events SET state='cancelled',provider_event=? WHERE event_id=?",
                                   (_json(raw), identifier))
                        db.execute("UPDATE authorized_events SET state='cancelled',provider_event=? WHERE event_id=?",
                                   (_json(raw), identifier))
                    continue
                events.append(normalize_event(raw))
                baselines[identifier] = deepcopy(raw)
        except Exception as exc:
            # In particular, 193003 for an active event is not silently called a
            # successful cancellation. Only execute's verified delete can do so.
            self._read_failed(exc)
            raise
        return events, baselines

    def _verify_receipt(self, projected, approval_key, result):
        """Verify an acknowledged write using reads only."""
        identifier = result["event_id"]
        if projected.operation == "CANCEL":
            verification = self.calendar.verify_cancelled(identifier, approved=True)
            if verification.get("verified") is not True:
                raise FeishuError("Cancellation has not been verified", details=verification)
            result["cancellation_verification"] = verification
            with self.connect() as db:
                db.execute("UPDATE managed_events SET state='cancelled' WHERE event_id=?", (identifier,))
                db.execute("UPDATE authorized_events SET state='cancelled' WHERE event_id=?", (identifier,))
        else:
            raw = self.calendar.get_event(identifier, approved=True)
            self._managed(raw)
            event = normalize_event(raw)
            self._verify_fields(event, projected, raw)
            result.update(provider_event=raw, event={"id": event.id, "fields": event.fields})
            with self.connect() as db:
                db.execute("UPDATE managed_events SET provider_event=? WHERE event_id=?", (_json(raw), identifier))
                db.execute("UPDATE authorized_events SET provider_event=? WHERE event_id=?", (_json(raw), identifier))
        with self.connect() as db:
            db.execute("UPDATE receipts SET state='succeeded',result=?,error=NULL,updated_at=? WHERE approval_key=?",
                       (_json(result), _now(), approval_key))
        self.status.update(connected=True, verified_at=_now(), error=None)
        return result

    def recover(self, approval_key: str, *, approved=False):
        """Retry verification of a frozen receipt; never resend a mutation."""
        if approved is not True:
            raise FeishuApprovalRequired("Explicit host approval is required")
        with self.connect() as db:
            row = db.execute("SELECT * FROM receipts WHERE approval_key=?", (approval_key,)).fetchone()
            command = db.execute("SELECT * FROM receipt_commands WHERE approval_key=?", (approval_key,)).fetchone()
        if row is None:
            raise FeishuError("No execution receipt exists", details={"state": "not_started"})
        if row["state"] == "succeeded":
            return dict(json.loads(row["result"]), already_applied=True)
        if command is None:
            raise FeishuError("Execution outcome is unresolved; recovery will not resend a write",
                              details={"approval_key": approval_key, "state": row["state"]})
        original = Proposal(**json.loads(command["proposal"]))
        projected = replace(original, changes={key: value for key, value in original.changes.items() if key != "participants"})
        acknowledged = row["state"] in {"write_acknowledged", "verify_pending"} and row["result"] is not None
        if acknowledged:
            result = json.loads(row["result"])
            result.setdefault("provider_acknowledged", True)
        elif row["state"] in {"outcome_unknown", "in_progress"} and projected.operation in {"UPDATE", "CANCEL"}:
            # A known target can be reconciled by its actual desired state. This
            # does not assert that the lost provider response acknowledged our
            # particular request, and it never repeats the mutation.
            result = {"scope": "configured_feishu_calendar", "operation": projected.operation,
                      "event_id": projected.target_event_id, "already_applied": False,
                      "excluded_participants": list(original.changes.get("participants") or []),
                      "attendee_sync": False, "reminders": False, "provider_acknowledged": False,
                      "reconciliation_evidence": "desired_state_verified_after_unknown_write"}
        else:
            raise FeishuError("Execution outcome is unresolved; recovery will not resend a write",
                              details={"approval_key": approval_key, "state": row["state"]})
        try:
            return dict(self._verify_receipt(projected, approval_key, result),
                        recovered_verification=True)
        except Exception as exc:
            self._read_failed(exc)
            with self.connect() as db:
                db.execute("UPDATE receipts SET state=?,error=?,updated_at=? WHERE approval_key=? AND state<>'succeeded'",
                           ("verify_pending" if acknowledged else "outcome_unknown", _json({"type": type(exc).__name__, "message": str(exc),
                                   "details": getattr(exc, "details", {})}), _now(), approval_key))
            raise

    def receipt(self, approval_key: str):
        """Local execution evidence only; does not read or write the provider."""
        with self.connect() as db:
            row = db.execute("SELECT state,result,error FROM receipts WHERE approval_key=?", (approval_key,)).fetchone()
        return None if row is None else {
            "state": row["state"], "result": json.loads(row["result"]) if row["result"] else None,
            "error": json.loads(row["error"]) if row["error"] else None}

    def execute(self, proposal: Proposal, approval_key: str, baselines: dict[str, dict], *,
                approved: bool = False, exclude_attendees: bool = False) -> dict:
        if approved is not True:
            raise FeishuApprovalRequired("Explicit host approval is required")
        if not isinstance(proposal, Proposal):
            raise FeishuError("Expected a core Proposal")
        if not isinstance(proposal.changes, dict):
            raise FeishuError("Proposal changes must be an object")
        excluded = list(proposal.changes.get("participants") or [])
        if excluded and exclude_attendees is not True:
            raise FeishuError("Explicit exclusion of attendee synchronization is required")
        projected = replace(proposal, changes={key: value for key, value in proposal.changes.items() if key != "participants"})
        _proposal(projected, projected.operation, approval_key)
        if projected.operation not in {"CREATE", "UPDATE", "CANCEL"}:
            raise FeishuError("Unsupported operation")
        baseline = None
        if projected.operation != "CREATE":
            baseline = baselines.get(projected.target_event_id)
            if not isinstance(baseline, dict) or baseline.get("event_id") != projected.target_event_id:
                raise FeishuError("Analysis baseline for the exact target event is required")
        fingerprint = hashlib.sha256(_json({"proposal": proposal.to_dict(), "baseline": baseline,
            "exclude_attendees": exclude_attendees}).encode("utf-8")).hexdigest()
        resume = False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM receipts WHERE approval_key=?", (approval_key,)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise FeishuError("Approval key was already used for a different request")
                if previous["state"] == "succeeded":
                    return dict(json.loads(previous["result"]), already_applied=True)
                if previous["state"] in {"write_acknowledged", "verify_pending"}:
                    resume = True
                elif previous["state"] != "failed_before_write":
                    raise FeishuError("Prior execution has an unresolved outcome; inspect receipt before any retry",
                                      details={"approval_key": approval_key, "state": previous["state"],
                                               "error": json.loads(previous["error"]) if previous["error"] else None})
            if not resume:
                if projected.target_event_id and db.execute(
                        "SELECT 1 FROM receipts WHERE target_event_id=? AND approval_key<>? AND state IN "
                        "('in_progress','outcome_unknown','write_acknowledged','verify_pending')",
                        (projected.target_event_id, approval_key)).fetchone():
                    raise FeishuError("Target has an unresolved execution; reconcile it before further writes")
                if previous:
                    db.execute("UPDATE receipts SET state='in_progress',error=NULL,updated_at=? WHERE approval_key=?",
                               (_now(), approval_key))
                else:
                    db.execute("INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?,?)", (approval_key, fingerprint,
                        projected.operation, projected.target_event_id, "in_progress", None, None, _now(), _now()))
                    db.execute("INSERT INTO receipt_commands VALUES(?,?,?,?)", (approval_key,
                        _json(proposal.to_dict()), _json(baseline), int(exclude_attendees)))
        if resume:
            return self.recover(approval_key, approved=True)
        mutation_started, acknowledged = False, False
        try:
            send = self.calendar.prepare_write(projected, baseline, approval_key, approved=True,
                                               authorize_target=self._authorize_existing)
            self.status.update(connected=True, verified_at=_now(), error=None)
            result = {"scope": "configured_feishu_calendar", "operation": projected.operation,
                      "already_applied": False, "excluded_participants": excluded,
                      "attendee_sync": False, "reminders": False}
            if projected.operation == "CREATE":
                mutation_started = True
                created = send()
                identifier = created.get("event_id")
                if not isinstance(identifier, str) or not identifier:
                    raise FeishuError("Provider creation did not return an event ID")
                result["event_id"] = identifier
                result["provider_acknowledged"] = True
                # Save ownership and acknowledgment together, before verification.
                with self.connect() as db:
                    db.execute("INSERT INTO managed_events VALUES(?,?,?,?)",
                               (identifier, "active", approval_key, _json(created)))
                    db.execute("UPDATE receipts SET state='write_acknowledged',result=?,updated_at=? WHERE approval_key=?",
                               (_json(result), _now(), approval_key))
                self.calendar.managed_event_ids.add(identifier)
                acknowledged = True
            elif projected.operation == "UPDATE":
                identifier = projected.target_event_id
                mutation_started = True
                send()
            else:
                identifier = projected.target_event_id
                mutation_started = True
                result["provider_response"] = send()
            result["event_id"] = identifier
            if not acknowledged:
                result["provider_acknowledged"] = True
                with self.connect() as db:
                    db.execute("UPDATE receipts SET state='write_acknowledged',result=?,updated_at=? WHERE approval_key=?",
                               (_json(result), _now(), approval_key))
                acknowledged = True
            return self._verify_receipt(projected, approval_key, result)
        except Exception as exc:
            self._read_failed(exc)
            error = {"message": str(exc), "type": type(exc).__name__, "details": getattr(exc, "details", {})}
            with self.connect() as db:
                db.execute("UPDATE receipts SET state=?,error=?,updated_at=? WHERE approval_key=?",
                           ("verify_pending" if acknowledged else "outcome_unknown" if mutation_started else "failed_before_write",
                            _json(error), _now(), approval_key))
            raise
