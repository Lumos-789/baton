---
name: baton
description: baton 接力调度——用确定性 ticket/DAG 控制面执行已批准的 OpenSpec change。当用户说「跑 baton」「跑一下 baton」「baton status」「用 baton 排活」「派活」「派发任务」「把这个 OpenSpec change 跑完」或问 baton 能不能排活时使用；本地 CLI 返回确定 actions，会话派发给隔离 worktree 子代理，验证全绿的密封回执才算完成。Also use when the user says "run baton", "baton status", or wants an OpenSpec change executed with verified integration.
whenToUse: The user mentions baton in any phrasing (跑 baton / baton status / 排活 / 派活 / dispatch) or wants an approved OpenSpec change executed or inspected in a git project.
---

# Baton

Baton is the **sole ticket / DAG / session-subagent control plane over OpenSpec
changes**. The main session never decides ticket state itself: it forwards the
deterministic `actions` returned by the Baton CLI to DSH subagents
verbatim, binds each returned agent id, and reports the terminal event back via
`reconcile`. Free-text subagent output is ignored; only a **sealed receipt**
(the assignment's verification commands run green in an isolated worktree)
counts as success.

```
main session (mechanical forwarder - no model-driven state decisions)
  |- <cli> <project> <change> actions --session-id ... --capacity<=5
  |     -> recover/reconcile sealed receipts, compute the frontier, claim
  |        tickets, prepare isolated worktrees + immutable Task Packets,
  |        return dispatch/recover/resolve_merge/wait/blocked/complete actions
  |- subagent(dispatch.prompt, cwd=dispatch.cwd, description=...) -> agent id
  |- <cli> <project> <change> bind <assignment_id> <claim_token> <agent_id>
  |- runtime sends the subagent completion notice
  |- <cli> <project> <change> reconcile <assignment_id> <claim_token>
  |        --agent-terminal <completed|failed|canceled> --session-id ... --capacity ...
  |        -> sealed receipt -> succeeded + integrated; otherwise unknown + recover
```

The plan face is the project's OpenSpec change
(`<project path>/openspec/changes/<change-id>/tasks.md`). Runtime additions
are made by editing `tasks.md` directly; the next `actions` call records a new
plan revision.

## Argument Parsing

- `run <project path> <change-id>` - the canonical
  actions -> subagent -> bind -> completion notice -> reconcile loop.
  The feature is an **OpenSpec change** at
  `<project path>/openspec/changes/<change-id>/`.
- `status <project path> <change-id>` - read-only snapshot via
  `<cli> <project path> <change-id> status`.
- No subcommand -> defaults to `run`.

`<baton-root>` is the directory containing this bundle's `package.json` (two
levels above this `SKILL.md`). Relative doc paths in this file are written
against that root, not against the skill directory.

`<python>` is the Python 3.10+ interpreter that runs the CLI:
`python3` on macOS/Linux, `python` on Windows, or the `BATON_PYTHON` override.

`<cli>` is `<python> <baton-root>/scripts/session_control.py`. Every verb prints
one JSON object to stdout and exits non-zero on a domain failure (one JSON
error object, no traceback). Quote project paths. If the harness truncates
stdout, write the batch to `.dsh/baton/batch.json` and read that file.

## Run

**Responsibility**: drive the deterministic session-subagent loop until every
required task of the change is verified and integrated.

### Prerequisites

- The `dsh-openspec-baton` bundle is installed, so this skill and
  `<baton-root>/scripts/session_control.py` are available.
- The active DSH profile provides `subagent` (from `dsh-base` or an equivalent
  delegation plugin). `list_agents` / `send_message` are only needed to
  inspect or steer a running child.
- Python 3.10+ is reachable as `python3` (macOS/Linux) or `python` (Windows),
  or through the `BATON_PYTHON` environment variable.
- The target project is a **git repository** (the controller prepares isolated
  worktrees).
- The project has an **OpenSpec change** at
  `<project path>/openspec/changes/<change-id>/` with `proposal.md` and
  `tasks.md` containing at least one unchecked
  `- [ ] N.M` task whose section predecessors are all checked.

### The loop

