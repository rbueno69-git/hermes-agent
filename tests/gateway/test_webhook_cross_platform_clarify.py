import asyncio
import sqlite3
import time
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.platforms.webhook import WebhookAdapter


@pytest.mark.asyncio
async def test_webhook_clarify_delegates_to_cross_platform_native_buttons():
    target = MagicMock()
    target.send_clarify = AsyncMock(return_value=SendResult(success=True))
    runner = MagicMock()
    runner._authorization_adapter.return_value = target

    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    adapter._delivery_info = {
        "opaque-delivery": {
            "deliver": "telegram",
            "deliver_extra": {"chat_id": "327791380", "message_thread_id": "42"},
        }
    }

    result = await adapter.send_clarify(
        chat_id="opaque-delivery",
        question="Approve?",
        choices=["Yes", "No"],
        clarify_id="clarify-1",
        session_key="webhook-session",
        metadata={"source": "test"},
    )

    assert result.success is True
    runner._authorization_adapter.assert_called_once_with(Platform.TELEGRAM, None)
    target.send_clarify.assert_awaited_once_with(
        chat_id="327791380",
        question="Approve?",
        choices=["Yes", "No"],
        clarify_id="clarify-1",
        session_key="webhook-session",
        metadata={"source": "test", "thread_id": "42", "is_approval_prompt": True},
    )


@pytest.mark.asyncio
async def test_webhook_clarify_without_interactive_destination_fails_before_wait():
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = MagicMock()
    adapter._delivery_info = {"opaque-delivery": {"deliver": "log"}}

    result = await adapter.send_clarify(
        chat_id="opaque-delivery",
        question="Approve?",
        choices=["Yes", "No"],
        clarify_id="clarify-2",
        session_key="webhook-session",
    )

    assert result.success is False
    assert "no interactive destination" in (result.error or "")


@pytest.mark.asyncio
async def test_webhook_timeout_retirement_reaches_delegated_adapter():
    target = MagicMock()
    target.send_clarify = AsyncMock(return_value=SendResult(success=True, message_id="501"))
    target.retire_clarify_card = AsyncMock()
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    adapter._delivery_info = {
        "delivery": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"}}
    }

    await adapter.send_clarify(
        chat_id="delivery", question="Approve?", choices=["Yes", "No"],
        clarify_id="clarify-timeout", session_key="session-timeout",
    )
    await adapter.retire_clarify_card("clarify-timeout", "Expired")

    target.retire_clarify_card.assert_awaited_once_with("clarify-timeout", "Expired")


@pytest.mark.asyncio
async def test_timeout_does_not_retire_a_durably_answered_card(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import clarify_gateway as cm

    binding = {
        "item_key": "owner/repo#161", "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64, "scope": "merge",
        "decision_digest_sha256": "3" * 64,
    }
    target = MagicMock()
    target.send_clarify = AsyncMock(return_value=SendResult(success=True, message_id="501"))
    target.retire_clarify_card = AsyncMock(return_value=True)
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    adapter._delivery_info = {
        "delivery": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"},
                     "approval_binding": binding}
    }
    cm.register("clarify-answered", "session-answered", "Approve?", ["Approve", "Reject"])
    await adapter.send_clarify(
        chat_id="delivery", question="Approve?", choices=["Approve", "Reject"],
        clarify_id="clarify-answered", session_key="session-answered",
    )
    assert cm.resolve_gateway_clarify("clarify-answered", "Approve") is True

    await adapter.retire_clarify_card("clarify-answered", "Expired")

    target.retire_clarify_card.assert_not_awaited()


