"""Local task, source version and explicit approval boundaries."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
import sqlite3

from threadplan import UpstreamContext
from threadplan.codec import _aware_time, fingerprint
from threadplan_host import AgentSession, ConversationPlanner, PlanningLimits, PlanningStore

from .calendar import SimulatedCalendar
from .scenarios import builtin_scenarios, recorded_scenarios, ReplayModel
from . import sandbox


def _now():
    return datetime.now(timezone.utc).timestamp()


def _digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _selected(mapping, names):
    return {name: mapping[name] for name in names if name in mapping}


class Workbench:
    def __init__(self, directory, *, use_archive=True, clock=_now, live_key=None):
        self.directory, self.clock = directory, clock
        self.live_key = live_key
        self.worker = ThreadPoolExecutor(max_workers=1) if live_key else None
        self.live_model = None
        if live_key:
            from threadplan.openrouter_transport import DEFAULT_MODEL
            self.live_model = DEFAULT_MODEL
        self.directory.mkdir(parents=True, exist_ok=True)
        if use_archive:
            recorded, self.catalog_info = recorded_scenarios()
        else:
            recorded, self.catalog_info = {}, {"recorded": 0, "expected": 50, "archive_available": False}
        self.cases = builtin_scenarios() | recorded
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS demo_sessions (owner TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS demo_tasks (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, scenario TEXT NOT NULL, version TEXT NOT NULL,
                    phase INTEGER NOT NULL, started REAL NOT NULL, request TEXT, closed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS demo_task_commands (
                    id TEXT NOT NULL, task TEXT NOT NULL, request TEXT NOT NULL, activity TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, expires REAL NOT NULL, approved REAL, PRIMARY KEY (task, id));
                CREATE TABLE IF NOT EXISTS demo_receipt_views (
                    task TEXT NOT NULL, request TEXT NOT NULL, activity TEXT NOT NULL, command TEXT NOT NULL,
                    PRIMARY KEY (task, request, activity));
                CREATE TABLE IF NOT EXISTS demo_sandbox (
                    task TEXT PRIMARY KEY, spec TEXT NOT NULL, state TEXT NOT NULL, error TEXT);
                CREATE TABLE IF NOT EXISTS demo_sandbox_attempts (owner TEXT NOT NULL, started REAL NOT NULL);
            """)
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='demo_commands'").fetchone():
                db.execute("""INSERT OR IGNORE INTO demo_task_commands
                           (id, task, request, activity, fingerprint, expires, approved)
                           SELECT id, task, request, activity, fingerprint, expires, approved FROM demo_commands""")
            db.execute("UPDATE demo_sandbox SET state='INTERRUPTED' WHERE state='RUNNING'")

    def close(self):
        if self.worker:
            self.worker.shutdown(wait=True)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.directory / "workspace.sqlite")
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def principal(self, token):
        if not token or not token.isascii() or len(token) > 100:
            return None
        owner = _digest(token)
        with self.connect() as db:
            return owner if db.execute("SELECT 1 FROM demo_sessions WHERE owner=?", (owner,)).fetchone() else None

    def create_session(self):
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("INSERT INTO demo_sessions VALUES (?)", (_digest(token),))
        return token

    def catalog(self):
        return {"cases": [case.summary() for case in self.cases.values()],
                "live_enabled": self.worker is not None,
                "live_model": self.live_model, **self.catalog_info}

    def _task(self, owner, identifier):
        with self.connect() as db:
            row = db.execute("SELECT * FROM demo_tasks WHERE id=? AND owner=?", (identifier, owner)).fetchone()
        if row is None:
            raise LookupError("task_not_found")
        return dict(row)

    def _case_version(self, case):
        return fingerprint({"input": case.intake, "fixture": case.fixture, "context": case.context,
                            "fault": case.fault, "anchor": case.anchor.isoformat(),
                            "phases": [vars(phase) for phase in case.phases]})

    def _case(self, task):
        live = self._sandbox_row(task["id"])
        case = sandbox.scenario(json.loads(live["spec"])) if live else self.cases.get(task["scenario"])
        if case is None or self._case_version(case) != task["version"]:
            raise ValueError("scenario_version_unavailable")
        return case

    def _runtime(self, task):
        case = self._case(task)
        directory = self.directory / task["id"]
        directory.mkdir(exist_ok=True)
        elapsed = timedelta(seconds=max(0, self.clock() - task["started"]))
        store = PlanningStore(directory / "planning.sqlite", clock=lambda: case.anchor + elapsed)
        calendar = SimulatedCalendar(directory, case)
        context = UpstreamContext(**case.context)
        limits = PlanningLimits(max_model_calls=3 if case.source == sandbox.SOURCE_LIVE else 4, deadline_seconds=1800)
        return AgentSession(store, context=context, calendar_reader=calendar.read,
                            backend=calendar.backend, allowed_operations=("CREATE", "UPDATE", "CANCEL"),
                            limits=limits), calendar

    def _run(self, task, transport=None):
        case = self._case(task)
        phase = case.phases[task["phase"]]
        intake = dict(case.intake, messages=phase.messages,
                      request_id=f'{case.intake["request_id"]}-phase-{task["phase"]}')
        session, calendar = self._runtime(task)
        planner = ConversationPlanner(session.store, context=session.context, calendar_reader=calendar.read,
                                      limits=session.limits)
        model = transport if transport is not None else ReplayModel(phase)
        row = asyncio.run(planner.analyze(intake, model))
        if transport is None and (model.mismatch or (not row.get("reused") and model.index != len(phase.steps))):
            raise ValueError(model.mismatch or "incomplete_replay")
        with self.connect() as db:
            db.execute("UPDATE demo_tasks SET request=? WHERE id=?", (row["id"], task["id"]))
        return row["state"]

    def _sandbox_row(self, identifier):
        with self.connect() as db:
            return db.execute("SELECT * FROM demo_sandbox WHERE task=?", (identifier,)).fetchone()

    def _reserve_sandbox(self, db, owner):
        if self.worker is None:
            raise ValueError("sandbox_disabled")
        if db.execute("SELECT 1 FROM demo_sandbox WHERE state='RUNNING'").fetchone():
            raise ValueError("sandbox_busy")
        after = self.clock() - 86400
        total, user = db.execute(
            "SELECT count(*), count(CASE WHEN owner=? THEN 1 END) FROM demo_sandbox_attempts WHERE started>=?",
            (owner, after)).fetchone()
        if total >= 8 or user >= 4:
            raise ValueError("sandbox_daily_limit")
        db.execute("INSERT INTO demo_sandbox_attempts VALUES (?,?)", (owner, self.clock()))

    def _live_run(self, owner, identifier):
        from threadplan.openrouter_transport import OpenRouterTransport

        error = None
        status = "FAILED"
        try:
            transport = OpenRouterTransport(runs_dir=self.directory / identifier / "model",
                                            model=self.live_model, api_key=self.live_key, timeout_seconds=30)
            status = self._run(self._task(owner, identifier), transport=transport)
        except (ValueError, RuntimeError, OSError, sqlite3.Error) as issue:
            error = type(issue).__name__
        with self.connect() as db:
            db.execute("UPDATE demo_sandbox SET state=?,error=? WHERE task=?", (status, error, identifier))

    def start_sandbox(self, owner, data):
        if self.worker is None:
            raise ValueError("sandbox_disabled")
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        spec = sandbox.create_input(data, now)
        identifier = secrets.token_hex(16)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reserve_sandbox(db, owner)
            db.execute("""INSERT INTO demo_tasks (id,owner,scenario,version,phase,started)
                          VALUES (?,?,?,?,0,?)""",
                       (identifier, owner, spec["id"], self._case_version(sandbox.scenario(spec)), self.clock()))
            db.execute("INSERT INTO demo_sandbox VALUES (?,?,'RUNNING',NULL)",
                       (identifier, json.dumps(spec, ensure_ascii=False)))
        self.worker.submit(self._live_run, owner, identifier)
        return {"id": identifier, "state": "RUNNING"}

    def followup(self, owner, identifier, data):
        if data.get("constructed") is not True:
            raise ValueError("constructed_input_confirmation_required")
        task = self._task(owner, identifier)
        live = self._sandbox_row(identifier)
        if not live or not task["request"] or live["state"] != "COMPLETED":
            raise ValueError("followup_unavailable")
        session, _ = self._runtime(task)
        items = (session.view(task["request"]).get("plan") or {}).get("items", [])
        if not any(item["decision"] == "WAIT" for item in items):
            raise ValueError("followup_unavailable")
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        spec = sandbox.followup(json.loads(live["spec"]), data.get("answer"), now)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reserve_sandbox(db, owner)
            db.execute("UPDATE demo_sandbox SET spec=?,state='RUNNING',error=NULL WHERE task=?",
                       (json.dumps(spec, ensure_ascii=False), identifier))
            db.execute("UPDATE demo_tasks SET request=NULL,version=? WHERE id=?",
                       (self._case_version(sandbox.scenario(spec)), identifier))
        self.worker.submit(self._live_run, owner, identifier)
        return {"id": identifier, "state": "RUNNING"}

    def create_task(self, owner, case_id):
        case = self.cases.get(case_id)
        if case is None:
            raise LookupError("scenario_not_found")
        identifier = secrets.token_hex(16)
        with self.connect() as db:
            db.execute("""INSERT INTO demo_tasks (id,owner,scenario,version,phase,started)
                          VALUES (?,?,?,?,0,?)""", (identifier, owner, case.id, self._case_version(case), self.clock()))
        self._run(self._task(owner, identifier))
        return self.task_view(owner, identifier)

    def restart(self, owner, identifier):
        old = self._task(owner, identifier)
        if self._sandbox_row(identifier):
            raise ValueError("sandbox_restart_requires_new_input")
        new = self.create_task(owner, old["scenario"])
        with self.connect() as db:
            db.execute("UPDATE demo_tasks SET closed=1 WHERE id=?", (old["id"],))
        return new

    def list_tasks(self, owner):
        with self.connect() as db:
            tasks = db.execute("""SELECT t.id,t.scenario,t.phase,t.closed,s.spec FROM demo_tasks t
                               LEFT JOIN demo_sandbox s ON s.task=t.id WHERE t.owner=?
                               ORDER BY t.rowid DESC LIMIT 100""", (owner,)).fetchall()
        return [dict(_selected(dict(row), ("id", "scenario", "phase", "closed")),
                     title=json.loads(row["spec"])["title"] if row["spec"] else self.cases[row["scenario"]].title)
                for row in tasks if row["spec"] or row["scenario"] in self.cases]

    def clarify(self, owner, identifier, choice):
        task = self._task(owner, identifier)
        case = self._case(task)
        valid = {phase.choice: index for index, phase in enumerate(case.phases) if phase.choice}
        if task["closed"] or task["phase"] or choice not in valid:
            raise ValueError("clarification_choice_unavailable")
        with self.connect() as db:
            db.execute("UPDATE demo_tasks SET phase=?,request=NULL WHERE id=?", (valid[choice], identifier))
        self._run(self._task(owner, identifier))
        return self.task_view(owner, identifier)

    def prepare(self, owner, identifier, activity):
        task = self._task(owner, identifier)
        if task["closed"] or not task["request"]:
            raise ValueError("task_not_open_for_approval")
        session, _ = self._runtime(task)
        plan = session.view(task["request"]).get("plan")
        operation_index = next((index for index, operation in enumerate((plan or {}).get("operations", []))
                                if operation["activity_ref"] == activity), None)
        if operation_index is None:
            raise ValueError("activity_not_executable")
        command = session.executor.prepare(task["request"], operation_index,
                                           expected_fingerprint=plan["plan_fingerprint"], approved=True)
        if command["request_key"] == task["request"]:
            deadline = _aware_time(session.store.get(task["request"])["deadline_at"])
            expiry = self.clock() + (deadline - session.store.now()).total_seconds()
            with self.connect() as db:
                db.execute("INSERT OR IGNORE INTO demo_task_commands VALUES (?,?,?,?,?,?,NULL)",
                           (command["id"], identifier, task["request"], activity, plan["plan_fingerprint"],
                            expiry))
        elif command["state"] == "SUCCEEDED":
            with self.connect() as db:
                db.execute("INSERT OR REPLACE INTO demo_receipt_views VALUES (?,?,?,?)",
                           (identifier, task["request"], activity, command["id"]))
        return self._public_command(session.status(command_id=command["id"]))

    def command(self, owner, identifier, command_id, action):
        task = self._task(owner, identifier)
        session, _ = self._runtime(task)
        with self.connect() as db:
            binding = db.execute("SELECT * FROM demo_task_commands WHERE id=? AND task=?",
                                 (command_id, identifier)).fetchone()
        if binding is None:
            raise LookupError("command_not_found")
        status = session.status(command_id=command_id)
        if action == "recover":
            if status["state"] in {"IN_FLIGHT", "OUTCOME_UNKNOWN", "VERIFY_PENDING"}:
                try:
                    status = session.recover(command_id)
                except RuntimeError:
                    status = session.status(command_id=command_id)
        elif action == "confirm" and status["state"] in {"PREPARED", "FAILED_BEFORE_WRITE"}:
            if task["closed"] or task["request"] != binding["request"]:
                raise ValueError("approval_source_changed")
            if self.clock() >= binding["expires"]:
                raise ValueError("approval_expired")
            plan = session.view(task["request"])["plan"]
            if plan["plan_fingerprint"] != binding["fingerprint"]:
                raise ValueError("approval_plan_changed")
            with self.connect() as db:
                db.execute("UPDATE demo_task_commands SET approved=? WHERE id=? AND task=?",
                           (self.clock(), command_id, identifier))
            try:
                status = session.execute(command_id)
            except RuntimeError:
                status = session.status(command_id=command_id)
        return self._public_command(status)

    @staticmethod
    def _public_command(status):
        data = {"id": status["id"], "state": status["state"]}
        result = status.get("result") or {}
        if "provider_acknowledged" in result:
            data["acknowledged"] = result["provider_acknowledged"]
        return data

    def task_view(self, owner, identifier):
        task = self._task(owner, identifier)
        case = self._case(task)
        live = self._sandbox_row(identifier)
        session, calendar = self._runtime(task)
        view = {"id": identifier, "closed": bool(task["closed"]), "scenario": case.summary(),
                "phase": task["phase"], "timezone": session.context.default_timezone, "actor": session.context.user_id,
                "calendar": calendar.display(), "initial_calendar": calendar.display(initial=True),
                "calendar_anchor": case.anchor.isoformat(),
                "choices": [phase.choice for phase in case.phases if phase.choice] if not task["phase"] and not task["closed"] else [],
                "live": bool(live), "live_available": self.worker is not None}
        if task["request"] is None:
            return dict(view, state=live["state"] if live else "INTERRUPTED", messages=case.phases[task["phase"]].messages,
                        reads=[], records=[], items=[], commands=self._commands(session, task))
        current = session.view(task["request"])
        plan = current.get("plan") or {}
        items = [_selected(item, ("activity_ref", "decision", "reason", "evidence_message_ids",
                                  "candidate_refs", "clarification_question", "missing_facts"))
                 for item in plan.get("items", [])]
        operations = {op["activity_ref"]: _selected(op, ("kind", "event_ref", "changes", "duration_source"))
                      for op in plan.get("operations", [])}
        for item in items:
            if item.get("activity_ref") in operations:
                item["operation"] = operations[item["activity_ref"]]
        reads = [_selected(read, ("query", "read_ref", "read_status", "activity_ref", "searched"))
                 for read in current["reads"]]
        metadata = None
        if live:
            model_traces = [trace for trace in session.store.traces(task["request"]) if trace["kind"] == "MODEL"]
            metadata = {"requested_model": self.live_model, "calls": len(model_traces), "actual_model": None}
            if model_traces:
                last = model_traces[-1]
                metadata.update(_selected(last["metadata"] or {}, ("actual_model", "status", "http_status")))
        return dict(view, state=current["state"], messages=current["messages"], items=items,
                    records=current["calendar_records"], reads=reads, commands=self._commands(session, task),
                    model=metadata)

    def _commands(self, session, task):
        with self.connect() as db:
            bindings = db.execute("SELECT id,activity,request FROM demo_task_commands WHERE task=? ORDER BY rowid",
                                  (task["id"],)).fetchall()
            aliases = db.execute("SELECT command,activity FROM demo_receipt_views WHERE task=? AND request=?",
                                 (task["id"], task["request"])).fetchall()
        commands = [dict(self._public_command(session.status(command_id=row["id"])),
                         activity=row["activity"], current=row["request"] == task["request"]) for row in bindings]
        commands.extend(dict(self._public_command(session.status(command_id=row["command"])),
                             activity=row["activity"], current=True) for row in aliases)
        return commands
