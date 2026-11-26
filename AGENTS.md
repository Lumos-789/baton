# Baton — Agent Project Conventions

## What This Is

Baton is the **sole ticket / DAG / session-subagent control plane over OpenSpec
changes**. It ships as the `dsh-openspec-baton` DSH bundle: the repository is
the plugin package, `index.js` mounts `skills/`, and the deterministic
controller is driven through `scripts/session_control.py`.

The plan face is the project's OpenSpec change (`openspec/changes/<id>/tasks.md`);
the cognitive phase is the OpenSpec workflow itself. The main session never
decides ticket state itself: it forwards the deterministic `actions` returned
by the CLI to native subagents, binds the agent id, waits for the terminal
event, and reconciles it against a sealed receipt. A ticket lands only when the
controller has also re-run the verification commands on the **merged**
integration branch and they passed. There is no PRD, no passes flags, no
background loop, no headless CLI, and no self-built cognitive state machine on
the active path.

## Directory Responsibilities

| Path | Responsibility |
|---|---|
| `package.json` | DSH bundle manifest (`dsh.bundle.patch`, peer deps, engine range) |
| `cordis.patch.yml` | bundle insert row (`dsh-openspec-baton`) |
| `index.js` | mounts the bundled `skills/` provider |
| `skills/baton/SKILL.md` | skill definition (frontmatter + the actions → subagent → bind → reconcile loop over OpenSpec changes) |
| `scripts/_ticket.py` | **OpenSpec tasks.md parser** — change→feature, task `N.M`→ticket, section-order DAG, `[x]` write-back (spec: `docs/ticket-format.md`) |
| `scripts/_frontier.py` | DAG + frontier (shared module) |
| `scripts/orchestration/session_controller.py` | **SessionController** — the sole deterministic scheduler: the `actions` two-pass loop (reconcile receipts, resolve stale attempts, claim), `reconcile`, `status` |
| `scripts/orchestration/receipt_flow.py` | sealed-receipt reconciliation: verify → integrate → **verify the merged tree** → finalize; the ticket-redefinition guard; the red-base check |
| `scripts/orchestration/lease_flow.py` | lease renewal from activity, retry budgets, recover actions |
| `scripts/orchestration/dispatch_flow.py` | assignment worktree + immutable packet + dispatch prompt |
| `scripts/orchestration/operator_flow.py` | operator verbs: `abandon`, `adopt`, `reopen` |
| `scripts/orchestration/run_settings.py` | what a run remembers between calls (verification commands, capacity, base branch); integration-branch ↔ base alignment |
| `scripts/orchestration/relay.py` | read-only relay judgments: elapsed vs lease budget, receipt existence, worktree/heartbeat activity (`activity_epoch` also renews leases), suggested action, and the abandon guard verdict |
| `scripts/orchestration/plan_store.py` | durable plan store (`queue.db`): plan revisions (synced from tasks.md edits), run settings, sealed result envelopes, completion facts, read-only `snapshot()`; keeps `.dsh/baton/` out of the target repo via `info/exclude` |
| `scripts/orchestration/attempt_store.py` | attempt lifecycle mixin of `PlanStore`: claim, lease expiry, agent binding, state transitions, finalize |
| `scripts/orchestration/budget_store.py` | lease renewal and the retry budget (mixin of `PlanStore`): renew, `attempts_used`, `reopen`, the per-ticket attempt epoch |
| `scripts/orchestration/store_base.py` | plan-store errors, the attempt-state vocabulary, retry budget and run-setting keys, and the atomic JSON write helper (shared by both store modules) |
| `scripts/orchestration/store_schema.py` | `queue.db` schema bootstrap (DDL + WAL busy-retry + version gate) |
| `scripts/orchestration/plan_model.py` | shared plan data model: `PlanTicket` / `PlanRevision` + `utc_now`/`stable_hash` |
| `scripts/orchestration/integrator.py` | serial git integration of assignment branches; verifies the merged tree and records the outcome per integration HEAD; preserves merge conflicts for `resolve_merge` |
| `scripts/orchestration/worktree.py` | isolated assignment + integration worktrees (one branch per assignment), base-branch alignment, cleanup of worktrees and merged branches |
| `scripts/orchestration/git_run.py` | the single git subprocess wrapper (`run_git` with caller-chosen error type) — every git call in `scripts/` goes through it |
| `scripts/orchestration/receipt.py` / `result.py` | sealed-receipt verify/seal core + `ResultEnvelope` (success evidence model) |
| `scripts/orchestration/session_model.py` | action batch models (dispatch/recover/resolve_merge/repair_integration/wait/blocked/complete), `ExecutionAttempt`, `SessionControllerError` + Task Packet finalization |
| `scripts/receipt.py` | subagent-facing `verify`/`seal` CLI — the only accepted success evidence |
| `scripts/session_control.py` | **the control-plane CLI** over `SessionController` (actions/bind/reconcile/status/cleanup/abandon/adopt/reopen) — the only adapter surface |
| `scripts/ticket_board.py` | static HTML board generator over OpenSpec changes (zero process, zero port) |
| `templates/` | ticket board HTML template |
| `docs/` | design documents |
| `tests/` | pytest tests + `tests/regression.sh` (the standard regression entry); `tests/baton_helpers.py` holds shared builders for the 0.5.0 regression suites |

