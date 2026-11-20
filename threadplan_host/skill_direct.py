"""Explicit personal submissions and narrow, one-time supplements for the Skill."""

from datetime import datetime, timedelta, timezone
import json
import sys
import unicodedata
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from threadplan.adapters.feishu import FeishuError, check_event_scope, event_record, reminder_minutes
from threadplan.codec import canonical_json, fingerprint
from threadplan.contracts import Proposal
from threadplan.timeops import add_minutes, instant, minutes
from .feishu import FeishuHost
from .skill_setup import CALENDAR_NAME, SkillCalendar


EXTRAS = {"location", "location_address", "description", "reminder_minutes"}
STATES = {"succeeded": "SUCCEEDED", "failed_before_write": "FAILED_BEFORE_WRITE",
          "write_acknowledged": "VERIFY_PENDING", "verify_pending": "VERIFY_PENDING",
          "in_progress": "IN_FLIGHT", "outcome_unknown": "OUTCOME_UNKNOWN"}


def text_field(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("invalid_direct_field:" + name)
    return value.strip()


def extra_fields(values):
    fields = {name: text_field(values[name], name, 4000 if name == "description" else 500)
              for name in EXTRAS - {"reminder_minutes"} if name in values}
    if "location_address" in fields and "location" not in fields:
        raise ValueError("location_address_requires_name")
    if "reminder_minutes" in values:
        reminders = values["reminder_minutes"]
        if (type(reminders) is not list or len(reminders) > 5
                or any(type(value) is not int or not 0 <= value <= 20160 for value in reminders)
                or len(reminders) != len(set(reminders))):
            raise ValueError("invalid_personal_reminder_minutes")
        fields["reminder_minutes"] = sorted(reminders)
    return fields


def event_time(value, zone=None):
    local = instant(value, zone)
    if local.microsecond:
        raise ValueError("direct_time_requires_whole_seconds")
    return local.astimezone(timezone.utc)


def create_fields(values):
    if type(values) is not dict or set(values) - EXTRAS - {
            "title", "start_at", "end_at", "duration_minutes", "timezone"}:
        raise ValueError("unsupported_direct_create_fields")
    if {"end_at", "duration_minutes"} <= values.keys():
        raise ValueError("provide_end_or_duration_not_both")
    zone = values.get("timezone")
    start = event_time(values.get("start_at"), zone)
    if "end_at" in values:
        end = event_time(values["end_at"], zone)
    else:
        end = event_time(add_minutes(start.isoformat(), minutes(values.get("duration_minutes", 30))).isoformat())
    if not timedelta(0) < end - start <= timedelta(days=31):
        raise ValueError("invalid_direct_event_interval")
    return {"title": text_field(values.get("title"), "title", 500), "start_at": start.isoformat(),
            "end_at": end.isoformat(), "reminder_minutes": [], **extra_fields(values)}


def title_key(title):
    return "".join(character for character in unicodedata.normalize("NFKC", title).casefold()
                   if character.isalnum())


def event_link(raw):
    link = raw.get("app_link")
    if not isinstance(link, str):
        return None
    try:
        parsed = urlsplit(link)
    except ValueError:
        return None
    if (parsed.scheme, parsed.netloc, parsed.path) == (
            "https", "applink.feishu.cn", "/client/calendar/event/detail"):
        return link
    return None


def display_event(raw, zone):
    fields = event_record(raw)["fields"]
    return {"title": fields.get("title"),
            **{name: instant(fields[name]).astimezone(zone).isoformat() if fields.get(name) else None
               for name in ("start_at", "end_at")},
            "location": fields.get("location"),
            "location_address": (raw.get("location") or {}).get("address"),
            "description": fields.get("description"), "reminder_minutes": reminder_minutes(raw),
            "link": event_link(raw)}


class DirectCalendar(SkillCalendar):
    direct_entry = True


class DirectSkill:
    def __init__(self, data, directory, allowed):
        self.calendar = DirectCalendar(data["calendar_id"], data["feishu_identity"])
        self.host = FeishuHost(self.calendar, directory / "provider.sqlite")
        self.zone = ZoneInfo(data.get("timezone") or data["context"]["default_timezone"])
        self.allowed = "CREATE" in allowed
        self.scope = {name: data[name] for name in ("calendar_id", "feishu_identity")}
        with self.host.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS skill_direct ("
                       "id TEXT PRIMARY KEY, scope TEXT NOT NULL, plan TEXT NOT NULL)")

    def save(self, identifier, plan):
        with self.host.connect() as db:
            db.execute("INSERT OR IGNORE INTO skill_direct VALUES(?,?,?)",
                       (identifier, canonical_json(self.scope), canonical_json(plan)))
            frozen = self.load(identifier, db)
        if frozen != plan:
            raise ValueError("direct_plan_conflict_use_original_receipt")
        return frozen

    def load(self, identifier, db=None, *, required=True):
        if db is None:
            with self.host.connect() as connection:
                return self.load(identifier, connection, required=required)
        row = db.execute("SELECT scope,plan FROM skill_direct WHERE id=?", (identifier,)).fetchone()
        if row is None and not required:
            return None
        if row is None or json.loads(row["scope"]) != self.scope:
            raise ValueError("unknown_direct_receipt")
        return json.loads(row["plan"])

    def status(self, identifier, *, recover=False):
        plan = self.load(identifier)
        receipt = self.host.receipt(identifier)
        recovered = None
        if recover and receipt and STATES[receipt["state"]] in {"VERIFY_PENDING", "OUTCOME_UNKNOWN", "IN_FLIGHT"}:
            try:
                recovered = self.host.recover(identifier, approved=True)
            except FeishuError:
                pass
            receipt = self.host.receipt(identifier)
        report = {"command_id": identifier, "state": STATES[receipt["state"]] if receipt else "PREPARED",
                  "calendar": CALENDAR_NAME}
        if receipt and receipt["error"]:
            errors = receipt["error"]
            report["error"] = {key: errors[key] for key in ("type", "message") if key in errors}
            details = errors.get("details") or {}
            for field in ("subtype", "missing_scopes", "action", "risk"):
                if field in details:
                    report["error"][field] = details[field]
        if receipt and report["state"] == "SUCCEEDED":
            result = receipt["result"]
            report.update(event=display_event(result["provider_event"], self.zone),
                          provider_acknowledged=result["provider_acknowledged"], receipt_ref=identifier)
        if recovered:
            report["recovered_verification"] = True
        if plan.get("overlaps"):
            report["overlaps"] = plan["overlaps"]
        if plan.get("duration_defaulted"):
            report["duration_defaulted_minutes"] = 30
        return report

    def _execute(self, identifier, plan):
        if not self.allowed:
            raise ValueError("trusted_execution_policy_required")
        prior = self.host.receipt(identifier)
        if prior is not None and prior["state"] != "failed_before_write":
            return dict(self.status(identifier), reused=True)
        baseline = plan.get("baseline")
        target = baseline["event_id"] if baseline is not None else None
        proposal = Proposal("UPDATE" if target else "CREATE", "confirmed", (), plan["fields"], target)
        try:
            result = self.host.execute(proposal, identifier, {target: baseline} if target else {}, approved=True)
        except FeishuError:
            if self.host.receipt(identifier) is None:
                raise
            return self.status(identifier)
        return dict(self.status(identifier), reused=result.get("already_applied", False))

    def record(self, values):
        if not self.allowed:
            raise ValueError("trusted_execution_policy_required")
        fields = create_fields(values)
        window = {name: fields[name] for name in ("start_at", "end_at")}
        name_key = title_key(fields["title"])
        identifier = "record-" + fingerprint({**window, "title": name_key, "scope": self.scope})
        plan = self.load(identifier, required=False)
        if plan is not None:
            if plan["fields"] != fields:
                raise ValueError("direct_plan_conflict_use_original_receipt")
            receipt = self.host.receipt(identifier)
            if receipt and receipt["state"] != "failed_before_write":
                return dict(self.status(identifier), reused=True)
        view = self.calendar.find_events(window, approved=True)
        if any(view.get(name) for name in ("has_more", "page_token", "next_page_token")):
            raise ValueError("direct_duplicate_check_incomplete")
        overlaps = []
        for raw in view.get("items") or []:
            if raw.get("status") == "cancelled":
                continue
            event = display_event(raw, self.zone)
            old_name = title_key(event["title"] or "")
            same_venue = bool(fields.get("location") and event.get("location") == fields["location"])
            start, end = event["start_at"], event["end_at"]
            if start and end and (instant(end) <= instant(window["start_at"])
                                  or instant(start) >= instant(window["end_at"])):
                continue
            same_slot = bool(start and end and instant(start) == instant(window["start_at"])
                             and instant(end) == instant(window["end_at"]))
            if ((old_name and name_key and (old_name in name_key or name_key in old_name))
                    or (same_slot and (same_venue or not old_name))):
                return {"state": "POSSIBLE_DUPLICATE", "calendar": CALENDAR_NAME, "event": event}
            overlaps.append({name: event[name] for name in ("title", "start_at", "end_at")})
        plan = plan or self.save(identifier, {"fields": fields, "overlaps": overlaps,
            "duration_defaulted": not {"end_at", "duration_minutes"}.intersection(values)})
        return self._execute(identifier, plan)

    def prepare_supplement(self, reference, values):
        if not self.allowed:
            raise ValueError("trusted_execution_policy_required")
        if type(values) is not dict or not values or set(values) - EXTRAS - {"append_description"}:
            raise ValueError("unsupported_supplement_fields")
        self.load(reference)
        receipt = self.host.receipt(reference)
        if not receipt or receipt["state"] != "succeeded":
            raise ValueError("supplement_requires_verified_receipt")
        before = receipt["result"]["provider_event"]
        baseline = self.calendar.get_event(before["event_id"], approved=True)
        check_event_scope(baseline, allow_reminders=True)
        if baseline.get("status") != "confirmed" or any(
                baseline.get(name) != before.get(name) for name in ("summary", "start_time", "end_time")):
            raise ValueError("supplement_target_changed")
        current = display_event(baseline, self.zone)
        fields = extra_fields(values)
        if "append_description" in values:
            if "description" in fields:
                raise ValueError("cannot_replace_and_append_description")
            addition = text_field(values["append_description"], "append_description", 4000)
            previous = current.get("description")
            description = (previous + "\n\n" if previous else "") + addition
            if len(description) > 4000:
                raise ValueError("invalid_direct_field:description")
            fields["description"] = description
        changes = {name: value for name, value in fields.items() if name not in {"location", "location_address"}
                   and value != current[name]}
        if fields.get("location") and (fields["location"] != current["location"]
                or "location_address" in fields and fields["location_address"] != current["location_address"]):
            changes["location"] = fields["location"]
            if "location_address" in fields:
                changes["location_address"] = fields["location_address"]
        fields = changes
        if not fields:
            return {"state": "NO_CHANGE", "receipt_ref": reference, "event": current}
        expires = datetime.now(timezone.utc) + timedelta(minutes=30)
        identifier = "supplement-" + fingerprint({"fields": fields, "target": reference,
                                                  "baseline": baseline, "expires": expires.isoformat()})
        self.save(identifier, {"fields": fields, "baseline": baseline, "expires": expires.isoformat()})
        return {"state": "APPROVAL_REQUIRED", "calendar": CALENDAR_NAME, "command_id": identifier,
                "expires_at": expires.isoformat(), "event": current, "changes": fields}

    def apply_supplement(self, identifier):
        plan = self.load(identifier)
        if "baseline" not in plan:
            raise ValueError("command_is_not_a_supplement")
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            raise ValueError("supplement_requires_user_in_terminal")
        if datetime.now(timezone.utc) >= instant(plan["expires"]):
            raise ValueError("supplement_approval_expired")
        if self.host.receipt(identifier):
            return dict(self.status(identifier), reused=True)
        print(json.dumps({"calendar": CALENDAR_NAME, "event": display_event(plan["baseline"], self.zone),
                          "changes": plan["fields"]}, ensure_ascii=False, indent=2), file=sys.stderr)
        print("仅更新以上字段。请本人键入「确认更新」继续：", end="", file=sys.stderr, flush=True)
        if sys.stdin.readline().strip() != "确认更新":
            return {"command_id": identifier, "state": "NOT_APPROVED"}
        if datetime.now(timezone.utc) >= instant(plan["expires"]):
            raise ValueError("supplement_approval_expired")
        return self._execute(identifier, plan)
