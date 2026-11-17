"""Concurrency regression for durable Kanban task idempotency."""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def test_concurrent_create_with_same_idempotency_key_returns_one_task(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    original_write_txn = kb.write_txn
    barrier = threading.Barrier(2)

    @contextlib.contextmanager
    def synchronized_write_txn(conn, *, allow_nested=False):
        barrier.wait(timeout=5)
        with original_write_txn(conn, allow_nested=allow_nested):
            yield conn

    monkeypatch.setattr(kb, "write_txn", synchronized_write_txn)
    ids: list[str] = []
    errors: list[BaseException] = []

    def create() -> None:
        conn = kbc.connect()
        try:
            ids.append(kb.create_task(
                conn,
                title="same logical work",
                assignee="default",
                idempotency_key="same-key",
            ))
        except BaseException as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert len(ids) == 2
    assert len(set(ids)) == 1
    with kbc.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = 'same-key'"
        ).fetchall()
        assert len(rows) == 1