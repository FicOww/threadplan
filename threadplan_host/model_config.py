"""Read only this project's explicitly configured model key; never execute .env."""

import os
from pathlib import Path


def openrouter_key(project_root: Path) -> str | None:
    value = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if value:
        return value
    path = Path(project_root) / ".env"
    if not path.is_file():
        return None
    found = None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, candidate = stripped.partition("=")
        if key.strip() != "OPENROUTER_API_KEY":
            continue
        if not separator or found is not None:
            raise ValueError("Set OPENROUTER_API_KEY once as a literal value in .env")
        candidate = candidate.strip()
        if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {"'", '"'}:
            candidate = candidate[1:-1]
        found = candidate
    return found or None
