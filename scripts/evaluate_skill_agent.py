"""Forward-test bridge: real packaged tools, constructed read-only calendars.

The evaluator receives original messages and SKILL.md. Reference labels are
loaded only by score(), after tool decisions, and never by the tool process.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def dataset_file(dataset, part):
    manifest = ROOT / "evals" / (dataset + ".json")
    if manifest.is_file():
        return ROOT / "evals" / json.loads(manifest.read_text(encoding="utf-8"))[part]
    return ROOT / "evals" / (dataset + "." + part + ".jsonl")


def rows(name, dataset="target_v1"):
    return [json.loads(line) for line in dataset_file(dataset, name).read_text(
        encoding="utf-8").splitlines() if line.strip()]


def prepare(archive, dataset="target_v1"):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = ROOT / "runs" / ("skill-agent-" + stamp)
    directory.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(directory / "installed")
    inputs = directory / "inputs"
    inputs.mkdir()
    cases = []
    for row in rows("inputs", dataset):
        path = inputs / (row["case_id"] + ".json")
        path.write_text(json.dumps(row["input"], ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        cases.append({"case_id": row["case_id"], "input_file": str(path)})
    manifest = {"source_category": "constructed_calendar_host_agent_forward_test", "dataset": dataset, "cases": cases,
        "skill": str(directory / "installed" / "threadplan" / "SKILL.md"),
        "archive_sha256": hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
        "dataset_sha256": {name: hashlib.sha256(dataset_file(dataset, name).read_bytes()).hexdigest()
                           for name in ("inputs", "fixtures", "expectations")}}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return {"directory": str(directory), "manifest": str(directory / "manifest.json"), "case_count": len(cases)}


def tool(directory, case_id, arguments):
    if not arguments or arguments[0] not in {"open", "read", "propose", "status"}:
        raise ValueError("forward_test_allows_only_open_read_propose_status")
    dataset = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))["dataset"]
    fixture = next(row for row in rows("fixtures", dataset) if row["case_id"] == case_id)
    script = directory / "installed" / "threadplan" / "scripts" / "threadplan_tool.py"
    spec = importlib.util.spec_from_file_location("packaged_threadplan_tool", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from threadplan.codec import _aware_time
    from threadplan_host.agent import AgentSession
    from threadplan_host.planning_store import PlanningStore

    class NoWrites:
        def snapshot(self, **kwargs):
            raise RuntimeError("forward_test_does_not_prepare_or_execute_calendar_writes")
        def execute(self, *args, **kwargs):
            raise RuntimeError("forward_test_writes_disabled")
        def receipt(self, identifier):
            return None
        def recover(self, *args, **kwargs):
            raise RuntimeError("forward_test_has_no_write_receipts")

    def reader(*, query, context):
        events = fixture["calendar_fixture"]["events"]
        if query["kind"] == "get_events":
            events = fixture["calendar_fixture"].get("detail_events", events)
            events = [event for event in events if event["id"] in query["event_ids"]]
        else:
            low, high = (_aware_time(query["range"][key]) for key in ("start_at", "end_at"))
            events = [event for event in events if _aware_time(event["fields"]["start_at"]) < high
                      and low < _aware_time(event["fields"]["end_at"])]
        return {"read_status": fixture["calendar_fixture"]["read_status"], "events": events,
                "source_category": "assistant_constructed_calendar_fixture",
                "searched": {"range": query.get("range"), "applied_filters": {}}}

    from threadplan import UpstreamContext
    context = UpstreamContext(**fixture["context"])
    runtime = AgentSession(PlanningStore(directory / (case_id + ".sqlite"),
        clock=lambda: _aware_time(fixture["evaluation_now"])), context=context,
        calendar_reader=reader, backend=NoWrites())
    from threadplan_host import cli
    cli.session = lambda config_path: runtime
    return module.main(arguments, host_only=True)


def score(directory):
    sys.path.insert(0, str(ROOT))
    from scripts.evaluate_simulated_chats import score as constraints
    from threadplan_host.planning_store import PlanningStore
    dataset = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))["dataset"]
    labels = {row["case_id"]: row["expected"] for row in rows("expectations", dataset)}
    report = {"source_category": "constructed_calendar_host_agent_forward_test",
              "independent_human_gold": False, "calendar_writes": 0, "cases": []}
    route_path = directory / "host_route.json"
    if route_path.is_file():
        route = json.loads(route_path.read_text(encoding="utf-8"))
        if route.get("evaluation_mode") == "output_replay":
            report.update(source_category="output_replay_constructed_calendar_packaged_tools",
                          fresh_model_inference=False)
    for entry in json.loads((directory / "manifest.json").read_text(encoding="utf-8"))["cases"]:
        case = entry["case_id"]
        file = directory / (case + ".sqlite")
        value, read_calls = None, 0
        if file.is_file():
            with sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                row = db.execute("SELECT * FROM v3_requests ORDER BY rowid DESC LIMIT 1").fetchone()
                value = PlanningStore.decode(row) if row else None
                if value:
                    read_calls = db.execute("SELECT COUNT(*) FROM v3_trace WHERE request_key=? AND kind='READ'", (value['id'],)).fetchone()[0]
        result = (value or {}).get("result") or {}
        verdict = constraints(result, labels[case])
        if value is None or value["state"] != "COMPLETED":
            verdict["concrete_constraints_passed"] = False
            verdict["errors"].append("no_completed_plan")
        report["cases"].append({"case_id": case, "score": verdict,
            "read_calls": read_calls})
    report["passed"] = sum(entry["score"]["concrete_constraints_passed"] for entry in report["cases"])
    (directory / "score.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", type=Path)
    parser.add_argument("--dataset", default="target_v1")
    parser.add_argument("--score", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--case")
    args, remaining = parser.parse_known_args()
    if args.prepare:
        print(json.dumps(prepare(args.prepare, args.dataset), ensure_ascii=False))
    elif args.score:
        print(json.dumps(score(args.score), ensure_ascii=False))
    else:
        raise SystemExit(tool(args.directory, args.case, remaining))
