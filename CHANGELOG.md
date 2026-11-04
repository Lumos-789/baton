# Changelog

## 0.3.0 (2026-11-04)

Baton is one product again: the ticket/DAG control plane. Two things that were
not part of it left the repository.

- **The generic resource queue moved out.** `scripts/queue.py`,
  `scripts/coordination/` and their five test files (3.1k lines of code, 2.9k of
  tests, 45% of the Python in this repo) implemented resource mutex, capacity
  and drain barriers — a separate product that the controller never imports.
  It now lives in the local `baton-queue` project; this bundle no longer ships
  it, and the skill no longer documents the `queue` verbs.
- **The repository's own OpenSpec change is gone.** `openspec/` held the design
  change for a past release of baton itself; `docs/architecture.md` carries the
  current design, so the change file only repeated it.

## 0.2.2 (2026-11-04)

Four CLI reliability fixes found by running the real commands. No protocol or
state-machine change: the action batch, the plan store and the sealed-receipt
contract are unchanged.

- **`queue.py exec` no longer wedges a resource on a spawn failure.** A command
  that cannot be started (typo, missing binary) used to keep its lease, so the
  resource stayed held by a dead owner and every later `gate` on it waited
  forever. The failure now releases the lease as `failed`; the error still
  surfaces as `Error: ...` (exit 1).
- **Bad `--capacity` / empty `--session-id` are usage errors, not tracebacks.**
  `actions` and `reconcile` documented capacity 1..5 as a hard cap but crashed
  with a Python traceback on `0`, `6` or `""`. Both flags are validated at the
  CLI boundary now: exit 2, one usage line naming the allowed range.
- **An out-of-range task id is the documented `TasksMarkdownError`.** `## 100.`
  or `1.100` in tasks.md escaped as a raw `ValueError` traceback from
  `session_control.py status|actions` and `ticket_board.py check`; it now reads
  like every other malformed-tasks.md case (one JSON error line, exit 1).
- **The `[x]` write-back preserves the file it edits.** `check_task` keeps the
  file's line endings (a CRLF tasks.md used to be rewritten to LF, turning one
  checkbox into a whole-file diff) and matches exactly the task lines the
  parser accepts (extra spaces inside the brackets), so a parsed ticket can
  always be checked off — previously such a line parsed fine and then failed
  as "task not found" after the assignment had already been integrated.

Regression: `./tests/regression.sh` — 309 passed, 2 skipped, 5/5 stages green.

## 0.2.1 (2026-11-03)

The CLI is the only control surface: the local MCP adapter is retired from the
bundle (`scripts/session_control.py` + `scripts/receipt.py` are the whole
surface).
