"""DAG + frontier + topological sort — the core logic of the baton bridge.

frontier definition (authoritative):
    status == "ready" AND every ticket referenced by blocked_by has
    status == "resolved".

This is the basis of the "only forward frontier tickets" strategy —
blocked tickets never enter prd.json, so ralph's linear priority naturally
cannot reach a ticket whose dependencies are not ready. This is how the DAG
problem is solved.
"""

from __future__ import annotations

from _ticket import Ticket


def compute_frontier(tickets: list[Ticket]) -> list[Ticket]:
    """Compute the frontier: ready + all blockers resolved.

    Args:
        tickets: all tickets (sorted by num or unsorted, both fine).

    Returns:
        frontier ticket list (unsorted; caller topo_sorts as needed).
    """
    resolved_nums = {t.num for t in tickets if t.status == "resolved"}
    frontier = [
        t
        for t in tickets
        if t.status == "ready"
        # every number in blocked_by must be resolved (empty set is a subset
        # of any set, i.e. no blockers — OK)
        and set(t.blocked_by) <= resolved_nums
    ]
    return frontier


def topo_sort(tickets: list[Ticket]) -> list[Ticket]:
    """Topologically sort a set of tickets by their blocked_by relations.

    Constraints:
    - Only dependencies internal to the passed-in batch are sorted (external
      dependencies are treated as satisfied, because the frontier already
      guarantees they are resolved).
    - Within the same layer (no mutual dependencies), sort by num ascending
      so the output is stable and predictable.
    - Returns a new list; the input is not mutated.

    Args:
        tickets: tickets to sort (typically the output of compute_frontier).

    Returns:
        The ticket list arranged in topological order.
    """
    if not tickets:
        return []

    by_num = {t.num: t for t in tickets}
    nums = set(by_num.keys())

    # In-degree: only count blockers that are "in this batch"
    in_degree: dict[int, int] = {}
    deps: dict[int, list[int]] = {t.num: [] for t in tickets}
    for t in tickets:
        local_deps = [d for d in t.blocked_by if d in nums]
        deps[t.num] = local_deps
        in_degree[t.num] = len(local_deps)

    # Kahn's algorithm; use a sorted list as the queue so that within the
    # same layer we go in ascending num order
    result: list[Ticket] = []
    available = sorted(n for n in nums if in_degree[n] == 0)

    while available:
        n = available.pop(0)
        result.append(by_num[n])
        # find every ticket that depends on n and decrement its in-degree
        newly_available = []
        for other_num in nums:
            if other_num == n:
                continue
            if n in deps[other_num] and in_degree[other_num] > 0:
                in_degree[other_num] -= 1
                if in_degree[other_num] == 0:
                    newly_available.append(other_num)
        # insert the newly unlocked nums into available, keeping it sorted
        if newly_available:
            available.extend(newly_available)
            available.sort()

    # Defensive: if result is shorter than tickets, there is a cycle (a DAG
    # should not have one)
    if len(result) != len(tickets):
        sorted_nums = sorted(n for n in nums if n not in {t.num for t in result})
        raise ValueError(f"ticket dependency graph has a cycle, cannot topologically sort; stuck ticket numbers: {sorted_nums}")

    return result