@pytest.mark.asyncio
async def test_equivalent_webhook_approvals_publish_one_card_across_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import clarify_gateway as cm
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._linked_entries.clear()
        cm._resolution_cbs.clear()
        cm._resolution_probes.clear()
        if hasattr(cm, "_expiry_cbs"):
            cm._expiry_cbs.clear()
        cm._resolving.clear()
    cm.register("clarify-a", "session-a", "Aprovar?", ["Aprovar", "Não aprovar"])
    cm.register("clarify-b", "session-b", "Aprovar?", ["Aprovar", "Não aprovar"])
    target = MagicMock()
    target.send_clarify = AsyncMock(
        side_effect=[
            SendResult(success=True, message_id="501"),
            AssertionError("equivalent approval emitted a second card"),
        ]
    )
    runner = MagicMock()
    runner._authorization_adapter.return_value = target

    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    binding = {
        "item_key": "rbueno69-git/carteira-investimentos#160",
        "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64,
        "scope": "Somente leitura; merge e deploy autorizados.",
    }
    adapter._delivery_info = {
        "delivery-a": {
            "deliver": "telegram",
            "deliver_extra": {"chat_id": "327791380"},
            "approval_binding": binding,
        },
        "delivery-b": {
            "deliver": "telegram",
            "deliver_extra": {"chat_id": "327791380"},
            "approval_binding": dict(binding),
        },
    }

    first, second = await asyncio.gather(
        adapter.send_clarify(
            chat_id="delivery-a", question="Aprovar?", choices=["Aprovar", "Não aprovar"],
            clarify_id="clarify-a", session_key="session-a",
        ),
        adapter.send_clarify(
            chat_id="delivery-b", question="Aprovar?", choices=["Aprovar", "Não aprovar"],
            clarify_id="clarify-b", session_key="session-b",
        ),
    )

    assert first.success is True
    assert second.success is True
    assert target.send_clarify.await_count == 1
    assert {first.message_id, second.message_id} == {"501"}


@pytest.mark.asyncio
async def test_originating_publisher_observes_answer_committed_by_another_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.approval_card_ledger import ApprovalCardLedger
    from tools import clarify_gateway as cm

    binding = {
        "item_key": "owner/repo#161", "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64, "scope": "merge",
        "decision_digest_sha256": "3" * 64,
    }
    target = MagicMock()
    target.send_clarify = AsyncMock(return_value=SendResult(success=True, message_id="501"))
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    adapter._delivery_info = {
        "first": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"},
                  "approval_binding": binding}
    }
    cm.register("owner-card", "owner-session", "Approve?", ["Approve", "Reject"])
    await adapter.send_clarify(
        chat_id="first", question="Approve?", choices=["Approve", "Reject"],
        clarify_id="owner-card", session_key="owner-session",
    )

    remote = ApprovalCardLedger(tmp_path / "approval_cards.db", instance_id="gateway-b")
    assert remote.answer_choice("owner-card", 0) == "Approve"

    assert cm.wait_for_response("owner-card", timeout=3.0) == "Approve"


@pytest.mark.asyncio
async def test_originating_publisher_observes_remote_supersession(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.approval_card_ledger import ApprovalCardLedger
    from tools import clarify_gateway as cm

    binding = {
        "item_key": "owner/repo#161", "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64, "scope": "merge",
        "decision_digest_sha256": "3" * 64,
    }
    target = MagicMock()
    target.send_clarify = AsyncMock(return_value=SendResult(success=True, message_id="501"))
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    adapter._delivery_info = {
        "first": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"},
                  "approval_binding": binding}
    }
    cm.register("owner-card", "owner-session", "Approve?", ["Approve", "Reject"])
    await adapter.send_clarify(
        chat_id="first", question="Approve?", choices=["Approve", "Reject"],
        clarify_id="owner-card", session_key="owner-session",
    )

    remote = ApprovalCardLedger(tmp_path / "approval_cards.db", instance_id="gateway-b")
    replacement = remote.claim(
        {**binding, "contract_digest_sha256": "3" * 64},
        platform="telegram", chat_id="327791380", clarify_id="new-card",
        session_key="new-session", choices=["Approve", "Reject"],
    )
    assert replacement.action == "send"

    assert cm.wait_for_response("owner-card", timeout=3.0) == cm.CANCELLED


