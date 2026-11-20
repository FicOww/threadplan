"""Thin host-model adapter for the bundled ThreadPlan tools."""

from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
RUNTIME = SCRIPT_DIR / "runtime"
if not RUNTIME.is_dir():
    raise SystemExit("Incomplete Skill package: scripts/runtime is required; use the complete ZIP.")
sys.path.insert(0, str(RUNTIME))

from threadplan_host.cli import main

if __name__ == "__main__":
    raise SystemExit(main(managed_skill=True))