## Development Conventions

- **Standard regression** — after changing anything under `scripts/` or docs, run `./tests/regression.sh` from the repo root and confirm all stages green. Quick check during iteration: `python3 -m pytest` (same suite, no live-CLI/docs stages).
- **Python style**: PEP 585 (`list[int]` not `List[int]`), dataclass, type annotations, `from __future__ import annotations`
- **Execution is session-driven** — the main session drives DSH subagents via the `subagent` tool; the deterministic controller owns all state. No background Python loops, no headless CLI subprocesses, no PRD/passes.
- **Ticket format**: see `docs/ticket-format.md` (authoritative source of the parser); changing the format requires updating `_ticket.py` + tests in sync
- **Docs follow code** — AGENTS.md / README / docs must not reference removed files or "pending removal" tools; `regression.sh` stage 5 enforces this mechanically
- **The CLI speaks one envelope** — every verb prints `{"ok", "command", ...}` with snake_case keys; a domain failure adds a stable `code`, and what a model must act on is a field (never JSON inside a string). `tests/test_doc_cli_contract.py` keeps documented commands equal to the real parser
- **Fail loud, never mask** — no silent fallbacks: an unnumbered checkbox, a redefined ticket, a red merged tree and a spent retry budget all stop and say so

## Agent Execution Mode (session-driven native subagents)

**Core principle**: the main session is a mechanical forwarder. The deterministic
`SessionController` owns all state-machine logic.

### The loop

```
main session (mechanical forwarder)
  ├─ session_control.py <project> <feature> actions --session-id ... --capacity≤5
  │     → SessionController.actions(): renew leases, expire stale ones,
  │       reconcile sealed receipts (verify → merge → verify the merged tree),
  │       compute frontier, atomically claim, prepare isolated worktrees +
  │       immutable Task Packets, return action batch + warnings
  ├─ for each dispatch action: subagent(prompt, cwd) → agent_id
  ├─ session_control.py <project> <feature> bind <assignment_id> <claim_token> <agent_id>
  ├─ wait for the subagent terminal lifecycle event
  └─ session_control.py <project> <feature> reconcile <assignment_id> <claim_token> --agent-terminal ...
        → sealed receipt → succeeded + integrated + merged tree verified; otherwise unknown + recover
```

### Why native subagents (not background loops)

- **Real subagents**: each ticket is done by a subagent with isolated context and full tool access — not a fresh headless CLI with amnesia.
- **Deterministic state**: claim, lease, worktree, receipt verification, integration, merged-tree verification, and completion are all scripted in `SessionController` + the durable plan store — never left to the model.
- **Crash-safe**: progress survives on disk (sealed receipts + leased attempts). Restart and `actions()` resumes.
- **Capacity-≤5 sliding window**: isolated worktrees per assignment; integration serialized by a repository-scoped lock; merge conflicts preserved (never auto-aborted).

> **Note on unattended runs**: this design requires the main session to stay open (subagents are children of the session). For true unattended/overnight runs, use DSH's scheduling tools to start a session that runs one batch at a time.

## Scheduling Rules (what the controller guarantees)

- **Lease = 7200 s of silence.** Heartbeat, edited files and commits renew it; a hard cap of three leases ends a zombie. There is no subagent-facing renewal call.
- **Automatic retries are bounded.** `MAX_ATTEMPTS_PER_TICKET` (3) per ticket; each retry is announced in `warnings` with the stale `agent_id`; `reopen <N.M>` grants a fresh budget. A ticket locked out by the budget stays visible (`retry-budget-spent`).
- **One lease judgment per call.** `actions()` sweeps expired leases once, at its top, and claims with `expire_first=False`: a lease that runs out mid-call is judged on the next call, so one batch never carries two subagents for one ticket.
- **The red-base hold applies on every path.** `reconcile` and `adopt` do not integrate directly; the receipt pass inside `actions` does, after the base check.
- **`<project>` is a repository root.** Baton refuses a directory inside another repository instead of creating branches there.
- **A red merged base blocks the run.** `post-merge-verification-failed` parks the attempt, other receipts wait, no new ticket is claimed, and the recorded outcome is reused until HEAD moves.
- **A changed task text parks its in-flight attempt** (`ticket-redefined` / `ticket-removed`) instead of delivering old wording against a new definition.
- **A succeeded receipt is durable.** Manual un-checks are reported (`resolved_but_unchecked`), never silently honored or silently ignored; `reopen` is the explicit reverse.
- **Run settings are declared once.** `--verify`, `--capacity` and `--base-branch` persist with the run; omitting them on `reconcile` reuses the stored values.