@pytest.mark.asyncio
async def test_material_change_retires_old_card_before_publishing_replacement(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import clarify_gateway as cm
    cm.register("clarify-old", "session-old", "Old?", ["Approve", "Reject"])
    cm.register("clarify-new", "session-new", "New?", ["Approve", "Reject"])
    calls = []
    target = MagicMock()

    async def _send(**kwargs):
        calls.append(("send", kwargs["clarify_id"]))
        return SendResult(success=True, message_id=str(500 + len(calls)))

    async def _retire(**kwargs):
        calls.append(("retire", kwargs["message_id"]))
        return True

    target.send_clarify = AsyncMock(side_effect=_send)
    target.retire_clarify = AsyncMock(side_effect=_retire)
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    old_binding = {
        "item_key": "rbueno69-git/carteira-investimentos#160",
        "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64,
        "scope": "Somente leitura.",
    }
    new_binding = {**old_binding, "contract_digest_sha256": "3" * 64}
    adapter._delivery_info = {
        "old": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"}, "approval_binding": old_binding},
        "new": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"}, "approval_binding": new_binding},
    }

    await adapter.send_clarify(
        chat_id="old", question="Old?", choices=["Approve", "Reject"],
        clarify_id="clarify-old", session_key="session-old",
    )
    await adapter.send_clarify(
        chat_id="new", question="New?", choices=["Approve", "Reject"],
        clarify_id="clarify-new", session_key="session-new",
    )

    assert calls == [("send", "clarify-old"), ("retire", "501"), ("send", "clarify-new")]


@pytest.mark.asyncio
async def test_submitted_decision_is_reused_after_gateway_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import clarify_gateway as cm

    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._linked_entries.clear()
        cm._resolution_cbs.clear()
        cm._resolution_probes.clear()
        if hasattr(cm, "_expiry_cbs"):
            cm._expiry_cbs.clear()
        cm._resolving.clear()

    binding = {
        "item_key": "rbueno69-git/carteira-investimentos#160",
        "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64,
        "scope": "Somente leitura.",
    }
    target = MagicMock()
    target.send_clarify = AsyncMock(return_value=SendResult(success=True, message_id="501"))
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    first = object.__new__(WebhookAdapter)
    first.gateway_runner = runner
    first._delivery_info = {
        "first": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"}, "approval_binding": binding}
    }
    cm.register("clarify-a", "session-a", "Approve?", ["Approve", "Reject"])
    await first.send_clarify(
        chat_id="first", question="Approve?", choices=["Approve", "Reject"],
        clarify_id="clarify-a", session_key="session-a",
    )
    assert cm.resolve_gateway_clarify("clarify-a", "Approve") is True
    assert cm.wait_for_response("clarify-a", timeout=1.0) == "Approve"

    restarted = object.__new__(WebhookAdapter)
    restarted.gateway_runner = runner
    restarted._delivery_info = {
        "replay": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"}, "approval_binding": binding}
    }
    target.send_clarify.reset_mock()
    cm.register("clarify-b", "session-b", "Approve?", ["Approve", "Reject"])

    result = await restarted.send_clarify(
        chat_id="replay", question="Approve?", choices=["Approve", "Reject"],
        clarify_id="clarify-b", session_key="session-b",
    )

    assert result.success is True
    target.send_clarify.assert_not_awaited()
    assert cm.wait_for_response("clarify-b", timeout=1.0) == "Approve"


@pytest.mark.asyncio
async def test_active_card_from_previous_gateway_is_reused_and_polled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.approval_card_ledger import ApprovalCardLedger
    from tools import clarify_gateway as cm

    binding = {
        "item_key": "owner/repo#1", "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64, "scope": "merge",
        "decision_digest_sha256": "3" * 64,
    }
    target = MagicMock()
    target.send_clarify = AsyncMock(return_value=SendResult(success=True, message_id="501"))
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    first = object.__new__(WebhookAdapter)
    first.gateway_runner = runner
    first._delivery_info = {
        "first": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"}, "approval_binding": binding}
    }
    cm.register("old-card", "old-session", "Approve?", ["Approve", "Reject"])
    await first.send_clarify(
        chat_id="first", question="Approve?", choices=["Approve", "Reject"],
        clarify_id="old-card", session_key="old-session",
    )
    cm.clear_session("old-session")

    restarted = object.__new__(WebhookAdapter)
    restarted.gateway_runner = runner
    restarted._delivery_info = {
        "replay": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"}, "approval_binding": binding}
    }
    target.send_clarify.reset_mock()
    cm.register("new-wait", "new-session", "Approve?", ["Approve", "Reject"])
    result = await restarted.send_clarify(
        chat_id="replay", question="Approve?", choices=["Approve", "Reject"],
        clarify_id="new-wait", session_key="new-session",
    )
    ledger = ApprovalCardLedger(tmp_path / "approval_cards.db", instance_id="test")
    assert ledger.answer_choice("old-card", 0) == "Approve"

    assert result.success is True
    target.send_clarify.assert_not_awaited()
    assert cm.wait_for_response("new-wait", timeout=3.0) == "Approve"


@pytest.mark.asyncio
async def test_material_successor_waits_until_ambiguous_predecessor_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import clarify_gateway as cm

    old_binding = {
        "item_key": "owner/repo#161", "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64, "scope": "merge",
        "decision_digest_sha256": "3" * 64,
    }
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    call_count = 0

    async def send_card(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            first_started.set()
            await release_first.wait()
        return SendResult(success=True, message_id=str(500 + call_count))

    target = MagicMock()
    target.send_clarify = AsyncMock(side_effect=send_card)
    target.retire_clarify = AsyncMock(return_value=True)
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    adapter._delivery_info = {
        "old": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"},
                "approval_binding": old_binding},
        "new": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"},
                "approval_binding": {**old_binding, "contract_digest_sha256": "3" * 64}},
    }
    cm.register("old-card", "old-session", "Old?", ["Approve", "Reject"])
    cm.register("new-card", "new-session", "New?", ["Approve", "Reject"])

    old_send = asyncio.create_task(adapter.send_clarify(
        chat_id="old", question="Old?", choices=["Approve", "Reject"],
        clarify_id="old-card", session_key="old-session",
    ))
    await first_started.wait()
    new_send = asyncio.create_task(adapter.send_clarify(
        chat_id="new", question="New?", choices=["Approve", "Reject"],
        clarify_id="new-card", session_key="new-session",
    ))
    await asyncio.sleep(0.15)

    assert target.send_clarify.await_count == 1
    assert not new_send.done()

    release_first.set()
    assert (await old_send).success is True
    assert (await asyncio.wait_for(new_send, timeout=3.0)).success is True
    assert target.send_clarify.await_count == 2
    target.retire_clarify.assert_awaited_once()


