from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from gateway.github_approval_broker import ApprovalProposal, default_broker
from gateway.platforms.base import SendResult, utf16_len
from plugins.platforms.telegram.adapter import TelegramAdapter


class SendingTelegram:
    def __init__(self):
        self._clarify_state = {}

    def approval_destination_identity(self, clarify_id=None):
        return "telegram-bot:501"

    async def send_clarify(self, **kwargs):
        self._clarify_state[kwargs["clarify_id"]] = kwargs["session_key"]
        return SendResult(success=True, message_id="501")


@pytest.mark.asyncio
async def test_restart_callback_preserves_original_html_and_removes_buttons(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    proposal = ApprovalProposal("owner/repo#498", "a" * 40, "b" * 64)
    broker = default_broker(instance_id="publisher")
    sent = await broker.request(
        proposal, adapter=SendingTelegram(), chat_id="42", question="Approve?",
        session_key="webhook:delivery", authorized_user_id="7",
    )

    adapter = object.__new__(TelegramAdapter)
    adapter._clarify_state = {}
    adapter._approval_clarify_state = {}
    adapter._bot = SimpleNamespace(id=501)
    adapter._callback_authorized = AsyncMock(return_value=True)
    adapter._edit_html_quiet = AsyncMock()
    adapter.retire_clarify_card = AsyncMock(side_effect=AssertionError("must preserve the approval prompt"))
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=7, first_name="Rogerio"),
        message=SimpleNamespace(text="Approve?", text_html="<b>Approve?</b>", message_id=501),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    callback_context = {"chat_id": 42, "chat_type": "private", "thread_id": None, "user_name": "Rogerio"}

    await adapter._handle_clarify_callback(
        query, f"cl:{sent.clarify_id}:0", callback_context)

    observed = default_broker(instance_id="restart").observe(proposal)
    assert observed is not None
    assert observed["response"] == "Approve"
    assert observed["approver"] == "telegram:7"
    assert observed["retirement_pending"] == 0
    query.edit_message_text.assert_awaited_once_with(
        text="<b>Approve?</b>\n\n— ✅ Approved once by Rogerio",
        parse_mode="HTML", reply_markup=None)
    adapter.retire_clarify_card.assert_not_awaited()
    query.answer.assert_awaited_once_with(text="✓ Approve")


@pytest.mark.asyncio
async def test_failed_edit_retries_exact_persisted_resolution_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    proposal = ApprovalProposal("owner/repo#499", "a" * 40, "b" * 64)
    broker = default_broker(instance_id="publisher")
    assert broker is not None
    sent = await broker.request(
        proposal, adapter=SendingTelegram(), chat_id="42", question="Approve?",
        session_key="webhook:delivery", authorized_user_id="7")

    adapter = object.__new__(TelegramAdapter)
    adapter._clarify_state = {}
    adapter._approval_clarify_state = {}
    adapter._bot = SimpleNamespace(id=501)
    adapter._callback_authorized = AsyncMock(return_value=True)
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=7, first_name="Rogerio <Admin>"),
        message=SimpleNamespace(text="Approve?", text_html="<b>Approve?</b>", message_id=501),
        answer=AsyncMock(), edit_message_text=AsyncMock(side_effect=TimeoutError("edit failed")))

    await adapter._handle_clarify_callback(
        query, f"cl:{sent.clarify_id}:0",
        {"chat_id": 42, "chat_type": "private", "thread_id": None})
    observed = broker.observe(proposal)
    assert observed is not None and observed["retirement_pending"] == 1

    fresh = object.__new__(TelegramAdapter)
    fresh._bot = SimpleNamespace(id=501)
    fresh._clarify_state = {}
    fresh._approval_clarify_state = {}
    # Simulate: Telegram applied the first edit, then the process crashed before settlement.
    edit = AsyncMock(side_effect=BadRequest("Message is not modified"))
    fresh._app = SimpleNamespace(bot=SimpleNamespace(edit_message_text=edit))

    restarted = default_broker(instance_id="restart")
    assert restarted is not None
    assert await restarted.retry_retirements(adapter=fresh) == 1
    edit.assert_awaited_once_with(
        chat_id="42", message_id=501,
        text="<b>Approve?</b>\n\n— ✅ Approved once by Rogerio &lt;Admin&gt;",
        parse_mode="HTML", reply_markup=None)
    verified_broker = default_broker(instance_id="verify")
    assert verified_broker is not None
    verified = verified_broker.observe(proposal)
    assert verified is not None and verified["retirement_pending"] == 0


@pytest.mark.asyncio
async def test_superseded_github_card_is_not_reported_as_expired(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    old = ApprovalProposal("owner/repo#500", "a" * 40, "b" * 64)
    newer = ApprovalProposal("owner/repo#500", "c" * 40, "d" * 64)
    broker = default_broker(instance_id="publisher")
    sender = SendingTelegram()
    sent = await broker.request(
        old, adapter=sender, chat_id="42", question="Approve old head?",
        session_key="webhook:old", authorized_user_id="7")
    await broker.request(
        newer, adapter=sender, chat_id="42", question="Approve new head?",
        session_key="webhook:new", authorized_user_id="7")

    adapter = object.__new__(TelegramAdapter)
    adapter._clarify_state = {}
    adapter._approval_clarify_state = {}
    adapter._bot = SimpleNamespace(id=501)
    adapter._callback_authorized = AsyncMock(return_value=True)
    adapter._edit_html_quiet = AsyncMock()
    adapter._notify_clarify_expired = AsyncMock(
        side_effect=AssertionError("superseded is not expired"))
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=7, first_name="Rogerio"),
        message=SimpleNamespace(
            text="Approve old head?", text_html="<b>Approve old head?</b>", message_id=501),
        answer=AsyncMock(), edit_message_text=AsyncMock())

    await adapter._handle_clarify_callback(
        query, f"cl:{sent.clarify_id}:0",
        {"chat_id": 42, "chat_type": "private", "thread_id": None})

    adapter._notify_clarify_expired.assert_not_awaited()
    query.answer.assert_awaited_once_with(
        text="This approval was superseded by a newer GitHub revision.", show_alert=True)
    query.edit_message_text.assert_awaited_once_with(
        text=("<b>Approve old head?</b>\n\n— "
              "This approval was superseded by a newer GitHub revision."),
        parse_mode="HTML", reply_markup=None)


@pytest.mark.asyncio
async def test_approval_prompt_reserves_space_for_resolution_suffix(monkeypatch):
    adapter = object.__new__(TelegramAdapter)
    adapter._clarify_state = {}
    adapter._approval_clarify_state = {}
    monkeypatch.setattr(adapter, "_metadata_thread_id", lambda metadata: None)
    captured = {}

    async def fake_send_prompt(_operation, _chat_id, _metadata, build, **_kwargs):
        text, _keyboard, _remember = build()
        captured["text"] = text
        return SendResult(success=True, message_id="501")

    monkeypatch.setattr(adapter, "_send_prompt", fake_send_prompt)
    await adapter.send_clarify(
        "42", "&" * 5000, ["Approve", "Do not approve"], "gha-test", "session",
        {"is_approval_prompt": True})

    assert captured["text"].endswith("...")
    assert utf16_len(captured["text"]) <= (
        adapter.MAX_MESSAGE_LENGTH - adapter._APPROVAL_RESOLUTION_RESERVE)
