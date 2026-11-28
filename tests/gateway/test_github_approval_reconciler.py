"""Regression coverage for durable GitHub approval projection."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from gateway.github_approval_broker import ApprovalProposal, GitHubApprovalBroker
from gateway.github_approval_reconciler import (
    ApprovalDecisionReconciler,
    ApprovalWatchdog,
    ContractRejected,
    IntakeScriptLiveReader,
    LiveContract,
)
from hermes_cli.kanban_db_connect import connect as connect_kanban


ITEM = "rbueno69-git/tutordigital-dashboard#519"
HEAD = "6283863c38a54b880d78dfc62591f2a71b71c343"
DIGEST = "66efb62cd86ab3d2c193571cc63421723fde5b79c005228e4011a4f812ab7ddc"


def _seed_decision(path: Path, *, response: str = "Approve") -> str:
    broker = GitHubApprovalBroker(path, instance_id="test")
    proposal_key = "5a58c9c3eb18111bc0447af6670dd297ef2cca653ca80620e8868d29460e3a50"
    now = 1_794_591_143.0
    with broker._connect() as db:
        db.execute(
            """INSERT INTO github_approval_proposals(
                proposal_key,item_key,head_sha,contract_digest_sha256,executor,state,clarify_id,
                session_key,chat_id,authorized_user_id,destination_id,question,message_id,
                callback_bound,response,approver,created_at,updated_at)
                VALUES(?,?,?,?,?,'answered',?,?,?,?,?,?,?,0,?,?,?,?)""",
            (
                proposal_key, ITEM, HEAD, DIGEST, "hermes", "gha-519", "github:519", "42", "7",
                "telegram-bot:test", "Approve bounded #519 contract?", "9069", response,
                "telegram:7", now, now,
            ),
        )
        db.execute(
            """INSERT INTO github_approval_decisions(
                proposal_key,response,approver,state,decided_at)
                VALUES(?,?,?,'pending',?)""",
            (proposal_key, response, "telegram:7", now),
        )
    return proposal_key


def _live(*, state: str = "open", head: str = HEAD, digest: str = DIGEST) -> LiveContract:
    return LiveContract(
        item_key=ITEM,
        state=state,
        head_sha=head,
        contract_digest_sha256=digest,
        accepted=state == "open",
        title="Preview school notices without sending email",
    )


def _reconciler(tmp_path: Path, live: LiveContract) -> ApprovalDecisionReconciler:
    broker_path = tmp_path / "broker.db"
    _seed_decision(broker_path)
    kanban_path = tmp_path / "kanban.db"
    conn = connect_kanban(kanban_path)
    conn.close()
    return ApprovalDecisionReconciler(
        broker_path=broker_path,
        ledger_path=tmp_path / "approvals.jsonl",
        kanban_path=kanban_path,
        live_reader=lambda _item: live,
        assignee="default",
    )


def _ledger_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _task_rows(path: Path) -> list[sqlite3.Row]:
    conn = connect_kanban(path)
    try:
        return conn.execute(
            "SELECT id,title,status,assignee,idempotency_key,completion_contract,body FROM tasks"
        ).fetchall()
    finally:
        conn.close()


def test_approved_pending_decision_projects_one_ledger_row_and_one_local_task(tmp_path):
    reconciler = _reconciler(tmp_path, _live())

    first = reconciler.reconcile()
    second = reconciler.reconcile()

    assert [result.outcome for result in first] == ["approved"]
    assert second == []
    ledger = _ledger_rows(tmp_path / "approvals.jsonl")
    assert len(ledger) == 1
    assert ledger[0]["status"] == "approved_terminal"
    assert ledger[0]["work_item"] == ITEM
    assert ledger[0]["head_sha"] == HEAD
    assert ledger[0]["contract_digest_sha256"] == DIGEST
    assert ledger[0]["decision_message_id"] == "9069"
    tasks = _task_rows(tmp_path / "kanban.db")
    assert len(tasks) == 1
    assert tasks[0]["assignee"] == "default"
    assert tasks[0]["completion_contract"] == "local-only"
    assert tasks[0]["status"] == "ready"
    assert ITEM in tasks[0]["body"]
    assert HEAD in tasks[0]["body"]
    assert DIGEST in tasks[0]["body"]


def test_inherited_successor_reuses_canonical_task_and_records_lineage(tmp_path):
    reconciler = _reconciler(tmp_path, _live())
    first = reconciler.reconcile()
    canonical_task = first[0].task_id
    successor_head = "7" * 40
    successor = ApprovalProposal(ITEM, successor_head, DIGEST)
    predecessor_key = "5a58c9c3eb18111bc0447af6670dd297ef2cca653ca80620e8868d29460e3a50"
    # The broker already verified same authority text, active task and descendant ancestry.
    with reconciler._connect() as db:
        now = 1_794_591_244.0
        key = successor.canonical_key()
        db.execute(
            """INSERT INTO github_approval_proposals(
                proposal_key,item_key,head_sha,contract_digest_sha256,executor,state,clarify_id,
                session_key,chat_id,authorized_user_id,destination_id,question,message_id,
                callback_bound,response,approver,created_at,updated_at)
                VALUES(?,?,?,?,?,'answered',?,?,?,?,?,?,NULL,0,'Approve',?,?,?)""",
            (key, ITEM, successor_head, DIGEST, "hermes", "gha-successor", "github:successor",
             "42", "7", "telegram-bot:test", "Approve bounded #519 contract?",
             "telegram:7", now, now),
        )
        db.execute(
            """INSERT INTO github_approval_decisions(
                proposal_key,response,approver,state,decided_at)
                VALUES(?,'Approve','telegram:7','pending',?) RETURNING decision_id""",
            (key, now),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO github_approval_inheritances VALUES(?,?,?,?)",
            (key, predecessor_key, canonical_task, now),
        )
    reconciler.live_reader = lambda _item: _live(head=successor_head)

    inherited = reconciler.reconcile()

    assert inherited[0].task_id == canonical_task
    assert len(_task_rows(tmp_path / "kanban.db")) == 1
    ledger = _ledger_rows(tmp_path / "approvals.jsonl")
    assert ledger[-1]["status"] == "approved_terminal_successor_contract"
    assert ledger[-1]["predecessor_proposal_key"] == predecessor_key
    assert ledger[-1]["inherited_task_id"] == canonical_task


def test_replay_after_ledger_append_without_receipt_creates_no_duplicate(tmp_path):
    reconciler = _reconciler(tmp_path, _live())
    reconciler.reconcile(stop_after="ledger")

    resumed = reconciler.reconcile()
    replay = reconciler.reconcile()

    assert [result.outcome for result in resumed] == ["approved"]
    assert replay == []
    assert len(_ledger_rows(tmp_path / "approvals.jsonl")) == 1
    assert len(_task_rows(tmp_path / "kanban.db")) == 1


def test_reconciler_waits_for_the_canonical_ledger_lock(tmp_path):
    reconciler = _reconciler(tmp_path, _live())
    canonical_lock = Path(f"{tmp_path / 'approvals.jsonl'}.lock")
    reached_live_read = threading.Event()
    completed = threading.Event()
    reconciler.live_reader = lambda _item: (reached_live_read.set(), _live())[1]

    from gateway.github_approval_reconciler import _exclusive_lock

    with _exclusive_lock(canonical_lock):
        worker = threading.Thread(
            target=lambda: (reconciler.reconcile(), completed.set()), daemon=True,
        )
        worker.start()
        assert not reached_live_read.wait(2.0)
        assert not completed.is_set()
    worker.join(timeout=5.0)

    assert completed.is_set()
    assert reached_live_read.is_set()
    assert len(_ledger_rows(tmp_path / "approvals.jsonl")) == 1


def _seed_bindable_cross_item_successor(
        reconciler: ApprovalDecisionReconciler,
        *, successor_item: str = "owner/repo#520") -> tuple[str, str]:
    successor = ApprovalProposal(successor_item, "7" * 40, "8" * 64)
    broker = GitHubApprovalBroker(reconciler.broker_path, instance_id="successor-seed")
    now = 1_794_591_244.0
    with broker._connect() as db:
        key = successor.canonical_key()
        db.execute(
            """INSERT INTO github_approval_proposals(
                proposal_key,item_key,head_sha,contract_digest_sha256,executor,state,clarify_id,
                session_key,chat_id,authorized_user_id,destination_id,question,message_id,
                callback_bound,response,approver,created_at,updated_at)
                VALUES(?,?,?,?,?,'answered',?,?,?,?,?,?,?,0,'Approve','telegram:7',?,?)""",
            (key, successor.item_key, successor.head_sha, successor.contract_digest_sha256,
             "hermes", "gha-successor", "github:successor", "42", "7", "telegram-bot:test",
             "Approve successor?", "9070", now, now),
        )
        decision_id = db.execute(
            """INSERT INTO github_approval_decisions(
                proposal_key,response,approver,state,decided_at)
                VALUES(?,'Approve','telegram:7','pending',?) RETURNING decision_id""",
            (key, now),
        ).fetchone()[0]
    conn = connect_kanban(reconciler.kanban_path)
    try:
        from hermes_cli.kanban_db import create_task
        task_id = create_task(
            conn, title="successor",
            body=f"Predecessor: `{ITEM}`\nSuccessor: `{successor.item_key}`",
            assignee="default", initial_status="running",
        )
    finally:
        conn.close()
    with broker._connect() as db:
        db.execute(
            "INSERT INTO github_approval_reconciliations VALUES(?,?, 'approved',NULL,'successor',?,?)",
            (decision_id, key, task_id, now),
        )
    return successor.item_key, task_id


def test_binding_during_projection_marks_selected_decision_stale_without_task(tmp_path):
    reconciler = _reconciler(tmp_path, _live())
    successor_item, successor_task_id = _seed_bindable_cross_item_successor(reconciler)
    broker = GitHubApprovalBroker(reconciler.broker_path, instance_id="binder")

    def bind_while_row_is_in_flight(_item):
        broker.bind_item_succession(
            predecessor_item_key=ITEM,
            successor_item_key=successor_item,
            successor_task_id=successor_task_id,
            kanban_path=reconciler.kanban_path,
        )
        return _live()

    reconciler.live_reader = bind_while_row_is_in_flight
    result = reconciler.reconcile()

    assert [(item.outcome, item.reason) for item in result] == [("stale", "item_succession")]
    assert not (tmp_path / "approvals.jsonl").exists()
    assert [row["id"] for row in _task_rows(reconciler.kanban_path)] == [successor_task_id]


def test_binding_after_projection_rejects_live_predecessor_task(tmp_path):
    reconciler = _reconciler(tmp_path, _live())
    successor_item, successor_task_id = _seed_bindable_cross_item_successor(reconciler)
    projected = reconciler.reconcile()
    assert projected[0].outcome == "approved"

    broker = GitHubApprovalBroker(reconciler.broker_path, instance_id="binder")
    with pytest.raises(ValueError, match="predecessor.*nonterminal"):
        broker.bind_item_succession(
            predecessor_item_key=ITEM,
            successor_item_key=successor_item,
            successor_task_id=successor_task_id,
            kanban_path=reconciler.kanban_path,
        )


def test_replay_after_task_creation_reuses_existing_task_without_mutation(tmp_path, monkeypatch):
    reconciler = _reconciler(tmp_path, _live())
    reconciler.reconcile(stop_after="ledger")
    with reconciler._connect():
        row = reconciler._pending(1)[0]
    from hermes_cli.kanban_db import create_task

    conn = connect_kanban(tmp_path / "kanban.db")
    try:
        existing_id = create_task(
            conn, title="existing", body="existing", assignee="default",
            idempotency_key=reconciler._idempotency_key(row), completion_contract="local-only",
        )
    finally:
        conn.close()
    import hermes_cli.kanban_db as kanban_db_module
    monkeypatch.setattr(
        kanban_db_module, "create_task",
        lambda *_args, **_kwargs: pytest.fail("existing idempotency key must be read before mutation"),
    )

    result = reconciler.reconcile()

    assert result[0].task_id == existing_id
    assert len(_task_rows(tmp_path / "kanban.db")) == 1


def test_archived_task_does_not_satisfy_approved_execution(tmp_path):
    reconciler = _reconciler(tmp_path, _live())
    reconciler.reconcile(stop_after="ledger")
    row = reconciler._pending(1)[0]
    from hermes_cli.kanban_db import create_task

    conn = connect_kanban(tmp_path / "kanban.db")
    try:
        archived_id = create_task(
            conn, title="archived", body="archived", assignee="default",
            idempotency_key=reconciler._idempotency_key(row), completion_contract="local-only",
        )
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (archived_id,))
        conn.commit()
    finally:
        conn.close()

    result = reconciler.reconcile()

    assert result[0].task_id != archived_id
    assert len(_task_rows(tmp_path / "kanban.db")) == 2


def test_negative_decision_records_terminal_receipt_without_task(tmp_path):
    broker_path = tmp_path / "broker.db"
    _seed_decision(broker_path, response="Do not approve")
    kanban_path = tmp_path / "kanban.db"
    connect_kanban(kanban_path).close()
    reconciler = ApprovalDecisionReconciler(
        broker_path=broker_path,
        ledger_path=tmp_path / "approvals.jsonl",
        kanban_path=kanban_path,
        live_reader=lambda _item: pytest.fail("negative decisions must not read GitHub"),
    )

    result = reconciler.reconcile()

    assert [item.outcome for item in result] == ["rejected"]
    assert not (tmp_path / "approvals.jsonl").exists()
    assert _task_rows(kanban_path) == []


def test_live_reader_revalidates_pr_through_the_pinned_intake_contract():
    calls = []

    def github(endpoint):
        calls.append(endpoint)
        if endpoint.endswith("/issues/519"):
            return {
                "number": 519, "state": "open", "title": "Preview notices",
                "pull_request": {"url": "https://api.github.test/pulls/519"},
            }
        if endpoint.endswith("/pulls/519"):
            return {"number": 519, "state": "open", "head": {"sha": HEAD}}
        raise AssertionError(endpoint)

    payloads = []

    def intake(payload):
        payloads.append(payload)
        return {
            "schema": "hermes.github-intake.v2", "item_key": ITEM,
            "head_sha": HEAD, "contract_accepted": True, "digest": DIGEST,
            "scope": ["preview only"], "limits": {"contract": "no send"},
            "risk": "medio", "binding_status": "pending",
        }

    live = IntakeScriptLiveReader(github_reader=github, intake_runner=intake)(ITEM)

    assert live == LiveContract(
        item_key=ITEM, state="open", head_sha=HEAD,
        contract_digest_sha256=DIGEST, accepted=True, title="Preview notices")
    assert calls == [
        "repos/rbueno69-git/tutordigital-dashboard/issues/519",
        "repos/rbueno69-git/tutordigital-dashboard/pulls/519",
    ]
    assert payloads[0]["action"] == "edited"
    assert payloads[0]["pull_request"]["head"]["sha"] == HEAD


def test_live_reader_marks_closed_issue_terminal_without_contract_validation():
    def github(endpoint):
        assert endpoint.endswith("/issues/519")
        return {"number": 519, "state": "closed", "title": "Closed"}

    reader = IntakeScriptLiveReader(
        github_reader=github,
        intake_runner=lambda _payload: pytest.fail("closed item must not validate an executable contract"),
    )

    live = reader(ITEM)

    assert live.state == "closed"
    assert live.accepted is False


def test_live_reader_returns_unaccepted_when_pinned_validator_rejects_contract():
    def github(endpoint):
        if endpoint.endswith("/issues/519"):
            return {"number": 519, "state": "open", "title": "Invalid", "pull_request": {}}
        if endpoint.endswith("/pulls/519"):
            return {"number": 519, "state": "open", "head": {"sha": HEAD}}
        raise AssertionError(endpoint)

    def reject(_payload):
        raise ContractRejected("contract rejected")

    live = IntakeScriptLiveReader(github_reader=github, intake_runner=reject)(ITEM)

    assert live.state == "open"
    assert live.head_sha == HEAD
    assert live.accepted is False


def test_live_reader_revalidates_exact_comment_contract_digest():
    calls = []

    def github(endpoint):
        calls.append(endpoint)
        if endpoint.endswith("/issues/519"):
            return {
                "number": 519, "state": "open", "title": "Comment contract",
                "labels": [{"name": "pink:auto"}], "pull_request": {},
            }
        if endpoint.endswith("/pulls/519"):
            return {"number": 519, "state": "open", "head": {"sha": HEAD}}
        if endpoint.endswith("/issues/519/comments?per_page=100"):
            return [
                {"id": 77, "body": "old note", "user": {"login": "author"}},
                {"id": 88, "body": "exact approved contract", "user": {"login": "author"}},
            ]
        raise AssertionError(endpoint)

    def intake(payload):
        comment = payload.get("comment")
        if not isinstance(comment, dict):
            raise ContractRejected("item body is not the approved comment contract")
        digest = DIGEST if comment.get("id") == 88 else "b" * 64
        return {
            "schema": "hermes.github-intake.v2", "item_key": ITEM,
            "head_sha": HEAD, "contract_accepted": True, "digest": digest,
            "scope": ["comment contract"], "limits": {"contract": "bounded"},
            "risk": "alto", "binding_status": "pending",
        }

    reader = IntakeScriptLiveReader(github_reader=github, intake_runner=intake)

    live = reader.for_digest(ITEM, DIGEST)

    assert live == LiveContract(
        item_key=ITEM, state="open", head_sha=HEAD,
        contract_digest_sha256=DIGEST, accepted=True, title="Comment contract")
    assert calls[-1].endswith("/issues/519/comments?per_page=100")


def test_reconciler_uses_digest_aware_live_reader_for_comment_contract(tmp_path):
    class DigestAwareReader:
        def __call__(self, _item):
            pytest.fail("generic item-body validation must not replace comment provenance")

        def for_digest(self, item, digest):
            assert item == ITEM
            assert digest == DIGEST
            return _live()

    reconciler = _reconciler(tmp_path, _live())
    reconciler.live_reader = DigestAwareReader()

    result = reconciler.reconcile()

    assert [item.outcome for item in result] == ["approved"]
    assert len(_task_rows(tmp_path / "kanban.db")) == 1


def test_live_reader_rejects_malformed_authority_fields():
    def github(endpoint):
        if endpoint.endswith("/issues/519"):
            return {"number": 519, "state": "open", "title": "Malformed", "pull_request": {}}
        return {"number": 519, "state": "open", "head": {"sha": HEAD}}

    def malformed(_payload):
        return {
            "schema": "hermes.github-intake.v2", "item_key": ITEM,
            "head_sha": HEAD, "contract_accepted": True, "digest": DIGEST,
            "scope": "not-a-list", "limits": {}, "risk": "medio",
            "binding_status": "pending",
        }

    with pytest.raises(RuntimeError, match="authority fields"):
        IntakeScriptLiveReader(github_reader=github, intake_runner=malformed)(ITEM)


def test_live_reader_treats_empty_success_from_validator_as_unaccepted(tmp_path):
    script = tmp_path / "intake"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o700)

    def github(endpoint):
        if endpoint.endswith("/issues/519"):
            return {"number": 519, "state": "open", "title": "Invalid", "pull_request": {}}
        return {"number": 519, "state": "open", "head": {"sha": HEAD}}

    live = IntakeScriptLiveReader(github_reader=github, intake_script=script)(ITEM)

    assert live.state == "open"
    assert live.accepted is False


def test_watchdog_recovers_missing_open_contract_once(tmp_path):
    broker_path = tmp_path / "broker.db"
    broker = GitHubApprovalBroker(broker_path, instance_id="test")
    sent = []

    class Destination:
        @staticmethod
        def approval_destination_identity(_clarify_id):
            return "telegram-bot:test"

    def sender(item_key, live, action):
        sent.append((item_key, action))
        broker.admit(
            ApprovalProposal(item_key, live.head_sha, live.contract_digest_sha256),
            adapter=Destination(), chat_id="42", question="Recovered contract",
            session_key="watchdog:1", authorized_user_id="7")

    watchdog = ApprovalWatchdog(
        broker_path=broker_path,
        candidate_items=lambda: [ITEM],
        live_reader=lambda _item: _live(),
        event_sender=sender,
    )

    first = watchdog.run()
    second = watchdog.run()

    assert first == [{"item_key": ITEM, "action": "recover"}]
    assert second == []
    assert sent == [(ITEM, "recover")]


def test_watchdog_retires_actionable_card_when_item_is_terminal(tmp_path):
    broker_path = tmp_path / "broker.db"
    _seed_decision(broker_path)
    sent = []
    watchdog = ApprovalWatchdog(
        broker_path=broker_path,
        candidate_items=list,
        live_reader=lambda _item: _live(state="closed"),
        event_sender=lambda item, _live_item, action: sent.append((item, action)),
    )

    result = watchdog.run()

    assert result == [{"item_key": ITEM, "action": "retire"}]
    assert sent == [(ITEM, "retire")]


def test_existing_pretty_printed_ledger_objects_do_not_block_projection(tmp_path):
    broker_path = tmp_path / "broker.db"
    ledger_path = tmp_path / "approvals.jsonl"
    kanban_path = tmp_path / "kanban.db"
    _seed_decision(broker_path)
    ledger_path.write_text(
        json.dumps({"approval_request_id": "older:approval", "status": "approved"}, indent=2)
        + "\n",
        encoding="utf-8",
    )

    reconciler = ApprovalDecisionReconciler(
        broker_path=broker_path,
        ledger_path=ledger_path,
        kanban_path=kanban_path,
        live_reader=lambda _item: _live(),
        assignee="github-worker",
    )

    result = reconciler.reconcile()

    assert result[0].outcome == "approved"
    raw = ledger_path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    values = []
    offset = 0
    while offset < len(raw):
        while offset < len(raw) and raw[offset].isspace():
            offset += 1
        if offset < len(raw):
            value, offset = decoder.raw_decode(raw, offset)
            values.append(value)
    assert [value["approval_request_id"] for value in values] == [
        "older:approval",
        f"hermes:github:{ITEM}:{HEAD[:12]}:{DIGEST[:16]}",
    ]


@pytest.mark.parametrize(
    ("live", "reason"),
    [
        (_live(state="closed"), "terminal_item"),
        (_live(head="a" * 40), "head_drift"),
        (_live(digest="b" * 64), "contract_drift"),
    ],
)
def test_terminal_or_drifted_decision_becomes_stale_without_task(tmp_path, live, reason):
    reconciler = _reconciler(tmp_path, live)

    result = reconciler.reconcile()

    assert [(item.outcome, item.reason) for item in result] == [("stale", reason)]
    assert not (tmp_path / "approvals.jsonl").exists()
    assert _task_rows(tmp_path / "kanban.db") == []
