"""Dependency-driven scheduling on working days (Mon-Fri)."""
from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import date, timedelta
from typing import Any, Iterable, Optional


def is_workday(d: date) -> bool:
    return d.weekday() < 5


def next_workday(d: date) -> date:
    while not is_workday(d):
        d += timedelta(days=1)
    return d


def add_workdays(start: date, n: int) -> date:
    """The date ``n`` working days after ``start`` (``start`` itself must be a workday)."""
    d = start
    while n > 0:
        d += timedelta(days=1)
        if is_workday(d):
            n -= 1
    return d


def task_duration_days(metadata: dict, estimated_hours: Optional[float]) -> int:
    days = metadata.get("duration_days") if isinstance(metadata, dict) else None
    if isinstance(days, (int, float)) and days > 0:
        return int(math.ceil(days))
    if estimated_hours:
        return max(1, int(math.ceil(float(estimated_hours) / 8)))
    return 1


class CycleError(ValueError):
    pass


def topo_order(task_ids: Iterable, edges: Iterable[tuple]) -> list:
    """Kahn's algorithm; ``edges`` are (task, depends_on). Raises CycleError on a cycle."""
    ids = list(task_ids)
    known = set(ids)
    indeg = {t: 0 for t in ids}
    children = defaultdict(list)
    for task, dep in edges:
        if task in known and dep in known:
            indeg[task] += 1
            children[dep].append(task)
    queue = deque(t for t in ids if indeg[t] == 0)
    order = []
    while queue:
        t = queue.popleft()
        order.append(t)
        for c in children[t]:
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)
    if len(order) != len(ids):
        raise CycleError("Task dependencies contain a cycle")
    return order


def schedule(
    tasks: list[dict],
    edges: list[tuple],
    project_start: date,
    *,
    keep_existing: bool = False,
) -> dict[Any, tuple[date, date]]:
    """
    Compute (start, end) for every task. ``tasks`` items need id, duration, status,
    start_date, due_date. Done tasks and (with keep_existing) already-dated tasks keep
    their dates; everything else starts as soon as all its dependencies have finished.
    """
    by_id = {t["id"]: t for t in tasks}
    deps = defaultdict(list)
    for task, dep in edges:
        if task in by_id and dep in by_id:
            deps[task].append(dep)
    plan: dict[Any, tuple[date, date]] = {}
    base = next_workday(project_start)
    for tid in topo_order(by_id.keys(), edges):
        t = by_id[tid]
        fixed = t.get("start_date") and t.get("due_date") and (keep_existing or t.get("status") == "done")
        if fixed:
            plan[tid] = (t["start_date"], t["due_date"])
            continue
        start = base
        for d in deps[tid]:
            start = max(start, next_workday(plan[d][1] + timedelta(days=1)))
        plan[tid] = (start, add_workdays(start, max(1, t["duration"]) - 1))
    return plan


def creates_cycle(edges: list[tuple], task_id, depends_on) -> bool:
    """Would adding task_id -> depends_on close a loop? (Is task_id reachable from depends_on?)"""
    graph = defaultdict(list)
    for t, d in edges:
        graph[t].append(d)
    seen, stack = set(), [depends_on]
    while stack:
        node = stack.pop()
        if node == task_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph[node])
    return False
