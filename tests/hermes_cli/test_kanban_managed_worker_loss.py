"""A lost managed worker must be DISPOSED OF on the board, within one dispatcher tick.

Regression for a card left ``running`` after its managed worker died in the ``worker.adopt``
handshake (claim expired, every recorded pid dead, the owner gateway alive and its admission
settled ``terminal/interrupted``). These tests pin the disposition the dispatcher owes every
lost attempt:

  A1  a killed worker under a live claim is booked ``crashed`` within one tick;
  A2  an elapsed ``claim_expires`` never leaves the card ``running`` after one tick, even
      with no heartbeat and no pid (and even behind a settled owner receipt);
  A3  every such booking increments ``consecutive_failures``, so ``kanban.failure_limit``
      covers this outcome class and the card blocks at the limit.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_owner_recovery

NOW = int(time.time())
HOST = kb._claimer_id().split(":", 1)[0]
FINGERPRINT = "6a232414-2dbf-40ab-a534-d9b71db05658:116|4162240"
CLAIM_LAPSED = -1800  # claim_expires_in for a claim that expired half an hour ago


def _absent_pid(base: int = 3_800_000) -> int:
    pid = base
    while psutil.pid_exists(pid):
        pid += 1
    return pid


@pytest.fixture
def board(tmp_path, monkeypatch):
    """An isolated HERMES_HOME board plus the owner ledger a receipt points at."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kbc.connect()
    ledger = tmp_path / "state.db"
    with sqlite3.connect(ledger) as store:
        store.execute(
            "CREATE TABLE session_admissions "
            "(target_session_id TEXT, request_id TEXT, status TEXT, outcome TEXT)"
        )
    scratch = _Board(conn, ledger)
    yield scratch
    conn.close()


class _Board:
    def __init__(self, conn, ledger: Path):
        self.conn = conn
        self.ledger = ledger

    # --- seeding ----------------------------------------------------------------------
    def running(
        self, tid: str = "t_lost", *, worker_pid=None, record_worker_pid: bool = True,
        claim_expires_in: int = 900, heartbeat_age=None, owner: bool = True,
        admission=("terminal", "interrupted"), worker_lost=None, dead_seconds: int = 120,
        owner_pid=None,
    ) -> int:
        """A ``running`` card whose managed worker was lost."""
        worker_pid = _absent_pid() if worker_pid is None else int(worker_pid)
        lock = f"{HOST}:{_absent_pid(base=3_900_000)}"
        started = NOW - dead_seconds
        run_id = int(self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'fan', 'running', ?, ?, ?, ?)",
            (tid, lock, NOW + claim_expires_in, worker_pid if record_worker_pid else None, started),
        ).lastrowid)
        self.conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at, claim_lock, claim_expires, "
            "worker_pid, worker_started_at, last_heartbeat_at, current_run_id, started_at, priority) "
            "VALUES (?, ?, 'fan', 'running', ?, ?, ?, ?, ?, ?, ?, ?, 5)",
            (tid, tid, NOW - 86400, lock, NOW + claim_expires_in,
             worker_pid if record_worker_pid else None,
             FINGERPRINT if record_worker_pid else None,
             (NOW - heartbeat_age) if heartbeat_age is not None else None, run_id, started),
        )
        self.event(tid, run_id, "claimed", {"lock": lock, "expires": NOW + claim_expires_in}, started)
        self.event(tid, run_id, "spawned",
                   {"pid": worker_pid, "started_at": FINGERPRINT} if record_worker_pid else {}, started)
        if owner:
            request_id = f'kanban:["default","{tid}",{run_id}]'
            self.event(tid, run_id, "owner_admitted",
                       {"db": str(self.ledger), "session_id": "sess-1",
                        "pid": os.getpid() if owner_pid is None else owner_pid,
                        "request_id": request_id}, started + 3)
            if admission:
                with sqlite3.connect(self.ledger) as store:
                    store.execute("INSERT INTO session_admissions VALUES ('sess-1', ?, ?, ?)",
                                  (request_id, admission[0], admission[1]))
        if worker_lost:
            self.event(tid, run_id, "worker_lost",
                       {"reason": worker_lost, "claim_lock": lock, "run_id": run_id,
                        "pid": worker_pid, "bound": False}, started + 24)
        self.conn.commit()
        return run_id

    def event(self, tid: str, run: int, kind: str, payload: dict, ts: int) -> None:
        self.conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (tid, run, kind, json.dumps(payload), ts))

    # --- reading ---------------------------------------------------------------------
    def state(self, tid: str) -> dict:
        return dict(self.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())

    def run(self, run_id: int) -> dict:
        return dict(self.conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone())

    def tick(self, *, failure_limit: int | None = None) -> kbd.DispatchResult:
        """One real dispatcher tick: reclaim phase + promotion, no spawn slot."""
        return kbd.dispatch_once(self.conn, max_spawn=0, failure_limit=failure_limit,
                                 stale_timeout_seconds=0)


