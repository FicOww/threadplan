"""Opt-in installed Skill CLI acceptance on one marked private Test event.

No model calls. Inputs and decisions are explicitly constructed engineering
operations; this is not an end-to-end semantic or real-user accuracy score.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from threadplan import Proposal
from scripts.feishu_test_support import DEFAULT_CONNECTION, open_test_host, check_test_calendar
from threadplan.adapters.feishu import FeishuCalendar, TEST_PREFIX
from scripts.build_skill import VERSION


def run(archive):
    started = time.perf_counter()
    if datetime.now(timezone.utc) >= datetime(2026, 11, 20, 2, tzinfo=timezone.utc):
        raise ValueError("frozen_test_dates_have_passed_prepare_new_explicit_scenario")
    directory = ROOT / "runs" / ("skill-real-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    directory.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(directory / "installed")
    tool = directory / "installed" / "threadplan" / "scripts" / "threadplan_tool.py"
    connection = json.loads(DEFAULT_CONNECTION.read_text(encoding="utf-8"))
    if (connection.get("provider"), connection.get("identity"), connection.get("summary")) != (
            "feishu", "user", "ThreadPlan Test"):
        raise ValueError("existing_explicit_test_connection_required")
    config = {"version": "1", "calendar_id": connection["calendar_id"], "state_directory": "state",
        "context": {"user_id": "skill-acceptance-harness", "default_timezone": "Asia/Shanghai",
            "connection_ref": "authorized-feishu-test", "scope_ref": connection["calendar_id"],
            "namespace": "skill-acceptance", "policy_ref": "user-authorized-private-test-only"},
        "execution_policy": {"mode": "preauthorized", "allowed_operations": ["CREATE", "UPDATE", "CANCEL"]}}
    config_path = directory / "config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8", newline="\n")
    receipt = {"source_category": "installed_skill_cli_constructed_operations_real_test_calendar",
               "model_calls": 0, "status": "started", "steps": [], "cleanup_pending": False}
    identifier, keys = None, []
    def save():
        (directory / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    def invoke(*arguments, payload=None):
        call_started = time.perf_counter()
        args = list(arguments)
        if payload is not None:
            path = directory / ("input-" + uuid.uuid4().hex + ".json")
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")
            args.extend(["--query" if args[0] == "read" else "--input", str(path)])
        completed = subprocess.run([sys.executable, "-X", "utf8", str(tool), "--config", str(config_path), *args],
            cwd=directory, capture_output=True, text=True, encoding="utf-8", timeout=240)
        envelope = json.loads(completed.stdout)
        with (directory / "calls.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"command": args[0], "elapsed_seconds": time.perf_counter()-call_started,
                                     "response": envelope}, ensure_ascii=False) + "\n")
        if completed.returncode != 0 or not envelope["ok"]:
            raise RuntimeError("skill_tool_failed:" + json.dumps(envelope.get("error") or envelope.get("result")))
        return envelope["result"]
    save()
    try:
        health = invoke("doctor", "--check-connection")
        if health.get("calendar_verified") is not True:
            raise AssertionError("Test scope was not verified")
        check_test_calendar(FeishuCalendar(connection["calendar_id"]).get_calendar(approved=True))
        title = TEST_PREFIX + "Skill acceptance " + uuid.uuid4().hex[:12]
        query = {"kind": "find_events", "range": {"start_at": "2026-11-20T00:00:00+08:00", "end_at": "2026-11-21T00:00:00+08:00"}}
        for kind in ("CREATE", "UPDATE", "CANCEL"):
            text = {"CREATE": "构造验收：创建11月20日10:00开始、20分钟的测试记录，备注为仅用于Skill执行验收。",
                    "UPDATE": "构造验收：把刚才创建的测试记录改到11月20日10:30，时长和备注不变。",
                    "CANCEL": "构造验收：取消刚才这条测试记录。"}[kind]
            source = {"schema_version": "0.3", "request_id": kind + "-" + uuid.uuid4().hex,
                "source": {"kind": "personal_note", "provider": "skill_acceptance_harness"},
                "messages": [{"id": "source-" + kind, "sender_id": "skill-acceptance-harness",
                    "sent_at": datetime.now(timezone.utc).isoformat(), "text": text + " 标题：" + title}]}
            key = invoke("open", payload=source)["request_key"]
            view = invoke("read", "--request", key, payload=query)
            if kind == "CREATE":
                operation = {"kind": kind, "fields": {"title": title, "start_at": "2026-11-20T10:00:00+08:00",
                             "duration_minutes": 20, "description": "仅用于Skill执行验收。"}}
            else:
                matches = [record for record in view["calendar_records"] if record["fields"].get("title") == title]
                if len(matches) != 1:
                    raise AssertionError("Constructed acceptance target is not unique")
                ref = matches[0]["event_ref"]
                invoke("read", "--request", key, payload={"kind": "get_events", "event_refs": [ref]})
                operation = {"kind": kind, "event_ref": ref}
                if kind == "UPDATE":
                    operation.update(changes={"start_at": "2026-11-20T10:30:00+08:00"}, preserve_duration=True)
            plan = invoke("propose", "--request", key, payload={"items": [{"decision": "PROPOSE", "operation": operation,
                "reason": "Explicit constructed engineering acceptance operation", "evidence_message_ids": ["source-" + kind]}]})["plan"]
            if plan["item_errors"] or len(plan["operations"]) != 1:
                raise AssertionError("constructed_plan_rejected:" + json.dumps(plan["item_errors"]))
            prepared = invoke("prepare", "--request", key, "--fingerprint", plan["plan_fingerprint"])
            if prepared["errors"] or len(prepared["commands"]) != 1:
                raise AssertionError("constructed_prepare_failed:" + json.dumps(prepared["errors"]))
            command = prepared["commands"][0]["id"]
            keys.append(command)
            done = invoke("execute", "--command-id", command)
            if done["state"] != "SUCCEEDED":
                raise AssertionError("Write is not verified")
            target = done["result"]["event_id"]
            if identifier is not None and target != identifier:
                raise AssertionError("Existing real ID changed")
            identifier = target
            repeated = invoke("execute", "--command-id", command)
            if repeated.get("already_applied") is not True:
                raise AssertionError("Successful receipt was not reused")
            observed = invoke("status", "--command-id", command)
            if observed["state"] != "SUCCEEDED":
                raise AssertionError("Durable status differs")
            receipt["steps"].append({"kind": kind, "command_id": command, "verified": True,
                                     "same_real_id": True, "repeated_receipt": True})
            save()
        receipt["status"] = "passed"
    except Exception as error:
        receipt.update(status="failed", error_type=type(error).__name__, error=str(error))
    finally:
        if keys:
            backend = open_test_host(db_path=directory / "state" / "provider.sqlite")
            unresolved = False
            for key in keys:
                row = backend.receipt(key)
                identifier = identifier or ((row or {}).get("result") or {}).get("event_id")
                if row and row["state"] in {"in_progress", "outcome_unknown", "write_acknowledged", "verify_pending"}:
                    try:
                        backend.recover(key, approved=True)
                    except Exception:
                        unresolved = True
            if identifier is not None and not unresolved:
                try:
                    if backend.calendar.verify_cancelled(identifier, approved=True).get("verified") is not True:
                        _, baseline = backend.snapshot(event_ids=[identifier])
                        backend.execute(Proposal("CANCEL", "cancelled", ("authorized-harness-cleanup",), {}, identifier),
                                        "skill-acceptance-cleanup", baseline, approved=True)
                        receipt["cleanup_by_harness"] = True
                    receipt["test_event_cancelled"] = True
                except Exception as error:
                    receipt.update(cleanup_pending=True, cleanup_error_type=type(error).__name__)
            elif unresolved:
                receipt["cleanup_pending"] = True
        receipt["elapsed_seconds"] = time.perf_counter()-started
        save()
    print(json.dumps({"status": receipt["status"], "verified_operations": len(receipt["steps"]),
        "cleanup_pending": receipt["cleanup_pending"], "directory": str(directory)}, ensure_ascii=False))
    return 0 if receipt["status"] == "passed" and not receipt["cleanup_pending"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--archive", type=Path, default=ROOT / "dist" / ("threadplan-" + VERSION + ".zip"))
    args = parser.parse_args()
    if args.run:
        raise SystemExit(run(args.archive))
    print("Plan only: installed Skill CLI, one private ThreadPlan Test event; CREATE/UPDATE/CANCEL, readback, "
          "same-ID and repeat-receipt checks. Constructed operations, zero model calls, no invitations or publication.")
