"""Export the reviewed mock-calendar workflows as a standalone HTML replay."""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from demo.scenarios import ROOT, SOURCE_RECORDED
from demo.workbench import Workbench


WEB = ROOT / "demo/web"
CONSTRUCTED_PROVIDERS = {
    "assistant_constructed_acceptance", "assistant_constructed_probe",
    "assistant_constructed_scope_extension", "assistant_constructed_v1_gap", "authored_diversity50",
}
PRIVATE_MARKERS = re.compile(
    r"https?://|[A-Z]:[\\/]|[A-Z0-9_.%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|"
    r"sk-[A-Z0-9_-]{12,}|Bearer\s+[A-Z0-9_.-]{12,}|\b1[3-9]\d{9}\b", re.I,
)
UNRESOLVED = {"OUTCOME_UNKNOWN", "VERIFY_PENDING", "IN_FLIGHT"}


def receipt_fields(receipt):
    return {field: receipt[field] for field in ("state", "acknowledged") if field in receipt}


def calendar_delta(before, after):
    old = {event["id"]: event for event in before}
    new = {event["id"]: event for event in after}
    return {"remove": sorted(old.keys() - new.keys()),
            "upsert": [new[key] for key in sorted(new) if new[key] != old.get(key)]}


def replay_actions(app, owner, task, activities):
    task_id = task["id"]
    previous = task["calendar"]
    seen_events = set()
    decisions = {}
    for activity in activities:
        command = app.prepare(owner, task_id, activity)
        if command["state"] != "PREPARED":
            raise ValueError("offline_prepare_not_verified:" + task["scenario"]["id"])
        committed = app.command(owner, task_id, command["id"], "confirm")
        current = app.task_view(owner, task_id)["calendar"]
        change = calendar_delta(previous, current)
        touched = set(change["remove"]) | {event["id"] for event in change["upsert"]}
        if touched & seen_events:
            raise ValueError("offline_operations_are_not_independent:" + task["scenario"]["id"])
        seen_events |= touched
        trace = {**change, "confirm": receipt_fields(committed)}
        if committed["state"] in UNRESOLVED:
            observed = app.command(owner, task_id, command["id"], "recover")
            if app.task_view(owner, task_id)["calendar"] != current:
                raise ValueError("offline_recovery_changed_calendar:" + task["scenario"]["id"])
            trace["recover"] = receipt_fields(observed)
        elif committed["state"] != "SUCCEEDED":
            raise ValueError("offline_execution_not_verified:" + task["scenario"]["id"])
        decisions[activity] = trace
        previous = current
    return decisions


def capture_phase(app, owner, case_id, choice=None):
    task = app.create_task(owner, case_id)
    if choice:
        task = app.clarify(owner, task["id"], choice)
    if task["state"] != "COMPLETED" or task["calendar"] != task["initial_calendar"]:
        raise ValueError("offline_initial_state_not_verified:" + case_id)
    chosen = [item["activity_ref"] for item in task["items"] if "operation" in item]
    if task["choices"] and chosen:
        raise ValueError("offline_branches_need_an_explicit_source_transition:" + case_id)
    baseline = {key: value for key, value in task.items() if key not in {"id", "calendar", "commands", "phase", "closed"}}
    decisions = replay_actions(app, owner, task, chosen)
    if len(chosen) > 1:
        alternate = app.create_task(owner, case_id)
        if choice:
            alternate = app.clarify(owner, alternate["id"], choice)
        if replay_actions(app, owner, alternate, list(reversed(chosen))) != decisions:
            raise ValueError("offline_operation_order_changed_outcome:" + case_id)
    return {**baseline, "actions": decisions}


def capture(builtin_only):
    with TemporaryDirectory(prefix="threadplan-html-") as directory:
        app = Workbench(Path(directory), use_archive=not builtin_only)
        if not builtin_only and app.catalog_info.get("recorded") != 50:
            raise ValueError("complete local diversity50 archive required; --builtin-only exports 12 cases")
        owner = app.principal(app.create_session())
        scenes = []
        for case in app.cases.values():
            if case.source == SOURCE_RECORDED and case.intake["source"]["provider"] not in CONSTRUCTED_PROVIDERS:
                raise ValueError("unreviewed source in " + case.id)
            initial = capture_phase(app, owner, case.id)
            choices = {choice: capture_phase(app, owner, case.id, choice) for choice in initial["choices"]}
            scene = {"initial": initial, "choices": choices}
            if PRIVATE_MARKERS.search(json.dumps(scene, ensure_ascii=False)):
                raise ValueError("possible private field in " + case.id)
            scenes.append(scene)
        return scenes


def render_html(scenes):
    packed = json.dumps(scenes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    version = hashlib.sha256(packed.encode("utf-8")).hexdigest()[:20]
    packed = packed.replace("<", "\\u003c")
    script = "\n".join([
        (WEB / "offline.js").read_text(encoding="utf-8"),
        'window.threadplanOffline = createOfflineReplay({version:"' + version + '",scenes:' + packed + "});",
        (WEB / "calendar.js").read_text(encoding="utf-8"),
        (WEB / "guide.js").read_text(encoding="utf-8"),
        (WEB / "app.js").read_text(encoding="utf-8"),
    ])
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    policy = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-" + digest +
              "'; connect-src 'none'; base-uri 'none'; form-action 'none'")
    template = (WEB / "index.html").read_text(encoding="utf-8")
    template = template.replace("<title>ThreadPlan</title>", "<title>ThreadPlan · 离线演示</title>")
    template = template.replace('<link rel="stylesheet" href="/style.css">',
                                '<meta http-equiv="Content-Security-Policy" content="' + policy + '">\n'
                                "<style>\n" + (WEB / "style.css").read_text(encoding="utf-8") +
                                "\n" + (WEB / "guide.css").read_text(encoding="utf-8") + "</style>")
    template = re.sub(r'^  <script src="/(?:calendar|app)\.js" defer></script>\n', "", template, flags=re.M)
    template = template.replace('href="/"', 'href="#"').replace('href="/?view=', 'href="#view=')
    guide = (WEB / "guide.html").read_text(encoding="utf-8")
    return template.replace("</body>", guide + "\n<script>" + script + "</script>\n</body>")


def build(output, *, builtin_only=False):
    scenes = capture(builtin_only)
    content = render_html(scenes).encode("utf-8")
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".html.tmp")
    temporary.write_bytes(content)
    temporary.replace(output)
    return {"artifact": str(output), "scenarios": len(scenes),
            "branches": sum(len(scene["choices"]) for scene in scenes), "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--builtin-only", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/ThreadPlan-离线演示.html")
    args = parser.parse_args()
    print(json.dumps(build(args.output, builtin_only=args.builtin_only), ensure_ascii=False))
