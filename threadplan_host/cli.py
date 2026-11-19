"""ThreadPlan CLI and host tools. Python standard library only."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from threadplan import UpstreamContext
from threadplan.codec import _reject_constant, _unique_object
from threadplan.adapters.feishu import FeishuCalendar, discover_cli
from threadplan_host.agent import AgentSession
from threadplan_host.feishu import FeishuHost
from threadplan_host.planning_feishu import FeishuPlanningReader
from threadplan_host.planning_store import PlanningStore


def load_json(path):
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8-sig")
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def external_state(file, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("state_directory_required")
    directory = Path(value).expanduser()
    directory = (directory if directory.is_absolute() else file.parent / directory).resolve()
    runtime_root = Path(__file__).resolve().parents[1]
    skill_root = (runtime_root.parent.parent
                  if runtime_root.name == "runtime" and runtime_root.parent.name == "scripts"
                  else runtime_root / "skills" / "threadplan")
    if any(target == skill_root or skill_root in target.parents for target in (file, directory)):
        raise ValueError("configuration_and_state_must_be_outside_skill_package")
    return directory


def configuration(path):
    if not path:
        raise ValueError("configuration_required_set_THREADPLAN_CONFIG_or_config")
    file = Path(path).expanduser().resolve()
    data = load_json(str(file))
    if type(data) is not dict or set(data) != {
            "version", "context", "calendar_id", "state_directory", "execution_policy"} or data["version"] != "1":
        raise ValueError("invalid_configuration")
    context = UpstreamContext(**data["context"])
    if not isinstance(data["calendar_id"], str) or not data["calendar_id"].strip() or data["calendar_id"].lower() == "primary":
        raise ValueError("explicit_canonical_calendar_id_required")
    directory = external_state(file, data["state_directory"])
    policy = data["execution_policy"]
    if type(policy) is not dict or set(policy) - {"mode", "allowed_operations"}:
        raise ValueError("invalid_execution_policy")
    mode = policy.get("mode")
    if mode == "read_only" and not policy.get("allowed_operations"):
        allowed = []
    elif mode == "preauthorized" and type(policy.get("allowed_operations")) is list and policy["allowed_operations"]:
        allowed = policy["allowed_operations"]
        if any(type(kind) is not str or kind not in {"CREATE", "UPDATE", "CANCEL"} for kind in allowed):
            raise ValueError("invalid_execution_policy")
    else:
        raise ValueError("invalid_execution_policy")
    return data, context, directory, allowed


def session(config_path):
    data, context, directory, allowed = configuration(config_path)
    calendar = FeishuCalendar(data["calendar_id"])
    backend = FeishuHost(calendar, directory / "provider.sqlite")
    reader = FeishuPlanningReader(calendar, connection_ref=context.connection_ref, scope_ref=context.scope_ref)
    return AgentSession(PlanningStore(directory / "agent.sqlite"), context=context,
        calendar_reader=reader, backend=backend, allowed_operations=allowed)


def doctor(config_path, check_connection=False):
    checks = {"python": sys.version.split()[0], "python_supported": sys.version_info >= (3, 11),
              "model_service_required": False, "explicit_calendar_scope": True}
    try:
        checks["lark_cli"] = discover_cli()
    except Exception as error:
        checks["lark_cli_error"] = str(error)
    try:
        data, context, directory, allowed = configuration(config_path)
        checks.update(configuration_valid=True, user_id=context.user_id,
                      timezone=context.default_timezone, state_directory=str(directory),
                      execution_allowed_operations=allowed)
    except Exception as error:
        checks.update(configuration_valid=False, configuration_error=str(error))
    if check_connection and checks.get("configuration_valid"):
        try:
            calendar = FeishuCalendar(data["calendar_id"])
            calendar.get_calendar(approved=True)
            checks["calendar_verified"] = True
        except Exception as error:
            details = getattr(error, "details", {})
            checks.update(calendar_verified=False, connection_error={
                "message":str(error), **{key: details[key] for key in
                    ("type", "subtype", "code", "missing_scopes") if key in details}})
    checks["ready"] = checks["python_supported"] and "lark_cli" in checks and checks["configuration_valid"]
    if check_connection:
        checks["ready"] = checks["ready"] and checks.get("calendar_verified", False)
    return checks


def configure(args):
    """Create a new external read-only configuration; never grant execution."""
    import hashlib
    if not args.config:
        raise ValueError("config_destination_required")
    path = Path(args.config).expanduser().resolve()
    if path.exists():
        raise ValueError("configuration_already_exists_no_overwrite")
    reference = hashlib.sha256((args.user_id + "\0" + args.calendar_id).encode()).hexdigest()[:24]
    data = {"version": "1", "context": {"user_id": args.user_id,
        "default_timezone": args.timezone, "connection_ref": "feishu-" + reference,
        "scope_ref": args.calendar_id, "namespace": "threadplan-skill"},
        "calendar_id": args.calendar_id, "state_directory": args.state_directory,
        "execution_policy": {"mode": "read_only"}}
    # Validate exactly as execution does, before creating persistent state.
    context = UpstreamContext(**data["context"])
    if not args.calendar_id.strip() or args.calendar_id.lower() == "primary":
        raise ValueError("explicit_canonical_calendar_id_required")
    external_state(path, args.state_directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {"configuration": str(path), "execution_policy": data["execution_policy"],
            "timezone": context.default_timezone, "connection_verified": False}


async def independent(args):
    # Provider selection is explicit; neither route falls back to another.
    from .planning import ConversationPlanner
    runtime = session(args.config)
    _, _, directory, _ = configuration(args.config)
    if args.provider == "openrouter":
        from threadplan.openrouter_transport import OpenRouterTransport
        from .model_config import openrouter_key
        key = openrouter_key(Path(args.config).expanduser().resolve().parent)
        if not key:
            raise ValueError("local_model_key_missing_no_fallback")
        model = OpenRouterTransport(api_key=key, runs_dir=directory / "model")
    else:
        from threadplan.codex_transport import CodexJSONSubscription
        from .limits import PlanningLimits
        model = CodexJSONSubscription(model=args.codex_model, reasoning="low", runs_dir=directory / "model")
        runtime.limits = PlanningLimits(model_timeout_seconds=120, deadline_seconds=420)
    planner = ConversationPlanner(runtime.store, context=runtime.context, calendar_reader=runtime.calendar_reader,
                                  limits=runtime.limits)
    row = await planner.analyze(load_json(args.input), model, retry=args.retry)
    value = {"analysis": runtime.view(row["id"]), "execution": [], "errors": []}
    if row.get("error"):
        value["errors"].append(row["error"])
    if row.get("result"):
        value["errors"].extend(row["result"].get("item_errors", []))
    if row["state"] == "COMPLETED" and args.command == "run" and runtime.allowed_operations:
        prepared = runtime.prepare(row["id"], row["result"]["plan_fingerprint"])
        value["errors"].extend(prepared["errors"])
        for command in prepared["commands"]:
            try:
                value["execution"].append(runtime.execute(command["id"]))
            except Exception as error:
                value["errors"].append({"command_id": command["id"], "type": type(error).__name__, "code": str(error)})
                value["execution"].append(runtime.status(command_id=command["id"]))
    return value


def main(argv=None, *, host_only=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("THREADPLAN_CONFIG"),
                        help="Trusted user/host configuration; never a model-generated grant")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("policy", help="Read the shared behavior contract without a model or connection")
    setup = sub.add_parser("configure", help="Create a new external read-only configuration")
    setup.add_argument("--user-id", required=True, help="Real original-message identity of this user")
    setup.add_argument("--calendar-id", required=True, help="User-selected canonical Feishu calendar ID")
    setup.add_argument("--timezone", required=True, help="User's IANA timezone")
    setup.add_argument("--state-directory", default="threadplan-state")
    if not host_only:
        for name in ("analyze", "run"):
            analyze = sub.add_parser(name, help="Independent model analysis; run also applies trusted preauthorization")
            analyze.add_argument("--input", required=True)
            analyze.add_argument("--provider", choices=("openrouter", "codex"), required=True)
            analyze.add_argument("--codex-model", default="gpt-6.1-sol")
            analyze.add_argument("--retry", action="store_true")
    health = sub.add_parser("doctor")
    health.add_argument("--check-connection", action="store_true", help="Read the configured calendar; no writes")
    opened = sub.add_parser("open")
    opened.add_argument("--input", required=True, help="Original v0.3 messages JSON file, or - for stdin")
    read = sub.add_parser("read")
    read.add_argument("--request", required=True)
    read.add_argument("--activity", help="Semantic activity_ref for independent read-failure handling")
    read.add_argument("--query", required=True, help="Query JSON file, or - for stdin")
    propose = sub.add_parser("propose")
    propose.add_argument("--request", required=True)
    propose.add_argument("--input", required=True, help="Agent decision JSON file, or - for stdin")
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--request", required=True)
    prepare.add_argument("--fingerprint", required=True)
    for command in ("execute", "recover"):
        sub.add_parser(command).add_argument("--command-id", required=True)
    status = sub.add_parser("status")
    target = status.add_mutually_exclusive_group(required=True)
    target.add_argument("--request")
    target.add_argument("--command-id")
    args = parser.parse_args(argv)
    try:
        if args.command == "policy":
            from threadplan.protocol import DECISION_POLICY
            value, code = {"policy": DECISION_POLICY}, 0
        elif args.command == "configure":
            value, code = configure(args), 0
        elif args.command in {"analyze", "run"}:
            value = asyncio.run(independent(args))
            code = 0 if value["analysis"]["state"] == "COMPLETED" and not value.get("errors") else 1
        elif args.command == "doctor":
            value = doctor(args.config, args.check_connection)
            code = 0 if value["ready"] else 2
        else:
            runtime = session(args.config)
            if args.command == "open":
                value = runtime.open(load_json(args.input))
            elif args.command == "read":
                value = asyncio.run(runtime.read(args.request, load_json(args.query), activity_ref=args.activity))
            elif args.command == "propose":
                value = runtime.propose(args.request, load_json(args.input))
            elif args.command == "prepare":
                value = runtime.prepare(args.request, args.fingerprint)
            elif args.command == "execute":
                value = runtime.execute(args.command_id)
            elif args.command == "recover":
                value = runtime.recover(args.command_id)
            else:
                value = runtime.status(request_key=args.request, command_id=args.command_id)
            code = 0
        print(json.dumps({"ok": code == 0, "result": value}, ensure_ascii=False, allow_nan=False))
        return code
    except Exception as error:
        print(json.dumps({"ok": False, "error": {"type": type(error).__name__, "code": str(error)},
                          "observed_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
