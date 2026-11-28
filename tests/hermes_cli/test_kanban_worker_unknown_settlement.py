"""An owner-side unknown receipt still settles from the exact managed-worker result."""
import json
import sqlite3
import sys
from types import SimpleNamespace


def test_unknown_receipt_uses_exact_worker_result(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_bootstrap", SimpleNamespace())
    from hermes_cli.kanban_worker_client import _exit_code

    db = tmp_path / "board.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT)")
    conn.execute(
        "INSERT INTO task_events(task_id, run_id, kind, payload) VALUES (?, ?, 'worker_result', ?)",
        ("t_cross", 7, json.dumps({"claim_lock": "host:1", "exit_code": 1,
                                    "last_output": "kanban_skills_unavailable"})),
    )
    conn.commit()
    conn.close()

    code = _exit_code(
        {"status": "unknown"},
        {"task_id": "t_cross", "run_id": 7, "claim_lock": "host:1"},
        db,
    )

    assert code == 1
