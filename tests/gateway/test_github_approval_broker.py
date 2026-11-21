import asyncio
import multiprocessing
import sqlite3
import time
from dataclasses import replace

import pytest

from gateway.github_approval_broker import (
    ApprovalProposal,
    GitHubApprovalBroker,
)
from gateway.platforms.base import SendResult


class TelegramStub:
    def __init__(self, *, fail=False, omit_message_id=False, bind=True, destination_id="telegram-bot:101"):
        self.fail = fail
        self.omit_message_id = omit_message_id
        self.bind = bind
        self.sent = []
        self.retired = []
        self.deleted = []
        self._clarify_state = {}
        self.destination_id = destination_id

    def approval_destination_identity(self, clarify_id=None):
        return self.destination_id

    async def send_clarify(self, **kwargs):
        self.sent.append(kwargs)
        if self.fail:
            return SendResult(success=False, error="telegram unavailable")
        if self.bind:
            self._clarify_state[kwargs["clarify_id"]] = kwargs["session_key"]
        return SendResult(
            success=True,
            message_id=None if self.omit_message_id else str(100 + len(self.sent)),
        )

    async def retire_clarify_card(self, clarify_id, notice):
        self.retired.append((clarify_id, notice))
        self._clarify_state.pop(clarify_id, None)

    async def delete_clarify_card(self, clarify_id):
        self.deleted.append(clarify_id)
        self._clarify_state.pop(clarify_id, None)
        return True


def _process_retry(path, barrier, results):
    async def run():
        adapter = TelegramStub()
        broker = GitHubApprovalBroker(path, instance_id=f"worker-{multiprocessing.current_process().pid}")
        barrier.wait()
        outcomes = await broker.retry_pending(adapter=adapter)
        results.put((len(adapter.sent), [outcome.action for outcome in outcomes]))
    asyncio.run(run())


def _process_claim_then_crash(path, entered):
    async def run():
        broker = GitHubApprovalBroker(path, instance_id="crashing", delivery_lease_seconds=1)

        async def crash_gap(_key, _adapter):
            entered.set()
            await asyncio.Event().wait()

        broker._deliver_claim = crash_gap
        await broker.retry_pending(adapter=TelegramStub())
    asyncio.run(run())


def proposal(*, head="a" * 40, digest="b" * 64):
    return ApprovalProposal(
        item_key="owner/repo#497",
        head_sha=head,
        contract_digest_sha256=digest,
        executor="hermes",
    )


@pytest.mark.asyncio
async def test_local_broker_invocation_publishes_one_telegram_native_card(tmp_path):
    adapter = TelegramStub()
    broker = GitHubApprovalBroker(tmp_path / "approvals.db", instance_id="local")

    outcome = await broker.request(
        proposal(), adapter=adapter, chat_id="327791380",
        question="Approve owner/repo#497?", session_key="cli:local", authorized_user_id="7",
    )

    assert outcome.action == "sent"
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["choices"] == ["Approve", "Do not approve"]
    assert adapter.sent[0]["metadata"]["is_approval_prompt"] is True
    assert broker.observe(proposal())["state"] == "active"
    assert broker.observe(proposal())["message_id"] == "101"


@pytest.mark.asyncio
async def test_changed_digest_or_head_supersedes_and_reissues(tmp_path):
    adapter = TelegramStub()
    broker = GitHubApprovalBroker(tmp_path / "approvals.db", instance_id="gateway")
    first = await broker.request(
        proposal(), adapter=adapter, chat_id="42", question="first", session_key="webhook:1", authorized_user_id="7")
    changed = replace(proposal(), contract_digest_sha256="c" * 64, head_sha="d" * 40)

    second = await broker.request(
        changed, adapter=adapter, chat_id="42", question="second", session_key="webhook:2", authorized_user_id="7")

    assert first.action == "sent" and second.action == "sent"
    assert len(adapter.sent) == 2
    assert adapter.retired == []
    assert adapter.deleted == [first.clarify_id]
    observed_old = broker.observe(proposal())
    assert observed_old is not None
    assert observed_old["state"] == "superseded"
    assert observed_old["retirement_pending"] == 0
    assert broker.observe(changed)["state"] == "active"


@pytest.mark.asyncio
async def test_superseded_tuple_replay_cannot_resurrect_old_contract(tmp_path):
    adapter = TelegramStub()
    broker = GitHubApprovalBroker(tmp_path / "approvals.db", instance_id="gateway")
    old = proposal()
    new = replace(old, head_sha="d" * 40)
    first = await broker.request(
        old, adapter=adapter, chat_id="42", question="approve", session_key="webhook:1",
        authorized_user_id="7")
    await broker.request(
        new, adapter=adapter, chat_id="42", question="approve", session_key="webhook:2",
        authorized_user_id="7")

    replay = await broker.request(
        old, adapter=adapter, chat_id="42", question="approve", session_key="webhook:3",
        authorized_user_id="7")

    assert replay.action == "superseded"
    assert broker.observe(old)["state"] == "superseded"
    assert broker.observe(new)["state"] == "active"
    assert len(adapter.sent) == 2
    assert first.clarify_id == replay.clarify_id


