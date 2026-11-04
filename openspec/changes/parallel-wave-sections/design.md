# Design: parallel-wave-sections

## Context

`_ticket.py::parse_tasks_md` is the single tickets.md → tickets converter.
Section headers match `^##\s+(\d+)\s*[.\u3002]?\s*(.*?)\s*$`; dependency
derivation is 6 lines in `parse_tasks_md`: walk sections in file order,
`blocked_by_section[sec] = sorted(earlier)`, accumulate. `Ticket` carries
`blocked_by`; everything downstream (`_frontier.py`,
`session_controller.py`, plan revisions, board) is generic over that list.
See proposal.md — Why for the r3 run evidence.

Constraints that shape the design:

- tasks.md is the single ticket source; the DAG must stay **declarable in
  the file** (a controller-side override parameter would fork the truth).
- Back-compat is hard-required: baton runs against existing changes in
  other projects; an unmarked file must produce identical `blocked_by`
  (plan-revision hashing keys off ticket projections, so a silent dep
  change would trigger spurious re-plans).
- Standard library only; parser + `docs/ticket-format.md` + tests change in
  lockstep (repo discipline).

## Goals / Non-Goals

**Goals:**

- One opt-in marker that merges adjacent sections into one parallel wave,
  expressible and reviewable in tasks.md itself.
- Zero behavior change for unmarked files, proven by a golden-fixture test.
- Acyclic-by-construction semantics with a clear parse error instead of a
  topo-sort cycle crash far away.

**Non-Goals:**

- Per-task dependency syntax (`- [ ] 9.1 (needs 8.1) …`) — fine-grained,
  noisy, and unnecessary for the observed failure mode (work-area sections).
- Changing `_frontier.py`, `session_controller.py`, or the CLI surface —
  capacity already supports 5; the DAG was the only blocker.
- Renumbering semantics, write-back changes, or ticket identity changes.
- Wave rendering in `ticket_board.py` (follow-up; noted in tasks as P2).

## Decisions

1. **Marker lives on the section header line** — `## 8. Title [[wave:<id>]]`.
   - Why not a standalone comment directive (`<!-- baton: wave 2 -->`
     between sections): header-local keeps the marker adjacent to what it
     modifies, survives section reordering, and stays visible in rendered
     markdown previews as literal text instead of hiding in comments.
   - Why not a suffix on every task line: N repetitions, drift-prone.
   - Regex: extend `_SECTION_RE` with an optional trailing
   `(?:\s*\[\[wave:([A-Za-z0-9_-]+)\]\])?`; strip the tag from the captured
   title. Exactly one tag per header enforced by the regex + a doubled-tag
   sanity check (a second tag stays in the title text → rejected as
   malformed via title validation, keeping the regex simple).

2. **Wave id is an opaque token, not a number.** Letters/digits/`-`/`_`.
   Equality is the only operation (`wave:2` vs `wave:P1` equally legal);
   no ordering semantics between groups — file order remains the only
   order. Avoids implying "wave 3 runs after wave 2" as a separate concept.

3. **Derivation: group-aware accumulate.** During the file-order walk,
   maintain `earlier` as today, but when emitting `blocked_by` for section
   S with group G, subtract tasks of earlier sections that share G. Since
   groups are validated contiguous (decision 4), "earlier sections of my
   group" is exactly the group prefix — subtraction is well-defined and
   the graph stays acyclic. Unmarked sections keep the exact current
   behavior (group = a singleton of itself that matches nothing).

4. **Contiguity validation over general cycle detection.** Non-contiguous
   same-tag groups can mutually block (9 blocked by 11's group prefix,
   11 blocked by 9's) → cycle that today would only surface as a
   `topo_sort` ValueError at claim time, far from the authoring mistake.
   Rejecting at parse time with the offending wave id and both line
   numbers is strictly better. Kahn-cycle detection in `_frontier.py`
   stays as the defensive net, unchanged.

5. **`Ticket` grows no new field.** The wave tag is a section-level
   authoring concern fully lowered into `blocked_by` at parse time.
   `PlanTicket` (durable model) is untouched → no store migration, no
   revision-hash perturbation for unmarked plans (projection hashes only
   existing fields). Trade-off: the board cannot group by wave without
   re-parsing tasks.md — acceptable, board rendering is the P2 follow-up.

## Risks / Trade-offs

- [Wider frontier → more concurrent integration conflicts] → Mitigation:
  integration is already serialized under a repository-scoped lock with
  union-style conflict resolution proven on r3 (tasks.md checkboxes,
  allowlist hot lines). Cross-section tickets touched disjoint file sets
  in every observed case; conflicts remain per-file, not semantic.
- [Authors tag sections that secretly depend on each other] → Mitigation:
  docs steer usage ("tag only sections with disjoint file footprints");
  verification_commands run per-assignment in isolated worktrees, so a bad
  ordering fails that assignment loudly, not silently.
- [Marker typo silently parses as title text] → Mitigation: title
  validation rejects any residual `[[wave:…]]` substring in a parsed title
  (catches doubled/misplaced tags) with a line-anchored error message.
- [Unmarked-plan hash drift] → Mitigation: golden fixture asserts
  `blocked_by` equality and `stable_hash` equality against pre-change
  output for a real unmarked tasks.md.

## Migration Plan

None needed: marker is opt-in per tasks.md; existing plans re-sync as new
revisions only when their file actually gains a tag (hash-stable
otherwise). Rollback = drop the tags from the file.

## Open Questions

- Board wave-lane rendering details (colors, collapse) — deferrable to the
  P2 task, does not affect parser semantics.
