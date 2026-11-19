"""Explicit split-flow Feishu user login. Never polls or prints credentials."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from threadplan.adapters.feishu import discover_cli

SCOPES = ["calendar:calendar:read",
          "calendar:calendar.event:create", "calendar:calendar.event:read",
          "calendar:calendar.event:update", "calendar:calendar.event:delete"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Start one user-authorized Feishu calendar login flow")
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({"mode": "plan_only", "scopes": SCOPES, "login_started": False}))
        return
    cli = discover_cli()
    environment = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
    result = subprocess.run([cli, "auth", "login", "--scope", " ".join(SCOPES), "--no-wait", "--json"],
                            capture_output=True, text=True, encoding="utf-8", timeout=60, env=environment)
    source = result.stdout if result.returncode == 0 else result.stderr
    try:
        payload = json.loads(source)
    except json.JSONDecodeError:
        raise SystemExit("Authentication returned no structured JSON; no polling or automatic retry performed")
    flow_dir = ROOT / "runs/feishu-auth" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    flow_dir.mkdir(parents=True, exist_ok=False)
    (flow_dir / "pending.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    record = payload.get("data", payload)
    url = record.get("verification_url") or record.get("verification_uri_complete") or record.get("console_url")
    if url is None:
        url = payload.get("error", {}).get("console_url")
    if url is None:
        print(json.dumps({"status": "authorization_not_started", "response_keys": list(payload),
                          "error_type": payload.get("error", {}).get("type"),
                          "private_response": str(flow_dir / "pending.json")}))
        raise SystemExit(1)
    qr_path = flow_dir / "authorization.png"
    qr = subprocess.run([cli, "auth", "qrcode", url, "--output", str(qr_path.relative_to(ROOT)), "--size", "256"],
                        capture_output=True, text=True, encoding="utf-8", timeout=30, cwd=ROOT, env=environment)
    (ROOT / "runs/feishu-auth/current.json").write_text(json.dumps({"active_flow_directory": str(flow_dir),
        "scope_request": SCOPES, "status": "awaiting_user_consent", "login_exit_code": result.returncode}), encoding="utf-8")
    print(json.dumps({"status": "awaiting_user_consent", "verification_url": url,
                      "qr_created": qr.returncode == 0 and qr_path.is_file(),
                      "qr_path": str(qr_path) if qr_path.is_file() else None,
                      "scope_request": SCOPES, "polling_started": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