@pytest.mark.asyncio
async def test_identical_tuple_with_changed_authority_binding_fails_closed(tmp_path):
    adapter = TelegramStub()
    broker = GitHubApprovalBroker(tmp_path / "approvals.db", instance_id="gateway")
    await broker.request(
        proposal(), adapter=adapter, chat_id="42", question="approve", session_key="webhook:1",
        authorized_user_id="7")

    conflict = await broker.request(
        proposal(), adapter=adapter, chat_id="42", question="approve", session_key="webhook:2",
        authorized_user_id="8")

    assert conflict.action == "binding_conflict"
    assert broker.observe(proposal())["state"] == "active"
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_identical_tuple_deduplicates_across_sessions(tmp_path):
    adapter = TelegramStub()
    path = tmp_path / "approvals.db"
    first = GitHubApprovalBroker(path, instance_id="webhook")
    second = GitHubApprovalBroker(path, instance_id="cli")

    sent = await first.request(
        proposal(), adapter=adapter, chat_id="42", question="approve", session_key="webhook:1", authorized_user_id="7")
    reused = await second.request(
        proposal(), adapter=adapter, chat_id="42", question="approve", session_key="cli:1", authorized_user_id="7")

    assert sent.action == "sent"
    assert reused.action == "reused"
    assert reused.clarify_id == sent.clarify_id
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_dispatch_lease_and_owner_cas_block_concurrent_republish(tmp_path):
    class BlockingTelegram(TelegramStub):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def send_clarify(self, **kwargs):
            self.sent.append(kwargs)
            self._clarify_state[kwargs["clarify_id"]] = kwargs["session_key"]
            self.entered.set()
            await self.release.wait()
            return SendResult(success=True, message_id="101")

    adapter = BlockingTelegram()
    path = tmp_path / "approvals.db"
    first = GitHubApprovalBroker(path, instance_id="webhook", delivery_lease_seconds=10)
    second = GitHubApprovalBroker(path, instance_id="cli", delivery_lease_seconds=10)
    publishing = asyncio.create_task(first.request(
        proposal(), adapter=adapter, chat_id="42", question="approve",
        session_key="webhook:1", authorized_user_id="7"))
    await adapter.entered.wait()

    dispatching = first.observe(proposal())
    concurrent = await second.request(
        proposal(), adapter=adapter, chat_id="42", question="approve",
        session_key="webhook:1", authorized_user_id="7")

    assert dispatching["state"] == "dispatching"
    assert dispatching["owner_instance_id"] == "webhook"
    assert dispatching["lease_expires_at"] > dispatching["updated_at"]
    assert concurrent.action == "operator_reconciliation"
    assert len(adapter.sent) == 1

    adapter.release.set()
    assert (await publishing).action == "sent"
    assert first.observe(proposal())["lease_expires_at"] is None


@pytest.mark.asyncio
async def test_identical_approved_tuple_replays_decision_without_republishing(tmp_path):
    adapter = TelegramStub()
    broker = GitHubApprovalBroker(tmp_path / "approvals.db", instance_id="gateway")
    sent = await broker.request(
        proposal(), adapter=adapter, chat_id="42", question="approve",
        session_key="webhook:1", authorized_user_id="7")
    broker.resolve_callback(
        sent.clarify_id, 0, approver="telegram:7", user_id="7", chat_id="42",
        thread_id=None, message_id="101", destination_id="telegram-bot:101")

    replay = await GitHubApprovalBroker(broker.path, instance_id="cli").request(
        proposal(), adapter=adapter, chat_id="42", question="approve",
        session_key="webhook:2", authorized_user_id="7")

    assert replay.action == "answered"
    assert replay.response == "Approve"
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_approved_descendant_same_contract_inherits_without_new_card(tmp_path):
    adapter = TelegramStub()
    path = tmp_path / "approvals.db"
    kanban = tmp_path / "kanban.db"
    broker = GitHubApprovalBroker(path, instance_id="gateway")
    old = proposal(head="a" * 40)
    new = proposal(head="c" * 40)
    old_question = f"Approve?\nHead: {old.head_sha}\nRisk: bounded"
    new_question = f"Approve?\nHead: {new.head_sha}\nRisk: bounded"
    sent = await broker.request(
        old, adapter=adapter, chat_id="42", question=old_question,
        session_key="webhook:1", authorized_user_id="7")
    assert sent.clarify_id is not None
    broker.resolve_callback(
        sent.clarify_id, 0, approver="telegram:7", user_id="7", chat_id="42",
        thread_id=None, message_id="101", destination_id="telegram-bot:101")
    with sqlite3.connect(path) as db:
        decision_id = db.execute(
            "SELECT decision_id FROM github_approval_decisions WHERE proposal_key=?",
            (old.canonical_key(),)).fetchone()[0]
        db.execute("""CREATE TABLE github_approval_reconciliations (
            decision_id INTEGER PRIMARY KEY, proposal_key TEXT NOT NULL UNIQUE,
            outcome TEXT NOT NULL, reason TEXT, approval_request_id TEXT,
            task_id TEXT, completed_at REAL NOT NULL)""")
        db.execute(
            "INSERT INTO github_approval_reconciliations VALUES(?,?, 'approved',NULL,'req','t_same',?)",
            (decision_id, old.canonical_key(), time.time()))
    with sqlite3.connect(kanban) as db:
        db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT NOT NULL,block_kind TEXT)")
        db.execute("INSERT INTO tasks VALUES('t_same','running',NULL)")

    inherited = broker.try_inherit_successor(
        new, question=new_question, session_key="webhook:2", chat_id="42",
        authorized_user_id="7", destination_id="telegram-bot:101", thread_id=None,
        kanban_path=kanban, ancestry_checker=lambda _item, old_head, new_head:
        old_head == old.head_sha and new_head == new.head_sha)

    assert inherited is not None and inherited.action == "inherited"
    assert inherited.response == "Approve"
    assert len(adapter.sent) == 1
    observed = broker.observe(new)
    assert observed is not None and observed["state"] == "answered"
    with sqlite3.connect(path) as db:
        lineage = db.execute(
            "SELECT predecessor_proposal_key,task_id FROM github_approval_inheritances "
            "WHERE proposal_key=?", (new.canonical_key(),)).fetchone()
        decision = db.execute(
            "SELECT state FROM github_approval_decisions WHERE proposal_key=?",
            (new.canonical_key(),)).fetchone()
    assert lineage == (old.canonical_key(), "t_same")
    assert decision == ("pending",)


