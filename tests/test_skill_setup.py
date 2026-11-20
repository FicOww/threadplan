"""Managed Skill setup checks with a constructed Feishu transport; no service I/O."""

from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from threadplan.adapters.feishu import FeishuError
from threadplan_host import cli, skill_setup


class SetupChecks(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addCleanup(patch.stopall)
        patch.object(Path, "home", return_value=self.root).start()
        self.account = {"profile":"fixture-profile", "open_id":"ou_fixture"}
        self.identity = patch.object(skill_setup, "identity", return_value=self.account).start()
        self.preflight = patch.object(skill_setup, "preflight", return_value=(self.account, [])).start()
        self.transport = patch.object(skill_setup, "LarkCLI").start()
        self.transport.return_value.invoke.side_effect = self.provider
        self.provider_calls = []
        self.created = None
        self.event = None
        self.patch_bodies = []
        self.args = Namespace(timezone="UTC", recover_calendar_id=None)

    def provider(self, resource, method, *, params=None, data=None, approved=False):
        self.assertTrue(approved)
        self.provider_calls.append((resource, method))
        if (resource, method) == ("calendars", "create"):
            self.created = dict(data, calendar_id="dedicated-id", role="owner", type="shared")
            return {"calendar":{"calendar_id":self.created["calendar_id"]}}
        if (resource, method) == ("calendars", "get"):
            self.assertEqual(params, {"calendar_id":"dedicated-id"})
            return self.created
        if resource == "events":
            self.assertEqual(params["calendar_id"], "dedicated-id")
            if method == "instance_view":
                return {"items":[self.event]} if self.event else {}
            if method == "create":
                self.assertIsNone(self.event)
                self.event = dict(data, event_id="fixture-event", status="confirmed")
                self.event["app_link"] = "https://applink.feishu.cn/client/calendar/event/detail?key=fixture"
                return {"event":self.event}
            if method == "get":
                self.assertEqual(params["event_id"], "fixture-event")
                return {"event":deepcopy(self.event)}
            if method == "patch":
                self.assertEqual(params["event_id"], "fixture-event")
                self.patch_bodies.append(deepcopy(data))
                self.event.update(data)
                return {"event":deepcopy(self.event)}
        self.fail((resource, method))

    def initialize(self):
        return skill_setup.initialize(self.args, load_json=cli.load_json, validate_path=cli.external_state)

    def command(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(args, managed_skill=True)
        return code, json.loads(output.getvalue())

    def test_one_calendar_and_no_setup_calls_for_following_open(self):
        code, answer = self.command("open", "--input", "missing.json")
        self.assertEqual((code, answer["error"]["code"]), (1, "skill_setup_required"))
        self.preflight.assert_not_called()
        code, answer = self.command("setup-check")
        self.assertEqual((code, answer["result"]["state"]), (0, "READY_TO_INITIALIZE"))
        code, answer = self.command("initialize", "--timezone", "UTC", "--allow-event-create")
        self.assertEqual(code, 0, answer)
        result = answer["result"]
        self.assertEqual(result["calendar_id"], "dedicated-id")
        self.assertEqual(self.created["summary"], "ThreadPlan")
        self.assertEqual(self.created["permissions"], "private")
        self.provider_calls.clear()
        self.preflight.reset_mock()
        self.identity.reset_mock()
        config = skill_setup.binding_path()
        data, context, _, allowed = cli.configuration(config, managed_skill=True)
        self.assertIsNone(context)
        self.assertNotIn("context", data)
        self.assertNotIn("user_id", cli.load_json(skill_setup.paths()[1]))
        self.assertEqual((data["version"], data["feishu_identity"], allowed), ("3", self.account, ["CREATE"]))
        source = self.root / "input.json"
        source.write_text(json.dumps({"schema_version":"0.3", "request_id":"new-chat",
            "source":{"kind":"personal_note", "provider":"fixture"},
            "messages":[{"id":"original-note", "sender_id":"original-source-user",
                         "sent_at":datetime.now(timezone.utc).isoformat(), "text":"Constructed note"}]}), encoding="utf-8")
        code, missing = self.command("open", "--input", str(source))
        self.assertEqual((code, missing["error"]["code"]), (1, "skill_source_identity_required"))
        code, opened = self.command("open", "--input", str(source), "--source-user-id", "original-source-user")
        self.assertEqual(code, 0, opened)
        self.assertEqual(opened["result"]["time_policy"]["source"], "PRODUCT_DEFAULT")
        code, resumed = self.command("status", "--request", opened["result"]["request_key"])
        self.assertEqual(code, 0, resumed)
        self.assertEqual(resumed["result"]["user"]["id"], "original-source-user")
        self.assertEqual(self.command("setup-check")[1]["result"]["state"], "BOUND")
        with self.assertRaisesRegex(ValueError, "skill_already_bound"):
            self.initialize()
        self.preflight.assert_not_called()
        self.identity.assert_not_called()
        self.assertEqual(self.provider_calls, [])

    def test_missing_scopes_do_not_create_pending_record(self):
        self.preflight.return_value = (self.account, ["calendar:calendar:create"])
        with self.assertRaisesRegex(ValueError, "missing_scopes"):
            self.initialize()
        self.assertFalse(skill_setup.paths()[1].exists())
        self.assertEqual(self.provider_calls, [])

    def test_legacy_bound_calendar_still_uses_its_configured_source_identity(self):
        config, _ = skill_setup.paths()
        config.parent.mkdir(parents=True)
        cli_config = {"version":"2", "context":{"user_id":"original-source-user", "default_timezone":"UTC",
            "connection_ref":"existing-connection", "scope_ref":"dedicated-id"}, "calendar_id":"dedicated-id",
            "state_directory":"threadplan-state", "feishu_identity":self.account,
            "execution_policy":{"mode":"preauthorized", "allowed_operations":["CREATE"]}}
        config.write_text(json.dumps(cli_config), encoding="utf-8")
        code, mismatch = self.command("open", "--source-user-id", "other-source", "--input", "unused.json")
        self.assertEqual((code, mismatch["error"]["code"]), (1, "skill_source_identity_mismatch"))
        with self.assertRaisesRegex(ValueError, "skill_already_bound"):
            self.initialize()
        self.assertEqual(cli.load_json(config), cli_config)
        self.preflight.assert_not_called()
        self.assertEqual(self.provider_calls, [])

    def test_corrupt_binding_is_not_missing(self):
        config, _ = skill_setup.paths()
        config.parent.mkdir(parents=True)
        config.write_text("{", encoding="utf-8")
        self.assertEqual(self.command("setup-check")[0], 1)
        with self.assertRaisesRegex(ValueError, "skill_already_bound"):
            self.initialize()
        self.preflight.assert_not_called()

    def test_unknown_creation_is_not_resent_and_recovery_checks_marker(self):
        def lost_response(resource, method, **kwargs):
            result = self.provider(resource, method, **kwargs)
            if method == "create":
                raise FeishuError("response lost")
            return result

        self.transport.return_value.invoke.side_effect = lost_response
        with self.assertRaisesRegex(FeishuError, "response lost"):
            self.initialize()
        with self.assertRaisesRegex(ValueError, "skill_setup_pending"):
            skill_setup.binding_path()
        with self.assertRaisesRegex(ValueError, "do_not_retry"):
            self.initialize()
        self.args.recover_calendar_id = "dedicated-id"
        marker = self.created["description"]
        self.created["description"] = "Unrelated similarly named calendar"
        with self.assertRaisesRegex(ValueError, "not_created_by_this_initialization"):
            self.initialize()
        self.created["description"] = marker
        self.assertEqual(self.initialize()["state"], "BOUND")
        self.assertEqual(self.provider_calls.count(("calendars", "create")), 1)

    def test_bound_calendar_rejects_primary_and_account_changes(self):
        self.initialize()
        calendar = skill_setup.SkillCalendar("dedicated-id", self.account)
        self.created["type"] = "primary"
        with self.assertRaisesRegex(FeishuError, "bound_private_calendar"):
            calendar.get_calendar(approved=True)
        self.identity.return_value = {"profile":"fixture-profile", "open_id":"ou_other"}
        self.provider_calls.clear()
        with self.assertRaisesRegex(FeishuError, "account_changed"):
            calendar.prepare_write(None, None, "fixture", approved=True)
        self.assertEqual(self.provider_calls, [])

    def test_scheduled_event_writes_only_to_bound_calendar_and_reads_back(self):
        self.initialize()
        self.provider_calls.clear()
        self.preflight.reset_mock()
        self.identity.reset_mock()

        def run(*args, payload=None, flag="--input"):
            if payload is not None:
                path = self.root / "input.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                args = (*args, flag, str(path))
            code, answer = self.command(*args)
            self.assertEqual(code, 0, answer)
            return answer["result"]

        request = run("open", "--source-user-id", "original-source-user", payload={
            "schema_version":"0.3", "request_id":"fixture-request",
            "source":{"kind":"personal_note", "provider":"fixture"},
            "messages":[{"id":"fixture-note", "sender_id":"original-source-user",
                         "sent_at":datetime.now(timezone.utc).isoformat(), "text":"Constructed note"}]})
        key = request["request_key"]
        run("read", "--request", key, "--activity", "shift", flag="--query", payload={"kind":"find_events",
            "range":{"start_at":"2026-11-20T00:00:00+00:00", "end_at":"2026-11-21T00:00:00+00:00"}})
        plan = run("propose", "--request", key, payload={"items":[{"decision":"PROPOSE", "activity_ref":"shift",
            "evidence_message_ids":["fixture-note"], "reason":"Constructed contract",
            "operation":{"kind":"CREATE", "fields":{"title":"User event", "start_at":"2026-11-20T17:30:00+00:00",
                                                    "end_at":"2026-11-20T23:00:00+00:00"}}}]})["plan"]
        prepared = run("prepare", "--request", key, "--fingerprint", plan["plan_fingerprint"])
        self.assertFalse(prepared["errors"])
        command_id = prepared["commands"][0]["id"]
        self.assertEqual(run("execute", "--command-id", command_id)["state"], "SUCCEEDED")
        self.assertTrue(run("execute", "--command-id", command_id)["already_applied"])
        self.assertEqual(self.provider_calls.count(("events", "create")), 1)
        self.assertIn(("events", "get"), self.provider_calls)
        self.assertNotIn(("calendars", "create"), self.provider_calls)
        self.preflight.assert_not_called()
        self.identity.assert_called_once()

    def direct_call(self, *args, payload=None):
        if payload is not None:
            with patch("sys.stdin", io.StringIO(json.dumps(payload, ensure_ascii=False))):
                return self.command(*args, "--input", "-")
        return self.command(*args)

    def confirm(self, identifier):
        answer, terminal = io.StringIO("确认更新\n"), io.StringIO()
        with patch("sys.stdin", answer), patch.object(answer, "isatty", return_value=True), \
                redirect_stderr(terminal), patch.object(terminal, "isatty", return_value=True):
            return self.command("apply-supplement", "--command-id", identifier)

    def test_direct_entry_and_one_time_supplement_use_original_event(self):
        self.initialize()
        data = {"title":"样品交接", "start_at":"2026-11-28T10:00:00+08:00",
                "end_at":"2026-11-28T10:30:00+08:00", "timezone":"Asia/Shanghai",
                "location":"访客中心", "description":"带签收单"}
        code, response = self.direct_call("record", payload=data)
        self.assertEqual(code, 0, response)
        recorded = response["result"]
        self.assertEqual(recorded["state"], "SUCCEEDED")
        self.assertEqual(recorded["event"]["location"], "访客中心")
        self.assertIsNone(recorded["event"]["location_address"])
        self.assertEqual(recorded["event"]["reminder_minutes"], [])
        self.assertTrue(recorded["event"]["link"])
        call_count = len(self.provider_calls)
        self.assertTrue(self.direct_call("record", payload=data)[1]["result"]["reused"])
        self.assertEqual(len(self.provider_calls), call_count)
        reference = recorded["receipt_ref"]

        code, response = self.direct_call("prepare-supplement", "--receipt", reference,
                                          payload={"start_at":"2026-11-29T10:00:00+08:00"})
        self.assertEqual((code, response["error"]["code"]), (1, "unsupported_supplement_fields"))
        code, response = self.direct_call("prepare-supplement", "--receipt", reference,
                                          payload={"reminder_minutes":[30]})
        self.assertEqual(code, 0, response)
        preview = response["result"]
        self.assertEqual(preview["state"], "APPROVAL_REQUIRED")
        self.assertFalse(self.patch_bodies)
        identifier = preview["command_id"]
        code, denied = self.command("apply-supplement", "--command-id", identifier)
        self.assertEqual((code, denied["error"]["code"]), (1, "supplement_requires_user_in_terminal"))
        self.assertFalse(self.patch_bodies)
        old_start = self.event["start_time"].copy()
        code, result = self.confirm(identifier)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["result"]["state"], "SUCCEEDED")
        self.assertEqual(result["result"]["event"]["reminder_minutes"], [30])
        self.assertEqual(self.patch_bodies, [{"need_notification":False, "reminders":[{"minutes":30}]}])
        self.assertEqual(self.event["start_time"], old_start)
        self.assertEqual(self.event["location"], {"name":"访客中心"})
        self.assertEqual(self.event["description"], data["description"])
        self.assertEqual(self.provider_calls.count(("events", "create")), 1)
        self.assertEqual(self.provider_calls.count(("calendars", "create")), 1)

        code, response = self.direct_call("prepare-supplement", "--receipt", result["result"]["receipt_ref"],
                                          payload={"append_description":"带工牌"})
        self.assertEqual(code, 0, response)
        stale = response["result"]["command_id"]
        self.event["description"] = "用户已另行修改"
        code, response = self.confirm(stale)
        self.assertEqual((code, response["result"]["state"]), (1, "FAILED_BEFORE_WRITE"))
        self.assertEqual(len(self.patch_bodies), 1)

        code, response = self.direct_call("prepare-supplement", "--receipt", reference,
                                          payload={"append_description":"带工牌"})
        self.assertEqual(code, 0, response)
        identifier = response["result"]["command_id"]
        original = self.provider

        def lose_update_reply(resource, method, **kwargs):
            result = original(resource, method, **kwargs)
            if (resource, method) == ("events", "patch"):
                raise FeishuError("lost update response")
            return result

        self.transport.return_value.invoke.side_effect = lose_update_reply
        self.assertEqual(self.confirm(identifier)[1]["result"]["state"], "OUTCOME_UNKNOWN")
        self.transport.return_value.invoke.side_effect = original
        recovered = self.command("direct-recover", "--command-id", identifier)[1]["result"]
        self.assertEqual(recovered["state"], "SUCCEEDED")
        self.assertFalse(recovered["provider_acknowledged"])
        self.assertEqual(recovered["event"]["description"], "用户已另行修改\n\n带工牌")
        self.assertEqual(len(self.patch_bodies), 2)

    def test_direct_create_does_not_adopt_existing_event_or_retry_unknown_write(self):
        self.initialize()
        fields = {"title":"简报", "start_at":"2026-11-20T17:30:00+08:00", "reminder_minutes":[15]}
        original = self.provider

        def lose_reply(resource, method, **kwargs):
            result = original(resource, method, **kwargs)
            if (resource, method) == ("events", "create"):
                raise FeishuError("lost write response")
            return result

        self.transport.return_value.invoke.side_effect = lose_reply
        code, response = self.direct_call("record", payload=fields)
        self.assertEqual((code, response["result"]["state"]), (1, "OUTCOME_UNKNOWN"))
        identifier = response["result"]["command_id"]
        self.assertEqual(self.event["reminders"], [{"minutes":15}])
        self.transport.return_value.invoke.side_effect = original
        count = len(self.provider_calls)
        self.assertTrue(self.direct_call("record", payload=fields)[1]["result"]["reused"])
        self.assertEqual(self.command("direct-recover", "--command-id", identifier)[1]["result"]["state"], "OUTCOME_UNKNOWN")
        self.assertEqual(len(self.provider_calls), count)
        code, duplicate = self.direct_call("record", payload=dict(fields, title="客户简报"))
        self.assertEqual((code, duplicate["result"]["state"]), (0, "POSSIBLE_DUPLICATE"))
        self.assertNotIn("receipt_ref", duplicate["result"])
        self.assertEqual(self.provider_calls.count(("events", "create")), 1)
