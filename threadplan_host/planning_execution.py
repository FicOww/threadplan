"""Explicitly authorized v0.3 execution over an existing journaled backend."""

from copy import deepcopy
import json

from threadplan.codec import canonical_json, fingerprint
from threadplan.operations import as_proposal
from threadplan.codec import _aware_time


class PlanningExecutor:
    def __init__(self, store, *, context, backend):
        if any(not callable(getattr(backend, method, None)) for method in ("snapshot", "execute", "receipt", "recover")):
            raise TypeError("journaled_calendar_backend_required")
        self.store, self.context, self.backend = store, context, backend

    def _stored_command(self, identifier):
        with self.store.connect() as db:
            command = self.store.decode(db.execute("SELECT * FROM v3_commands WHERE id=?", (identifier,)).fetchone())
        self._scope(command["payload"]["context"], recovery=True)
        return command

    def command(self, identifier):
        command = self._stored_command(identifier)
        receipt = self.backend.receipt(identifier)
        if receipt is None:
            # Legacy rows remain readable without rewriting or deleting data.
            command["state"] = {"DISPATCHING": "IN_FLIGHT", "ISSUED": "OUTCOME_UNKNOWN"}.get(
                command["state"], command["state"])
            return command
        command.update(state={"succeeded": "SUCCEEDED", "failed_before_write": "FAILED_BEFORE_WRITE",
            "write_acknowledged": "VERIFY_PENDING", "verify_pending": "VERIFY_PENDING",
            "in_progress": "IN_FLIGHT"}.get(receipt["state"], "OUTCOME_UNKNOWN"),
            result=receipt.get("result"), error=receipt.get("error"))
        if command["state"] == "SUCCEEDED":
            self._verified_result(command)
        return command

    def _scope(self, context, *, recovery=False):
        expected = self.context.to_dict()
        keys = ("namespace", "user_id", "connection_ref", "scope_ref") if recovery else tuple(expected)
        if any(context.get(key) != expected[key] for key in keys):
            raise ValueError("execution_scope_mismatch")

    def _same_fields(self, expected, actual):
        for key, value in expected.items():
            if key == "participants" or value is None:
                continue
            other = actual.get(key)
            if key.endswith("_at"):
                if _aware_time(value) != _aware_time(other):
                    return False
            elif value != other:
                return False
        return True

    def _fresh_baseline(self, operation, records, request_key):
        if operation["kind"] == "CREATE":
            events, _ = self.backend.snapshot(range={name: operation["changes"][name]
                                                    for name in ("start_at", "end_at")})
            start, end = (_aware_time(operation["changes"].get(name)) for name in ("start_at", "end_at"))
            def in_range(fields):
                low, high = (_aware_time(fields.get(name)) for name in ("start_at", "end_at"))
                return low is not None and high is not None and low < end and start < high
            baseline = {record["id"]: record["fields"] for record in records.values()
                        if record["status"] != "cancelled"}
            # Verified writes from this same approved plan are expected changes,
            # including overlapping independent activities and partial retries.
            with self.store.connect() as db:
                identifiers = [row[0] for row in db.execute(
                    "SELECT id FROM v3_commands WHERE request_key=?", (request_key,))]
            completed = [command for key in identifiers if (command := self.command(key))["state"] == "SUCCEEDED"]
            for command in completed:
                result = command["result"]
                if command["payload"]["operation"]["kind"] == "CANCEL":
                    baseline.pop(result["event_id"], None)
                else:
                    baseline[result["event_id"]] = result["event"]["fields"]
            expected = {key: fields for key, fields in baseline.items() if in_range(fields)}
            actual = {event.id: event.fields for event in events if in_range(event.fields)}
            # Disappeared overlaps cannot add a duplicate. New candidates need
            # semantic judgment; neither overlap nor a different title proves
            # identity. Preserve the model's judgment on unchanged known rows.
            if actual.keys() - expected.keys():
                raise ValueError("new_calendar_candidates_reanalysis_required")
            if any(not self._same_fields(
                    {k:v for k,v in expected[key].items()
                     if k in {"title", "start_at", "end_at", "location", "description"}}, fields)
                    for key, fields in actual.items()):
                raise ValueError("calendar_baseline_changed_reanalysis_required")
            return {}
        target = records[operation["event_ref"]]
        events, baselines = self.backend.snapshot(event_ids=[target["id"]])
        current = next((event for event in events if event.id == target["id"]), None)
        if current is None or target["id"] not in baselines:
            raise ValueError("target_not_in_authorized_backend_snapshot")
        actual = deepcopy(current.fields)
        raw = baselines[target["id"]]
        for key in ("free_busy_status", "recurrence"):
            if key in raw:
                actual[key] = raw[key]
        comparable = {k: v for k, v in target["fields"].items()
                      if k in {"title", "start_at", "end_at", "location", "description", "free_busy_status", "recurrence"}}
        if not self._same_fields(comparable, actual):
            raise ValueError("calendar_baseline_changed_reanalysis_required")
        return {target["id"]: deepcopy(raw)}

    def prepare(self, request_key, operation_index, *, expected_fingerprint, approved=False):
        if approved is not True:
            raise ValueError("explicit_upstream_approval_required")
        row = self.store.current(request_key)
        self._scope(row["context"])
        result = row["result"]
        if row["state"] != "COMPLETED" or result is None or result.get("protocol") != "0.3":
            raise ValueError("plan_not_executable")
        if expected_fingerprint != result["plan_fingerprint"]:
            raise ValueError("plan_fingerprint_mismatch")
        if type(operation_index) is not int or not 0 <= operation_index < len(result["operations"]):
            raise ValueError("invalid_operation_index")
        if self.store.now() >= _aware_time(row["deadline_at"]):
            raise ValueError("approval_deadline_exceeded")
        operation = deepcopy(result["operations"][operation_index])
        records = result["records"]
        native = records[operation["event_ref"]]["id"] if operation["event_ref"] else None
        sources = [message for message in result["messages"] if message["id"] in operation["evidence_message_ids"]]
        # Stable source/operation identity also reuses a completed receipt for a
        # redelivery carrying a new request ID. Distinct activities remain distinct.
        identical_before = sum(op["kind"] == operation["kind"] and op["event_ref"] == operation["event_ref"]
                               and op["changes"] == operation["changes"] for op in result["operations"][:operation_index])
        signature = fingerprint({"conversation": row["conversation"], "source": sources,
            "kind": operation["kind"], "target": native, "changes": operation["changes"], "occurrence": identical_before})
        identifier = fingerprint({"request": request_key, "signature": signature})
        with self.store.connect() as db:
            previous = db.execute("SELECT * FROM v3_commands WHERE id=?", (identifier,)).fetchone()
            if previous is None:
                for existing in db.execute("SELECT * FROM v3_commands"):
                    if (json.loads(existing["payload"]).get("signature") == signature
                            and self.command(existing["id"])["state"] == "SUCCEEDED"):
                        previous = existing
                        break
        if previous is not None:
            return self.command(previous["id"])
        baselines = self._fresh_baseline(operation, records, request_key)
        payload = {"context": row["context"], "operation": operation, "native_id": native,
                   "baselines": baselines, "records": records, "conversation": row["conversation"], "signature": signature}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute("SELECT request_key FROM v3_latest WHERE conversation=?", (row["conversation"],)).fetchone()
            if latest is None or latest[0] != request_key:
                raise ValueError("stale_request")
            db.execute("INSERT OR IGNORE INTO v3_commands VALUES(?,?,?,?,'PREPARED',NULL,NULL)",
                       (identifier, request_key, expected_fingerprint, canonical_json(payload)))
        return self.command(identifier)

    def _verified_result(self, command):
        result = command["result"]
        op, payload = command["payload"]["operation"], command["payload"]
        if not isinstance(result, dict) or not isinstance(result.get("event_id"), str) or not result["event_id"]:
            raise ValueError("verified_result_missing_real_id")
        if payload["native_id"] is not None and result["event_id"] != payload["native_id"]:
            raise ValueError("execution_target_mismatch")
        if op["kind"] == "CANCEL":
            if result.get("cancellation_verification", {}).get("verified") is not True:
                raise ValueError("cancellation_not_verified")
        elif not self._same_fields(op["changes"], result.get("event", {}).get("fields", {})):
            raise ValueError("execution_readback_mismatch")

    def _sync(self, identifier, *, dispatch_failed=False):
        command = self.command(identifier)
        receipt = self.backend.receipt(identifier)
        result, payload = command["result"], command["payload"]
        op = payload["operation"]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if receipt is not None:
                db.execute("UPDATE v3_commands SET state='ISSUED',result=NULL,error=NULL WHERE id=?", (identifier,))
            elif dispatch_failed:
                # A journaled backend must persist a receipt before dispatching
                # any write. Only a returned exception releases this local latch;
                # a concurrent recovery without a receipt cannot do so.
                db.execute("UPDATE v3_commands SET state='FAILED_BEFORE_WRITE',error=? WHERE id=? AND state='DISPATCHING'",
                           (canonical_json({"backend_state": "not_started"}), identifier))
            if command["state"] == "SUCCEEDED":
                record = {"id": result["event_id"], "fields": result.get("event", {}).get("fields", {}),
                          "status": "cancelled" if op["kind"] == "CANCEL" else "confirmed",
                          "source_message_ids": op["evidence_message_ids"], "verified_by": identifier}
                existing = db.execute("SELECT record FROM v3_bindings WHERE conversation=? AND event_id=?",
                                      (payload["conversation"], result["event_id"])).fetchone()
                # Source supersession orders frozen commands. Replaying or
                # recovering an older receipt must not regress a newer binding.
                prior = json.loads(existing[0]).get("verified_by") if existing else None
                newer = db.execute("SELECT 1 FROM v3_commands prior JOIN v3_commands current ON current.id=? "
                                   "WHERE prior.id=? AND prior.rowid>current.rowid", (identifier, prior)).fetchone()
                if not newer:
                    db.execute("INSERT INTO v3_bindings VALUES(?,?,?) ON CONFLICT(conversation,event_id) DO UPDATE SET record=excluded.record",
                               (payload["conversation"], result["event_id"], canonical_json(record)))
        return self.command(identifier)

    def execute(self, identifier, *, approved=False):
        if approved is not True:
            raise ValueError("explicit_upstream_approval_required")
        command = self.command(identifier)
        payload = command["payload"]
        self._scope(payload["context"])
        if command["state"] == "SUCCEEDED":
            return dict(self._sync(identifier), already_applied=True)
        if command["state"] not in {"PREPARED", "FAILED_BEFORE_WRITE"}:
            raise ValueError("execution_unresolved_use_read_only_recovery")
        row = self.store.current(command["request_key"])
        if row["result"]["plan_fingerprint"] != command["plan_fingerprint"]:
            raise ValueError("plan_changed")
        if self.store.now() >= _aware_time(row["deadline_at"]):
            raise ValueError("approval_deadline_exceeded")
        operation = payload["operation"]
        # Required target/baseline reads are separate from advisory overlap checks.
        if operation["kind"] == "CREATE":
            self._fresh_baseline(operation, payload["records"], command["request_key"])
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute("SELECT request_key FROM v3_latest WHERE conversation=?", (payload["conversation"],)).fetchone()
            if latest is None or latest[0] != command["request_key"]:
                raise ValueError("stale_request")
            pending = db.execute("SELECT id,payload FROM v3_commands WHERE id<>?", (identifier,)).fetchall()
            for other in pending:
                other_payload = json.loads(other["payload"])
                same_scope = all(other_payload["context"].get(key) == payload["context"].get(key)
                                 for key in ("namespace", "user_id", "connection_ref", "scope_ref"))
                if (same_scope
                        and other_payload["native_id"] == payload["native_id"]
                        and self.command(other["id"])["state"] in {"IN_FLIGHT", "OUTCOME_UNKNOWN", "VERIFY_PENDING"}):
                    raise ValueError("resource_has_unresolved_execution")
            if not db.execute("UPDATE v3_commands SET state='DISPATCHING' WHERE id=? AND state IN ('PREPARED','FAILED_BEFORE_WRITE','ISSUED')",
                              (identifier,)).rowcount:
                raise ValueError("execution_already_dispatched")
        try:
            self.backend.execute(as_proposal(operation, payload["native_id"]), identifier,
                                 payload["baselines"], approved=True, exclude_attendees=True)
        except Exception:
            self._sync(identifier, dispatch_failed=True)
            raise
        return self._sync(identifier)

    def recover(self, identifier, *, approved=False):
        if approved is not True:
            raise ValueError("explicit_upstream_approval_required")
        command = self.command(identifier)
        self._scope(command["payload"]["context"], recovery=True)
        if command["state"] in {"PREPARED", "FAILED_BEFORE_WRITE", "SUCCEEDED"}:
            return self._sync(identifier)
        try:
            self.backend.recover(identifier, approved=True)
        except Exception:
            self._sync(identifier)
            raise
        return self._sync(identifier)