# --- A1: a killed worker under a LIVE claim is booked crashed within one tick --------------

def test_a_killed_managed_worker_is_disposed_of_within_one_tick(board):
    run_id = board.running("t_killed", claim_expires_in=900, heartbeat_age=30,
                           worker_lost="managed_worker_failed")
    board.tick(failure_limit=2)
    row, run = board.state("t_killed"), board.run(run_id)
    assert row["status"] == "ready"                      # left ``running``
    assert (row["claim_lock"], row["worker_pid"], row["current_run_id"]) == (None, None, None)
    assert run["outcome"] == "crashed"
    assert "managed worker lost: managed_worker_failed" in run["error"]
    assert json.loads(run["metadata"])["attempt_note"] == "managed worker lost: managed_worker_failed"
    assert row["consecutive_failures"] == 1               # A3: the breaker sees it
    assert row["last_failure_error"]


def test_a_killed_managed_worker_blocks_the_card_at_the_failure_limit(board):
    """The breaker covers this class: one loss short of the limit, the next loss blocks.

    ``_account_crashes`` books a plain crash against ``DEFAULT_FAILURE_LIMIT`` (the sweep
    resolves the threshold there rather than against the dispatcher's ``failure_limit``),
    so the card is seeded one failure short of that limit instead of pinning a threshold
    this path never reads.
    """
    board.running("t_killed", claim_expires_in=900, heartbeat_age=30,
                  worker_lost="managed_worker_failed")
    board.conn.execute("UPDATE tasks SET consecutive_failures = ? WHERE id = 't_killed'",
                       (kbd.DEFAULT_FAILURE_LIMIT - 1,))
    board.conn.commit()
    board.tick()
    row = board.state("t_killed")
    assert row["status"] == "blocked"
    assert row["consecutive_failures"] == kbd.DEFAULT_FAILURE_LIMIT
    kinds = [r["kind"] for r in board.conn.execute(
        "SELECT kind FROM task_events WHERE task_id = 't_killed' ORDER BY id")]
    assert "gave_up" in kinds


def test_gateway_restart_interruption_requeues_without_spending_failure_budget(board):
    """A dead gateway that settled the attempt as interrupted is infrastructure, not task failure.

    The replacement gateway must reclaim and immediately make the card dispatchable without
    tripping the circuit breaker, even when the card was one failure below its limit.
    """
    dead_gateway = _absent_pid(base=3_955_000)
    run_id = board.running(
        "t_restart", claim_expires_in=900, heartbeat_age=30,
        owner_pid=dead_gateway, worker_lost="managed_worker_failed",
    )
    board.conn.execute(
        "UPDATE tasks SET consecutive_failures = ? WHERE id = 't_restart'",
        (kbd.DEFAULT_FAILURE_LIMIT - 1,),
    )
    board.conn.commit()

    board.tick()

    row, run = board.state("t_restart"), board.run(run_id)
    assert row["status"] == "ready"
    assert row["consecutive_failures"] == kbd.DEFAULT_FAILURE_LIMIT - 1
    assert json.loads(run["metadata"])["infrastructure"] is True
    assert not list(board.conn.execute(
        "SELECT 1 FROM task_events WHERE task_id='t_restart' AND kind='gave_up'"
    ))


def test_deliberate_interruption_without_worker_loss_still_charges_failure(board):
    """Dead-owner evidence alone must not turn Stop/cancellation into a restart retry."""
    dead_gateway = _absent_pid(base=3_955_500)
    run_id = board.running(
        "t_stopped", claim_expires_in=900, heartbeat_age=30,
        owner_pid=dead_gateway,
    )

    board.tick(failure_limit=2)

    row, run = board.state("t_stopped"), board.run(run_id)
    assert row["status"] == "ready"
    assert row["consecutive_failures"] == 1
    assert "infrastructure" not in json.loads(run["metadata"])