@pytest.mark.asyncio
async def test_crashed_predecessor_requires_explicit_recovery_before_successor(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.approval_card_ledger import ApprovalCardLedger
    from tools import clarify_gateway as cm

    old_binding = {
        "item_key": "owner/repo#161", "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64, "scope": "merge",
        "decision_digest_sha256": "3" * 64,
    }
    crashed = ApprovalCardLedger(tmp_path / "approval_cards.db", instance_id="gateway-a")
    crashed.claim(
        old_binding, platform="telegram", chat_id="327791380",
        clarify_id="old-card", session_key="old-session", choices=["Approve", "Reject"],
    )
    sent = []
    successor_sent = asyncio.Event()

    async def send_card(**kwargs):
        sent.append(kwargs)
        if kwargs["clarify_id"].startswith("approval-rcv-"):
            asyncio.get_running_loop().call_soon(
                cm.resolve_gateway_clarify,
                kwargs["clarify_id"],
                "Reemitir aprovação",
            )
            return SendResult(success=True, message_id="600")
        successor_sent.set()
        return SendResult(success=True, message_id="601")

    target = MagicMock()
    target.send_clarify = AsyncMock(side_effect=send_card)
    target.retire_clarify_card = AsyncMock(return_value=True)
    target.send = AsyncMock(return_value=SendResult(success=True, message_id="notice"))
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    adapter._delivery_info = {
        "new": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"},
                "approval_binding": {**old_binding, "contract_digest_sha256": "3" * 64}},
    }
    cm.register("new-card", "new-session", "New?", ["Approve", "Reject"])

    result = await asyncio.wait_for(adapter.send_clarify(
        chat_id="new", question="New?", choices=["Approve", "Reject"],
        clarify_id="new-card", session_key="new-session",
    ), timeout=3.0)

    assert result.success is True
    assert result.message_id == "600"
    await asyncio.wait_for(successor_sent.wait(), timeout=3.0)
    assert len(sent) == 2
    assert sent[0]["clarify_id"].startswith("approval-rcv-")
    assert sent[0]["choices"] == ["Reemitir aprovação", "Manter bloqueado"]
    assert sent[1]["clarify_id"] == "new-card"
    target.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_timeout_durably_expires_before_waiter_disappears(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.approval_card_ledger import ApprovalCardLedger
    from tools import clarify_gateway as cm

    monkeypatch.setattr(cm, "get_clarify_timeout", lambda: 0.05)
    old_binding = {
        "item_key": "owner/repo#161", "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64, "scope": "merge",
        "decision_digest_sha256": "3" * 64,
    }
    crashed = ApprovalCardLedger(tmp_path / "approval_cards.db", instance_id="gateway-a")
    old = crashed.claim(
        old_binding, platform="telegram", chat_id="327791380",
        clarify_id="old-card", session_key="old-session", choices=["Approve", "Reject"],
    )
    target = MagicMock()
    target.send_clarify = AsyncMock(return_value=SendResult(success=True, message_id="600"))
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    adapter._delivery_info = {
        "new": {
            "deliver": "telegram", "deliver_extra": {"chat_id": "327791380"},
            "approval_binding": {
                "item_key": "owner/repo#161", "head_sha": "1" * 40,
                "contract_digest_sha256": "4" * 64, "scope": "merge",
            },
        },
    }
    cm.register("new-card", "new-session", "New?", ["Approve", "Reject"])

    result = await adapter.send_clarify(
        chat_id="new", question="New?", choices=["Approve", "Reject"],
        clarify_id="new-card", session_key="new-session",
    )
    assert result.success is True
    recovery_id = target.send_clarify.await_args.kwargs["clarify_id"]
    await asyncio.sleep(0.2)

    assert crashed.observe_recovery(old.card_id).status == "failed"
    assert cm.is_registered(recovery_id) is False


@pytest.mark.asyncio
async def test_recovery_delivery_without_message_id_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.approval_card_ledger import ApprovalCardLedger
    from tools import clarify_gateway as cm

    old_binding = {
        "item_key": "owner/repo#161", "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64, "scope": "merge",
        "decision_digest_sha256": "3" * 64,
    }
    crashed = ApprovalCardLedger(tmp_path / "approval_cards.db", instance_id="gateway-a")
    old = crashed.claim(
        old_binding, platform="telegram", chat_id="327791380",
        clarify_id="old-card", session_key="old-session", choices=["Approve", "Reject"],
    )
    target = MagicMock()

    async def send_without_identity(**kwargs):
        asyncio.get_running_loop().call_soon(
            cm.resolve_gateway_clarify, kwargs["clarify_id"], "Manter bloqueado")
        return SendResult(success=True, message_id=None)

    target.send_clarify = AsyncMock(side_effect=send_without_identity)
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    adapter._delivery_info = {
        "new": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"},
                "approval_binding": {**old_binding, "contract_digest_sha256": "3" * 64}},
    }
    cm.register("new-card", "new-session", "New?", ["Approve", "Reject"])

    result = await adapter.send_clarify(
        chat_id="new", question="New?", choices=["Approve", "Reject"],
        clarify_id="new-card", session_key="new-session",
    )

    assert result.success is False
    assert "message_id" in (result.error or "")
    assert crashed.observe_recovery(old.card_id).status == "failed"