1. **Run the `actions` verb**:

   ```bash
   <cli> "<project path>" <change-id> actions \
     --session-id baton-run --capacity 1 --base-branch main
   ```

   Default `session_id="baton-run"` and `capacity=1`; state the chosen
   capacity before raising it above 1. Pass change-wide verification commands
   with `--verify "<cmd>"` before the verb when the project does not use the
   default `git diff --check`. The result is `{ok, run_id, state, actions:[...]}`.

2. **Handle every action in the batch**:
   - `dispatch` -> call `subagent(prompt=action.prompt,
     cwd=action.cwd, description="<change> ticket <num>")`. The call returns
     the child id immediately; DSH notifies this session when the child
     finishes. Immediately bind it:

     ```bash
     <cli> "<project path>" <change-id> bind \
       <assignment_id> <claim_token> <agent_id>
     ```
   - `resolve_merge` -> call
     `subagent(prompt=action.prompt, cwd=action.cwd,
     description="<change> merge <assignment_id>")`. It resolves the preserved
     conflict, runs the packet's verification commands, and commits the merge.
     Reconcile when it finishes.
   - `wait` -> nothing to spawn; end the turn and wait for the outstanding
     completion notices.
   - `recover` -> the attempt's outcome is unknown (no sealed receipt).
     Surface it and decide whether to retry or repair. Never mark it
     succeeded.
   - `blocked` -> report the block code and reason; nothing to spawn.
   - `complete` -> every required ticket has a verified, integrated receipt.
     Run the `status` verb for the final summary and stop.

3. **On each subagent completion notice**, run `reconcile`:

   ```bash
   <cli> "<project path>" <change-id> reconcile \
     <assignment_id> <claim_token> \
     --agent-terminal completed --session-id baton-run --capacity 1
   ```

   DSH settlement notices are prose, not a status enum. Map them explicitly:
   "finished" -> `completed`; stopped / canceled / interrupted -> `canceled`;
   failed / "ran out of room" / declined / abnormal -> `failed`.

   Reconcile honors only a sealed receipt written by
   `<baton-root>/scripts/receipt.py verify` / `seal` in the assignment
   worktree; a terminal event without one flips the attempt to `unknown` and
   returns a `recover` action.

4. **Process the action batch returned by `reconcile` directly.** Call
   `actions` again only when starting a run or resuming after a restart.
   Continue until a batch contains `complete`.

If a batch produces only running children, end the turn and wait for their
completion notices instead of polling. Use `list_agents` / `send_message` only
when you need to inspect or steer a child.

### Concurrency

- Capacity 1 is strictly serial. Capacity 2-5 dispatches that many isolated
  worktrees in parallel; the controller serializes integration into the shared
  integration worktree under a repository-scoped lock.
- Merge conflicts are preserved (never auto-aborted) and surfaced as
  `resolve_merge` actions.
- Never exceed capacity 5 (the controller rejects it) and never spawn more
  children than the batch returned `dispatch` actions for.

### Completion and cleanup

At `complete` the controller auto-removes the per-assignment worktrees
(`baton/<feature>-tic-*`) and reports them in
`worktrees_removed` / `worktrees_skipped`. The shared integration worktree
(`baton/<feature>`) is left in place: merge it into the project's main branch,
then remove it with `cleanup --include-integration`.

## Status

Run the `status` verb:

```bash
<cli> "<project path>" <change-id> status
```

It returns
`{ok, runId, revision, complete, missingRequiredTickets:["2.1","3.2"], attempts:[...]}`
(missing tickets are `N.M` task ids, the same ids tasks.md uses).
Completion comes from the plan store's required-ticket check, not from any PRD
`passes` flags.

## Cleanup

```bash
<python> <baton-root>/scripts/session_control.py <project path> <change-id> cleanup
<python> <baton-root>/scripts/session_control.py <project path> <change-id> cleanup --include-integration
<python> <baton-root>/scripts/session_control.py <project path> <change-id> cleanup --dry-run
```

Without `--include-integration` only leftover assignment worktrees are
removed. The shared integration worktree is removed only when explicitly
requested. Cleanup never touches the project's main working tree and never
force-removes a dirty worktree (it skips it and reports). `--dry-run` lists
the matched worktrees (`wouldRemove`) without removing anything. A change-id
that never ran (typo) fails with `no baton worktrees or branches found`
instead of an empty success.

## OpenSpec change -> ticket mapping

```markdown
# Tasks: <change-id>

## 1. First phase

- [x] 1.1 already-done task (resolved seed)
- [ ] 1.2 another task

## 2. Second phase

- [ ] 2.1 independent task A
- [ ] 2.2 independent task B
```