def test_replacement_gateway_recovers_a_block_already_charged_during_shutdown(board):
    """A shutdown race may book the interruption before the old owner exits.

    On the replacement gateway's first tick the now-dead owner is authoritative
    evidence that the last charged failure was infrastructure, so exactly that
    charge is rolled back and the card becomes dispatchable.
    """
    dead_gateway = _absent_pid(base=3_956_000)
    run_id = board.running(
        "t_shutdown_race", claim_expires_in=900, heartbeat_age=30,
        owner_pid=dead_gateway, worker_lost="managed_worker_failed",
    )
    board.conn.execute(
        "UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=? WHERE id=?",
        (NOW - 1, run_id),
    )
    board.conn.execute(
        "UPDATE tasks SET status='blocked', claim_lock=NULL, claim_expires=NULL, "
        "worker_pid=NULL, worker_started_at=NULL, current_run_id=NULL, "
        "consecutive_failures=? WHERE id='t_shutdown_race'",
        (kbd.DEFAULT_FAILURE_LIMIT,),
    )
    board.event(
        "t_shutdown_race", run_id, "gave_up",
        {"failures": kbd.DEFAULT_FAILURE_LIMIT, "effective_limit": kbd.DEFAULT_FAILURE_LIMIT,
         "trigger_outcome": "crashed", "retry_status": "ready"},
        NOW - 1,
    )
    board.conn.commit()

    board.tick()

    row = board.state("t_shutdown_race")
    assert row["status"] == "ready"
    assert row["consecutive_failures"] == kbd.DEFAULT_FAILURE_LIMIT - 1
    event = board.conn.execute(
        "SELECT payload FROM task_events WHERE task_id='t_shutdown_race' "
        "AND kind='restart_recovered' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert json.loads(event[0])["run_id"] == run_id


def test_terminal_provider_failure_stays_blocked_across_gateway_restart(board):
    """A dead owner never downgrades an explicit unhealable provider verdict."""
    dead_gateway = _absent_pid(base=3_957_000)
    run_id = board.running(
        "t_provider", claim_expires_in=900, heartbeat_age=30,
        owner_pid=dead_gateway,
    )
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    log = kb.worker_log_path("t_provider")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        f"provider rejected configured credential\n{KANBAN_WORKER_EXIT_TRAILER}"
        f"{kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE}\n",
        encoding="utf-8",
    )

    board.tick()

    row, run = board.state("t_provider"), board.run(run_id)
    assert row["status"] == "blocked"
    assert row["consecutive_failures"] == 1
    metadata = json.loads(run["metadata"])
    assert metadata["terminal_provider"] is True
    assert "infrastructure" not in metadata


def test_explicit_sticky_block_is_not_released_by_restart_recovery(board):
    dead_gateway = _absent_pid(base=3_958_000)
    run_id = board.running(
        "t_sticky", claim_expires_in=900, heartbeat_age=30,
        owner_pid=dead_gateway, worker_lost="managed_worker_failed",
    )
    board.conn.execute(
        "UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=? WHERE id=?",
        (NOW - 1, run_id),
    )
    board.conn.execute(
        "UPDATE tasks SET status='blocked', claim_lock=NULL, claim_expires=NULL, "
        "worker_pid=NULL, worker_started_at=NULL, current_run_id=NULL, "
        "consecutive_failures=? WHERE id='t_sticky'",
        (kbd.DEFAULT_FAILURE_LIMIT,),
    )
    board.event(
        "t_sticky", run_id, "gave_up",
        {"failures": kbd.DEFAULT_FAILURE_LIMIT, "effective_limit": kbd.DEFAULT_FAILURE_LIMIT,
         "trigger_outcome": "crashed", "retry_status": "ready"},
        NOW - 2,
    )
    board.event("t_sticky", None, "blocked", {"sticky": True, "reason": "operator"}, NOW - 1)
    board.conn.commit()

    board.tick()

    row = board.state("t_sticky")
    assert row["status"] == "blocked"
    assert row["consecutive_failures"] == kbd.DEFAULT_FAILURE_LIMIT
    assert not list(board.conn.execute(
        "SELECT 1 FROM task_events WHERE task_id='t_sticky' AND kind='restart_recovered'"
    ))