@pytest.mark.asyncio
async def test_late_recovery_sender_retires_control_after_losing_generation(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.approval_card_ledger import ApprovalCardLedger
    from tools import clarify_gateway as cm

    old_binding = {
        "item_key": "owner/repo#161", "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64, "scope": "merge",
        "decision_digest_sha256": "3" * 64,
    }
    crashed = ApprovalCardLedger(tmp_path / "approval_cards.db", instance_id="gateway-a")
    old = crashed.claim(
        old_binding, platform="telegram", chat_id="327791380",
        clarify_id="old-card", session_key="old-session", choices=["Approve", "Reject"],
    )

    async def late_send(**kwargs):
        asyncio.get_running_loop().call_soon(
            cm.resolve_gateway_clarify, kwargs["clarify_id"], "Manter bloqueado")
        return SendResult(success=True, message_id="stale-600")

    target = MagicMock()
    target.send_clarify = AsyncMock(side_effect=late_send)
    target.retire_clarify = AsyncMock(return_value=True)
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    adapter._delivery_info = {
        "new": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"},
                "approval_binding": {**old_binding, "contract_digest_sha256": "3" * 64}},
    }
    cm.register("new-card", "new-session", "New?", ["Approve", "Reject"])
    monkeypatch.setattr(ApprovalCardLedger, "activate_recovery", lambda *args: False)

    result = await adapter.send_clarify(
        chat_id="new", question="New?", choices=["Approve", "Reject"],
        clarify_id="new-card", session_key="new-session",
    )
    await asyncio.sleep(0)

    assert result.success is True
    target.retire_clarify.assert_awaited_once_with(
        chat_id="327791380", message_id="stale-600", reason="superseded",
    )


