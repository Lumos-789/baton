"""test_frontier.py — DAG / frontier tests.

Fixture DAG structure (see tests/fixtures/sample-change/tasks.md):

    01 (resolved) ─┬─→ 02 (ready) ─→ 03 (ready)
                   └─→ 04 (ready)

    05 (draft)

So frontier = {02, 04} (each depends only on 01, which is resolved).
03 is blocked by 02 (02 is still ready, not resolved), so it is not in the frontier.
05 is draft, so it is not in the frontier.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _frontier import compute_frontier  # noqa: E402
from _ticket import parse_tasks_md  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "sample-change" / "tasks.md"


class TestComputeFrontier:
    def test_frontier_contains_parallel_section_only(self):
        """1.1 resolved; 2.1/2.2 unblocked; 3.1 blocked by the whole section 2."""
        tickets = parse_tasks_md(FIXTURES)
        f = compute_frontier(tickets)
        nums = sorted(t.num for t in f)
        assert nums == [201, 202]

    def test_frontier_excludes_blocked(self):
        tickets = parse_tasks_md(FIXTURES)
        f = compute_frontier(tickets)
        assert 301 not in {t.num for t in f}

    def test_frontier_excludes_resolved(self):
        tickets = parse_tasks_md(FIXTURES)
        f = compute_frontier(tickets)
        assert 101 not in {t.num for t in f}

    def test_frontier_excludes_in_progress(self):
        """Construct an in-progress ticket and confirm it is not in the frontier."""
        tickets = parse_tasks_md(FIXTURES)
        # Mark 2.1 as in-progress (simulating a claimed assignment).
        tickets[0].status = "in-progress"  # 2.1
        f = compute_frontier(tickets)
        assert 201 not in {t.num for t in f}

    def test_empty_input(self):
        assert compute_frontier([]) == []