@pytest.mark.asyncio
async def test_blocked_predecessor_inherits_only_through_bound_live_internal_continuation(tmp_path):
    adapter = TelegramStub()
    path = tmp_path / "approvals.db"
    kanban = tmp_path / "kanban.db"
    broker = GitHubApprovalBroker(path, instance_id="gateway")
    old = proposal(head="a" * 40)
    new = proposal(head="c" * 40)
    old_question = f"Approve?\nHead: {old.head_sha}\nRisk: bounded"
    new_question = f"Approve?\nHead: {new.head_sha}\nRisk: bounded"
    sent = await broker.request(
        old, adapter=adapter, chat_id="42", question=old_question,
        session_key="webhook:1", authorized_user_id="7")
    broker.resolve_callback(
        sent.clarify_id, 0, approver="telegram:7", user_id="7", chat_id="42",
        thread_id=None, message_id="101", destination_id="telegram-bot:101")
    with sqlite3.connect(path) as db:
        decision_id = db.execute(
            "SELECT decision_id FROM github_approval_decisions WHERE proposal_key=?",
            (old.canonical_key(),)).fetchone()[0]
        db.execute("""CREATE TABLE github_approval_reconciliations (
            decision_id INTEGER PRIMARY KEY, proposal_key TEXT NOT NULL UNIQUE,
            outcome TEXT NOT NULL, reason TEXT, approval_request_id TEXT,
            task_id TEXT, completed_at REAL NOT NULL)""")
        db.execute(
            "INSERT INTO github_approval_reconciliations VALUES(?,?, 'approved',NULL,'req','t_blocked',?)",
            (decision_id, old.canonical_key(), time.time()))
    continuation_key = "github-corrective:owner/repo#497:" + old.head_sha + ":" + old.contract_digest_sha256
    with sqlite3.connect(kanban) as db:
        db.execute("""CREATE TABLE tasks(
            id TEXT PRIMARY KEY,status TEXT NOT NULL,idempotency_key TEXT,block_kind TEXT)""")
        db.execute("INSERT INTO tasks VALUES('t_blocked','blocked',NULL,'needs_input')")
        db.execute("INSERT INTO tasks VALUES('t_fix','running',?,NULL)", (continuation_key,))
    broker.bind_internal_continuation(
        predecessor_proposal_key=old.canonical_key(),
        predecessor_task_id="t_blocked",
        continuation_task_id="t_fix",
        kanban_path=kanban,
    )
    redundant = proposal(head="b" * 40)
    redundant_sent = await broker.request(
        redundant, adapter=adapter, chat_id="42",
        question=f"Approve?\nHead: {redundant.head_sha}\nRisk: bounded",
        session_key="webhook:redundant", authorized_user_id="7")
    assert redundant_sent.action == "sent"

    inherited = broker.try_inherit_successor(
        new, question=new_question, session_key="webhook:2", chat_id="42",
        authorized_user_id="7", destination_id="telegram-bot:101", thread_id=None,
        kanban_path=kanban, ancestry_checker=lambda *_args: True)

    assert inherited is not None and inherited.action == "inherited"
    with sqlite3.connect(path) as db:
        lineage = db.execute(
            "SELECT predecessor_proposal_key,task_id FROM github_approval_inheritances "
            "WHERE proposal_key=?", (new.canonical_key(),)).fetchone()
        binding = db.execute(
            "SELECT predecessor_task_id,continuation_task_id,continuation_idempotency_key "
            "FROM github_approval_continuations WHERE predecessor_proposal_key=?",
            (old.canonical_key(),)).fetchone()
    assert lineage == (old.canonical_key(), "t_fix")
    assert binding == ("t_blocked", "t_fix", continuation_key)
    redundant_observed = broker.observe(redundant)
    assert redundant_observed is not None
    assert redundant_observed["state"] == "superseded"
    await broker.retire_inherited_cards(inherited, adapter=adapter)
    assert adapter.deleted == [redundant_sent.clarify_id]
    assert len(adapter.sent) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["missing_binding", "dead_continuation", "terminal_item"])
