"""Persistent constructed calendar. Implements only the adapter's local transport boundary."""

from contextlib import closing
from copy import deepcopy
import json
import sqlite3
import uuid

from threadplan.adapters.feishu import FeishuCalendar, FeishuError, event_record
from threadplan.codec import _aware_time
from threadplan_host.feishu import FeishuHost


def _seconds(value):
    return str(int(_aware_time(value).timestamp()))


def _raw(event):
    fields = event["fields"]
    raw = {"event_id": event["id"], "status": event["status"], "summary": fields["title"],
           "start_time": {"timestamp": _seconds(fields["start_at"]), "timezone": "UTC"},
           "end_time": {"timestamp": _seconds(fields["end_at"]), "timezone": "UTC"}}
    if "description" in fields:
        raw["description"] = fields["description"]
    if "location" in fields:
        raw["location"] = {"name": fields["location"]}
    if "free_busy_status" in fields:
        raw["free_busy_status"] = fields["free_busy_status"]
    return raw


class SimulatedCalendar:
    calendar_id = "threadplan-local-scenario"

    def __init__(self, directory, scenario):
        self.path = directory / "simulated-calendar.sqlite"
        self.scenario = scenario
        self.calendar = FeishuCalendar(self.calendar_id, cli=self)
        self.backend = FeishuHost(self.calendar, directory / "receipts.sqlite")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS calendar_events (
                    id TEXT PRIMARY KEY, source TEXT, raw TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calendar_meta (
                    key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            """)
            if db.execute("INSERT OR IGNORE INTO calendar_meta VALUES ('mutations', 0)").rowcount:
                db.executemany("INSERT INTO calendar_events VALUES (?, ?, ?)",
                               ((source["id"], json.dumps(source), json.dumps(raw))
                                for source, raw in self._initial_events()))

    def _initial_events(self):
        fixture = self.scenario.fixture
        details = {event["id"]: event for event in fixture.get("detail_events", [])}
        return [(source, _raw(details.get(source["id"], source))) for source in fixture["events"]]

    def mutations(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT value FROM calendar_meta WHERE key='mutations'").fetchone()[0]

    def _events(self, db):
        return [json.loads(row[0]) for row in db.execute("SELECT raw FROM calendar_events ORDER BY rowid")]

    def _maybe_fail_readback(self, db):
        return bool(db.execute("DELETE FROM calendar_meta WHERE key='readback-failure'").rowcount)

    def invoke(self, resource, method, *, params, data=None, approved=False):
        if approved is not True or params["calendar_id"] != self.calendar_id:
            raise FeishuError("simulated_calendar_scope_mismatch")
        if (resource, method) == ("calendars", "get"):
            return {"calendar_id": self.calendar_id, "type": "shared", "role": "owner"}
        if resource != "events":
            raise FeishuError("unsupported_simulated_calendar_resource")
        identifier = params.get("event_id")
        with closing(sqlite3.connect(self.path)) as db, db:
            if method == "instance_view":
                low, high = int(params["start_time"]), int(params["end_time"])
                return {"items": [event for event in self._events(db) if event["status"] != "cancelled"
                                  and int(event["start_time"]["timestamp"]) < high
                                  and low < int(event["end_time"]["timestamp"])]}
            if method == "get":
                if self._maybe_fail_readback(db):
                    db.commit()
                    raise FeishuError("simulated_readback_unavailable")
                row = db.execute("SELECT raw FROM calendar_events WHERE id=?", (identifier,)).fetchone()
                if row is None:
                    raise FeishuError("simulated_event_missing")
                return {"event": json.loads(row[0])}
            if method not in {"create", "patch", "delete"}:
                raise FeishuError("unsupported_simulated_calendar_method")
            db.execute("BEGIN IMMEDIATE")
            if method == "create":
                identifier = "demo-" + uuid.UUID(params["idempotency_key"]).hex
                existing = db.execute("SELECT raw FROM calendar_events WHERE id=?", (identifier,)).fetchone()
                if existing is not None:
                    return {"event": json.loads(existing[0])}
                event = dict(data, event_id=identifier, status="confirmed")
                db.execute("INSERT INTO calendar_events VALUES (?, NULL, ?)", (identifier, json.dumps(event)))
            else:
                row = db.execute("SELECT raw FROM calendar_events WHERE id=?", (identifier,)).fetchone()
                if row is None or json.loads(row[0])["status"] == "cancelled":
                    raise FeishuError("simulated_event_missing")
                event = json.loads(row[0])
                event.update(data if method == "patch" else {"status": "cancelled"})
                db.execute("UPDATE calendar_events SET raw=?,source=NULL WHERE id=?", (json.dumps(event), identifier))
            db.execute("UPDATE calendar_meta SET value=value+1 WHERE key='mutations'")
            if self.scenario.fault == "readback":
                db.execute("INSERT OR REPLACE INTO calendar_meta VALUES ('readback-failure', 1)")
        if self.scenario.fault == "lost_ack":
            raise FeishuError("simulated_acknowledgement_lost")
        return {} if method == "delete" else {"event": event}

    def read(self, *, query, context):
        fixture = self.scenario.fixture
        bounds = query.get("range")
        with closing(sqlite3.connect(self.path)) as db:
            if query["kind"] == "get_events" and "detail_events" in fixture and self.mutations() == 0:
                events = deepcopy(fixture["detail_events"])
            else:
                events = [json.loads(source) if source else event_record(json.loads(raw)) for source, raw in db.execute(
                    "SELECT source,raw FROM calendar_events ORDER BY rowid")]
        if query["kind"] == "get_events":
            events = [event for event in events if event["id"] in query["event_ids"]]
        else:
            low, high = (_aware_time(bounds[field]) for field in ("start_at", "end_at"))
            events = [event for event in events if _aware_time(event["fields"]["start_at"]) < high
                      and low < _aware_time(event["fields"]["end_at"])]
        return {"read_status": fixture["read_status"], "events": events,
                "searched": {"range": deepcopy(bounds), "applied_filters": {}},
                "connection_ref": context.connection_ref, "scope_ref": context.scope_ref,
                "source_category": "assistant_constructed_calendar_fixture"}

    def display(self, *, initial=False):
        if initial:
            events = [raw for _, raw in self._initial_events()]
        else:
            with closing(sqlite3.connect(self.path)) as db:
                events = self._events(db)
        return [{"id": record["id"], "fields": record["fields"], "status": record["status"]}
                for raw in events if (record := event_record(raw))["status"] != "cancelled"]