@pytest.mark.asyncio
async def test_late_recovery_sender_fails_closed_when_exact_retirement_fails(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.approval_card_ledger import ApprovalCardLedger
    from tools import clarify_gateway as cm

    old_binding = {
        "item_key": "owner/repo#161", "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64, "scope": "merge",
        "decision_digest_sha256": "3" * 64,
    }
    crashed = ApprovalCardLedger(tmp_path / "approval_cards.db", instance_id="gateway-a")
    crashed.claim(
        old_binding, platform="telegram", chat_id="327791380",
        clarify_id="old-card", session_key="old-session", choices=["Approve", "Reject"],
    )
    target = MagicMock()
    target.send_clarify = AsyncMock(
        return_value=SendResult(success=True, message_id="stale-600"))
    target.retire_clarify = AsyncMock(return_value=False)
    target.retire_clarify_card = AsyncMock(return_value=False)
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    adapter._delivery_info = {
        "new": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"},
                "approval_binding": {**old_binding, "contract_digest_sha256": "3" * 64}},
    }
    monkeypatch.setattr(ApprovalCardLedger, "activate_recovery", lambda *args: False)
    cm.register("new-card", "new-session", "New?", ["Approve", "Reject"])

    result = await adapter.send_clarify(
        chat_id="new", question="New?", choices=["Approve", "Reject"],
        clarify_id="new-card", session_key="new-session",
    )

    assert result.success is False
    assert result.raw_response == {"ambiguous": True}
    assert result.error == "Stale approval recovery control could not be retired"
    target.retire_clarify_card.assert_awaited_once()


@pytest.mark.asyncio
async def test_restart_hydrates_webhook_delivery_from_recovery_continuation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.approval_card_ledger import ApprovalCardLedger

    crashed = ApprovalCardLedger(tmp_path / "approval_cards.db", instance_id="gateway-a")
    old = crashed.claim(
        {
            "item_key": "owner/repo#161", "head_sha": "1" * 40,
            "contract_digest_sha256": "2" * 64, "scope": "merge",
            "decision_digest_sha256": "3" * 64,
        },
        platform="telegram", chat_id="327791380", clarify_id="old-card",
        session_key="old-session", choices=["Approve", "Reject"],
    )
    delivery = {
        "deliver": "telegram",
        "deliver_extra": {"chat_id": "327791380"},
        "approval_binding": {
            "item_key": "owner/repo#161", "head_sha": "1" * 40,
            "contract_digest_sha256": "2" * 64, "scope": "merge",
        },
    }
    ApprovalCardLedger(tmp_path / "approval_cards.db", instance_id="gateway-b").claim_recovery(
        old.card_id, platform="telegram", chat_id="327791380",
        continuation={
            "session_chat_id": "webhook-delivery", "delivery": delivery,
            "question": "Approve?", "choices": ["Approve", "Reject"],
            "clarify_id": "new-card", "session_key": "new-session",
        },
        expires_at=10**12,
    )
    restarted = object.__new__(WebhookAdapter)
    restarted._approval_instance_id = "gateway-c"
    restarted._approval_ledgers = {}
    restarted._delivery_info = {}
    restarted._delivery_info_created = {}
    restarted._delivery_info_order = deque()

    restarted._hydrate_approval_delivery_info()

    assert restarted._delivery_info["webhook-delivery"] == delivery


