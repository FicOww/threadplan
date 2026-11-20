"""Business CLI through the real session/executor with constructed model/provider."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from threadplan import UpstreamContext
from threadplan import Proposal
from threadplan.adapters.feishu import FeishuError
from threadplan_host.agent import AgentSession
from threadplan_host import cli, PlanningStore
from threadplan_host.feishu import FeishuHost
from test_feishu_host import FakeCalendar
from test_planning_execution_v3 import SnapshotReader
from test_planning_v3 import ScriptedModel, decision, read_request, request


class CliChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.context = UpstreamContext("me", "Asia/Shanghai", "c", "s")
        self.calendar = FakeCalendar()
        self.backend = FeishuHost(self.calendar, self.directory / "provider.sqlite")
        self.runtime = AgentSession(PlanningStore(self.directory / "agent.sqlite"), context=self.context,
            calendar_reader=SnapshotReader(self.backend), backend=self.backend)
        self.input = self.directory / "input.json"
        self.input.write_text(json.dumps(request()), encoding="utf-8")

    def invoke(self, command):
        output = io.StringIO()
        model = ScriptedModel(read_request(), decision("CREATE"))
        with patch.object(cli, "session", return_value=self.runtime), \
                patch.object(cli, "configuration", return_value=({}, self.context, self.directory, [])), \
                patch("threadplan.codex_transport.CodexJSONSubscription", return_value=model), redirect_stdout(output):
            code = cli.main([command, "--provider", "codex", "--input", str(self.input)])
        return code, json.loads(output.getvalue())

    def test_analyze_and_read_only_run_return_plan_without_calendar_write(self):
        code, result = self.invoke("analyze")
        self.assertEqual(code, 0, result)
        self.assertEqual(len(result["result"]["analysis"]["plan"]["operations"]), 1)
        self.assertFalse(self.calendar.raw)
        code, result = self.invoke("run")
        self.assertEqual(code, 0, result)
        self.assertFalse(result["result"]["execution"])
        self.assertFalse(self.calendar.raw)

    def test_preauthorized_run_executes_reads_back_and_reuses_receipt(self):
        self.runtime.allowed_operations = frozenset({"CREATE"})
        code, result = self.invoke("run")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["result"]["execution"][0]["state"], "SUCCEEDED")
        self.assertEqual(len(self.calendar.raw), 1)
        code, result = self.invoke("run")
        self.assertEqual(code, 0, result)
        self.assertTrue(result["result"]["execution"][0]["already_applied"])
        self.assertEqual(len(self.calendar.raw), 1)

    def test_one_write_rejection_does_not_block_an_independent_command(self):
        identifier = self.backend.execute(Proposal("CREATE", "confirmed", ("seed",), {
            "title":"old", "start_at":"2026-11-11T14:00:00+08:00", "end_at":"2026-11-11T14:30:00+08:00"}),
            "seed", {}, approved=True)["event_id"]
        self.calendar.raw[identifier]["summary"] = "unmarked event"
        self.calendar.raw[identifier]["reminders"] = [{"minutes":10}]
        self.runtime.allowed_operations = frozenset({"CREATE", "UPDATE"})
        final = decision()
        final["items"].append(decision("CREATE")["items"][0])
        model = ScriptedModel(read_request(), final)
        output = io.StringIO()
        with patch.object(cli, "session", return_value=self.runtime), \
                patch.object(cli, "configuration", return_value=({}, self.context, self.directory, [])), \
                patch("threadplan.codex_transport.CodexJSONSubscription", return_value=model), redirect_stdout(output):
            code = cli.main(["run", "--provider", "codex", "--input", str(self.input)])
        result = json.loads(output.getvalue())["result"]
        self.assertEqual(code, 1)
        self.assertEqual([r["state"] for r in result["execution"]], ["FAILED_BEFORE_WRITE", "SUCCEEDED"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(self.calendar.raw[identifier]["summary"], "unmarked event")
        self.assertEqual(len(self.calendar.raw), 2)


class ConfigurationChecks(unittest.TestCase):
    def test_actual_configuration_loads_and_resolves_external_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {"version":"1", "context":{"user_id":"constructed-user", "default_timezone":"Asia/Shanghai",
                "connection_ref":"c", "scope_ref":"s"}, "calendar_id":"constructed-test-calendar",
                "state_directory":"state", "execution_policy":{"mode":"read_only"}}
            path = root / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            data, context, directory, allowed = cli.configuration(path)
            self.assertEqual(directory, (root / "state").resolve())
            self.assertEqual(context.default_duration_minutes, 30)
            self.assertEqual(allowed, [])
            self.assertFalse(directory.exists(), "Loading configuration must not create a ledger")
            with patch.object(cli, "discover_cli", return_value="constructed-native.exe"):
                self.assertTrue(cli.doctor(path)["configuration_valid"])
            config["state_directory"] = str(Path(cli.__file__).resolve().parents[1] / "skills/threadplan/state")
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside_skill_package"):
                cli.configuration(path)

    def test_connection_failure_does_not_mislabel_valid_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps({"version":"1", "context":{"user_id":"constructed-user",
                "default_timezone":"Asia/Shanghai", "connection_ref":"c", "scope_ref":"s"},
                "calendar_id":"constructed-test", "state_directory":"state",
                "execution_policy":{"mode":"read_only"}}),encoding="utf-8")
            with patch.object(cli, "discover_cli", return_value="constructed-native.exe"), \
                    patch.object(cli, "FeishuCalendar") as calendar:
                calendar.return_value.get_calendar.side_effect = FeishuError("Authentication required",
                    details={"type":"authentication","subtype":"token_missing","access_token":"must-not-appear"})
                health = cli.doctor(path, check_connection=True)
            self.assertTrue(health["configuration_valid"])
            self.assertFalse(health["ready"])
            self.assertEqual(health["connection_error"]["subtype"], "token_missing")
            self.assertNotIn("must-not-appear", json.dumps(health))
