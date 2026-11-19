"""Opt-in real-calendar fault injection; no model calls and no fake provider.

Uses one clearly marked event in the existing private ThreadPlan Test calendar.
Injected receiver failures are labelled; they are not actual service outages.
Recovery must read desired state without another mutation. Default: plan only.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from threadplan_host.feishu import FeishuHost
from scripts.feishu_test_support import open_test_host
from threadplan import Proposal
from threadplan.adapters.feishu import FeishuError


def run():
    if datetime.now(timezone.utc) >= datetime(2026, 11, 20, 16, tzinfo=timezone.utc):
        raise ValueError("Prepare new explicit scenario dates; do not silently change this fixture")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = ROOT / "runs" / ("calendar-recovery-" + stamp)
    directory.mkdir(parents=True, exist_ok=False)
    record = {"status": "started", "source_category": "real_calendar_controlled_fault_injection",
              "model_calls": 0, "injected_failures": True, "steps": [], "cleanup_pending": False}
    def save():
        (directory / "receipt.json").write_text(json.dumps(record, indent=2), encoding="utf-8", newline="\n")
    save()
    backend, identifier = None, None
    calls = []
    try:
        backend = open_test_host(db_path=directory / "provider.sqlite")
        native = backend.calendar.cli.invoke
        lose_response_for = [None]
        def observed(resource, method, **kwargs):
            calls.append((resource, method))
            response = native(resource, method, **kwargs)
            if resource == "events" and method == lose_response_for[0]:
                lose_response_for[0] = None
                raise FeishuError("injected receiver loss after real " + method.upper() + " acknowledgment")
            return response
        backend.calendar.cli.invoke = observed
        backend.snapshot()
        created_proposal = Proposal("CREATE", "confirmed", ("explicit-harness-fixture",), {
            "title": "ThreadPlan recovery trial " + stamp, "start_at": "2026-11-20T16:00:00Z",
            "end_at": "2026-11-20T16:30:00Z"})
        original_get = backend.calendar.get_event
        fail_read = [True]
        def lost_read(*args, **kwargs):
            if fail_read[0]:
                fail_read[0] = False
                raise FeishuError("injected receiver read failure after real CREATE")
            return original_get(*args, **kwargs)
        backend.calendar.get_event = lost_read
        try:
            backend.execute(created_proposal, "recovery-create", {}, approved=True)
            raise AssertionError("Expected the labelled read failure")
        except FeishuError:
            receipt = backend.receipt("recovery-create")
            if receipt is None or receipt["state"] != "verify_pending":
                raise
            identifier = receipt["result"]["event_id"]
        finally:
            backend.calendar.get_event = original_get
        before = len([call for call in calls if call[1] in {"create", "patch", "delete"}])
        created = backend.recover("recovery-create", approved=True)
        after = len([call for call in calls if call[1] in {"create", "patch", "delete"}])
        if created["event_id"] != identifier or before != after:
            raise AssertionError("Read recovery sent a mutation or changed target")
        record["steps"].append({"operation": "CREATE", "verified": True, "fault": "injected_read_failure",
                                "recovery_mutation_invocations": after - before})
        save()

        _, baseline = backend.snapshot()
        update = Proposal("UPDATE", "confirmed", ("explicit-harness-fixture",),
                          {"title": "ThreadPlan recovered update " + stamp}, identifier)
        lose_response_for[0] = "patch"
        try:
            backend.execute(update, "recovery-update", baseline, approved=True)
            raise AssertionError("Expected the labelled response loss")
        except FeishuError:
            if backend.receipt("recovery-update")["state"] != "outcome_unknown":
                raise
        finally:
            lose_response_for[0] = None
        before = len([call for call in calls if call[1] in {"create", "patch", "delete"}])
        updated = backend.recover("recovery-update", approved=True)
        after = len([call for call in calls if call[1] in {"create", "patch", "delete"}])
        if (updated["event"]["fields"]["title"] != update.changes["title"] or before != after
                or updated["provider_acknowledged"] is not False):
            raise AssertionError("Unknown update was not reconciled truthfully by reads")
        record["steps"].append({"operation": "UPDATE", "verified": True, "fault": "injected_receiver_response_loss",
                                "recovery_mutation_invocations": after - before, "receiver_observed_ack": False})
        save()

        _, baseline = backend.snapshot()
        cancel = Proposal("CANCEL", "cancelled", ("explicit-harness-fixture",), {}, identifier)
        lose_response_for[0] = "delete"
        try:
            backend.execute(cancel, "recovery-cancel", baseline, approved=True)
            raise AssertionError("Expected the labelled response loss")
        except FeishuError:
            if backend.receipt("recovery-cancel")["state"] != "outcome_unknown":
                raise
        finally:
            lose_response_for[0] = None
        before = len([call for call in calls if call[1] in {"create", "patch", "delete"}])
        cancelled = backend.recover("recovery-cancel", approved=True)
        after = len([call for call in calls if call[1] in {"create", "patch", "delete"}])
        if cancelled["cancellation_verification"]["verified"] is not True or before != after:
            raise AssertionError("Unknown cancellation recovery was not read-only")
        record["steps"].append({"operation": "CANCEL", "verified": True, "fault": "injected_receiver_response_loss",
                                "recovery_mutation_invocations": after - before, "receiver_observed_ack": False})
        record.update(status="passed", event_id=identifier, native_mutation_invocations=after)
    except Exception as error:
        record.update(status="failed", error_type=type(error).__name__)
    finally:
        if backend is not None and identifier is None:
            receipt = backend.receipt("recovery-create")
            returned = (receipt or {}).get("result") or {}
            identifier = returned.get("event_id")
            if identifier is None and receipt and receipt["state"] in {"in_progress", "outcome_unknown"}:
                record.update(cleanup_pending=True, cleanup_reason="unknown_create_without_returned_id")
        if backend is not None and identifier is not None:
            try:
                if backend.calendar.verify_cancelled(identifier, approved=True).get("verified") is not True:
                    for key in ("recovery-create", "recovery-update", "recovery-cancel"):
                        receipt = backend.receipt(key)
                        if receipt and receipt["state"] in {"verify_pending", "outcome_unknown"}:
                            backend.recover(key, approved=True)
                    _, baseline = backend.snapshot()
                    backend.execute(Proposal("CANCEL", "cancelled", ("harness-cleanup",), {}, identifier),
                                    "authorized-fault-test-cleanup", baseline, approved=True)
                    record["cleanup_by_harness"] = True
            except Exception as error:
                record.update(cleanup_pending=True, cleanup_error_type=type(error).__name__)
        if record["status"] == "passed" and record["cleanup_pending"]:
            record["status"] = "cleanup_pending"
        save()
    print(json.dumps({"status": record["status"], "verified_fault_cases": len(record["steps"]),
                      "model_calls": 0, "cleanup_pending": record["cleanup_pending"], "run_directory": str(directory)}))
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if args.run:
        raise SystemExit(run())
    print("Plan only: one private ThreadPlan Test event; real CREATE/PATCH/DELETE with labelled receiver failures; "
          "read-only recovery; no model calls, invitations, reminders or primary-calendar changes.")