async def test_blocked_predecessor_continuation_inheritance_fails_closed(tmp_path, failure_mode):
    adapter = TelegramStub()
    path = tmp_path / "approvals.db"
    kanban = tmp_path / "kanban.db"
    broker = GitHubApprovalBroker(path, instance_id="gateway")
    old = proposal(head="a" * 40)
    new = proposal(head="c" * 40)
    question = "Risk: bounded"
    sent = await broker.request(
        old, adapter=adapter, chat_id="42", question=f"Head: {old.head_sha}\n{question}",
        session_key="webhook:1", authorized_user_id="7")
    broker.resolve_callback(
        sent.clarify_id, 0, approver="telegram:7", user_id="7", chat_id="42",
        thread_id=None, message_id="101", destination_id="telegram-bot:101")
    with sqlite3.connect(path) as db:
        decision_id = db.execute(
            "SELECT decision_id FROM github_approval_decisions WHERE proposal_key=?",
            (old.canonical_key(),)).fetchone()[0]
        db.execute("""CREATE TABLE github_approval_reconciliations (
            decision_id INTEGER PRIMARY KEY, proposal_key TEXT NOT NULL UNIQUE,
            outcome TEXT NOT NULL, reason TEXT, approval_request_id TEXT,
            task_id TEXT, completed_at REAL NOT NULL)""")
        db.execute(
            "INSERT INTO github_approval_reconciliations VALUES(?,?, 'approved',NULL,'req','t_blocked',?)",
            (decision_id, old.canonical_key(), time.time()))
    continuation_key = "github-corrective:owner/repo#497:" + old.head_sha + ":" + old.contract_digest_sha256
    with sqlite3.connect(kanban) as db:
        db.execute("""CREATE TABLE tasks(
            id TEXT PRIMARY KEY,status TEXT NOT NULL,idempotency_key TEXT,block_kind TEXT)""")
        db.execute("INSERT INTO tasks VALUES('t_blocked','blocked',NULL,'needs_input')")
        db.execute("INSERT INTO tasks VALUES('t_fix','running',?,NULL)", (continuation_key,))
    if failure_mode != "missing_binding":
        broker.bind_internal_continuation(
            predecessor_proposal_key=old.canonical_key(),
            predecessor_task_id="t_blocked",
            continuation_task_id="t_fix",
            kanban_path=kanban,
        )
    if failure_mode == "dead_continuation":
        with sqlite3.connect(kanban) as db:
            db.execute("UPDATE tasks SET status='done' WHERE id='t_fix'")
    if failure_mode == "terminal_item":
        redundant = proposal(head="b" * 40)
        redundant_sent = await broker.request(
            redundant, adapter=adapter, chat_id="42",
            question=f"Head: {redundant.head_sha}\n{question}",
            session_key="webhook:terminal", authorized_user_id="7")
        assert redundant_sent.action == "sent"
        assert await broker.retire_item(old.item_key, status="closed", adapter=adapter) == 1

    inherited = broker.try_inherit_successor(
        new, question=f"Head: {new.head_sha}\n{question}", session_key="webhook:2",
        chat_id="42", authorized_user_id="7", destination_id="telegram-bot:101",
        thread_id=None, kanban_path=kanban, ancestry_checker=lambda *_args: True)

    assert inherited is None
    assert broker.observe(new) is None


@pytest.mark.asyncio
async def test_pending_exact_successor_is_converted_to_inherited_and_its_card_is_retired(tmp_path):
    adapter = TelegramStub()
    path = tmp_path / "approvals.db"
    kanban = tmp_path / "kanban.db"
    broker = GitHubApprovalBroker(path, instance_id="gateway")
    old = proposal(head="a" * 40)
    new = proposal(head="c" * 40)
    old_question = f"Approve?\nHead: {old.head_sha}\nRisk: bounded"
    new_question = f"Approve?\nHead: {new.head_sha}\nRisk: bounded"
    sent = await broker.request(
        old, adapter=adapter, chat_id="42", question=old_question,
        session_key="webhook:1", authorized_user_id="7")
    broker.resolve_callback(
        sent.clarify_id, 0, approver="telegram:7", user_id="7", chat_id="42",
        thread_id=None, message_id="101", destination_id="telegram-bot:101")
    with sqlite3.connect(path) as db:
        decision_id = db.execute(
            "SELECT decision_id FROM github_approval_decisions WHERE proposal_key=?",
            (old.canonical_key(),)).fetchone()[0]
        db.execute("""CREATE TABLE github_approval_reconciliations (
            decision_id INTEGER PRIMARY KEY, proposal_key TEXT NOT NULL UNIQUE,
            outcome TEXT NOT NULL, reason TEXT, approval_request_id TEXT,
            task_id TEXT, completed_at REAL NOT NULL)""")
        db.execute(
            "INSERT INTO github_approval_reconciliations VALUES(?,?, 'approved',NULL,'req','t_blocked',?)",
            (decision_id, old.canonical_key(), time.time()))
    continuation_key = "github-corrective:owner/repo#497:" + old.head_sha + ":" + old.contract_digest_sha256
    with sqlite3.connect(kanban) as db:
        db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT NOT NULL,idempotency_key TEXT,block_kind TEXT)")
        db.execute("INSERT INTO tasks VALUES('t_blocked','blocked',NULL,'needs_input')")
        db.execute("INSERT INTO tasks VALUES('t_fix','running',?,NULL)", (continuation_key,))
    pending = await broker.request(
        new, adapter=adapter, chat_id="42", question=new_question,
        session_key="webhook:2", authorized_user_id="7")
    assert pending.action == "sent"
    broker.bind_internal_continuation(
        predecessor_proposal_key=old.canonical_key(), predecessor_task_id="t_blocked",
        continuation_task_id="t_fix", kanban_path=kanban)

    inherited = broker.try_inherit_successor(
        new, question=new_question, session_key="webhook:2", chat_id="42",
        authorized_user_id="7", destination_id="telegram-bot:101", thread_id=None,
        kanban_path=kanban, ancestry_checker=lambda *_args: True)

    assert inherited is not None and inherited.action == "inherited"
    assert inherited.clarify_id == pending.clarify_id
    assert inherited.retired_clarify_ids == (pending.clarify_id,)
    delete = adapter.delete_clarify_card

    async def fail_delete(_clarify_id):
        return False

    adapter.delete_clarify_card = fail_delete
    assert await broker.retire_inherited_cards(inherited, adapter=adapter) == 0
    observed = broker.observe(new)
    assert observed is not None and observed["state"] == "answered"
    assert observed["response"] == "Approve"
    assert observed["retirement_pending"] == 1
    assert observed["retirement_action"] == "delete"

    adapter.delete_clarify_card = delete
    assert await broker.retry_retirements(adapter=adapter) == 1
    assert adapter.deleted == [pending.clarify_id]
    assert broker.observe(new)["retirement_pending"] == 0


