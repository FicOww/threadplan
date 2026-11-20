"""Release artifact checks in a relocated temporary directory, no service I/O."""

import hashlib
from datetime import datetime, timezone
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
        with tempfile.TemporaryDirectory(prefix="技能 portable ") as temporary:
            directory = Path(temporary)
            one, two = directory / "one.zip", directory / "two.zip"
            first, second = build(one), build(two)
            self.assertEqual(first["sha256"], second["sha256"])
            with zipfile.ZipFile(one) as archive:
                archive.extractall(directory / "另一个宿主")
            skill = directory / "另一个宿主" / "threadplan"
            manifest = json.loads((skill / "manifest.json").read_text(encoding="utf-8"))
            actual = {path.relative_to(skill).as_posix() for path in skill.rglob("*") if path.is_file()}
            self.assertEqual(actual, set(manifest["files"]) | {"manifest.json"})
            self.assertEqual((skill / "references/decision-policy.md").read_bytes(),
                             (skill / "scripts/runtime/threadplan/decision_policy.md").read_bytes())
            self.assertFalse((skill / "scripts/runtime/threadplan_host/planning.py").exists())
            self.assertFalse((skill / "scripts/runtime/threadplan/planning.py").exists())
            self.assertFalse((skill / "scripts/runtime/threadplan_host/model_errors.py").exists())
            for name, expected in manifest["files"].items():
                payload = (skill / name).read_bytes()
                self.assertEqual(hashlib.sha256(payload).hexdigest(), expected)
                self.assertFalse(any(part in {"data", "runs", "evals", ".git", ".env", "__pycache__"}
                                     for part in Path(name).parts))
                self.assertNotIn(Path(name).name, {"codex_transport.py", "openrouter_transport.py", "model_config.py"})
                for private_marker in (b"C:/Users/", b"C:\\Users\\", b"/home/", b"data/feishu_test"):
                    self.assertNotIn(private_marker, payload, name)
            for document in ("SKILL.md", "references/setup.md"):
                self.assertNotIn("ThreadPlan Test", (skill / document).read_text(encoding="utf-8"))
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            environment.update(HOME=str(directory), USERPROFILE=str(directory),
                               THREADPLAN_CONFIG=str(directory / "legacy-ignored.json"))
            tool = skill / "scripts" / "threadplan_tool.py"
            result = subprocess.run([sys.executable, "-X", "utf8", str(tool), "--help"],
                                    cwd=directory, env=environment, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("propose", result.stdout)
            self.assertNotIn("analyze", result.stdout)
            self.assertNotIn("--config", result.stdout)
            missing = subprocess.run([sys.executable, "-X", "utf8", str(tool), "open", "--input", "missing.json"],
                                     cwd=directory, env=environment, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(missing.returncode, 1, missing.stderr)
            report = json.loads(missing.stdout)
            self.assertEqual(report["error"]["code"], "skill_setup_required")
            config = {"version":"3", "timezone":"UTC", "connection_ref":"c", "calendar_id":"constructed-dedicated-id",
                "feishu_identity":{"profile":"constructed-profile", "open_id":"ou_constructed"},
                "state_directory":"state", "execution_policy":{"mode":"read_only"}}
            config_path = directory / ".config" / "threadplan" / "skill.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps(config), encoding="utf-8")
            environment["PATH"] = ""
            for index in range(2):
                source = directory / f"source-{index}.json"
                source.write_text(json.dumps({"schema_version":"0.3", "request_id":f"new-chat-{index}",
                    "source":{"kind":"personal_note", "provider":"fixture"},
                    "messages":[{"id":f"original-{index}", "sender_id":"constructed-user",
                                 "sent_at":datetime.now(timezone.utc).isoformat(), "text":"Constructed note"}]}),
                    encoding="utf-8")
                valid = subprocess.run([sys.executable, "-B", "-X", "utf8", str(tool), "open",
                    "--source-user-id", "constructed-user", "--input", str(source)],
                    cwd=directory, env=environment, capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
                self.assertTrue(json.loads(valid.stdout)["result"]["request_key"])
            config["state_directory"] = str(skill / "state")
            config_path.write_text(json.dumps(config), encoding="utf-8")
            unsafe = subprocess.run([sys.executable, "-B", "-X", "utf8", str(tool), "open", "--input", str(source)],
                cwd=directory, env=environment, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(unsafe.returncode, 1)
            self.assertIn("outside_skill_package", json.loads(unsafe.stdout)["error"]["code"])
            api = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c",
                'import sys; sys.path.insert(0, sys.argv[1]); from threadplan_host import *; import threadplan_host; '
                'print(",".join(threadplan_host.__all__))', str(skill / "scripts/runtime")],
                cwd=directory, env=environment, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(api.returncode, 0, api.stderr)
            self.assertIn("AgentSession", api.stdout)
            self.assertNotIn("ConversationPlanner", api.stdout)


if __name__ == "__main__":
    unittest.main()