- change -> feature; task `N.M` -> ticket (num = `N*100+M`)
- `[x]` = resolved, `[ ]` = ready; there is no draft state
- **DAG**: task `N.M` is blocked by every task in earlier sections; tasks
  within a section are independent (parallel frontier)
- verification: the change-wide `--verify` CLI flag
  (default `git diff --check`)
- write-back: an integrated receipt flips the checkbox to `[x]` in `tasks.md`

See `<baton-root>/docs/ticket-format.md` for the exact parsing rules.

## Completing a request with Baton

"Use baton to finish X" means: follow the project's OpenSpec workflow to write
and get approval for the change, then run it.

1. Explore the codebase and ask only the questions whose answers change the
   plan.
2. Write `openspec/changes/<change-id>/proposal.md` and `tasks.md`
   (sections = phases, `N.M` tasks).
3. The user approves, then run the change to `complete`.
4. Optionally archive the change with the project's OpenSpec conventions.

A new session resumes from the change directory plus `baton status`; no extra
state is needed.

## Live plan - edit tasks.md

Add, reorder, or split tasks by editing the change's `tasks.md`. The next
`actions` call records a new plan revision and recomputes the frontier.

- A running assignment keeps its original `planRevision` and
  `taskPacketHash`; packets are immutable.
- Completion is judged against the latest revision, so every unchecked task
  in the final `tasks.md` must be delivered.
- Tasks added to an earlier section block later sections; keep runtime
  additions in a trailing section unless they must gate pending work.

## Runtime directory layout

```
business project root/
|-- openspec/
|   `-- changes/<change-id>/
|       |-- proposal.md            why/what (the human-approved plan face)
|       `-- tasks.md               task checklist (ticket source + [x] write-back)
`-- .dsh/
    `-- baton/
        |-- queue.db               durable plan store
        |-- runs/<assignment_id>/
        |   |-- packet.json        immutable Task Packet
        |   |-- verification.json  verify run output
        |   `-- receipt.json       sealed receipt (the only success evidence)
        `-- <change-id>/
            |-- plan.json          latest plan revision snapshot
            `-- results/           result envelopes (audit)
```

There is no `prd-<feature>.json` on the active path. Historical `prd.json`
files in business projects are inert; new sessions must not use them to judge
completion.

## Installation

Baton ships as the `dsh-openspec-baton` DSH bundle. Installing the bundle:

- mounts `skills/` so this `baton` skill is available.

The skill drives `<baton-root>/scripts/session_control.py`; Python 3.10+ must be
reachable as `python3` / `python`, or set `BATON_PYTHON` to the interpreter you
want. No background process or manual tool registration is required.

## Typical usage

```
(the change exists at openspec/changes/add-login/ with proposal.md + tasks.md,
 and the user approved it)

run baton on /path/to/project add-login
  -> session_control.py actions
  -> for each dispatch: subagent(prompt, cwd) -> bind
  -> completion notice -> reconcile (sealed receipt) -> next batch
  -> repeat until a `complete` action is returned
```

Check progress at any time with
`session_control.py /path/to/project add-login status`.

## FAQ

- **Frontier empty**: check `tasks.md` - are the target task's section
  predecessors all `[x]`? Is the task itself unchecked?
- **An attempt is `unknown`**: the subagent finished without a sealed receipt.
  Reconcile surfaced a `recover` action; retry or repair, never assume
  success.
- **A `resolve_merge` action appeared**: two assignments touched overlapping
  files; the conflict is preserved in the integration worktree. Resolve it in
  a subagent, then reconcile again.
- **`capacity > 5` rejected**: the controller caps concurrency at 5.
- **Sealed receipt != semantic correctness**: a sealed receipt binds the
  packet hash, a clean advanced HEAD, and exit-0 verification commands into a
  JSON file. It is integrity evidence produced by `receipt.py`, not a
  cryptographic proof that a subagent cannot forge local files - pass strong
  `verification_commands` and still review destructive outcomes.
- **Per-task verification**: verification is change-wide via the `--verify`
  CLI flag; `tasks.md` carries no per-task override.
- **tasks.md changed mid-run**: fine - the next `actions` call records a new
  plan revision; in-flight packets stay immutable.
