"""Developer-only isolation. Never included in the distributable Skill."""

from dataclasses import replace
from pathlib import Path

from threadplan.adapters.feishu import (FeishuCalendar, FeishuError, LarkCLI, TEST_PREFIX,
                                       _approved, _managed_description, _text)
from threadplan_host.feishu import FeishuHost

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONNECTION = ROOT / "data" / "feishu_test" / "connection.json"
TEST_CALENDAR = "ThreadPlan Test"


def check_test_calendar(calendar):
    if (calendar.get("summary") != TEST_CALENDAR or calendar.get("type") != "shared"
            or calendar.get("permissions") != "private"
            or calendar.get("role") not in {"owner", "writer"}
            or calendar.get("is_deleted") or calendar.get("is_third_party")):
        raise FeishuError("Developer execution requires the private ThreadPlan Test calendar")


def open_test_host(*, db_path, path=DEFAULT_CONNECTION):
    backend = FeishuHost.from_connection(path=path, db_path=db_path)
    check_test_calendar(backend.calendar.get_calendar(approved=True))
    original = backend.calendar.prepare_write

    def guarded(proposal, baseline, approval_key, *, approved=False, authorize_target=None):
        _approved(approved)
        check_test_calendar(backend.calendar.get_calendar(approved=True))
        if proposal.operation == "CREATE":
            changes = dict(proposal.changes)
            changes["title"] = TEST_PREFIX + changes["title"]
            changes["description"] = _managed_description(changes.get("description"))
            proposal = replace(proposal, changes=changes)
        else:
            current = backend.calendar.get_event(proposal.target_event_id, approved=True)
            if not current.get("summary", "").startswith(TEST_PREFIX):
                raise FeishuError("Developer writes require a marked test event")
        return original(proposal, baseline, approval_key, approved=True, authorize_target=authorize_target)

    backend.calendar.prepare_write = guarded
    return backend


def create_private_test_calendar(*, cli=None, approved=False):
    _approved(approved)
    transport = cli or LarkCLI()
    response = transport.invoke("calendars", "create", data={
        "summary": TEST_CALENDAR, "permissions": "private",
        "description": "ThreadPlan dedicated integration test calendar; no attendee invitations."}, approved=True)
    calendar = response.get("calendar")
    if not isinstance(calendar, dict):
        raise FeishuError("Calendar creation returned no calendar; do not retry blindly")
    identifier = calendar.get("calendar_id")
    try:
        _text(identifier, "returned calendar_id")
        if not {"type", "summary", "permissions", "role"} <= calendar.keys():
            calendar = FeishuCalendar(identifier, cli=transport).get_calendar(approved=True)
        check_test_calendar(calendar)
    except FeishuError as error:
        error.details.update(created_calendar_id=identifier, created_calendar=response["calendar"],
                             outcome_known_created=True)
        raise
    return calendar