@pytest.mark.asyncio
async def test_internal_continuation_binding_rejects_arbitrary_or_nonlive_task(tmp_path):
    adapter = TelegramStub()
    path = tmp_path / "approvals.db"
    kanban = tmp_path / "kanban.db"
    broker = GitHubApprovalBroker(path, instance_id="gateway")
    old = proposal(head="a" * 40)
    sent = await broker.request(
        old, adapter=adapter, chat_id="42", question=f"Head: {old.head_sha}\nRisk: bounded",
        session_key="webhook:1", authorized_user_id="7")
    broker.resolve_callback(
        sent.clarify_id, 0, approver="telegram:7", user_id="7", chat_id="42",
        thread_id=None, message_id="101", destination_id="telegram-bot:101")
    with sqlite3.connect(path) as db:
        decision_id = db.execute(
            "SELECT decision_id FROM github_approval_decisions WHERE proposal_key=?",
            (old.canonical_key(),)).fetchone()[0]
        db.execute("""CREATE TABLE github_approval_reconciliations (
            decision_id INTEGER PRIMARY KEY, proposal_key TEXT NOT NULL UNIQUE,
            outcome TEXT NOT NULL, reason TEXT, approval_request_id TEXT,
            task_id TEXT, completed_at REAL NOT NULL)""")
        db.execute(
            "INSERT INTO github_approval_reconciliations VALUES(?,?, 'approved',NULL,'req','t_blocked',?)",
            (decision_id, old.canonical_key(), time.time()))
    canonical = "github-corrective:owner/repo#497:" + old.head_sha + ":" + old.contract_digest_sha256
    with sqlite3.connect(kanban) as db:
        db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT NOT NULL,idempotency_key TEXT,block_kind TEXT)")
        db.execute("INSERT INTO tasks VALUES('t_blocked','blocked',NULL,'needs_input')")
        db.execute("INSERT INTO tasks VALUES('t_arbitrary','running','github-corrective:other/repo#1:x:y',NULL)")
        db.execute("INSERT INTO tasks VALUES('t_dead','done',?,NULL)", (canonical,))

    with pytest.raises(ValueError, match="canonical idempotency"):
        broker.bind_internal_continuation(
            predecessor_proposal_key=old.canonical_key(), predecessor_task_id="t_blocked",
            continuation_task_id="t_arbitrary", kanban_path=kanban)
    with pytest.raises(ValueError, match="live"):
        broker.bind_internal_continuation(
            predecessor_proposal_key=old.canonical_key(), predecessor_task_id="t_blocked",
            continuation_task_id="t_dead", kanban_path=kanban)


