"""Build a self-contained ThreadPlan Skill ZIP from an explicit source allowlist."""

import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "threadplan"
VERSION = "1.1.0"
SKILL_FILES = ("SKILL.md", "scripts/threadplan_tool.py", "references/tools.md",
               "references/setup.md", "references/config.example.json")
RUNTIME_FILES = (
    "threadplan/__init__.py", "threadplan/contracts.py", "threadplan/codec.py",
    "threadplan/intake.py", "threadplan/protocol.py", "threadplan/operations.py", "threadplan/timeops.py",
    "threadplan/adapters/__init__.py", "threadplan/adapters/feishu.py",
    "threadplan_host/__init__.py", "threadplan_host/agent.py", "threadplan_host/cli.py",
    "threadplan_host/feishu.py", "threadplan_host/limits.py",
    "threadplan_host/planning_execution.py", "threadplan_host/planning_feishu.py",
    "threadplan_host/planning_reads.py", "threadplan_host/planning_store.py",
    "threadplan_host/planning_validation.py")


def build(output):
    output = Path(output).resolve()
    payload = {}
    for name in SKILL_FILES:
        payload[name] = (SKILL / name).read_bytes()
    for name in RUNTIME_FILES:
        payload["scripts/runtime/" + name] = (ROOT / name).read_bytes()
    # This artifact exposes host tools, not an unshipped model coordinator.
    # Its package API must advertise only dependencies actually included here.
    payload["scripts/runtime/threadplan_host/__init__.py"] = (
        '"""ThreadPlan host-tool artifact API; autonomous coordination is not bundled."""\n'
        'from .agent import AgentSession\nfrom .limits import PlanningLimits\n'
        'from .planning_store import PlanningStore\nfrom .planning_execution import PlanningExecutor\n'
        '__all__ = ["AgentSession", "PlanningLimits", "PlanningStore", "PlanningExecutor"]\n'
    ).encode("utf-8")
    policy = ROOT / "threadplan" / "decision_policy.md"
    payload["scripts/runtime/threadplan/decision_policy.md"] = policy.read_bytes()
    payload["references/decision-policy.md"] = policy.read_bytes()
    manifest = {"name": "threadplan", "version": VERSION, "python": ">=3.11",
        "scope": "explicit user-authorized Feishu calendar; timed non-recurring events",
        "external_runtime": ["existing native lark-cli", "user authentication", "IANA timezone data"],
        "model_service_in_bundle": False,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(payload.items())}}
    payload["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payload.items()):
            info = zipfile.ZipInfo("threadplan/" + name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    temporary.replace(output)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(digest + "  " + output.name + "\n", encoding="utf-8", newline="\n")
    return {"artifact": str(output), "sha256": digest, "files": len(payload), "version": VERSION}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / ("threadplan-" + VERSION + ".zip"))
    args = parser.parse_args()
    print(json.dumps(build(args.output), ensure_ascii=False))
