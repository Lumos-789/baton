#!/usr/bin/env python3
"""Live CLI smoke test — real subprocess, real git repo, real JSON stdout.

Covers the contract the skill depends on: `actions` returns a dispatch batch,
`status` returns the same snapshot shape as the controller, and a domain
failure exits non-zero with one JSON error object.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts" / "session_control.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def _project(root: Path) -> Path:
    project = root / "proj"
    project.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "smoke@test")
    git("config", "user.name", "smoke")
    (project / "README.md").write_text("# smoke\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")
    change = project / "openspec" / "changes" / "demo"
    change.mkdir(parents=True)
    (change / "proposal.md").write_text("# Demo\n", encoding="utf-8")
    (change / "tasks.md").write_text(
        "# Tasks: demo\n\n## 1. Phase\n\n- [ ] 1.1 smoke task\n",
        encoding="utf-8",
    )
    return project


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(Path(tmp))

        actions = _run(str(project), "demo", "actions", "--session-id", "smoke")
        if actions.returncode != 0:
            print(f"CLI SMOKE FAIL: actions exited {actions.returncode}\n{actions.stderr}")
            return 1
        batch = json.loads(actions.stdout)
        dispatches = [a for a in batch["actions"] if a["kind"] == "dispatch"]
        if not batch.get("ok") or len(dispatches) != 1:
            print(f"CLI SMOKE FAIL: unexpected actions batch: {batch}")
            return 1
        if not dispatches[0]["prompt"] or not dispatches[0]["cwd"]:
            print("CLI SMOKE FAIL: dispatch is missing prompt or cwd")
            return 1

        status = _run(str(project), "demo", "status")
        snapshot = json.loads(status.stdout)
        if status.returncode != 0 or snapshot.get("runId") != "RUN-demo":
            print(f"CLI SMOKE FAIL: unexpected status: {snapshot}")
            return 1
        if snapshot.get("missingRequiredTickets") != ["1.1"]:
            print(f"CLI SMOKE FAIL: unexpected missing tickets: {snapshot}")
            return 1

        failure = _run(str(project), "missing-change", "status")
        error = json.loads(failure.stdout)
        if failure.returncode == 0 or "tasks.md not found" not in error.get("error", ""):
            print(f"CLI SMOKE FAIL: domain error contract broken: {failure.stdout}")
            return 1

    print("CLI SMOKE PASS: actions dispatch, status snapshot, error exit — all green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