@pytest.mark.asyncio
async def test_successor_inheritance_fails_closed_on_material_question_change(tmp_path):
    adapter = TelegramStub()
    path = tmp_path / "approvals.db"
    kanban = tmp_path / "kanban.db"
    broker = GitHubApprovalBroker(path, instance_id="gateway")
    old = proposal(head="a" * 40)
    new = proposal(head="c" * 40)
    sent = await broker.request(
        old, adapter=adapter, chat_id="42", question=f"Head: {old.head_sha}\nRisk: bounded",
        session_key="webhook:1", authorized_user_id="7")
    assert sent.clarify_id is not None
    broker.resolve_callback(
        sent.clarify_id, 0, approver="telegram:7", user_id="7", chat_id="42",
        thread_id=None, message_id="101", destination_id="telegram-bot:101")
    with sqlite3.connect(path) as db:
        decision_id = db.execute(
            "SELECT decision_id FROM github_approval_decisions WHERE proposal_key=?",
            (old.canonical_key(),)).fetchone()[0]
        db.execute("""CREATE TABLE github_approval_reconciliations (
            decision_id INTEGER PRIMARY KEY, proposal_key TEXT NOT NULL UNIQUE,
            outcome TEXT NOT NULL, reason TEXT, approval_request_id TEXT,
            task_id TEXT, completed_at REAL NOT NULL)""")
        db.execute(
            "INSERT INTO github_approval_reconciliations VALUES(?,?, 'approved',NULL,'req','t_same',?)",
            (decision_id, old.canonical_key(), time.time()))
    with sqlite3.connect(kanban) as db:
        db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT NOT NULL,block_kind TEXT)")
        db.execute("INSERT INTO tasks VALUES('t_same','running',NULL)")

    inherited = broker.try_inherit_successor(
        new, question=f"Head: {new.head_sha}\nRisk: expanded", session_key="webhook:2",
        chat_id="42", authorized_user_id="7", destination_id="telegram-bot:101",
        thread_id=None, kanban_path=kanban,
        ancestry_checker=lambda *_args: True)

    assert inherited is None
    assert broker.observe(new) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["terminal_predecessor", "task_finishes_during_ancestry"])
async def test_successor_inheritance_rechecks_terminal_boundaries(tmp_path, failure_mode):
    adapter = TelegramStub()
    path = tmp_path / "approvals.db"
    kanban = tmp_path / "kanban.db"
    broker = GitHubApprovalBroker(path, instance_id="gateway")
    old = proposal(head="a" * 40)
    new = proposal(head="c" * 40)
    old_question = f"Head: {old.head_sha}\nRisk: bounded"
    new_question = f"Head: {new.head_sha}\nRisk: bounded"
    sent = await broker.request(
        old, adapter=adapter, chat_id="42", question=old_question,
        session_key="webhook:1", authorized_user_id="7")
    assert sent.clarify_id is not None
    broker.resolve_callback(
        sent.clarify_id, 0, approver="telegram:7", user_id="7", chat_id="42",
        thread_id=None, message_id="101", destination_id="telegram-bot:101")
    with sqlite3.connect(path) as db:
        decision_id = db.execute(
            "SELECT decision_id FROM github_approval_decisions WHERE proposal_key=?",
            (old.canonical_key(),)).fetchone()[0]
        db.execute("""CREATE TABLE github_approval_reconciliations (
            decision_id INTEGER PRIMARY KEY, proposal_key TEXT NOT NULL UNIQUE,
            outcome TEXT NOT NULL, reason TEXT, approval_request_id TEXT,
            task_id TEXT, completed_at REAL NOT NULL)""")
        db.execute(
            "INSERT INTO github_approval_reconciliations VALUES(?,?, 'approved',NULL,'req','t_same',?)",
            (decision_id, old.canonical_key(), time.time()))
        if failure_mode == "terminal_predecessor":
            db.execute(
                "UPDATE github_approval_proposals SET state='superseded' WHERE proposal_key=?",
                (old.canonical_key(),))
    with sqlite3.connect(kanban) as db:
        db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT NOT NULL,block_kind TEXT)")
        db.execute("INSERT INTO tasks VALUES('t_same','running',NULL)")

    def ancestry(*_args):
        if failure_mode == "task_finishes_during_ancestry":
            with sqlite3.connect(kanban) as db:
                db.execute("UPDATE tasks SET status='done' WHERE id='t_same'")
        return True

    inherited = broker.try_inherit_successor(
        new, question=new_question, session_key="webhook:2", chat_id="42",
        authorized_user_id="7", destination_id="telegram-bot:101", thread_id=None,
        kanban_path=kanban, ancestry_checker=ancestry)

    assert inherited is None
    assert broker.observe(new) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["closed", "merged", "superseded"])
async def test_terminal_item_status_fail_closes_active_buttons_locally(tmp_path, status):
    adapter = TelegramStub()
    broker = GitHubApprovalBroker(tmp_path / "approvals.db", instance_id="gateway")
    await broker.request(
        proposal(), adapter=adapter, chat_id="42", question="approve",
        session_key="webhook:1", authorized_user_id="7")

    retired = await broker.retire_item(proposal().item_key, status=status, adapter=adapter)

    assert retired == 1
    assert adapter.retired == []
    observed = broker.observe(proposal())
    assert observed is not None
    assert observed["state"] == "superseded"
    assert observed["retirement_pending"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("adapter", "expected_message_id"), [
    (TelegramStub(omit_message_id=True), None),
    (TelegramStub(bind=False), "101"),
])
async def test_card_is_never_active_without_message_and_callback_binding(
        tmp_path, adapter, expected_message_id):
    broker = GitHubApprovalBroker(tmp_path / "approvals.db", instance_id="gateway")

    outcome = await broker.request(
        proposal(), adapter=adapter, chat_id="42", question="approve", session_key="webhook:1", authorized_user_id="7")

    assert outcome.action == "operator_reconciliation"
    observed = broker.observe(proposal())
    assert observed["state"] == "delivery_unknown"
    assert observed["message_id"] == expected_message_id


