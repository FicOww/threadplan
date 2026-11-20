"""Durable v0.3 planning traces, source versions and frozen command index.

These tables coexist with the old journal; no legacy data is rewritten.
Execution results live in the backend receipt. v3_commands keeps legacy
columns for compatible reads; new dispatched rows do not mirror those results.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid

from threadplan.codec import canonical_json, fingerprint
from threadplan.intake import InputError
from threadplan.codec import _aware_time


class PlanningStore:
    def __init__(self, path, *, clock=lambda: datetime.now(timezone.utc)):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS v3_requests (
                    id TEXT PRIMARY KEY, conversation TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    request TEXT NOT NULL, context TEXT NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL, deadline_at TEXT NOT NULL, token TEXT,
                    work TEXT NOT NULL, result TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS v3_latest (conversation TEXT PRIMARY KEY, request_key TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS v3_sources (
                    conversation TEXT NOT NULL, id TEXT NOT NULL, record TEXT NOT NULL,
                    PRIMARY KEY(conversation,id));
                CREATE TABLE IF NOT EXISTS v3_trace (
                    request_key TEXT NOT NULL, kind TEXT NOT NULL, number INTEGER NOT NULL,
                    input TEXT NOT NULL, output TEXT, error TEXT, metadata TEXT,
                    PRIMARY KEY(request_key,kind,number));
                CREATE TABLE IF NOT EXISTS v3_commands (
                    id TEXT PRIMARY KEY, request_key TEXT NOT NULL, plan_fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL, result TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS v3_bindings (
                    conversation TEXT NOT NULL, event_id TEXT NOT NULL, record TEXT NOT NULL,
                    PRIMARY KEY(conversation,event_id));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def now(self):
        instant = self.clock()
        if not isinstance(instant, datetime) or instant.utcoffset() is None:
            raise ValueError("invalid_store_clock")
        return instant.astimezone(timezone.utc)

    @staticmethod
    def decode(row):
        if row is None:
            raise ValueError("unknown_request")
        result = dict(row)
        for field in ("request", "context", "work", "result", "error", "payload", "metadata", "input", "output"):
            if field in result and result[field] is not None:
                result[field] = json.loads(result[field])
        return result

    def get(self, key):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM v3_requests WHERE id=?", (key,)).fetchone())

    def request_for_command(self, identifier):
        with self.connect() as db:
            row = db.execute("SELECT request_key FROM v3_commands WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("unknown_command")
        return row["request_key"]

    def register(self, intake, context, *, duration_seconds):
        data, trusted = intake.to_dict(), context.to_dict()
        conversation = intake.conversation_key(context)
        key = fingerprint({"namespace": context.namespace, "owner": context.user_id,
                           "connection": context.connection_ref, "scope": context.scope_ref,
                           "request_id": data["request_id"]})
        digest = fingerprint({"request": data, "context": trusted})
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM v3_requests WHERE id=?", (key,)).fetchone()
            if row:
                if row["fingerprint"] != digest:
                    raise InputError("REQUEST_ID_CONFLICT")
                return self.decode(row), True
            for message in data["messages"]:
                old = db.execute("SELECT record FROM v3_sources WHERE conversation=? AND id=?",
                                 (conversation, message["id"])).fetchone()
                if old:
                    prior = json.loads(old["record"])
                    if message != prior:
                        if message["sender_id"] != prior["sender_id"] or _aware_time(message["sent_at"]) != _aware_time(prior["sent_at"]):
                            raise InputError("SOURCE_IDENTITY_OR_TIME_CHANGED", message["id"])
                        if message["text"] != prior["text"]:
                            updated = _aware_time(message.get("updated_at"))
                            if updated is None:
                                raise InputError("SOURCE_EDIT_REQUIRES_UPDATED_AT", message["id"])
                            before = _aware_time(prior.get("updated_at", prior["sent_at"]))
                            if updated <= before:
                                raise InputError("STALE_OR_CONFLICTING_SOURCE", message["id"])
                        elif prior.get("updated_at"):
                            incoming = _aware_time(message.get("updated_at"))
                            if incoming is None or incoming < _aware_time(prior["updated_at"]):
                                message = dict(message, updated_at=prior["updated_at"])
                db.execute("INSERT INTO v3_sources VALUES(?,?,?) ON CONFLICT(conversation,id) DO UPDATE SET record=excluded.record",
                           (conversation, message["id"], canonical_json(message)))
            from datetime import timedelta
            now = self.now()
            deadline = now + timedelta(seconds=duration_seconds)
            db.execute("UPDATE v3_requests SET state='SUPERSEDED' WHERE conversation=? AND state IN ('RECEIVED','RUNNING','COMPLETED')",
                       (conversation,))
            db.execute("INSERT INTO v3_requests VALUES(?,?,?,?,?,'RECEIVED',?,?,NULL,?,NULL,NULL)",
                       (key, conversation, digest, canonical_json(data), canonical_json(trusted),
                        now.isoformat(), deadline.isoformat(), canonical_json({"reads": [], "records": {}, "messages": data["messages"]})))
            db.execute("INSERT INTO v3_latest VALUES(?,?) ON CONFLICT(conversation) DO UPDATE SET request_key=excluded.request_key",
                       (conversation, key))
        return self.get(key), False

    def current(self, key, *, token=None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM v3_requests WHERE id=?", (key,)).fetchone()
            data = self.decode(row)
            latest = db.execute("SELECT request_key FROM v3_latest WHERE conversation=?", (data["conversation"],)).fetchone()
            if latest is None or latest[0] != key or data["state"] == "SUPERSEDED":
                raise ValueError("stale_request")
            if token is not None and (data["token"] != token or data["state"] != "RUNNING"):
                raise ValueError("stale_request_owner")
            return data

    def claim(self, key):
        token = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM v3_requests WHERE id=?", (key,)).fetchone()
            if row["state"] != "RECEIVED":
                return None
            db.execute("UPDATE v3_requests SET state='RUNNING',token=? WHERE id=?", (token, key))
        return token

    def expire(self, key):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM v3_requests WHERE id=?", (key,)).fetchone()
            if row and row["state"] in {"RECEIVED", "RUNNING"} and self.now() >= _aware_time(row["deadline_at"]):
                db.execute("UPDATE v3_requests SET state='FAILED',token=NULL,error=? WHERE id=?",
                           (canonical_json({"code": "request_deadline_exceeded", "retryable": False}), key))
        return self.get(key)

    def retry(self, key):
        row = self.current(key)
        error = row["error"] or {}
        if row["state"] != "FAILED" or not (error.get("retryable") is True or
                                           error.get('manual_retry_allowed') is True):
            raise ValueError("request_not_retryable")
        if self.now() >= _aware_time(row["deadline_at"]):
            raise ValueError("request_deadline_exceeded")
        if error.get("not_before") and self.now() < _aware_time(error["not_before"]):
            return row
        with self.connect() as db:
            db.execute("UPDATE v3_requests SET state='RECEIVED',error=NULL WHERE id=? AND state='FAILED'", (key,))
        return self.get(key)

    def save_work(self, key, token, work):
        self.current(key, token=token)
        with self.connect() as db:
            if not db.execute("UPDATE v3_requests SET work=? WHERE id=? AND token=? AND state='RUNNING'",
                              (canonical_json(work), key, token)).rowcount:
                raise ValueError("stale_request_owner")

    def finish(self, key, token, *, result=None, error=None):
        self.current(key, token=token)
        with self.connect() as db:
            db.execute("UPDATE v3_requests SET state=?,result=?,error=?,token=NULL WHERE id=? AND token=? AND state='RUNNING'",
                       ("FAILED" if error else "COMPLETED", canonical_json(result) if result is not None else None,
                        canonical_json(error) if error else None, key, token))
        return self.get(key)

    def reserve(self, key, token, kind, value, maximum):
        row = self.current(key, token=token)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            number = db.execute("SELECT COUNT(*) FROM v3_trace WHERE request_key=? AND kind=?", (key, kind)).fetchone()[0] + 1
            if number > maximum:
                raise ValueError(kind.lower() + "_budget_exhausted")
            db.execute("INSERT INTO v3_trace VALUES(?,?,?,?,NULL,NULL,NULL)", (key, kind, number, canonical_json(value)))
        return number

    def trace_result(self, key, kind, number, *, output=None, error=None, metadata=None):
        with self.connect() as db:
            db.execute("UPDATE v3_trace SET output=?,error=?,metadata=? WHERE request_key=? AND kind=? AND number=?",
                       (canonical_json(output) if output is not None else None,
                        canonical_json(error) if error is not None else None,
                        canonical_json(metadata) if metadata is not None else None, key, kind, number))

    def traces(self, key):
        with self.connect() as db:
            return [self.decode(row) for row in db.execute("SELECT * FROM v3_trace WHERE request_key=? ORDER BY kind,number", (key,))]

    def bindings(self, conversation):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT record FROM v3_bindings WHERE conversation=?", (conversation,))]
