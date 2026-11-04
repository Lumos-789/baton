# Proposal: parallel-wave-sections

## Why

The section-order DAG serializes sections unconditionally: every task in
section N is blocked by **all** tasks in **every** earlier section
(`_ticket.py` → `docs/ticket-format.md`). Real-world `tasks.md` files are
mostly sectioned by **work area** (backend/eval, backend root, tests,
frontend, skills), not by dependency phase — those areas are genuinely
independent, yet the rule forces one-section-at-a-time execution.

Triggering run (evidence, 2026-08-19/20): a private production project's
change `refactor-split-large-files-3` — 10 sections executed as 10 serial waves of
width ≤ 3, while session capacity had just been raised 3 → 5 (commit 8552311)
precisely to go faster. The raise is structurally unusable whenever section
width < capacity: remaining waves were 3 / 3 / 4 wide, so 5 slots never
saturated. The only workaround today is renumbering tasks into one section,
which destroys ticket identity mid-run (num is the durable assignment key)
and erases the human-readable phase structure.

## What Changes

- tasks.md section headers gain an **optional wave tag**:
  `## 8. P1 · tests [[wave:2]]`. Sections sharing a wave tag form one
  parallel group: each task's `blocked_by` becomes the union of tasks in all
  **earlier sections outside its group** (instead of every earlier section).
  Sections inside the same group do not block each other.
- **Back-compat**: files without any tag parse byte-for-byte as today
  (section-order barrier, identical `blocked_by`, unchanged plan hashes).
- **Validation** (`TasksMarkdownError`): a wave tag must appear on
  contiguous sections in file order (non-contiguous same tag would create
  dependency cycles); malformed tags are rejected with the offending line.
- Spec doc `docs/ticket-format.md` dependency-semantics section extended
  (authoritative source stays ahead of the parser); AGENTS.md glossary line
  "section order defines the DAG" updated to wave-aware wording.
- Optional observability follow-up: `ticket_board.py` renders wave groups as
  one lane (P2, may land separately).

## Capabilities

### New Capabilities
- `ticket-dag-waves`: wave-tag parsing + dependency derivation + validation
  semantics for tasks.md (the DAG declaration surface of baton's plan face).

### Modified Capabilities

(none — `openspec/specs/` is empty in this repo; this is the first
capability spec.)

## Impact

- `scripts/_ticket.py` — section header regex accepts `[[wave:<id>]]`;
  `blocked_by_section` derivation becomes wave-aware; new validations.
- `scripts/_frontier.py`, `scripts/orchestration/*` — **no change**
  (frontier/topo-sort are generic over `blocked_by`; plan revisions and the
  immutable-packet rule pick the new deps up automatically on the next
  `actions()` sync).
- `docs/ticket-format.md` — spec increment (parsing rules, mapping,
  dependency semantics, validation errors).
- `tests/` — parser unit tests (tagged file, back-compat file, contiguity
  error, malformed tag error) + `tests/regression.sh` must stay green.
- `AGENTS.md` glossary + `SKILL.md` capacity note — wording sync (docs
  follow code).
- Zero impact on in-flight runs: live plans re-sync from tasks.md edits as
  new revisions; in-flight assignments keep immutable packets.