@pytest.mark.asyncio
async def test_delivery_failure_remains_retryable(tmp_path):
    path = tmp_path / "approvals.db"
    failed = GitHubApprovalBroker(path, instance_id="gateway-a")
    outcome = await failed.request(
        proposal(), adapter=TelegramStub(fail=True), chat_id="42",
        question="approve", session_key="webhook:1", authorized_user_id="7")
    assert outcome.action == "pending_retry"

    restarted = GitHubApprovalBroker(path, instance_id="gateway-b")
    adapter = TelegramStub()
    retried = await restarted.retry_pending(adapter=adapter)

    assert [item.action for item in retried] == ["sent"]
    assert restarted.observe(proposal())["state"] == "active"
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_delivery_failure_is_not_retried_by_another_telegram_bot(tmp_path):
    path = tmp_path / "approvals.db"
    broker = GitHubApprovalBroker(path, instance_id="gateway-a")
    await broker.request(
        proposal(), adapter=TelegramStub(fail=True), chat_id="42",
        question="approve", session_key="webhook:1", authorized_user_id="7")
    other_bot = TelegramStub(destination_id="telegram-bot:202")

    assert await GitHubApprovalBroker(path, instance_id="gateway-b").retry_pending(adapter=other_bot) == []
    assert other_bot.sent == []
    assert broker.observe(proposal())["state"] == "pending_notification"


def test_restart_recovery_lists_active_card_and_rebuilds_callback_binding(tmp_path):
    path = tmp_path / "approvals.db"
    adapter = TelegramStub()
    first = GitHubApprovalBroker(path, instance_id="gateway-a")
    asyncio.run(first.request(
        proposal(), adapter=adapter, chat_id="42", question="approve", session_key="webhook:1", authorized_user_id="7"))

    restarted = GitHubApprovalBroker(path, instance_id="gateway-b")
    fresh_adapter = TelegramStub()
    recovered = restarted.recover_callbacks(fresh_adapter)

    assert len(recovered) == 1
    assert fresh_adapter._clarify_state[recovered[0]["clarify_id"]] == "webhook:1"
    assert restarted.observe(proposal())["state"] == "active"


def test_restart_recovery_does_not_bind_card_to_another_telegram_bot(tmp_path):
    path = tmp_path / "approvals.db"
    publisher = TelegramStub()
    broker = GitHubApprovalBroker(path, instance_id="gateway-a")
    asyncio.run(broker.request(
        proposal(), adapter=publisher, chat_id="42", question="approve",
        session_key="webhook:1", authorized_user_id="7"))
    other_bot = TelegramStub(destination_id="telegram-bot:202")

    recovered = GitHubApprovalBroker(path, instance_id="gateway-b").recover_callbacks(other_bot)

    assert recovered == []
    assert other_bot._clarify_state == {}


def test_callback_persists_exact_decision_without_exposing_execution_claim(tmp_path):
    path = tmp_path / "approvals.db"
    adapter = TelegramStub()
    broker = GitHubApprovalBroker(path, instance_id="gateway-a")
    sent = asyncio.run(broker.request(
        proposal(), adapter=adapter, chat_id="42", question="approve", session_key="webhook:1", authorized_user_id="7"))

    decision = broker.resolve_callback(sent.clarify_id, 0, approver="telegram:7", user_id="7",
                                       chat_id="42", thread_id=None, message_id="101",
                                       destination_id="telegram-bot:101")
    assert decision is not None and decision.response == "Approve"
    restarted = GitHubApprovalBroker(path, instance_id="gateway-b")

    assert not hasattr(restarted, "claim_resumptions")
    assert not hasattr(restarted, "resume_approved")
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT state, response, approver FROM github_approval_decisions").fetchone()
    assert row == ("pending", "Approve", "telegram:7")


@pytest.mark.asyncio
async def test_async_completion_status_cannot_orphan_active_card(tmp_path):
    broker = GitHubApprovalBroker(tmp_path / "approvals.db", instance_id="gateway")
    adapter = TelegramStub()
    await broker.request(
        proposal(), adapter=adapter, chat_id="42", question="approve", session_key="webhook:1", authorized_user_id="7")

    broker.record_origin_status(proposal(), "origin_completed")

    observed = broker.observe(proposal())
    assert observed["state"] == "active"
    assert observed["origin_status"] == "origin_completed"


@pytest.mark.asyncio
async def test_inflight_supersession_fail_closes_card_sent_after_generation_changed(tmp_path):
    class BarrierTelegram(TelegramStub):
        def __init__(self):
            super().__init__()
            self.first_entered = asyncio.Event()
            self.release_first = asyncio.Event()

        async def send_clarify(self, **kwargs):
            self.sent.append(kwargs)
            if len(self.sent) == 1:
                self.first_entered.set()
                await self.release_first.wait()
            self._clarify_state[kwargs["clarify_id"]] = kwargs["session_key"]
            return SendResult(success=True, message_id=str(100 + len(self.sent)))

    adapter = BarrierTelegram()
    broker = GitHubApprovalBroker(tmp_path / "approvals.db", instance_id="gateway")
    old = proposal()
    new = replace(old, head_sha="c" * 40)
    sending_old = asyncio.create_task(broker.request(
        old, adapter=adapter, chat_id="42", question="old", session_key="webhook:1",
        authorized_user_id="7"))
    await adapter.first_entered.wait()
    sent_new = await broker.request(
        new, adapter=adapter, chat_id="42", question="new", session_key="webhook:2",
        authorized_user_id="7")
    adapter.release_first.set()
    stale = await sending_old

    assert sent_new.action == "sent"
    assert stale.action == "superseded"
    assert adapter.retired == []
    assert adapter.deleted == [stale.clarify_id]
    observed = broker.observe(old)
    assert observed is not None
    assert observed["retirement_pending"] == 0


