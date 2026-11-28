import json

import pytest

from gateway.github_approval_broker import ApprovalProposal

from hermes_cli.kanban_db_connect import connect as connect_kanban
from hermes_cli.kanban_db import create_task
from tests.gateway.test_github_approval_reconciler import (
    DIGEST,
    HEAD,
    ITEM,
    _live,
    _reconciler,
    _task_rows,
)


def test_existing_successor_ledger_binding_reuses_live_canonical_task(tmp_path):
    """A late duplicate button must not create a second executor.

    The canonical successor binding can reach the append-only ledger just before
    an already-rendered redundant proposal is answered.  Reconciliation must
    consume that exact binding instead of treating the broker proposal as fresh
    authority and creating another Kanban task.
    """
    reconciler = _reconciler(tmp_path, _live())
    with reconciler._connect():
        row = reconciler._pending(1)[0]
    request_id = reconciler._request_id(row)

    conn = connect_kanban(tmp_path / "kanban.db")
    try:
        canonical_task = create_task(
            conn,
            title="canonical terminal continuation",
            body=f"existing approved successor for {ITEM} digest {DIGEST}",
            assignee="default",
            idempotency_key=f"github-terminal:{ITEM}:{HEAD}:{DIGEST}",
            completion_contract="local-only",
        )
    finally:
        conn.close()

    reconciler.ledger_path.write_text(
        json.dumps(
            {
                "schema": "hermes.github-approval.v1",
                "approval_request_id": request_id,
                "status": "approved_terminal_successor_contract",
                "work_item": ITEM,
                "head_sha": HEAD,
                "contract_digest_sha256": DIGEST,
                "executor": "Hermes",
                "inherited_task_id": canonical_task,
                "inherited_from_approval_request_id": f"hermes:github:{ITEM}:original:{DIGEST[:16]}",
                "original_decision_id": 183,
                "reconciliation_reason": "same material authority and descendant head",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    result = reconciler.reconcile()

    assert result[0].task_id == canonical_task
    assert len(_task_rows(tmp_path / "kanban.db")) == 1
    with reconciler._connect() as db:
        receipt = db.execute(
            "SELECT task_id FROM github_approval_reconciliations WHERE decision_id=?",
            (row["decision_id"],),
        ).fetchone()
    assert receipt[0] == canonical_task


def test_existing_successor_ledger_binding_rejects_unrelated_live_task(tmp_path):
    reconciler = _reconciler(tmp_path, _live())
    with reconciler._connect():
        row = reconciler._pending(1)[0]
    request_id = reconciler._request_id(row)

    conn = connect_kanban(tmp_path / "kanban.db")
    try:
        unrelated = create_task(
            conn,
            title="unrelated live work",
            body="another repository and contract",
            assignee="default",
            idempotency_key="github-terminal:unrelated",
            completion_contract="local-only",
        )
    finally:
        conn.close()
    reconciler.ledger_path.write_text(
        json.dumps(
            {
                "schema": "hermes.github-approval.v1",
                "approval_request_id": request_id,
                "status": "approved_terminal_successor_contract",
                "work_item": ITEM,
                "head_sha": HEAD,
                "contract_digest_sha256": DIGEST,
                "executor": "Hermes",
                "inherited_task_id": unrelated,
                "inherited_from_approval_request_id": f"hermes:github:{ITEM}:original:{DIGEST[:16]}",
                "original_decision_id": 183,
                "reconciliation_reason": "claimed successor",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="task conflicts"):
        reconciler.reconcile()

    assert len(_task_rows(tmp_path / "kanban.db")) == 1


def test_broker_inherited_successor_replays_its_own_ledger_record(tmp_path):
    reconciler = _reconciler(tmp_path, _live())
    first = reconciler.reconcile()
    canonical_task = first[0].task_id
    successor_head = "7" * 40
    successor = ApprovalProposal(ITEM, successor_head, DIGEST)
    with reconciler._connect() as db:
        predecessor_key = db.execute(
            "SELECT proposal_key FROM github_approval_proposals ORDER BY created_at LIMIT 1"
        ).fetchone()[0]
        now = 1_794_591_244.0
        key = successor.canonical_key()
        db.execute(
            """INSERT INTO github_approval_proposals(
                proposal_key,item_key,head_sha,contract_digest_sha256,executor,state,clarify_id,
                session_key,chat_id,authorized_user_id,destination_id,question,message_id,
                callback_bound,response,approver,created_at,updated_at)
                VALUES(?,?,?,?,?,'answered',?,?,?,?,?,?,NULL,0,'Approve',?,?,?)""",
            (key, ITEM, successor_head, DIGEST, "hermes", "gha-successor", "github:successor",
             "42", "7", "telegram-bot:test", "Approve bounded successor?",
             "telegram:7", now, now),
        )
        db.execute(
            """INSERT INTO github_approval_decisions(
                proposal_key,response,approver,state,decided_at)
                VALUES(?,'Approve','telegram:7','pending',?)""",
            (key, now),
        )
        db.execute(
            "INSERT INTO github_approval_inheritances VALUES(?,?,?,?)",
            (key, predecessor_key, canonical_task, now),
        )
    reconciler.live_reader = lambda _item: _live(head=successor_head)

    reconciler.reconcile(stop_after="ledger")
    resumed = reconciler.reconcile()

    assert resumed[0].task_id == canonical_task
    assert len(_task_rows(tmp_path / "kanban.db")) == 1
