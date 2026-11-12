"""Explicit continuation parents prevent phase tasks from being stranded behind aggregates."""
from contextlib import closing
from types import SimpleNamespace

import pytest


def _done(conn, kb, title):
    task_id = kb.create_task(conn, title=title)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
    return task_id


def test_continuation_parent_must_be_a_direct_dependency(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    with closing(connect()) as conn:
        phase = _done(conn, kb, "terminal phase")
        aggregate = kb.create_task(conn, title="aggregate", initial_status="blocked")

        with pytest.raises(ValueError, match="continuation parent must be a direct parent"):
            kb.create_task(
                conn,
                title="next phase",
                parents=[aggregate],
                continuation_parent_id=phase,
            )

        assert conn.execute("SELECT count(*) FROM tasks WHERE title='next phase'").fetchone()[0] == 0


def test_continuation_supports_multiple_parents_and_keeps_cycle_guard(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    with closing(connect()) as conn:
        phase = _done(conn, kb, "terminal phase")
        evidence = _done(conn, kb, "terminal evidence")
        child = kb.create_task(
            conn,
            title="next phase",
            parents=[phase, evidence],
            continuation_parent_id=phase,
        )

        task = kb.get_task(conn, child)
        assert task.status == "ready"
        assert task.continuation_parent_id == phase
        with pytest.raises(ValueError, match="cycle"):
            kb.link_tasks(conn, child, phase)


def test_terminal_continuation_parent_surfaces_nonterminal_extra_parent(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    with closing(connect()) as conn:
        phase = _done(conn, kb, "terminal phase")
        aggregate = kb.create_task(conn, title="aggregate", initial_status="blocked")

        child = kb.create_task(
            conn,
            title="next phase",
            parents=[phase, aggregate],
            continuation_parent_id=phase,
        )

        assert kb.get_task(conn, child).status == "triage"
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='continuation_parent_conflict' "
            "ORDER BY id DESC LIMIT 1",
            (child,),
        ).fetchone()
        assert aggregate in event[0]


def test_corrupt_continuation_link_is_triaged_before_dispatch(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    with closing(connect()) as conn:
        phase = _done(conn, kb, "terminal phase")
        aggregate = kb.create_task(conn, title="aggregate", initial_status="blocked")
        child = kb.create_task(
            conn,
            title="next phase",
            parents=[phase],
            continuation_parent_id=phase,
        )
        # Simulate a legacy/manual graph rewrite that bypassed the public unlink guard.
        with kb.write_txn(conn):
            conn.execute("DELETE FROM task_links WHERE parent_id=? AND child_id=?", (phase, child))
            conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (aggregate, child))
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (child,))

        kb.recompute_ready(conn)

        assert kb.get_task(conn, child).status == "triage"
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='continuation_link_invalid' "
            "ORDER BY id DESC LIMIT 1",
            (child,),
        ).fetchone()
        assert phase in event[0]


def test_corrupt_ready_continuation_cannot_be_claimed(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    with closing(connect()) as conn:
        phase = _done(conn, kb, "terminal phase")
        child = kb.create_task(
            conn, title="next phase", parents=[phase], continuation_parent_id=phase,
        )
        with kb.write_txn(conn):
            conn.execute("DELETE FROM task_links WHERE parent_id=? AND child_id=?", (phase, child))

        assert kb.claim_task(conn, child) is None
        assert kb.get_task(conn, child).status == "triage"


@pytest.mark.parametrize("status", ["done", "archived", "running"])
def test_claim_does_not_triage_ineligible_task_with_corrupt_continuation(
    tmp_path, monkeypatch, status,
):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    with closing(connect()) as conn:
        phase = _done(conn, kb, "terminal phase")
        child = kb.create_task(
            conn, title="next phase", parents=[phase], continuation_parent_id=phase,
        )
        if status == "running":
            assert kb.claim_task(conn, child) is not None
        else:
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, child))
        with kb.write_txn(conn):
            conn.execute("DELETE FROM task_links WHERE parent_id=? AND child_id=?", (phase, child))
        before = conn.execute(
            "SELECT status, claim_lock, current_run_id FROM tasks WHERE id=?", (child,),
        ).fetchone()
        event_count = conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=?", (child,),
        ).fetchone()[0]

        assert kb.claim_task(conn, child) is None

        after = conn.execute(
            "SELECT status, claim_lock, current_run_id FROM tasks WHERE id=?", (child,),
        ).fetchone()
        assert tuple(after) == tuple(before)
        assert after["status"] == status
        assert conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=?", (child,),
        ).fetchone()[0] == event_count


def test_corrupt_review_continuation_cannot_be_claimed(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    with closing(connect()) as conn:
        phase = _done(conn, kb, "terminal phase")
        child = kb.create_task(
            conn, title="review phase", parents=[phase], continuation_parent_id=phase,
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (child,))
            conn.execute("DELETE FROM task_links WHERE parent_id=? AND child_id=?", (phase, child))

        assert kb.claim_review_task(conn, child) is None
        assert kb.get_task(conn, child).status == "triage"


def test_declared_continuation_parent_cannot_be_hard_deleted(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    with closing(connect()) as conn:
        phase = _done(conn, kb, "terminal phase")
        kb.create_task(conn, title="next phase", parents=[phase], continuation_parent_id=phase)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (phase,))

        with pytest.raises(ValueError, match="declared continuation parent"):
            kb.delete_archived_task(conn, phase)


def test_cli_purge_reports_declared_continuation_conflict(tmp_path, monkeypatch, capsys):
    from hermes_cli import kanban
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    with closing(connect()) as conn:
        phase = _done(conn, kb, "terminal phase")
        child = kb.create_task(
            conn, title="next phase", parents=[phase], continuation_parent_id=phase,
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (phase,))

    rc = kanban._cmd_archive(SimpleNamespace(task_ids=[], purge_ids=[phase]))

    assert rc == 1
    stderr = capsys.readouterr().err
    assert phase in stderr
    assert child in stderr
    assert "continuation parent" in stderr