@pytest.mark.asyncio
async def test_terminal_close_during_send_fail_closes_late_card_locally(tmp_path):
    class BarrierTelegram(TelegramStub):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def send_clarify(self, **kwargs):
            self.sent.append(kwargs)
            self.entered.set()
            await self.release.wait()
            self._clarify_state[kwargs["clarify_id"]] = kwargs["session_key"]
            return SendResult(success=True, message_id="101")

    adapter = BarrierTelegram()
    broker = GitHubApprovalBroker(tmp_path / "approvals.db", instance_id="gateway")
    sending = asyncio.create_task(broker.request(
        proposal(), adapter=adapter, chat_id="42", question="approve",
        session_key="webhook:1", authorized_user_id="7"))
    await adapter.entered.wait()
    await broker.retire_item(proposal().item_key, status="closed", adapter=adapter)
    adapter.release.set()
    outcome = await sending

    assert outcome.action == "superseded"
    assert adapter.retired == []
    assert adapter.deleted == [outcome.clarify_id]
    observed = broker.observe(proposal())
    assert observed is not None
    assert observed["retirement_pending"] == 0


def test_multiprocess_outbox_claim_has_one_transport_sender(tmp_path):
    path = tmp_path / "approvals.db"
    broker = GitHubApprovalBroker(path, instance_id="admission")
    broker.admit(
        proposal(), adapter=TelegramStub(), chat_id="42", question="approve",
        session_key="webhook:1", authorized_user_id="7")
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    results = ctx.Queue()
    workers = [ctx.Process(target=_process_retry, args=(path, barrier, results)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0
    outcomes = [results.get(timeout=2) for _ in workers]
    assert sum(sent for sent, _actions in outcomes) == 1


def test_crash_after_reservation_before_send_is_reclaimed_after_bounded_lease(tmp_path):
    path = tmp_path / "approvals.db"
    broker = GitHubApprovalBroker(path, instance_id="admission", delivery_lease_seconds=1)
    broker.admit(
        proposal(), adapter=TelegramStub(), chat_id="42", question="approve",
        session_key="webhook:1", authorized_user_id="7")
    ctx = multiprocessing.get_context("fork")
    entered = ctx.Event()
    worker = ctx.Process(target=_process_claim_then_crash, args=(path, entered))
    worker.start()
    assert entered.wait(5)
    worker.terminate()
    worker.join(5)
    reserved = broker.observe(proposal())
    assert reserved["state"] == "pending_notification"
    assert reserved["owner_instance_id"] == "crashing"

    time.sleep(1.1)  # health: allow HX013 -- this test exercises real wall-clock lease expiry
    adapter = TelegramStub()
    outcomes = asyncio.run(
        GitHubApprovalBroker(path, instance_id="recovery", delivery_lease_seconds=1)
        .retry_pending(adapter=adapter))
    assert [outcome.action for outcome in outcomes] == ["sent"]
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_terminal_retirement_never_calls_failing_cosmetic_edit(tmp_path):
    class FailingRetire(TelegramStub):
        async def retire_clarify_card(self, clarify_id, notice):
            raise TimeoutError("edit failed")

    broker = GitHubApprovalBroker(tmp_path / "approvals.db", instance_id="gateway")
    publisher = TelegramStub()
    await broker.request(
        proposal(), adapter=publisher, chat_id="42", question="approve",
        session_key="webhook:1", authorized_user_id="7")
    await broker.retire_item(proposal().item_key, status="closed", adapter=FailingRetire())
    assert broker.observe(proposal())["retirement_pending"] == 0
    assert await broker.retry_retirements(adapter=publisher) == 0


@pytest.mark.asyncio
async def test_supersession_never_calls_failing_cosmetic_edit(tmp_path):
    class FailSecondRetire(TelegramStub):
        async def retire_clarify_card(self, clarify_id, notice):
            raise TimeoutError("edit failed")

    broker = GitHubApprovalBroker(tmp_path / "approvals.db", instance_id="gateway")
    publisher = TelegramStub()
    await broker.request(
        proposal(), adapter=publisher, chat_id="42", question="old",
        session_key="webhook:1", authorized_user_id="7")
    changed = replace(proposal(), head_sha="c" * 40)
    failing = FailSecondRetire()
    outcome = await broker.request(
        changed, adapter=failing, chat_id="42",
        question="new", session_key="webhook:2", authorized_user_id="7")
    assert outcome.action == "sent"
    assert len(failing.sent) == 1
    observed_old = broker.observe(proposal())
    observed_new = broker.observe(changed)
    assert observed_old is not None and observed_new is not None
    assert observed_old["retirement_pending"] == 0
    assert observed_new["state"] == "active"