def test_restart_infrastructure_is_excluded_from_systemic_crash_fingerprint(board):
    details = []
    for index, infrastructure in enumerate((True, True, False), start=1):
        task_id = f"t_wave_{index}"
        board.running(task_id, claim_expires_in=900, heartbeat_age=30)
        dead = kbd._DeadWorker(
            kind="signal", code=-9, error_text="same worker error",
            event_kind="crashed", event_payload={}, infrastructure=infrastructure,
        )
        details.append((task_id, 50_000 + index, f"claim-{index}", dead))

    blocked = kbd._account_crashes(board.conn, details)

    assert blocked == []
    assert board.state("t_wave_3")["status"] == "running"
    assert board.state("t_wave_3")["consecutive_failures"] == 1


def test_a_lost_worker_that_exited_cleanly_still_charges_the_breaker(board):
    """rc=0 after a lost managed execution is not a protocol violation: the turn never ran."""
    run_id = board.running("t_clean", claim_expires_in=900, heartbeat_age=30,
                           worker_lost="managed_worker_failed")
    # The worker's own exit trailer says rc=0 (the CLI's epilogue after a lost turn).
    log = kb.worker_log_path("t_clean")
    log.parent.mkdir(parents=True, exist_ok=True)
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    log.write_text(f"I fixed the protocol violation from the last run.\n{KANBAN_WORKER_EXIT_TRAILER}0\n",
                   encoding="utf-8")
    board.tick(failure_limit=2)
    run = board.run(run_id)
    assert run["outcome"] == "crashed"
    assert "managed worker lost" in run["error"]
    assert run["error"].startswith("pid ") and "exited cleanly after its managed worker was lost." in run["error"]
    assert "I fixed the protocol violation" in run["error"]  # the worker's own output is kept
    assert run["error"].index("[managed worker lost") < run["error"].index("Worker's last output")
    assert "without kanban_complete" not in run["error"]
    assert board.state("t_clean")["consecutive_failures"] == 1
    # Not a protocol violation as well: one loss is charged to one budget.
    assert "protocol_violation" not in json.loads(run["metadata"] or "{}")
    kinds = [r[0] for r in board.conn.execute(
        "SELECT kind FROM task_events WHERE task_id='t_clean' AND run_id=?", (run_id,))]
    assert "protocol_violation" not in kinds and "crashed" in kinds
    assert kbd._protocol_violation_streak(board.conn, "t_clean") == 0


def test_a_crash_whose_output_quotes_protocol_violation_is_not_one(board):
    """The streak's text match reads our own error head, never the worker's quoted output."""
    run_id = board.running("t_quote", claim_expires_in=900, heartbeat_age=30, owner=False)
    log = kb.worker_log_path("t_quote")
    log.parent.mkdir(parents=True, exist_ok=True)
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    log.write_text(f"Retrying after the earlier protocol violation.\n{KANBAN_WORKER_EXIT_TRAILER}1\n",
                   encoding="utf-8")
    board.tick(failure_limit=5)
    run = board.run(run_id)
    assert run["outcome"] == "crashed" and "protocol violation" in run["error"]
    assert kbd._protocol_violation_streak(board.conn, "t_quote") == 0


# --- A2: an elapsed claim lock is never "still running" after one tick ---------------------

@pytest.mark.parametrize("shape, error", [
    (dict(record_worker_pid=False, owner=False), "stale_lock="),
    (dict(record_worker_pid=False, owner=True), "owner admission settled interrupted"),
    (dict(owner=False), "stale_lock="),
    (dict(owner=True, worker_lost="managed_worker_failed"), "managed worker lost: managed_worker_failed"),
], ids=["no-pid", "no-pid-receipt", "dead-pid", "dead-pid-lost"])
def test_an_elapsed_claim_never_stays_running_after_one_tick(board, shape, error):
    run_id = board.running("t_lapsed", claim_expires_in=CLAIM_LAPSED, heartbeat_age=None, **shape)
    board.tick(failure_limit=2)
    row, run = board.state("t_lapsed"), board.run(run_id)
    assert row["status"] == "ready"
    assert run["outcome"] == "reclaimed"
    assert error in run["error"]
    assert row["consecutive_failures"] == 1


