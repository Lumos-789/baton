"""test_ticket.py — unit tests for the OpenSpec tasks.md parser.

Covers:
- section headers (## N. title) and task lines (- [ ] N.M text / bullets /
  checkbox variants)
- composite nums (N*100+M) and the display task_id
- statuses: unchecked → ready, checked → resolved
- section-order DAG: task N.M blocked by all tasks of earlier sections
- error paths: missing file, no sections, no tasks, duplicate ids, misfiled ids
- check_task: flip [ ]→[x], idempotency, unknown task
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _ticket import (  # noqa: E402
    TasksMarkdownError,
    Ticket,
    check_task,
    parse_tasks_md,
    task_num,
    validate_change_id,
)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "tasks.md"
    path.write_text(body, encoding="utf-8")
    return path


SAMPLE = """# Tasks: demo

## 1. Seed

- [x] 1.1 seed the data
- [ ] 1.2 second seed

## 2. Build

- [ ] 2.1 first build
* [X] 2.2 second build done
+ [ ] 2.3 third build

## 3. 收尾

- [ ] 3.1 final task
"""


def test_parse_maps_sections_tasks_statuses(tmp_path):
    tickets = parse_tasks_md(_write(tmp_path, SAMPLE))
    by_id = {t.task_id: t for t in tickets}
    assert sorted(by_id) == ["1.1", "1.2", "2.1", "2.2", "2.3", "3.1"]
    assert by_id["1.1"].status == "resolved"
    assert by_id["2.1"].status == "ready"
    assert by_id["2.2"].status == "resolved"  # [X] tolerated
    assert by_id["2.1"].title == "first build"
    assert by_id["2.1"].what_to_build == "first build"
    assert by_id["3.1"].section == "收尾"


def test_composite_num_is_reversible(tmp_path):
    tickets = parse_tasks_md(_write(tmp_path, SAMPLE))
    for t in tickets:
        sec, item = t.task_id.split(".")
        assert t.num == task_num(int(sec), int(item))


def test_section_order_dag(tmp_path):
    tickets = parse_tasks_md(_write(tmp_path, SAMPLE))
    by_id = {t.task_id: t for t in tickets}
    assert by_id["1.1"].blocked_by == []
    assert by_id["1.2"].blocked_by == []
    assert by_id["2.1"].blocked_by == [101, 102]
    assert by_id["2.3"].blocked_by == [101, 102]
    assert by_id["3.1"].blocked_by == [101, 102, 201, 202, 203]


def test_criteria_is_the_task_text(tmp_path):
    tickets = parse_tasks_md(_write(tmp_path, SAMPLE))
    first = tickets[0]
    assert first.criteria == [(True, "seed the data")]


def test_missing_file_raises_with_creation_hint(tmp_path):
    with pytest.raises(TasksMarkdownError, match="proposal.md \\+ tasks.md"):
        parse_tasks_md(tmp_path / "tasks.md")


def test_no_sections_raises(tmp_path):
    with pytest.raises(TasksMarkdownError, match="no `## N.` sections"):
        parse_tasks_md(_write(tmp_path, "- [ ] 1.1 orphan task\n"))


def test_no_tasks_raises(tmp_path):
    with pytest.raises(TasksMarkdownError, match="no `- \\[ \\] N.M` tasks"):
        parse_tasks_md(_write(tmp_path, "## 1. Empty\n\nnothing here\n"))


def test_duplicate_task_id_raises(tmp_path):
    body = "## 1. A\n\n- [ ] 1.1 one\n- [ ] 1.1 again\n"
    with pytest.raises(TasksMarkdownError, match="duplicate task id 1.1"):
        parse_tasks_md(_write(tmp_path, body))


def test_task_under_wrong_section_raises(tmp_path):
    body = "## 1. A\n\n- [ ] 1.1 fine\n\n## 2. B\n\n- [ ] 1.2 misfiled\n"
    with pytest.raises(TasksMarkdownError, match="listed under section 2"):
        parse_tasks_md(_write(tmp_path, body))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("## 100. A\n\n- [ ] 100.1 big section\n", "task id 100.1 out of supported range"),
        ("## 1. A\n\n- [ ] 1.100 big item\n", "task id 1.100 out of supported range"),
    ],
)
def test_out_of_range_task_id_raises_tasks_error(tmp_path, body, message):
    # docs/ticket-format.md names TasksMarkdownError the validation error; a
    # raw ValueError from task_num() escapes every caller as a traceback.
    with pytest.raises(TasksMarkdownError, match=message):
        parse_tasks_md(_write(tmp_path, body))


def test_tasks_outside_any_section_are_ignored(tmp_path):
    body = "- [ ] 9.9 stray before any section\n\n## 1. A\n\n- [ ] 1.1 real\n"
    tickets = parse_tasks_md(_write(tmp_path, body))
    assert [t.task_id for t in tickets] == ["1.1"]


def test_check_task_flips_and_is_idempotent(tmp_path):
    path = _write(tmp_path, SAMPLE)
    check_task(path, 201)
    check_task(path, 201)  # second call must not fail
    statuses = {t.task_id: t.status for t in parse_tasks_md(path)}
    assert statuses["2.1"] == "resolved"
    assert statuses["2.3"] == "ready"


def test_check_task_unknown_num_raises(tmp_path):
    path = _write(tmp_path, SAMPLE)
    with pytest.raises(TasksMarkdownError, match="task 4.2 not found"):
        check_task(path, 402)


def test_check_task_preserves_surrounding_lines(tmp_path):
    path = _write(tmp_path, SAMPLE)
    before = path.read_text(encoding="utf-8").splitlines()
    check_task(path, 301)
    after = path.read_text(encoding="utf-8").splitlines()
    assert len(before) == len(after)
    changed = [(b, a) for b, a in zip(before, after) if b != a]
    assert changed == [("- [ ] 3.1 final task", "- [x] 3.1 final task")]


def test_check_task_preserves_crlf_line_endings(tmp_path):
    # A CRLF tasks.md (Windows checkout) must keep its line endings: rewriting
    # the whole file to LF turns one checkbox into a whole-file diff.
    path = tmp_path / "tasks.md"
    path.write_bytes(b"# Tasks: demo\r\n\r\n## 1. A\r\n\r\n- [ ] 1.1 todo\r\n")
    check_task(path, 101)
    assert path.read_bytes() == b"# Tasks: demo\r\n\r\n## 1. A\r\n\r\n- [x] 1.1 todo\r\n"


@pytest.mark.parametrize("checkbox", ["- [  ] ", "* [ ] ", "+ [ ] ", "-\t[ ] "])
def test_check_task_matches_the_checkbox_forms_the_parser_accepts(tmp_path, checkbox):
    # parse_tasks_md tolerates extra spaces inside the brackets and any bullet;
    # the write-back must agree or a parsed ticket can never be checked off.
    path = tmp_path / "tasks.md"
    path.write_text(f"## 1. A\n\n{checkbox}1.1 todo\n", encoding="utf-8")
    assert [t.task_id for t in parse_tasks_md(path)] == ["1.1"]
    check_task(path, 101)
    assert [t.status for t in parse_tasks_md(path)] == ["resolved"]
    check_task(path, 101)  # idempotent through the same tolerant match


def test_ticket_dataclass_defaults():
    ticket = Ticket(num=101, title="x")
    assert ticket.status == "ready"
    assert ticket.blocked_by == []
    assert ticket.task_id == "1.1"


@pytest.mark.parametrize("change_id", ["", ".", "..", "../escape", "a/b", "a\\b", "a."])
def test_validate_change_id_rejects_unsafe_segments(change_id):
    with pytest.raises(ValueError):
        validate_change_id(change_id)


@pytest.mark.parametrize("change_id", ["feature", "add-login", "wave_2.v1"])
def test_validate_change_id_accepts_single_segments(change_id):
    assert validate_change_id(change_id) == change_id
