"""Complete the one active split flow after the user confirms consent."""

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from threadplan.adapters.feishu import discover_cli


def main():
    state_path = ROOT / "runs/feishu-auth/current.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") != "awaiting_user_consent":
        raise SystemExit("No pending active consent flow; do not reuse an earlier device code")
    directory = Path(state["active_flow_directory"]).resolve()
    if not directory.is_relative_to((ROOT / "runs/feishu-auth").resolve()):
        raise SystemExit("Invalid active flow location")
    pending = json.loads((directory / "pending.json").read_text(encoding="utf-8"))
    record = pending.get("data", pending)
    device_code = record.get("device_code")
    if not isinstance(device_code, str) or not device_code:
        raise SystemExit("Active flow has no device code; no replacement login started")
    environment = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
    result = subprocess.run([discover_cli(), "auth", "login", "--device-code", device_code, "--json"],
                            capture_output=True, text=True, encoding="utf-8", env=environment, timeout=60)
    output = result.stdout if result.returncode == 0 else result.stderr
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        payload = {"unparsed_response": True, "exit_code": result.returncode}
    (directory / "completion.private.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    state["status"] = "completed" if result.returncode == 0 else "completion_failed"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    print(json.dumps({"status": state["status"], "exit_code": result.returncode,
                      "response_keys": list(payload), "error_type": payload.get("error", {}).get("type"),
                      "error_message": payload.get("error", {}).get("message")}))
    if result.returncode != 0:
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
