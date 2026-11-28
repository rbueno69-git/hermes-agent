"""Deterministic state-machine simulations for Kanban orchestration liveness."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def sim_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "unknown")
    monkeypatch.setattr(kbd, "count_running_tasks_other_boards", lambda board=None: 0)
    monkeypatch.setattr(
        kbd, "count_running_tasks_by_assignee_other_boards", lambda board=None: {},
    )
    return home


def _spawn(task, workspace, board=None):
    return None


def _dispatch(conn, cap):
    return kbd.dispatch_once(
        conn,
        spawn_fn=_spawn,
        max_in_progress=cap,
        stale_timeout_seconds=0,
        reconcile_orphans=False,
    )


def _assert_invariants(conn, cap, *, after_dispatch=False):
    tasks = conn.execute(
        "SELECT id, status, claim_lock, current_run_id FROM tasks ORDER BY id"
    ).fetchall()
    running = [row for row in tasks if row["status"] == "running"]
    assert len(running) <= cap
    for row in tasks:
        open_runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND ended_at IS NULL",
            (row["id"],),
        ).fetchone()[0]
        if row["status"] == "running":
            assert row["claim_lock"] is not None
            assert row["current_run_id"] is not None
            assert open_runs == 1
        else:
            assert open_runs == 0
        if row["status"] in {"ready", "running"}:
            assert kb.unsatisfied_parents(conn, row["id"]) == []
        if row["status"] == "todo":
            assert kb.unsatisfied_parents(conn, row["id"])
    if after_dispatch and len(running) < cap:
        assert not any(row["status"] == "ready" for row in tasks)
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("cap,backlog", [(1, 1), (2, 5), (4, 9)])
def test_model_fills_capacity_without_underfill(sim_home, cap, backlog):
    with kbc.connect() as conn:
        for i in range(backlog):
            kb.create_task(conn, title=f"root-{i}", assignee="worker", priority=i)
        _dispatch(conn, cap)
        _assert_invariants(conn, cap, after_dispatch=True)
        assert kbd.count_running_tasks(conn) == min(cap, backlog)


@pytest.mark.parametrize("width", [1, 4, 16])
def test_model_parent_completion_releases_full_fanout(sim_home, width):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        children = [
            kb.create_task(
                conn, title=f"child-{i}", assignee="worker", parents=[parent], priority=i,
            )
            for i in range(width)
        ]
        assert all(kb.get_task(conn, child).status == "todo" for child in children)
        assert kb.complete_task(conn, parent, result="done", summary="parent complete")
        assert all(kb.get_task(conn, child).status == "ready" for child in children)
        _dispatch(conn, min(width, 4))
        _assert_invariants(conn, min(width, 4), after_dispatch=True)


def test_model_fanin_waits_for_every_parent(sim_home):
    with kbc.connect() as conn:
        parents = [
            kb.create_task(conn, title=f"parent-{i}", assignee="worker")
            for i in range(4)
        ]
        child = kb.create_task(
            conn, title="fanin", assignee="worker", parents=parents,
        )
        for parent in parents[:-1]:
            assert kb.complete_task(conn, parent, result="done", summary="complete")
            assert kb.get_task(conn, child).status == "todo"
        assert kb.complete_task(conn, parents[-1], result="done", summary="complete")
        assert kb.get_task(conn, child).status == "ready"
        _dispatch(conn, 2)
        _assert_invariants(conn, 2, after_dispatch=True)


@pytest.mark.parametrize("seed", [0, 1, 2, 0xC0FFEE])
@pytest.mark.parametrize("cap", [1, 2, 4])
def test_model_random_lifecycle_sequences(sim_home, seed, cap):
    rng = random.Random(seed)
    with kbc.connect() as conn:
        task_ids: list[str] = []
        for step in range(100):
            rows = conn.execute("SELECT id, status FROM tasks ORDER BY created_at, id").fetchall()
            running = [row["id"] for row in rows if row["status"] == "running"]
            action = rng.choices(
                ["create", "child", "dispatch", "complete"],
                weights=[30, 25, 25, 20],
                k=1,
            )[0]
            if action == "create" or not task_ids:
                task_ids.append(kb.create_task(
                    conn, title=f"root-{step}", assignee="worker", priority=step,
                ))
            elif action == "child":
                candidates = [
                    row["id"] for row in rows
                    if row["status"] not in {"archived"}
                ]
                if candidates:
                    parent = rng.choice(candidates)
                    task_ids.append(kb.create_task(
                        conn,
                        title=f"child-{step}",
                        assignee="worker",
                        parents=[parent],
                        priority=step,
                    ))
            elif action == "dispatch":
                _dispatch(conn, cap)
                _assert_invariants(conn, cap, after_dispatch=True)
                continue
            elif running:
                task_id = rng.choice(running)
                task = kb.get_task(conn, task_id)
                assert kb.complete_task(
                    conn,
                    task_id,
                    result=f"done-{step}",
                    summary="simulated completion",
                    expected_run_id=task.current_run_id,
                )
            _assert_invariants(conn, cap)
        _dispatch(conn, cap)
        _assert_invariants(conn, cap, after_dispatch=True)