## Continuous Iteration Discipline (long-term principle · remember across sessions)

baton is a **living framework**, not a one-off deliverable. Any agent in any session encountering the following situations should proactively capture improvements — **don't wait for the user to call it out**:

1. **Fix and capture when problems surface** — CLI error/ticket parsing issue/scheduling conflict/race condition/state inconsistency. After fixing, write the root cause and fix into the corresponding doc.
2. **Absorb when you see a better way** — read a new approach, discover a better abstraction, find a cleaner implementation → proactively suggest it, write it into the ticket DAG or change it directly. Design docs are not gospel, code is truth.
3. **Feed lessons learned into knowledge promptly** — pitfalls you hit, patterns that worked, real constraints you discovered — write them into the project wiki or your own knowledge base.
4. **Design docs follow the code** — when the implementation drifts from the `docs/*.md` design notes, either align the code to the design or update the design to match the code. Never leave "docs say one thing, code does another".
5. **Tune prompts against real runs** — when running assignments you find that the dispatch prompt instructions aren't clear enough and cause subagents to drift, come back and tune the prompt and note it.
6. **Glance back on cross-project reuse** — when baton is connected to a new project and you find you need to modify baton to make it work, sync the change back to the main repository.

## Progress Board (observability)

`scripts/ticket_board.py` generates a pure static HTML board (zero process, zero port):
- `build <project> <feature>` — single-feature board
- `build-all <project>` — global multi-feature board (each ticket carries colored status tags)

## Runtime Artifacts (not in git)

Baton generates the following in the **target business project** (not this repository) at runtime:

- `<project>/.dsh/baton/queue.db` — durable plan store
- `<project>/.dsh/baton/runs/<assignment_id>/` — `packet.json` / `verification.json` / `receipt.json`, plus the optional `progress` heartbeat (a bare file, mtime-only, refreshed by the subagent outside every worktree)
- `<project>/.dsh/baton/<change-id>/{plan.json,results/}` — live plan projection (rewritten per claim; the immutable revision history lives in `queue.db`) + result envelopes

`.dsh/baton/` is appended to the repository's `info/exclude` on first use (local, untracked), so a blanket `git add -A` in the target project never stages it; a tracked `.gitignore` is never touched.

The plan face (`openspec/changes/<id>/tasks.md`) is written by the project's
OpenSpec workflow, not by baton — baton only flips checkboxes on integration,
as an uncommitted edit that is committed together with the merge.

This repository holds only source code and tests, no runtime state. There is
no `prd-<feature>.json` on the active path.

## Test Fixtures

`tests/fixtures/sample-change/` is an OpenSpec change fixture:

```
section 1: 1.1 (resolved seed)
section 2: 2.1 / 2.2 (parallel pair)   ← frontier
section 3: 3.1 (blocked by sections 1+2)
```

Changing fixtures requires updating assertions in tests in sync.

## Glossary

- **SessionController**: the sole deterministic control plane for native session subagents — owns actions, claim, lease, worktree, receipt reconciliation, merged-tree verification, integration, recovery, and completion.
- **Change / Feature**: an OpenSpec change at `openspec/changes/<change-id>/` — the unit baton runs (`run baton on <project> <change-id>`).
- **Task / Ticket**: one `- [ ] N.M` checklist item in the change's tasks.md; num = `N*100+M`; section order defines the DAG.
- **Assignment**: one durable attempt to execute a ticket with a native subagent (`ASG-<change>-<ticket>-r<revision>-a<attempt>`).
- **Task Packet**: immutable per-assignment contract (goal, criteria, verification commands, worktree, claim token) — the subagent reads it and runs `receipt.py verify`/`seal`.
- **Sealed Receipt**: the only accepted success evidence — produced after the packet's verification commands run green in the worktree on a clean, advanced HEAD; its whole body is covered by `receiptHash` and verified before integration.
- **Integration Worktree**: the shared branch where sealed assignment branches are merged serially under a repository-scoped lock (`baton-integration.lock` in the common git dir); every merge is verified there before the attempt is finalized.
- **Live Plan / Plan Revision**: tasks.md edits are picked up as new plan revisions on the next `actions()` call; the latest revision defines the scope of completion. In-flight assignments keep their immutable packets, and a changed task text parks the attempt for an operator.
