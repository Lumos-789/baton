"""Shared builders for the regression tests (not collected by pytest)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from orchestration.receipt import seal_receipt  # noqa: E402
from orchestration.session_controller import SessionController  # noqa: E402


def git(cwd, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}:\n{result.stderr}")
    return result.stdout.strip()


def tasks_md(sections: dict[str, list[str]]) -> str:
    """``{"Backend": ["a", "b"], "Frontend": ["c"]}`` → a tasks.md whose
    tickets are numbered ``N.M`` by section and position."""
    lines = ["# Tasks: feature", ""]
    for index, (title, tasks) in enumerate(sections.items(), start=1):
        lines += [f"## {index}. {title}", ""]
        lines += [
            f"- [ ] {index}.{item} {text}" for item, text in enumerate(tasks, start=1)
        ]
        lines.append("")
    return "\n".join(lines)


def project_with(
    tmp_path: Path,
    sections: dict[str, list[str]] | None = None,
    *,
    tasks: str | None = None,
    files: dict[str, str] | None = None,
    ignore_dsh: bool = False,
    change_id: str = "feature",
    name: str = "project",
) -> Path:
    """A git repo holding one OpenSpec change.

    ``.dsh/`` is deliberately not ignored unless ``ignore_dsh`` is set: baton
    has to keep its own runtime state out of the project's commits by itself.
    """
    project = tmp_path / name
    project.mkdir(parents=True)
    git(project, "init", "-q", "-b", "main")
    git(project, "config", "user.email", "t@t.t")
    git(project, "config", "user.name", "test")
    (project / ".gitignore").write_text(
        (".dsh/\n" if ignore_dsh else "") + "__pycache__/\n"
    )
    (project / "base.txt").write_text("base\n")
    for filename, content in (files or {}).items():
        (project / filename).parent.mkdir(parents=True, exist_ok=True)
        (project / filename).write_text(content)
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "init")
    change = project / "openspec" / "changes" / change_id
    change.mkdir(parents=True)
    (change / "proposal.md").write_text("# Proposal\n\ndo the thing\n")
    (change / "tasks.md").write_text(
        tasks if tasks is not None else tasks_md(sections or {})
    )
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "openspec change")
    return project


def controller_for(project: Path, commands=("test -f base.txt",), **kwargs) -> SessionController:
    kwargs.setdefault("verification_commands", list(commands))
    return SessionController(project, "feature", **kwargs)


def dispatches(batch):
    return [action for action in batch.actions if action.kind == "dispatch"]


def action_of(batch, kind: str):
    return next((action for action in batch.actions if action.kind == kind), None)


def codes(batch, kind: str = "blocked") -> list[str]:
    return [action.code for action in batch.actions if action.kind == kind]


def warning_codes(batch) -> list[str]:
    return [warning["code"] for warning in batch.warnings]


def warning_of(batch, code: str) -> dict:
    return next(warning for warning in batch.warnings if warning["code"] == code)


def work(action, files: dict[str, str], *, commit: bool = True, seal: bool = True):
    """Play the subagent: write files, commit, seal the receipt."""
    worktree = Path(action.cwd)
    for filename, content in files.items():
        (worktree / filename).parent.mkdir(parents=True, exist_ok=True)
        (worktree / filename).write_text(content)
    if commit:
        git(worktree, "add", "-A")
        git(worktree, "commit", "-q", "-m", "work")
    return seal_receipt(action.packet_path) if seal else None


def packet_of(action) -> dict:
    return json.loads(Path(action.packet_path).read_text(encoding="utf-8"))


def integration_of(action) -> Path:
    return Path(packet_of(action)["integrationPath"])


def heartbeat(action) -> Path:
    return Path(action.packet_path).parent / "progress"


# --- builders over the sample-change fixture (shared by the controller suites) ---


FIXTURES = ROOT / "tests" / "fixtures" / "sample-change"


def fixture_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-b", "main")
    git(project, "config", "user.email", "t@t.t")
    git(project, "config", "user.name", "test")
    (project / ".gitignore").write_text(".dsh/\n", encoding="utf-8")
    (project / "base.txt").write_text("base\n", encoding="utf-8")
    git(project, "add", "-A")
    git(project, "commit", "-m", "init")
    change = project / "openspec" / "changes" / "feature"
    change.mkdir(parents=True)
    for source in FIXTURES.glob("*.md"):
        shutil.copy(source, change / source.name)
    return project


VERIFY = ["test -f base.txt"]


def fixture_controller(project: Path, **kwargs) -> SessionController:
    kwargs.setdefault("verification_commands", VERIFY)
    return SessionController(project, "feature", **kwargs)


def finish(action, marker: str) -> None:
    worktree = Path(action.cwd)
    (worktree / marker).write_text("done\n", encoding="utf-8")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-m", f"finish {marker}")
    seal_receipt(action.packet_path)


def packet_payload(action) -> dict:
    return json.loads(Path(action.packet_path).read_text(encoding="utf-8"))


def seal_all(assignments) -> None:
    for action in assignments:
        worktree = Path(action.cwd)
        git(worktree, "add", "-A")
        git(worktree, "commit", "-m", f"ticket {action.ticket_num}")
        seal_receipt(action.packet_path)
