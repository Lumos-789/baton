"""test_frontier.py — DAG / frontier / topological-sort tests.

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

from _frontier import compute_frontier, topo_sort  # noqa: E402
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


class TestTopoSort:
    def test_linear_chain(self):
        """Linear chain: A blocked_by B, B blocked_by C → [C, B, A]."""
        from _ticket import Ticket

        a = Ticket(num=1, title="A", what_to_build="", blocked_by=[2], status="ready")
        b = Ticket(num=2, title="B", what_to_build="", blocked_by=[3], status="ready")
        c = Ticket(num=3, title="C", what_to_build="", blocked_by=[], status="ready")
        result = topo_sort([a, b, c])
        assert [t.num for t in result] == [3, 2, 1]

    def test_no_deps_sorted_by_num(self):
        """Tickets with no dependencies are sorted by num ascending."""
        from _ticket import Ticket

        a = Ticket(num=3, title="A", what_to_build="", status="ready")
        b = Ticket(num=1, title="B", what_to_build="", status="ready")
        c = Ticket(num=2, title="C", what_to_build="", status="ready")
        result = topo_sort([a, b, c])
        assert [t.num for t in result] == [1, 2, 3]

    def test_diamond(self):
        """Diamond dependency: 04 depends on 02 and 03, both 02/03 depend on 01.
        With the whole frontier ready, topo_sort yields [01 → 02,03 → 04] (02/03 are in the same layer, ordered by num)."""
        from _ticket import Ticket

        t1 = Ticket(num=1, title="t1", what_to_build="", status="ready")
        t2 = Ticket(num=2, title="t2", what_to_build="", blocked_by=[1], status="ready")
        t3 = Ticket(num=3, title="t3", what_to_build="", blocked_by=[1], status="ready")
        t4 = Ticket(num=4, title="t4", what_to_build="", blocked_by=[2, 3], status="ready")
        result = topo_sort([t1, t2, t3, t4])
        nums = [t.num for t in result]
        assert nums[0] == 1
        assert nums[-1] == 4
        # Either 2 or 3 may come first, but both must come after 1 and before 4.
        assert set(nums[1:3]) == {2, 3}
        assert nums.index(2) < nums.index(4)
        assert nums.index(3) < nums.index(4)

    def test_empty(self):
        assert topo_sort([]) == []

    def test_preserves_input(self):
        """topo_sort does not mutate the input list."""
        from _ticket import Ticket

        a = Ticket(num=1, title="A", what_to_build="", status="ready")
        b = Ticket(num=2, title="B", what_to_build="", blocked_by=[1], status="ready")
        original = [b, a]
        result = topo_sort(original)
        assert [t.num for t in original] == [2, 1]  # input order unchanged
        assert [t.num for t in result] == [1, 2]


class TestFrontierOnFixture:
    def test_full_flow(self):
        """End-to-end: parse → frontier → topo_sort."""
        tickets = parse_tasks_md(FIXTURES)
        f = compute_frontier(tickets)
        ordered = topo_sort(f)
        # frontier = {2.1, 2.2}; both depend only on 1.1 with no inter-dependencies → ascending by num.
        assert [t.num for t in ordered] == [201, 202]
