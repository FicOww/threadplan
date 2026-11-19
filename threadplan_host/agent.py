"""Shared persistent tools; semantic reasoning belongs to the calling agent."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import inspect
import time
import uuid
from zoneinfo import ZoneInfo

from threadplan import Intake, UpstreamContext
from threadplan.codec import _aware_time, canonical_json, fingerprint
from threadplan.protocol import decode
from .limits import PlanningLimits
from .planning_execution import PlanningExecutor
from .planning_reads import merge_calendar_reply, overlaps, validate_query
from .planning_validation import resolve_candidate


async def invoke_callback(callback, **kwargs):
    if inspect.iscoroutinefunction(callback):
        return await callback(**kwargs)
    value = await asyncio.to_thread(callback, **kwargs)
    return await value if inspect.isawaitable(value) else value


class AgentSession:
    def __init__(self, store, *, context, calendar_reader, backend=None, history_reader=None,
                 allowed_operations=(), limits=None, deadline_seconds=None):
        if not isinstance(context, UpstreamContext) or not callable(calendar_reader) or (history_reader is not None and not callable(history_reader)):
            raise ValueError("invalid_agent_configuration")
        if not set(allowed_operations) <= {"CREATE", "UPDATE", "CANCEL"}:
            raise ValueError("invalid_execution_policy")
        self.limits = limits or PlanningLimits(deadline_seconds=deadline_seconds or 1800)
        self.store, self.context = store, context
        self.calendar_reader, self.history_reader = calendar_reader, history_reader
        self.allowed_operations = frozenset(allowed_operations)
        self.executor = PlanningExecutor(store, context=context, backend=backend) if backend is not None else None

    def _scope(self, row):
        if row["context"] != self.context.to_dict():
            raise ValueError("agent_scope_mismatch")

    @contextmanager
    def _locked(self, key, *, collecting=False):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.store.current(key)
            self._scope(row)
            if collecting and (row["state"] != "RUNNING" or self.store.now() >= _aware_time(row["deadline_at"])):
                raise ValueError("agent_session_closed_or_expired")
            yield db, row

    def _fields(self, fields):
        view = deepcopy(fields)
        for key in ("start_at", "end_at"):
            value = _aware_time(view.get(key))
            if value is not None:
                view[key] = value.astimezone(ZoneInfo(self.context.default_timezone)).isoformat()
        return view

    def view(self, key):
        row = self.store.get(key)
        self._scope(row)
        result, work = row["result"], row["work"]
        reads = [{k: deepcopy(v) for k, v in read.items() if k != "events"} for read in work["reads"]]
        value = {"request_key": key, "state": row["state"], "evaluation_now": row["created_at"],
            "deadline_at": row["deadline_at"], "source": row["request"]["source"], "messages": work["messages"],
            "user": {"id": self.context.user_id, "default_timezone": self.context.default_timezone},
            "scope_mode": self.context.scope_mode,
            "time_policy": {"default_duration_minutes": self.context.default_duration_minutes,
                            "source": self.context.duration_policy_source},
            "calendar_records": [{"event_ref": ref, "fields": self._fields(record["fields"]) if record.get("read_verified", True) else {},
                "status": record["status"], "read_verified": record.get("read_verified", True)}
                for ref, record in work["records"].items()], "reads": reads}
        if result is not None:
            value["plan"] = {name: result[name] for name in
                ("items", "operations", "conflict_reports", "plan_fingerprint", "item_errors")}
        return deepcopy(value)

    def model_input(self, key):
        view = self.view(key)
        result = {name: view[name] for name in
            ("messages", "user", "scope_mode", "time_policy", "evaluation_now", "calendar_records", "reads")}
        result.update(source_kind=view["source"]["kind"],
            available_reads=["find_events", "get_events"] + (["get_messages"] if self.history_reader else []),
            max_query_days=self.limits.max_range_days,
            write_capabilities={"fields": ["title", "start_at", "end_at", "location", "description", "duration_minutes"],
                                "informational_only": ["participants"], "invitations": False})
        return result

    def open(self, request, *, retry=False):
        intake = request if isinstance(request, Intake) else Intake(request)
        if len(canonical_json(intake.to_dict()).encode("utf-8")) > self.limits.max_input_bytes:
            raise ValueError("request_too_large")
        row, reused = self.store.register(intake, self.context, duration_seconds=self.limits.deadline_seconds)
        if row["state"] in {"RECEIVED", "RUNNING"} and self.store.now() >= _aware_time(row["deadline_at"]):
            row = self.store.expire(row["id"])
        if retry and row["state"] == "FAILED":
            row = self.store.retry(row["id"])
        claimed = False
        if row["state"] == "RECEIVED":
            with self._locked(row["id"]) as (db, current):
                if current["state"] == "RECEIVED":
                    work = current["work"]
                    if not work["records"]:
                        for binding in self.store.bindings(current["conversation"]):
                            if binding["status"] == "cancelled" or len(work["records"]) >= self.limits.max_candidates:
                                continue
                            ref = "c" + str(len(work["records"]) + 1)
                            work["records"][ref] = {"key": fingerprint({"connection": self.context.connection_ref,
                                "id": binding["id"]}), "id": binding["id"], "fields": {}, "sources": [],
                                "status": "unknown", "read_verified": False}
                    db.execute("UPDATE v3_requests SET state='RUNNING',token=?,work=? WHERE id=?",
                               (uuid.uuid4().hex, canonical_json(work), row["id"]))
                    claimed = True
        return dict(self.view(row["id"]), claimed=claimed, reused=reused)

    async def read(self, key, query, *, activity_ref=None):
        if activity_ref is not None and (type(activity_ref) is not str or not activity_ref.strip()):
            raise ValueError("invalid_activity_ref")
        # Reserve under a short transaction; network waits never hold SQLite locks.
        with self._locked(key, collecting=True) as (db, row):
            validate_query(query, row["work"]["records"], row["work"]["messages"], max_range_days=self.limits.max_range_days)
            for read in reversed(row["work"]["reads"]):
                if read["query"] == query:
                    return dict(self.view(key), latest_read=deepcopy(read), cached=True)
            number = db.execute("SELECT COUNT(*) FROM v3_trace WHERE request_key=? AND kind='READ'", (key,)).fetchone()[0] + 1
            if number > self.limits.max_read_calls:
                raise ValueError("read_budget_exhausted")
            native = deepcopy(query)
            if query["kind"] == "get_events":
                native["event_ids"] = [row["work"]["records"][ref]["id"] for ref in query["event_refs"]]
                native.pop("event_refs")
            db.execute("INSERT INTO v3_trace VALUES(?,'READ',?,?,NULL,NULL,NULL)",
                       (key, number, canonical_json({"query": query, "activity_ref": activity_ref})))
        started, response, error = time.perf_counter(), None, None
        try:
            callback = self.history_reader if query["kind"] == "get_messages" else self.calendar_reader
            if callback is None:
                response = {"read_status": "UNAVAILABLE", "reason": "upstream_original_messages_required"}
            else:
                arguments = dict(query=native, context=self.context)
                if query["kind"] == "get_messages":
                    arguments["source"] = deepcopy(row["request"]["source"])
                remaining = (_aware_time(row["deadline_at"]) - self.store.now()).total_seconds()
                if remaining <= 0:
                    raise ValueError("request_deadline_exceeded")
                response = await asyncio.wait_for(invoke_callback(callback, **arguments),
                                                   min(remaining, self.limits.read_timeout_seconds))
            with self._locked(key, collecting=True) as (db, current):
                work = deepcopy(current["work"])
                if response.get("read_status") == "UNAVAILABLE" and callback is None:
                    read = {"query": deepcopy(query), **response}
                elif query["kind"] == "get_messages":
                    if response.get("conversation_id", row["request"]["source"].get("conversation_id")) != row["request"]["source"].get("conversation_id"):
                        raise ValueError("history_conversation_mismatch")
                    read = self._merge_history(work, query, response)
                else:
                    read = merge_calendar_reply(work, query, response, self.context, max_candidates=self.limits.max_candidates)
                read.update(read_ref="r" + str(number), activity_ref=activity_ref)
                work["reads"].append(read)
                db.execute("UPDATE v3_requests SET work=? WHERE id=?", (canonical_json(work), key))
        except BaseException as failure:
            error = {"type": type(failure).__name__, "code": str(failure)[:300]}
            if hasattr(failure, "native_cli_invocations"):
                error["native_cli_invocations"] = failure.native_cli_invocations
            read = {"query": deepcopy(query), "read_ref": "r" + str(number), "activity_ref": activity_ref,
                    "read_status": "FAILED", "reason": error["code"] or error["type"], "events": []}
            try:
                with self._locked(key, collecting=True) as (db, current):
                    work = current["work"]
                    work["reads"].append(read)
                    db.execute("UPDATE v3_requests SET work=? WHERE id=?", (canonical_json(work), key))
            except ValueError:
                pass  # A closed/superseded session never receives late callback facts.
            if not isinstance(failure, Exception):
                raise
        finally:
            self.store.trace_result(key, "READ", number, output=response, error=error,
                                    metadata={"elapsed_seconds": time.perf_counter() - started,
                                        "native_cli_invocations": (error or {}).get("native_cli_invocations",
                                            (response or {}).get("native_cli_invocations") if type(response) is dict else None)})
        return dict(self.view(key), latest_read=deepcopy(read), cached=False)

    def _merge_history(self, work, query, response):
        if type(response) is not dict or response.get("read_status") not in {"COMPLETE", "PARTIAL", "FAILED"}:
            raise ValueError("invalid_history_response")
        view = {"query": deepcopy(query), "read_status": response["read_status"], "reason": response.get("reason")}
        if response["read_status"] == "FAILED":
            return view
        messages = response.get("messages")
        if type(messages) is not list:
            raise ValueError("invalid_history_messages")
        existing = {m["id"]: m for m in work["messages"]}
        if response.get("conversation_id") is not None and not isinstance(response["conversation_id"], str):
            raise ValueError("invalid_history_conversation")
        for message in messages:
            # Reuse the same strict message admission rules, never fill times.
            Intake({"schema_version": "0.3", "request_id": "history-validation",
                    "source": {"kind": "personal_note", "provider": "trusted-history"}, "messages": [message]})
            prior = existing.get(message["id"])
            if prior is not None and prior != message:
                raise ValueError("history_cannot_rewrite_original")
            existing[message["id"]] = deepcopy(message)
        ordered = response.get("ordered_message_ids")
        if ordered is not None:
            if (type(ordered) is not list or any(type(value) is not str for value in ordered)
                    or len(ordered) != len(set(ordered)) or set(ordered) != set(existing)
                    or [identifier for identifier in ordered if identifier in {m["id"] for m in work["messages"]}]
                       != [m["id"] for m in work["messages"]]):
                raise ValueError("invalid_history_source_order")
            combined = [existing[identifier] for identifier in ordered]
        else:
            combined = deepcopy(work["messages"])
            for message in reversed(messages):
                if any(old["id"] == message["id"] for old in combined):
                    continue
                child = next((index for index, value in enumerate(combined) if value.get("reply_to") == message["id"]), None)
                if child is None:
                    raise ValueError("history_source_order_required")
                combined.insert(child, deepcopy(message))
        work["messages"] = combined
        return view

    def propose(self, key, raw_candidate):
        candidate = decode(raw_candidate)
        if type(candidate) is not dict or type(candidate.get("items")) is not list or not candidate["items"]:
            raise ValueError("result.items:expected_nonempty_array")
        with self._locked(key) as (db, row):
            if row["state"] == "COMPLETED":
                if row["result"]["candidate"] != candidate:
                    raise ValueError("frozen_plan_requires_new_request")
            else:
                if row["state"] != "RUNNING" or self.store.now() >= _aware_time(row["deadline_at"]):
                    raise ValueError("agent_session_closed_or_expired")
                if any(type(item) is dict and item.get("decision") == "NEED_MORE" for item in candidate.get("items", [])):
                    raise ValueError("perform_requested_read_before_propose")
                work = row["work"]
                admission = resolve_candidate(candidate, work, self.context)
                result = {"protocol": "0.3", "policy_version": "1", **admission,
                    "candidate": candidate, "records": work["records"], "reads": work["reads"], "messages": work["messages"],
                    "conflict_reports": [overlaps(operation, work) for operation in admission["operations"]],
                    "conflict_policy": "REPORT_AND_EXECUTE_IF_AUTHORIZED"}
                result["plan_fingerprint"] = fingerprint({"request": row["fingerprint"], "plan": result})
                db.execute("INSERT INTO v3_trace VALUES(?,'DECISION',1,?,NULL,NULL,NULL)",
                           (key, canonical_json(raw_candidate)))
                db.execute("UPDATE v3_requests SET state='COMPLETED',result=?,token=NULL WHERE id=?", (canonical_json(result), key))
        return self.view(key)

    def prepare(self, key, expected_fingerprint):
        if self.executor is None:
            raise ValueError("execution_backend_required")
        row = self.store.current(key)
        self._scope(row)
        if row["state"] != "COMPLETED" or row["result"] is None:
            raise ValueError("plan_not_executable")
        if row["result"]["plan_fingerprint"] != expected_fingerprint:
            raise ValueError("plan_fingerprint_mismatch")
        commands, errors = [], []
        for index, operation in enumerate(row["result"]["operations"]):
            try:
                command = self.executor.prepare(key, index, expected_fingerprint=expected_fingerprint, approved=True)
                commands.append({"id": command["id"], "state": command["state"], "operation": command["payload"]["operation"]})
            except Exception as error:
                errors.append({"operation_index": index, "activity_ref": operation.get("activity_ref"),
                               "type": type(error).__name__, "code": str(error)})
        return {"request_key": key, "plan_fingerprint": expected_fingerprint, "commands": commands,
                "errors": errors, "execution_policy_allows": sorted(self.allowed_operations)}

    def execute(self, command_id):
        if self.executor is None:
            raise ValueError("execution_backend_required")
        command = self.executor.command(command_id)
        if command["payload"]["operation"]["kind"] not in self.allowed_operations:
            raise ValueError("trusted_execution_policy_required")
        done = self.executor.execute(command_id, approved=True)
        return {key: done[key] for key in ("id", "state", "result", "error", "already_applied") if key in done}

    def status(self, *, request_key=None, command_id=None):
        if command_id is None:
            return self.view(request_key)
        if self.executor is None:
            raise ValueError("execution_backend_required")
        command = self.executor.command(command_id)
        return {key: command[key] for key in ("id", "state", "result", "error")}

    def recover(self, command_id):
        if self.executor is None:
            raise ValueError("execution_backend_required")
        command = self.executor.recover(command_id, approved=True)
        return {key: command[key] for key in ("id", "state", "result", "error")}