def test_a_settled_receipt_with_a_live_gateway_owner_no_longer_holds_the_claim(board):
    """The fence itself: a live owner PROCESS is not a live ATTEMPT."""
    import os
    board.running("t_gateway", claim_expires_in=CLAIM_LAPSED, owner=True, owner_pid=os.getpid(),
                  admission=("terminal", "interrupted"))
    assert kanban_owner_recovery.owner_reclaim_paused(board.conn, "t_gateway") is True
    assert kanban_owner_recovery.owner_attempt_provably_over(board.conn, "t_gateway") is True
    board.tick(failure_limit=2)
    assert board.state("t_gateway")["status"] == "ready"


# --- the reclaim's kill step: an absent pid is gone, not "survived" -----------------------

@pytest.mark.parametrize("live", [False, True], ids=["absent-pid", "live-pid"])
def test_a_refused_signal_reads_the_pid_not_the_error(board, live):
    """A bare OSError from the kill (Windows reports an absent pid as error 87) is a success only
    when the pid is really gone; a live worker that refuses the signal is not terminated."""
    import os
    pid = os.getpid() if live else _absent_pid(base=3_970_000)

    def refusing_kill(pid, sig):
        raise OSError(87, "The parameter is incorrect")

    info = kbd._terminate_reclaimed_worker(pid, f"{HOST}:{_absent_pid(base=3_980_000)}",
                                           started_at=kbd._process_fingerprint(pid) if live else None,
                                           signal_fn=refusing_kill)
    assert info["terminated"] is (not live)


# --- D1: the OWNER writes the loss where the dispatcher can read it -----------------------
# ``worker.adopt`` precedes ``bind_worker_context``, so a worker that dies in the handshake
# writes neither ``worker_bound`` nor ``worker_result``: without this event the run learns
# nothing about its own loss and the dispatcher can only guess from a dead pid.

def _owner_policy(board, tid, run_id):
    from types import SimpleNamespace
    row = board.state(tid)
    return SimpleNamespace(kanban_json=json.dumps({
        "task_id": tid, "db": str(kb.kanban_db_path()), "run_id": run_id,
        "claim_lock": row["claim_lock"], "board": None,
    }))


@pytest.mark.asyncio
async def test_the_owner_records_the_loss_on_the_board_and_one_tick_disposes_of_it(board):
    """Through the gateway's loss handler: a Stop, a superseded generation or a cancellation
    records nothing; a real loss leaves one worker_lost event that the next tick books."""
    import asyncio
    from types import SimpleNamespace
    from gateway.session_managed_worker import _record_lost_attempt
    run_id = board.running("t_owner", claim_expires_in=900, heartbeat_age=30)
    policy = _owner_policy(board, "t_owner", run_id)
    authority = SimpleNamespace()
    for exc, reason in [(RuntimeError(), "managed_worker_stopped"), (RuntimeError(), "stale_generation"),
                        (asyncio.CancelledError(), "CancelledError"),
                        (RuntimeError(), "managed_worker_failed")]:
        recording = _record_lost_attempt(authority, policy, exc, reason, None)
        if recording is not None:
            await recording
    events = [json.loads(r[0]) for r in board.conn.execute(
        "SELECT payload FROM task_events WHERE task_id='t_owner' AND kind='worker_lost'")]
    assert [e["reason"] for e in events] == ["managed_worker_failed"]

    board.tick(failure_limit=2)
    run = board.run(run_id)
    assert run["outcome"] == "crashed"
    assert "managed worker lost: managed_worker_failed" in run["error"]
    assert board.state("t_owner")["consecutive_failures"] == 1


def test_a_loss_recorded_against_a_superseded_run_is_dropped(board):
    """The owner's note is run-scoped: it may never be evidence about a LATER attempt."""
    from gateway.session_kanban import record_worker_loss
    run_id = board.running("t_stale", claim_expires_in=900, heartbeat_age=30)
    policy = _owner_policy(board, "t_stale", run_id)

    def count():
        return board.conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id='t_stale' AND kind='worker_lost'").fetchone()[0]
    record_worker_loss(policy, "managed_worker_failed", pid=1234, bound=False)
    assert count() == 1
    board.conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL WHERE id='t_stale'")
    board.conn.commit()
    record_worker_loss(policy, "managed_worker_failed", pid=1234, bound=False)
    assert count() == 1
