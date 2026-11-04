# Tasks: parallel-wave-sections

## 1. Spec doc first (authoritative format)

- [ ] 1.1 Extend `docs/ticket-format.md`: wave tag syntax (`[[wave:<id>]]` trailing the section title, id charset `A-Za-z0-9_-`, one tag per header), wave-group dependency semantics (blocked_by = earlier sections outside own group), contiguity validation rule, and a tagged/untagged side-by-side example
- [ ] 1.2 Add the malformed-tag rejection rule to the `TasksMarkdownError` list in `docs/ticket-format.md` (doubled tag, empty id, disallowed charset, residual tag in title)

## 2. Parser implementation (`scripts/_ticket.py`)

- [ ] 2.1 Extend `_SECTION_RE` with the optional trailing `[[wave:<id>]]` capture; strip the tag from the section title and keep file-order section walk intact
- [ ] 2.2 Reject malformed tags at parse time: empty id, disallowed charset, doubled/misplaced tags (residual `[[wave:` substring surviving in a title) — each error names the offending line
- [ ] 2.3 Make `blocked_by_section` wave-aware: emit `blocked_by` as the union of tasks in earlier sections minus tasks of earlier sections sharing the same wave id; unmarked sections behave exactly as today
- [ ] 2.4 Add contiguity validation: non-contiguous same-tag groups raise `TasksMarkdownError` listing the wave id and both section positions

## 3. Tests

- [ ] 3.1 Unit: tagged sibling sections (7 unmarked, 8–9 `[[wave:2]]`, 10 unmarked) → sections 8/9 blocked only by 7; section 10 blocked by 7+8+9; frontier after resolving 7 contains tasks from both 8 and 9
- [ ] 3.2 Back-compat golden: a real unmarked tasks.md (use `tests/fixtures/sample-change/tasks.md`) produces byte-identical `blocked_by` and identical `_tickets_hash` before/after the change
- [ ] 3.3 Error paths: non-contiguous group, empty wave id, doubled tag, tag with disallowed characters — each raises `TasksMarkdownError` with the offending line/section in the message
- [ ] 3.4 End-to-end through `_frontier.py`: with capacity-style claiming (frontier + topo_sort), tasks from two tagged sections interleave in one wave and no cycle error fires

## 4. Docs follow code

- [ ] 4.1 Update AGENTS.md glossary entry ("section order defines the DAG" → wave-aware wording: unmarked sections serialize by file order; tagged contiguous sections form one parallel wave) and the Test Fixtures note if fixtures gain a tagged case
- [ ] 4.2 Update SKILL.md capacity/parallelism note: parallelism is bounded by min(session capacity, widest frontier) and wave tags widen the frontier

## 5. Verification

- [ ] 5.1 `python3 -m pytest` green; `./tests/regression.sh` from repo root all stages green (docs-vs-code consistency stage included)
- [ ] 5.2 Smoke on a live plan copy: run `parse_tasks_md` against the production project's `refactor-split-large-files-3/tasks.md` with §8–§10 hypothetically tagged (scratch copy) and confirm the frontier widens from 3 to 10 tickets; do not touch the live run