def test_answered_recovery_continuation_has_one_lease_owner_and_fenced_completion(tmp_path):
    from gateway.approval_card_ledger import ApprovalCardLedger

    binding = {
        "item_key": "owner/repo#161", "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64, "scope": "merge",
        "decision_digest_sha256": "3" * 64,
    }
    path = tmp_path / "approval-cards.db"
    crashed = ApprovalCardLedger(path, instance_id="gateway-a")
    old = crashed.claim(
        binding, platform="telegram", chat_id="327791380",
        clarify_id="clarify-old", session_key="session-old", choices=["Approve", "Reject"],
    )
    continuation = {
        "session_chat_id": "delivery", "delivery": {}, "question": "Approve?",
        "choices": ["Approve", "Reject"], "clarify_id": "clarify-new",
        "session_key": "session-new",
    }
    ledger = ApprovalCardLedger(path, instance_id="gateway-b")
    recovery = ledger.claim_recovery(
        old.card_id, platform="telegram", chat_id="327791380",
        continuation=continuation, expires_at=time.time() + 60.0,
    )
    assert ledger.activate_recovery(old.card_id, recovery.delivery_generation, "600")
    assert ledger.answer_choice(recovery.clarify_id, 0) == "Reemitir aprovação"

    first = ApprovalCardLedger(path, instance_id="gateway-c").claim_answered_recovery_continuations(
        now=100.0, lease_seconds=30.0,
    )
    assert len(first) == 1
    assert ApprovalCardLedger(
        path, instance_id="gateway-d"
    ).claim_answered_recovery_continuations(now=100.0, lease_seconds=30.0) == []
    takeover = ApprovalCardLedger(
        path, instance_id="gateway-d"
    ).claim_answered_recovery_continuations(now=131.0, lease_seconds=30.0)

    assert len(takeover) == 1
    assert takeover[0]["claim_id"] != first[0]["claim_id"]
    assert ledger.complete_recovery_continuation(
        old.card_id, recovery.delivery_generation, first[0]["claim_id"]
    ) is False
    assert ledger.complete_recovery_continuation(
        old.card_id, recovery.delivery_generation, takeover[0]["claim_id"]
    ) is True
    assert ledger.claim_answered_recovery_continuations(now=200.0) == []


@pytest.mark.asyncio
async def test_restart_worker_reissues_answered_recovery_without_webhook_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.approval_card_ledger import ApprovalCardLedger
    from tools import clarify_gateway as cm

    old_binding = {
        "item_key": "owner/repo#161", "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64, "scope": "merge",
        "decision_digest_sha256": "3" * 64,
    }
    crashed = ApprovalCardLedger(tmp_path / "approval_cards.db", instance_id="gateway-a")
    old = crashed.claim(
        old_binding, platform="telegram", chat_id="327791380",
        clarify_id="old-card", session_key="old-session", choices=["Approve", "Reject"],
    )
    delivery = {
        "deliver": "telegram",
        "deliver_extra": {"chat_id": "327791380"},
        "approval_binding": {
            "item_key": "owner/repo#161", "head_sha": "1" * 40,
            "contract_digest_sha256": "4" * 64, "scope": "merge",
        },
    }
    ledger = ApprovalCardLedger(tmp_path / "approval_cards.db", instance_id="gateway-b")
    recovery = ledger.claim_recovery(
        old.card_id, platform="telegram", chat_id="327791380",
        continuation={
            "session_chat_id": "new", "delivery": delivery,
            "question": "New?", "choices": ["Approve", "Reject"],
            "clarify_id": "new-card", "session_key": "new-session",
        },
        expires_at=10**12,
    )
    assert ledger.activate_recovery(old.card_id, recovery.delivery_generation, "600") is True
    assert ledger.answer_choice(recovery.clarify_id, 0) == "Reemitir aprovação"
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._resolution_cbs.clear()
        cm._resolution_probes.clear()
        cm._expiry_cbs.clear()

    target = MagicMock()
    target.send_clarify = AsyncMock(return_value=SendResult(success=True, message_id="601"))
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    restarted = object.__new__(WebhookAdapter)
    restarted.gateway_runner = runner
    restarted._approval_instance_id = "gateway-c"
    restarted._approval_ledgers = {}
    restarted._delivery_info = {}
    restarted._delivery_info_created = {}
    restarted._delivery_info_order = deque()
    restarted._hydrate_approval_delivery_info()

    await restarted._drain_approval_recovery_continuations_once()
    await restarted._drain_approval_recovery_continuations_once()

    assert target.send_clarify.await_count == 1
    assert target.send_clarify.await_args.kwargs["clarify_id"] == "new-card"
    assert cm.is_registered("new-card") is True


