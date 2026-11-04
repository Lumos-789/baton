#!/usr/bin/env bash
# tests/regression.sh — Baton 标准回测流程（the standard regression entry).
#
# One command, five stages, strict exit code. Run it from anywhere; it cds to
# the repo root first:
#
#   ./tests/regression.sh          # full regression (~25s)
#
# Stages:
#   1. env sanity        — python3 >= 3.10, git available
#   2. byte-compile      — python3 -m compileall over scripts/
#   3. pytest suite      — full suite incl. temp-git-fixture e2e and the CLI
#                          failure contract
#   4. live CLI smoke    — run the real CLI against a temp git repo: actions
#                          dispatch, status snapshot, error exit (tests/cli_smoke.py)
#   5. docs consistency  — every backticked repo path in the instruction docs
#                          exists on disk (tests/check_docs.py)
#
# Any stage failing fails the run. Expected skips: 2 (psutil not installed —
# PID-reuse tests degrade by design; install psutil to run them).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

stage_num=0
stage_total=5

stage() {
  stage_num=$((stage_num + 1))
  printf '\n[stage %d/%d] %s\n' "$stage_num" "$stage_total" "$1"
}

trap 'printf "\n✗ REGRESSION FAIL at stage %d (exit %d)\n" "$stage_num" "$?"' ERR

# --- 1. env sanity ---------------------------------------------------------
stage "env sanity"
python3 - <<'PY'
import shutil, sys
if sys.version_info < (3, 10):
    raise SystemExit(f"python3 >= 3.10 required, got {sys.version.split()[0]}")
if not shutil.which("git"):
    raise SystemExit("git not found on PATH")
print(f"  python {sys.version.split()[0]}, git ok")
PY

# --- 2. byte-compile -------------------------------------------------------
stage "byte-compile scripts/"
python3 -m compileall -q scripts
echo "  compileall clean"

# --- 3. pytest suite -------------------------------------------------------
stage "pytest suite (unit + fixture e2e + CLI contract)"
python3 -m pytest --tb=short -q
echo "  pytest green"

# --- 4. live CLI smoke -----------------------------------------------------
stage "live CLI smoke (real subprocess)"
python3 tests/cli_smoke.py

# --- 5. docs consistency ---------------------------------------------------
stage "docs/file consistency"
python3 tests/check_docs.py

printf '\n✓ REGRESSION PASS — all %d stages green\n' "$stage_total"
