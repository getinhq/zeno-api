"""Pure scheduling engine tests (no database)."""
from datetime import date

import pytest

from app.schedule import engine


def test_working_days():
    fri = date(2027, 1, 8)
    assert engine.is_workday(fri)
    assert engine.next_workday(date(2027, 1, 9)) == date(2027, 1, 11)  # Sat -> Mon
    assert engine.add_workdays(fri, 1) == date(2027, 1, 11)  # Fri + 1 workday = Mon
    assert engine.add_workdays(fri, 0) == fri


def test_chain_respects_dependencies_and_weekends():
    tasks = [
        {"id": "model", "duration": 3, "status": "todo"},
        {"id": "rig", "duration": 2, "status": "todo"},
        {"id": "anim", "duration": 1, "status": "todo"},
    ]
    edges = [("rig", "model"), ("anim", "rig")]
    plan = engine.schedule(tasks, edges, date(2027, 1, 6))  # Wednesday
    assert plan["model"] == (date(2027, 1, 6), date(2027, 1, 8))   # Wed-Fri
    assert plan["rig"] == (date(2027, 1, 11), date(2027, 1, 12))   # Mon-Tue
    assert plan["anim"] == (date(2027, 1, 13), date(2027, 1, 13))


def test_parallel_and_join():
    tasks = [{"id": x, "duration": d, "status": "todo"} for x, d in (("a", 2), ("b", 5), ("c", 1))]
    plan = engine.schedule(tasks, [("c", "a"), ("c", "b")], date(2027, 1, 4))  # Monday
    assert plan["a"][0] == plan["b"][0] == date(2027, 1, 4)
    assert plan["c"][0] == date(2027, 1, 11)  # after the longer branch (b ends Fri)


def test_keep_existing_and_done_tasks_are_pinned():
    tasks = [
        {"id": "a", "duration": 2, "status": "done", "start_date": date(2027, 2, 1), "due_date": date(2027, 2, 2)},
        {"id": "b", "duration": 1, "status": "todo", "start_date": date(2027, 3, 1), "due_date": date(2027, 3, 1)},
        {"id": "c", "duration": 1, "status": "todo"},
    ]
    plan = engine.schedule(tasks, [("c", "a")], date(2027, 1, 4))
    assert plan["a"] == (date(2027, 2, 1), date(2027, 2, 2))
    assert plan["b"][0] == date(2027, 1, 4)  # not pinned without keep_existing
    assert plan["c"][0] == date(2027, 2, 3)
    pinned = engine.schedule(tasks, [], date(2027, 1, 4), keep_existing=True)
    assert pinned["b"] == (date(2027, 3, 1), date(2027, 3, 1))


def test_cycles():
    with pytest.raises(engine.CycleError):
        engine.topo_order(["a", "b"], [("a", "b"), ("b", "a")])
    assert engine.creates_cycle([("b", "a")], "a", "b")
    assert not engine.creates_cycle([("b", "a")], "c", "b")


def test_duration_sources():
    assert engine.task_duration_days({"duration_days": 3}, None) == 3
    assert engine.task_duration_days({}, 12) == 2
    assert engine.task_duration_days({}, None) == 1
