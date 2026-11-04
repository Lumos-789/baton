# Delta Spec: ticket-dag-waves

## Purpose

Defines how tasks.md sections declare parallel execution waves so that
independent work areas can run concurrently under baton's session capacity,
while unmarked files keep the exact section-order DAG semantics.

## ADDED Requirements

### Requirement: Wave tag syntax

A section header MAY carry exactly one wave tag of the form `[[wave:<id>]]`
trailing the section title: `## 8. P1 · tests [[wave:2]]`. `<id>` is a
non-empty token of letters, digits, `-`, or `_`. Sections without a tag are
"unmarked" and belong to no wave group.

#### Scenario: tagged header parses

- **WHEN** tasks.md contains `## 8. P1 · tests [[wave:2]]`
- **THEN** section 8 parses with wave id `2` and the tag is stripped from
  the human-readable section title

#### Scenario: malformed tag is rejected

- **WHEN** a header carries `[[wave:]]`, `[[wave]]`, a doubled tag, or a tag
  containing characters outside the allowed set
- **THEN** parsing fails with `TasksMarkdownError` naming the offending
  line

### Requirement: Wave-group dependency semantics

All sections sharing one wave id form a single parallel group. A task's
`blocked_by` set SHALL be the union of tasks in every section that appears
earlier in file order **and is outside the task's own group**. Sections
inside the same group MUST NOT block each other; sections after the group
are unaffected (still blocked by the whole group, since every group member
appears earlier than them).

#### Scenario: two tagged sibling sections run concurrently

- **WHEN** section 8 and section 9 both carry `[[wave:2]]`, section 7 is
  fully resolved, and all of section 8 and 9 tasks are unchecked
- **THEN** the frontier contains tasks from both section 8 and section 9,
  bounded only by session capacity

#### Scenario: group still gates later sections

- **WHEN** section 8 and 9 carry `[[wave:2]]` and some of their tasks are
  unresolved
- **THEN** every task in a later section (e.g. 10) remains blocked by the
  unresolved group tasks

#### Scenario: unmarked file is byte-for-byte compatible

- **WHEN** tasks.md contains no wave tag
- **THEN** every task's `blocked_by` is identical to the pre-change
  section-order DAG (validated by an unchanged golden `blocked_by` fixture)

### Requirement: Wave-group validation

Wave groups MUST be contiguous in file order: between the first and last
section of one wave id there MUST NOT appear a section with a different tag
or no tag. Violations fail parsing with `TasksMarkdownError` (this
guarantees the derived DAG stays acyclic, because non-contiguous groups
would mutually block through the sections between them).

#### Scenario: non-contiguous group rejected

- **WHEN** sections 8 and 11 carry `[[wave:2]]` while sections 9–10 are
  unmarked or carry another tag
- **THEN** parsing fails with `TasksMarkdownError` explaining the
  contiguity rule

#### Scenario: mixed contiguous runs are legal

- **WHEN** sections 7 (unmarked), 8–9 (`[[wave:2]]`), 10 (unmarked) appear
  in that order
- **THEN** parsing succeeds; sections 8–9 are blocked by section 7 tasks
  only, and section 10 is blocked by sections 7, 8, and 9
