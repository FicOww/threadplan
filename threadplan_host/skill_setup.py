"""Local binding and one-time setup for the installed Skill."""

import json
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from threadplan.adapters.feishu import FeishuCalendar, FeishuError, LarkCLI, _approved


CALENDAR_NAME = "ThreadPlan"
REQUIRED_SCOPES = {"calendar:calendar:create", "calendar:calendar:read",
                   "calendar:calendar.event:read", "calendar:calendar.event:create"}


def paths():
    root = Path.home() / ".config" / "threadplan"
    return root / "skill.json", root / "skill-initializing.json"


def present(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def binding_path():
    config, pending = paths()
    if not present(config):
        raise ValueError("skill_setup_pending" if present(pending) else "skill_setup_required")
    return config


def validate_timezone(timezone):
    if not isinstance(timezone, str) or not timezone.strip():
        raise ValueError("INVALID_DEFAULT_TIMEZONE")
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("INVALID_DEFAULT_TIMEZONE") from error


def local_cli(transport, *arguments):
    result = subprocess.run(transport.command(*arguments), capture_output=True, text=True,
                            encoding="utf-8", timeout=10, shell=False)
    if result.returncode:
        raise ValueError("skill_feishu_login_required")
    return json.loads(result.stdout)


def identity(transport):
    who = local_cli(transport, "whoami", "--as", "user")
    open_id = (who.get("onBehalfOf") or {}).get("openId")
    profile = who.get("profile")
    if (who.get("brand") != "feishu" or who.get("identity") != "user" or who.get("available") is not True
            or not isinstance(open_id, str) or not open_id.startswith("ou_")
            or not isinstance(profile, str) or not profile.strip()):
        raise ValueError("skill_feishu_login_required")
    return {"profile": profile, "open_id": open_id}


def preflight():
    account = identity(LarkCLI())
    status = local_cli(LarkCLI(profile=account["profile"]), "auth", "status", "--json")
    user = (status.get("identities") or {}).get("user") or {}
    if not user.get("available") or user.get("openId") != account["open_id"]:
        raise ValueError("skill_feishu_login_required")
    return account, sorted(REQUIRED_SCOPES - set((user.get("scope") or "").split()))


def setup_check():
    config, pending = paths()
    if present(config) or present(pending):
        return {"state": "BOUND" if present(config) else "PENDING"}
    account, missing = preflight()
    return {"state": "MISSING_SCOPES" if missing else "READY_TO_INITIALIZE",
            "feishu_identity": account, "missing_scopes": missing}


class SkillCalendar(FeishuCalendar):
    def __init__(self, calendar_id, account):
        self.account = account
        super().__init__(calendar_id, cli=LarkCLI(profile=account["profile"]))

    @staticmethod
    def _check_calendar(calendar, *, writable=False):
        FeishuCalendar._check_calendar(calendar, writable=writable)
        if (calendar.get("type"), calendar.get("permissions"), calendar.get("role")) != (
                "shared", "private", "owner"):
            raise FeishuError("skill_requires_bound_private_calendar_owned_by_user")

    def prepare_write(self, proposal, baseline, approval_key, *, approved=False, authorize_target=None):
        _approved(approved)
        if identity(self.cli) != self.account:
            raise FeishuError("skill_feishu_account_changed")
        return super().prepare_write(proposal, baseline, approval_key, approved=approved,
                                     authorize_target=authorize_target)


def write_json(path, value, *, exclusive=False):
    if exclusive:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
        return
    with NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, dir=path.parent) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def initialize(args, *, load_json, validate_path):
    config, pending_path = paths()
    if present(config):
        raise ValueError("skill_already_bound_repair_existing_binding")
    validate_path(config.resolve(), "threadplan-state")
    validate_timezone(args.timezone)
    if present(pending_path):
        pending = load_json(pending_path)
        account = identity(LarkCLI(profile=pending["account"]["profile"]))
        if (account, args.timezone) != (pending["account"], pending["timezone"]):
            raise ValueError("skill_setup_requires_original_account_and_settings")
        known_id = pending.get("calendar_id")
        if known_id and args.recover_calendar_id and args.recover_calendar_id != known_id:
            raise ValueError("skill_setup_recovery_id_mismatch")
        calendar_id = known_id or args.recover_calendar_id
        if not calendar_id:
            raise ValueError("skill_setup_outcome_unknown_do_not_retry_creation")
    else:
        if args.recover_calendar_id:
            raise ValueError("skill_setup_no_pending_creation")
        account, missing = preflight()
        if missing:
            raise ValueError("skill_feishu_missing_scopes:" + ",".join(missing))
        transport = LarkCLI(profile=account["profile"])
        if identity(transport) != account:
            raise ValueError("skill_feishu_account_changed")
        pending = {"account": account, "timezone": args.timezone,
                   "description": "ThreadPlan 专用日历 · " + uuid4().hex}
        config.parent.mkdir(parents=True, exist_ok=True)
        write_json(pending_path, pending, exclusive=True)
        response = transport.invoke("calendars", "create", data={"summary": CALENDAR_NAME,
            "permissions": "private", "description": pending["description"]}, approved=True)
        calendar_id = (response.get("calendar") or {}).get("calendar_id")
        if not isinstance(calendar_id, str) or not calendar_id.strip():
            raise ValueError("skill_setup_outcome_unknown_do_not_retry_creation")
        pending["calendar_id"] = calendar_id
        write_json(pending_path, pending)
    calendar = SkillCalendar(calendar_id, account).get_calendar(approved=True, writable=True)
    if (calendar.get("summary"), calendar.get("description")) != (CALENDAR_NAME, pending["description"]):
        raise ValueError("skill_setup_calendar_not_created_by_this_initialization")
    data = {"version":"3", "timezone":args.timezone, "calendar_id":calendar_id,
            "connection_ref":"feishu-" + pending["description"].split()[-1],
            "state_directory":"threadplan-state", "feishu_identity":account,
            "execution_policy":{"mode":"preauthorized", "allowed_operations":["CREATE"]}}
    write_json(config, data, exclusive=True)
    return {"state":"BOUND", "calendar_name":CALENDAR_NAME, "calendar_id":calendar_id,
            "execution_allowed_operations":["CREATE"]}
