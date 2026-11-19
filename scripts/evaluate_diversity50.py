"""One frozen 50-case campaign in ten bounded batches. No calendar writes."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(args):
    ids = [json.loads(line)["case_id"] for line in
           (ROOT / "evals/diversity50.inputs.jsonl").read_text(encoding="utf-8").splitlines()]
    if len(ids) != 50 or len(set(ids)) != 50:
        raise ValueError("expected_fifty_unique_cases")
    if not args.run:
        print(json.dumps({"mode":"preview", "cases":50, "batches":10, "max_calls_per_batch":15,
                          "provider":args.provider, "calendar_writes":0}))
        return 0
    if not args.provider:
        raise ValueError("explicit_provider_required")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip():
        raise ValueError("freeze_and_commit_worktree_before_campaign")
    directory = ROOT / "runs" / ("diversity50-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    directory.mkdir(parents=True)
    frozen = [path for part in ("threadplan", "threadplan_host") for path in (ROOT / part).rglob("*.py")]
    frozen += list((ROOT / "evals").glob("diversity50.*"))
    frozen += [ROOT/"threadplan/decision_policy.md", ROOT/"scripts/evaluate_simulated_chats.py", Path(__file__)]
    hashes = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in frozen}
    receipt = {"status":"running", "case_order":ids, "case_count":50, "provider":args.provider,
        "requested_model":args.codex_model if args.provider == "codex" else "fixed_free_route",
        "reference_status":"assistant_defined_development_not_human_gold",
        "calendar_backend":"simulated", "calendar_writes":0,
        "git_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
        "frozen_sha256":hashes, "batches":[]}
    def save():
        (directory / "receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    save()
    print(json.dumps({"campaign_directory":str(directory)}),flush=True)
    for offset in range(0, 50, 5):
        if any(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest for name,digest in hashes.items()):
            receipt["status"] = "stopped_source_or_dataset_changed"; save(); return 1
        batch = {"batch":offset//5+1, "cases":ids[offset:offset+5]}
        receipt["batches"].append(batch); save()
        log = directory / ("batch-%02d.log" % batch["batch"])
        argv = [sys.executable,"-X","utf8",str(ROOT/"scripts/evaluate_simulated_chats.py"),
            "--dataset","diversity50","--provider",args.provider,"--codex-model",args.codex_model,
            "--max-calls","15","--run","--cases",*batch["cases"]]
        with log.open("w",encoding="utf-8",newline="\n") as stream:
            result = subprocess.run(argv,cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,shell=False)
        lines = log.read_text(encoding="utf-8").splitlines()
        header = next((json.loads(line) for line in lines if line.startswith('{"run_directory":')), None)
        batch["returncode"] = result.returncode
        if result.returncode or not header:
            receipt["status"]="stopped_batch_failure"; save(); return 1
        batch["run_directory"]=str(Path(header["run_directory"]).relative_to(ROOT))
        detail = json.loads((ROOT/batch["run_directory"]/"receipt.json").read_text(encoding="utf-8"))
        batch["model_calls"]=detail["model_calls"]
        batch["constraint_passed"]=sum(c["chain"]["score"]["concrete_constraints_passed"] for c in detail["cases"])
        # No provider/route fallback. Stop before consuming more batches on transport availability errors.
        errors = json.dumps([case["chain"].get("error") for case in detail["cases"]]).lower()
        print(json.dumps(batch,ensure_ascii=False),flush=True)
        if any(token in errors for token in ("usage_limit", "quota", "authentication", "rate_limit", "429")):
            receipt["status"]="stopped_provider_unavailable"; save(); return 1
        save()
    receipt["status"]="complete_review_pending"
    receipt["model_calls"]=sum(b["model_calls"] for b in receipt["batches"])
    receipt["constraint_passed"]=sum(b["constraint_passed"] for b in receipt["batches"])
    save()
    print(json.dumps({key:receipt[key] for key in ("status","model_calls","constraint_passed")}),flush=True)
    return 0


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--provider", choices=("codex","openrouter"))
    parser.add_argument("--codex-model", default="gpt-6.1-sol")
    raise SystemExit(run(parser.parse_args()))
