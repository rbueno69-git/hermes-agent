from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.github_approval_broker import ApprovalProposal, default_broker
from gateway.platforms.base import SendResult
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
async def test_restart_callback_persists_decision_and_failed_retirement_for_retry(tmp_path, monkeypatch):
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
    adapter.retire_clarify_card = AsyncMock(side_effect=TimeoutError("telegram edit failed"))
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=7, first_name="Rogerio"),
        message=SimpleNamespace(text="Approve?", message_id=501),
        answer=AsyncMock(),
    )
    callback_context = {"chat_id": 42, "chat_type": "private", "thread_id": None, "user_name": "Rogerio"}

    await adapter._handle_clarify_callback(
        query, f"cl:{sent.clarify_id}:0", callback_context)

    observed = default_broker(instance_id="restart").observe(proposal)
    assert observed is not None
    assert observed["response"] == "Approve"
    assert observed["approver"] == "telegram:7"
    assert observed["retirement_pending"] == 1
    adapter.retire_clarify_card.assert_awaited_once()
    query.answer.assert_awaited_once_with(text="✓ Approve")
