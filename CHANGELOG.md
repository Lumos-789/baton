# Changelog

## 0.3.1 (2026-11-05)

Five release blockers found by an extended verification round. The sealed
receipt, the lease state machine, the verification configuration and receipt
identity are now enforced by the controller instead of documented only.

- **The sealed receipt is actually tamper-evident.** `seal` now digests the
  whole envelope body (canonical JSON: sorted keys, no whitespace,
  `receiptHash` excluded) into `receiptHash` and records a `receiptSegments`
  table for diagnostics. `reconcile` recomputes the digest from the file
  before integration and rejects edits to `verification[*].exitCode`,
  `changed_files`, `commit_sha` or any other field with a non-zero exit that
  names the changed segment. Receipts sealed by 0.3.0 lack the digest and are
  rejected as unsealed — re-seal the worktree, or abandon the attempt.
- **An expired lease no longer deadlocks the run.** Expired attempts are
  retired before a batch is planned, a retry replaces the stale attempt (which
  is marked `superseded`) instead of being re-dispatched, and a superseded
  attempt stops emitting `recover`. The frontier advances to later tickets.
  New operator escape: `session_control.py <project> <change> abandon
  <assignment_id>` retires a stuck attempt, prints exactly what it destroys
  (the lease and future reconciliation) and what it preserves (worktree,
  branch, commits, packet, receipt), and makes the ticket claimable again.
- **`--verify` is persisted with the run.** The first claim stores the
  change-wide verification commands in the plan store; every later packet
  (including ones prepared by `reconcile` without the flag) reuses them. No
  commands configured is now a loud failure instead of a silent
  `git diff --check` downgrade. Passing `--verify` again is an explicit
  override for new packets.
- **A wrong-ticket receipt is rejected before the merge.** `reconcile`
  validates the receipt's `ticket_num`, `assignment_id`, `plan_revision`,
  `attempt` and packet hash against the attempt before touching the
  integration branch. A mismatch leaves the attempt `unknown` (not stuck in
  `integrating`) and the ticket unresolved.
- **Release hygiene.** `.npmignore` explicitly excludes `__pycache__/` and
  `*.pyc` (npm no longer relies on the `.gitignore` fallback); packaging tests
  assert no bytecode is tracked or packed and that the orchestration modules
  still import after the queue module moved out.

Regression: `./tests/regression.sh` — 156 passed, 5/5 stages green (the 16 that
went away were exclusive to the deleted queue/worktree/topo-sort surface).

### Simplification pass (same unreleased version)

- **`SKILL.md` was teaching a command that does not exist.** It documented
  `run <project> <change-id>` as the canonical verb, "no subcommand -> defaults
  to `run`", and `Default session_id="baton-run"`. The real CLI has no `run`
  verb, `add_subparsers(required=True)`, and `--session-id` is required. An
  agent following the skill would have failed on its first call. The skill now
  tables the six real verbs with their argument contracts.
- SKILL.md 351 → 229 lines: the ASCII loop diagram (duplicated in README,
  architecture and AGENTS), the `tasks.md` sample (duplicated verbatim in
  `docs/ticket-format.md`), the `status` response schema and the "Typical usage"
  walkthrough were all second copies. Every removed section is now a pointer.
- Leases are documented: fixed **7200 s**, no renewal entry point, so a ticket
  longer than two hours always returns `lease-expired` and is re-dispatched.
- ~1340 lines of dead code removed, every deletion pre-verified as zero-call:
  the local resource queue that moved out in 0.3.0 left six public methods,
  nine helpers and two tables behind; the pre-0.3.0 worker API in `worktree.py`
  was superseded by the assignment worktrees; `model.py` still carried eight
  symbols from the retired agent-profile model; and `topo_sort`, `plan_history`,
  `record_assignment`, `heartbeat_execution_attempt` and `record_result_envelope`
  had no callers at all. `attempt_resource_leases` (the live path) is untouched.
- `README_CN.md` caught up with 0.3.1 (receiptHash, abandon, persisted `--verify`,
  plus the History and Requirements sections) and `tests/check_docs.py` now
  covers README_CN, architecture and ticket-format, so the next drift fails CI.

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
