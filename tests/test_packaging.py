"""Release hygiene: no Python bytecode in git or the npm tarball.

The bundle ships ``scripts/`` wholesale (``package.json`` ``files``), and npm
only falls back to ``.gitignore`` when ``.npmignore`` is absent — an accidental
``npm pack`` from a dirty checkout used to be able to include
``scripts/**/__pycache__/*.pyc``. These tests pin the explicit guard.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATTERNS = {"__pycache__/", "*.pyc"}


def _ignore_patterns(path: Path) -> set[str]:
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def test_gitignore_and_npmignore_exclude_python_caches():
    assert CACHE_PATTERNS <= _ignore_patterns(ROOT / ".gitignore")
    npmignore = ROOT / ".npmignore"
    assert npmignore.is_file(), ".npmignore is required so npm never ships Python caches"
    assert CACHE_PATTERNS <= _ignore_patterns(npmignore)


def test_no_python_bytecode_is_tracked_or_packed():
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert [
        path for path in tracked
        if "__pycache__" in path or path.endswith(".pyc")
    ] == []

    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm is not installed; tarball check skipped")
    packed = subprocess.run(
        [npm, "pack", "--dry-run", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    files = [
        entry["path"]
        for entry in json.loads(packed.stdout)[0].get("files", [])
    ]
    assert "__pycache__" not in "\n".join(files)
    assert [path for path in files if path.endswith(".pyc")] == []
    # The bundle still ships its real payload.
    assert {"index.js", "scripts/session_control.py"} <= set(files)


def test_orchestration_modules_import_cleanly_without_the_removed_queue():
    script = (
        "import importlib, sys\n"
        "sys.path.insert(0, 'scripts')\n"
        "for name in ('model', 'plan_model', 'plan_store', 'result', 'receipt', "
        "'session_model', 'session_controller', 'integrator', 'worktree'):\n"
        "    importlib.import_module('orchestration.' + name)\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
