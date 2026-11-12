"""Cross-profile Kanban admissions bind execution to the assignee's owner store."""
import asyncio
from contextlib import closing
import sys
from types import SimpleNamespace

import pytest


def _claimed_named_task(tmp_path, monkeypatch, *, skills=None, assign_after_create=False):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    board_root = tmp_path / "boards"
    default_home = tmp_path / "default"
    named_home = tmp_path / "profiles" / "beta"
    workspace = tmp_path / "workspace"
    for path in (default_home, named_home, workspace):
        path.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(board_root))
    monkeypatch.setenv("HERMES_HOME", str(named_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD", raising=False)
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda name: str(default_home if name == "default" else named_home),
    )
    kb.create_board("owned")
    with closing(connect(board="owned")) as conn:
        task_id = kb.create_task(
            conn,
            title="cross-profile",
            assignee="default" if assign_after_create else "beta",
            workspace_kind="dir",
            workspace_path=str(workspace),
            session_id="default-origin-session",
            skills=skills,
        )
        if assign_after_create:
            assert kb.assign_task(conn, task_id, "beta")
        task = kb.claim_task(conn, task_id)
        assert task is not None
        board_db = conn.execute("PRAGMA database_list").fetchone()[2]
    return task_id, task, board_db, named_home


def _connection(tmp_path, named_home, monkeypatch, *, receipt):
    from gateway.session_contract import SessionActor

    monkeypatch.setitem(sys.modules, "hermes_bootstrap", SimpleNamespace())
    config = {"model": {"default": "loop-model", "provider": "custom"},
              "platform_toolsets": {"cli": ["terminal"]}}
    monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(
        _load_gateway_config=lambda: config,
        _resolve_gateway_model=lambda _config: "loop-model",
    ))
    monkeypatch.setattr(
        "gateway.session_local.create_local_session",
        lambda *a, **k: SimpleNamespace(session_id="named-owner-session", profile_id=str(named_home)),
    )
    calls = []

    async def submit(actor, submission, **kwargs):
        calls.append(("submit", submission.request_id))
        return receipt

    async def resume(ref, params):
        calls.append(("resume", ref.session_id))

    db = SimpleNamespace(get_session=lambda sid: None, db_path=str(named_home / "state.db"))
    authority = SimpleNamespace(profile_id=str(named_home), db=db, submit=submit)
    actor = SessionActor("owner", str(named_home), frozenset({"session:create"}), "transport")
    return SimpleNamespace(authority=authority, actor=actor, native_owner=True, resume=resume), calls


def test_default_origin_executes_and_settles_in_named_profile_store(tmp_path, monkeypatch):
    from hermes_cli.kanban_db_connect import connect
    from gateway import session_kanban
    from gateway.session_kanban import run_task

    monkeypatch.setattr(session_kanban, "asdict", lambda value: vars(value))
    task_id, task, _board_db, named_home = _claimed_named_task(tmp_path, monkeypatch)
    receipt = SimpleNamespace(admission_id="a", status="terminal", outcome="completed")
    connection, calls = _connection(tmp_path, named_home, monkeypatch, receipt=receipt)
    params = dict(board="owned", task_id=task_id, run_id=task.current_run_id, claim_lock=task.claim_lock)

    result = asyncio.run(run_task(connection, params))

    assert result["session_id"] == "named-owner-session"
    assert calls == [("resume", "named-owner-session"),
                     ("submit", f'kanban:["owned","{task_id}",{task.current_run_id}]')]
    with closing(connect(board="owned")) as conn:
        stored = conn.execute("SELECT session_id FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
        marker = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='owner_admitted'",
            (task_id, task.current_run_id),
        ).fetchone()[0]
    assert stored == "default-origin-session"  # notification provenance is not execution identity
    assert str(named_home / "state.db") in marker
    assert "named-owner-session" in marker


def test_default_task_reassigned_to_named_profile_uses_named_owner_store(tmp_path, monkeypatch):
    from hermes_cli.kanban_db_connect import connect
    from gateway import session_kanban
    from gateway.session_kanban import run_task

    monkeypatch.setattr(session_kanban, "asdict", lambda value: vars(value))
    task_id, task, _board_db, named_home = _claimed_named_task(
        tmp_path, monkeypatch, assign_after_create=True,
    )
    receipt = SimpleNamespace(admission_id="assigned", status="terminal", outcome="completed")
    connection, calls = _connection(tmp_path, named_home, monkeypatch, receipt=receipt)
    params = dict(board="owned", task_id=task_id, run_id=task.current_run_id, claim_lock=task.claim_lock)

    result = asyncio.run(run_task(connection, params))

    assert result["session_id"] == "named-owner-session"
    assert calls[-1] == ("submit", f'kanban:["owned","{task_id}",{task.current_run_id}]')
    with closing(connect(board="owned")) as conn:
        stored = conn.execute(
            "SELECT assignee, session_id FROM tasks WHERE id=?", (task_id,),
        ).fetchone()
        marker = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='owner_admitted'",
            (task_id, task.current_run_id),
        ).fetchone()[0]
    assert tuple(stored) == ("beta", "default-origin-session")
    assert str(named_home / "state.db") in marker


def test_missing_named_profile_skills_fail_before_owner_admission(tmp_path, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError
    from hermes_cli.kanban_db_connect import connect
    from gateway.session_kanban import run_task

    task_id, task, _board_db, named_home = _claimed_named_task(
        tmp_path, monkeypatch, skills=["skill-only-in-default"],
    )
    receipt = SimpleNamespace(admission_id="should-not-exist", status="terminal", outcome="completed")
    connection, calls = _connection(tmp_path, named_home, monkeypatch, receipt=receipt)
    params = dict(board="owned", task_id=task_id, run_id=task.current_run_id, claim_lock=task.claim_lock)

    with pytest.raises(RuntimeStoreError, match="kanban_skills_unavailable"):
        asyncio.run(run_task(connection, params))

    assert calls == []
    with closing(connect(board="owned")) as conn:
        assert conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=? AND run_id=? AND kind='owner_admitted'",
            (task_id, task.current_run_id),
        ).fetchone()[0] == 0