@pytest.mark.asyncio
async def test_ambiguous_approval_send_remains_durably_delivering(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import clarify_gateway as cm

    binding = {
        "item_key": "owner/repo#161", "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64, "scope": "merge",
        "decision_digest_sha256": "3" * 64,
    }
    target = MagicMock()
    target.send_clarify = AsyncMock(return_value=SendResult(
        success=False, error="late ack", raw_response={"ambiguous": True}))
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    adapter._delivery_info = {
        "delivery": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"},
                     "approval_binding": binding}
    }
    cm.register("ambiguous-card", "ambiguous-session", "Approve?", ["Approve", "Reject"])

    result = await adapter.send_clarify(
        chat_id="delivery", question="Approve?", choices=["Approve", "Reject"],
        clarify_id="ambiguous-card", session_key="ambiguous-session",
    )

    assert result.raw_response == {"ambiguous": True}
    with sqlite3.connect(tmp_path / "approval_cards.db") as connection:
        assert connection.execute(
            "SELECT status FROM approval_cards WHERE clarify_id = 'ambiguous-card'"
        ).fetchone()[0] == "delivering"
        assert connection.execute(
            "SELECT status FROM approval_bindings"
        ).fetchone()[0] == "pending"


@pytest.mark.asyncio
async def test_timeout_expires_durable_card_before_waiter_returns(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.approval_card_ledger import ApprovalCardLedger
    from tools import clarify_gateway as cm

    binding = {
        "item_key": "owner/repo#161", "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64, "scope": "merge",
        "decision_digest_sha256": "3" * 64,
    }
    target = MagicMock()
    target.send_clarify = AsyncMock(return_value=SendResult(success=True, message_id="501"))
    target.retire_clarify_card = AsyncMock(return_value=True)
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    adapter._delivery_info = {
        "delivery": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"},
                     "approval_binding": binding}
    }
    cm.register("timeout-card", "timeout-session", "Approve?", ["Approve", "Reject"])
    await adapter.send_clarify(
        chat_id="delivery", question="Approve?", choices=["Approve", "Reject"],
        clarify_id="timeout-card", session_key="timeout-session",
    )

    assert cm.wait_for_response("timeout-card", timeout=0.01) == cm.CANCELLED
    await adapter.retire_clarify_card("timeout-card", "Expired")
    target.retire_clarify_card.assert_awaited_once_with("timeout-card", "Expired")
    remote = ApprovalCardLedger(tmp_path / "approval_cards.db", instance_id="gateway-b")
    assert remote.answer_choice("timeout-card", 0) is None
    assert remote.observe_clarify("timeout-card").status == "failed"


@pytest.mark.asyncio
async def test_late_send_success_is_retired_after_durable_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import clarify_gateway as cm

    binding = {
        "item_key": "owner/repo#161", "head_sha": "1" * 40,
        "contract_digest_sha256": "2" * 64, "scope": "merge",
        "decision_digest_sha256": "3" * 64,
    }
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_send(**kwargs):
        started.set()
        await release.wait()
        return SendResult(success=True, message_id="501")

    target = MagicMock()
    target.send_clarify = AsyncMock(side_effect=delayed_send)
    target.retire_clarify = AsyncMock(return_value=True)
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    adapter._delivery_info = {
        "delivery": {"deliver": "telegram", "deliver_extra": {"chat_id": "327791380"},
                     "approval_binding": binding}
    }
    cm.register("late-card", "late-session", "Approve?", ["Approve", "Reject"])
    sending = asyncio.create_task(adapter.send_clarify(
        chat_id="delivery", question="Approve?", choices=["Approve", "Reject"],
        clarify_id="late-card", session_key="late-session",
    ))
    await started.wait()

    assert await asyncio.to_thread(cm.wait_for_response, "late-card", 0.01) == cm.CANCELLED
    release.set()
    assert (await sending).success is True
    target.retire_clarify.assert_awaited_once_with(
        chat_id="327791380", message_id="501", reason="expired")
