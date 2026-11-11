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
            await asyncio.sleep(60)

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
    assert adapter.retired == [(first.clarify_id, "Superseded by a newer GitHub contract")]
    assert broker.observe(proposal())["state"] == "superseded"
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
@pytest.mark.parametrize("status", ["closed", "merged", "superseded"])
async def test_terminal_item_status_retires_active_buttons(tmp_path, status):
    adapter = TelegramStub()
    broker = GitHubApprovalBroker(tmp_path / "approvals.db", instance_id="gateway")
    sent = await broker.request(
        proposal(), adapter=adapter, chat_id="42", question="approve",
        session_key="webhook:1", authorized_user_id="7")

    retired = await broker.retire_item(proposal().item_key, status=status, adapter=adapter)

    assert retired == 1
    assert adapter.retired == [(sent.clarify_id, f"GitHub item {status}")]
    assert broker.observe(proposal())["state"] == "superseded"


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", [TelegramStub(omit_message_id=True), TelegramStub(bind=False)])
async def test_card_is_never_active_without_message_and_callback_binding(tmp_path, adapter):
    broker = GitHubApprovalBroker(tmp_path / "approvals.db", instance_id="gateway")

    outcome = await broker.request(
        proposal(), adapter=adapter, chat_id="42", question="approve", session_key="webhook:1", authorized_user_id="7")

    assert outcome.action == "operator_reconciliation"
    observed = broker.observe(proposal())
    assert observed["state"] == "delivery_unknown"
    assert observed["message_id"] is None


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


def test_callback_persists_decision_and_resumption_is_claimed_once(tmp_path):
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
    first_claim = restarted.claim_resumptions(owner="worker-1")
    second_claim = restarted.claim_resumptions(owner="worker-2")

    assert len(first_claim) == 1
    assert second_claim == []
    assert restarted.claim_resumptions(owner="worker-3") == []
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT state, approver FROM github_approval_decisions").fetchone()
    assert row == ("claimed", "telegram:7")


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
async def test_inflight_supersession_retires_card_sent_after_generation_changed(tmp_path):
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
    assert adapter.retired.count((stale.clarify_id, "Superseded during delivery")) == 1
    assert set(adapter._clarify_state) == {sent_new.clarify_id}


@pytest.mark.asyncio
async def test_terminal_close_during_send_retires_late_card(tmp_path):
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
    assert adapter.retired[-1] == (outcome.clarify_id, "Superseded during delivery")
    assert adapter._clarify_state == {}


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

    time.sleep(1.1)
    adapter = TelegramStub()
    outcomes = asyncio.run(
        GitHubApprovalBroker(path, instance_id="recovery", delivery_lease_seconds=1)
        .retry_pending(adapter=adapter))
    assert [outcome.action for outcome in outcomes] == ["sent"]
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_failed_terminal_retirement_is_durable_and_retried(tmp_path):
    class FailingRetire(TelegramStub):
        async def retire_clarify_card(self, clarify_id, notice):
            raise TimeoutError("edit failed")

    broker = GitHubApprovalBroker(tmp_path / "approvals.db", instance_id="gateway")
    publisher = TelegramStub()
    await broker.request(
        proposal(), adapter=publisher, chat_id="42", question="approve",
        session_key="webhook:1", authorized_user_id="7")
    await broker.retire_item(proposal().item_key, status="closed", adapter=FailingRetire())
    assert broker.observe(proposal())["retirement_pending"] == 1

    assert await broker.retry_retirements(adapter=publisher) == 1
    assert broker.observe(proposal())["retirement_pending"] == 0


@pytest.mark.asyncio
async def test_failed_supersession_retirement_is_durable(tmp_path):
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
    assert outcome.action == "pending_retry"
    assert failing.sent == []
    assert broker.observe(proposal())["retirement_pending"] == 1
    assert broker.observe(changed)["state"] == "pending_notification"
