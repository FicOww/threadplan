"""Release artifact checks in a relocated temporary directory, no service I/O."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from scripts.build_skill import build


class SkillPackageChecks(unittest.TestCase):
    def test_relocated_bundle_imports_runs_cli_and_has_only_manifested_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            one, two = directory / "one.zip", directory / "two.zip"
            first, second = build(one), build(two)
            self.assertEqual(first["sha256"], second["sha256"])
            with zipfile.ZipFile(one) as archive:
                archive.extractall(directory / "installed")
            skill = directory / "installed" / "threadplan"
            manifest = json.loads((skill / "manifest.json").read_text(encoding="utf-8"))
            actual = {path.relative_to(skill).as_posix() for path in skill.rglob("*") if path.is_file()}
            self.assertEqual(actual, set(manifest["files"]) | {"manifest.json"})
            self.assertEqual((skill / "references/decision-policy.md").read_bytes(),
                             (skill / "scripts/runtime/threadplan/decision_policy.md").read_bytes())
            self.assertFalse((skill / "scripts/runtime/threadplan_host/planning.py").exists())
            self.assertFalse((skill / "scripts/runtime/threadplan/planning.py").exists())
            self.assertFalse((skill / "scripts/runtime/threadplan_host/model_errors.py").exists())
            for name, expected in manifest["files"].items():
                self.assertEqual(hashlib.sha256((skill / name).read_bytes()).hexdigest(), expected)
                self.assertFalse(any(part in {"data", "runs", "evals", ".git", ".env", "__pycache__"}
                                     for part in Path(name).parts))
                self.assertNotIn(Path(name).name, {"codex_transport.py", "openrouter_transport.py", "model_config.py"})
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            environment.pop("THREADPLAN_CONFIG", None)
            tool = skill / "scripts" / "threadplan_tool.py"
            result = subprocess.run([sys.executable, "-X", "utf8", str(tool), "--help"],
                                    cwd=directory, env=environment, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("propose", result.stdout)
            missing = subprocess.run([sys.executable, "-X", "utf8", str(tool), "doctor"],
                                     cwd=directory, env=environment, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(missing.returncode, 2, missing.stderr)
            report = json.loads(missing.stdout)
            self.assertFalse(report["ok"])
            self.assertFalse(report["result"]["configuration_valid"])
            self.assertIn("configuration_required", report["result"]["configuration_error"])
            config = {"version":"1", "context":{"user_id":"constructed-user", "default_timezone":"Asia/Shanghai",
                "connection_ref":"c", "scope_ref":"s"}, "calendar_id":"constructed-test-calendar",
                "state_directory":"state", "execution_policy":{"mode":"read_only"}}
            config_path = directory / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            valid = subprocess.run([sys.executable, "-B", "-X", "utf8", str(tool), "--config", str(config_path), "doctor"],
                cwd=directory, env=environment, capture_output=True, text=True, encoding="utf-8")
            health = json.loads(valid.stdout)["result"]
            self.assertTrue(health["configuration_valid"], health)
            self.assertFalse((directory / "state").exists())
            config["state_directory"] = str(skill / "state")
            config_path.write_text(json.dumps(config), encoding="utf-8")
            unsafe = subprocess.run([sys.executable, "-B", "-X", "utf8", str(tool), "--config", str(config_path), "doctor"],
                cwd=directory, env=environment, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(unsafe.returncode, 2)
            self.assertIn("outside_skill_package", json.loads(unsafe.stdout)["result"]["configuration_error"])
            api = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c",
                'import sys; sys.path.insert(0, sys.argv[1]); from threadplan_host import *; import threadplan_host; '
                'print(",".join(threadplan_host.__all__))', str(skill / "scripts/runtime")],
                cwd=directory, env=environment, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(api.returncode, 0, api.stderr)
            self.assertIn("AgentSession", api.stdout)
            self.assertNotIn("ConversationPlanner", api.stdout)


if __name__ == "__main__":
    unittest.main()
