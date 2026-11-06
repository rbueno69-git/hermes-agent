"""Tests for Telegram inline keyboard clarify buttons.

Mirrors test_telegram_approval_buttons.py for the new ``send_clarify`` and
``cl:`` callback dispatch added in feat/clarify-gateway-buttons.
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.platforms.base import unauthorized_action_notice

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Minimal Telegram mock so TelegramAdapter can be imported (mirrors
# test_telegram_approval_buttons.py)
# ---------------------------------------------------------------------------
from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import PlatformConfig


def _make_adapter(extra=None):
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


@pytest.mark.asyncio
async def test_retire_clarify_falls_back_to_non_actionable_edit_when_delete_fails():
    adapter = _make_adapter()
    adapter._bot.delete_message = AsyncMock(side_effect=RuntimeError("delete window elapsed"))
    adapter._bot.edit_message_text = AsyncMock(return_value=True)

    retired = await adapter.retire_clarify(chat_id="12345", message_id="100", reason="superseded")

    assert retired is True
    kwargs = adapter._bot.edit_message_text.await_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert kwargs["message_id"] == 100
    assert kwargs["reply_markup"] is None
    assert "substituída" in kwargs["text"]


@pytest.mark.asyncio
async def test_timeout_retirement_uses_persisted_card_coordinates():
    adapter = _make_adapter()
    mock_msg = MagicMock()
    mock_msg.message_id = 100
    adapter._bot.send_message = AsyncMock(return_value=mock_msg)
    adapter._bot.edit_message_text = AsyncMock(return_value=True)

    await adapter.send_clarify(
        chat_id="12345", question="Approve?", choices=["Approve", "Reject"],
        clarify_id="clarify-timeout", session_key="session-timeout",
    )
    await adapter.retire_clarify_card("clarify-timeout", "This approval request expired.")

    kwargs = adapter._bot.edit_message_text.await_args.kwargs
    assert kwargs == {
        "chat_id": 12345,
        "message_id": 100,
        "text": "This approval request expired.",
        "reply_markup": None,
    }
    assert "clarify-timeout" not in adapter._clarify_state


def _clear_clarify_state():
    from tools import clarify_gateway as cm
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()
        cm._linked_entries.clear()
        cm._resolution_cbs.clear()
        cm._resolution_probes.clear()
        if hasattr(cm, "_expiry_cbs"):
            cm._expiry_cbs.clear()
        cm._resolving.clear()


# ===========================================================================
# send_clarify — render
# ===========================================================================

class TestTelegramSendClarify:
    """Verify the rendered prompt has buttons or none, and stores state."""

    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_timeout_after_possible_remote_commit_is_ambiguous(self):
        adapter = _make_adapter()
        adapter._send_control_message = AsyncMock(side_effect=asyncio.TimeoutError("late ack"))

        result = await adapter.send_clarify(
            chat_id="12345",
            question="Approve?",
            choices=["Approve", "Reject"],
            clarify_id="cid-timeout",
            session_key="sk-timeout",
        )

        assert result.success is False
        assert result.raw_response == {"ambiguous": True}

    @pytest.mark.asyncio
    async def test_python_telegram_bot_timeout_after_dispatch_is_ambiguous(self):
        from telegram.error import TimedOut

        adapter = _make_adapter()
        adapter._send_control_message = AsyncMock(side_effect=TimedOut("late ack"))

        result = await adapter.send_clarify(
            chat_id="12345",
            question="Approve?",
            choices=["Approve", "Reject"],
            clarify_id="cid-ptb-timeout",
            session_key="sk-ptb-timeout",
        )

        assert result.success is False
        assert result.raw_response == {"ambiguous": True}

    @pytest.mark.asyncio
    async def test_multi_choice_renders_buttons_and_other(self):
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 100
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        result = await adapter.send_clarify(
            chat_id="12345",
            question="Which option?",
            choices=["alpha", "beta", "gamma"],
            clarify_id="cid1",
            session_key="sk1",
        )

        assert result.success is True
        assert result.message_id == "100"

        kwargs = adapter._bot.send_message.call_args[1]
        assert kwargs["chat_id"] == 12345
        assert "Which option?" in kwargs["text"]
        # Full option text rendered in the message body (not just buttons)
        assert "1. alpha" in kwargs["text"]
        assert "2. beta" in kwargs["text"]
        assert "3. gamma" in kwargs["text"]
        # InlineKeyboardMarkup with N+1 buttons (3 choices + Other)
        markup = kwargs["reply_markup"]
        assert markup is not None
        # Mocked InlineKeyboardMarkup — just verify it was constructed
        # with rows.  We check state instead of poking the mock structure.
        assert "cid1" in adapter._clarify_state
        assert adapter._clarify_state["cid1"] == "sk1"


        # The button label should be short ("1"), not the long choice
        # (we can't inspect mock button labels directly, but the send
        # succeeded — old truncation code could raise on edge cases)

    @pytest.mark.asyncio
    async def test_html_escapes_question(self):
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 103
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        await adapter.send_clarify(
            chat_id="12345",
            question="<script>alert(1)</script>",
            choices=["x"],
            clarify_id="cid5",
            session_key="sk5",
        )
        kwargs = adapter._bot.send_message.call_args[1]
        # Must NOT contain raw <script> — html.escape should have neutralized
        assert "<script>" not in kwargs["text"]
        assert "&lt;script&gt;" in kwargs["text"]


# ===========================================================================
# Callback dispatch — _handle_callback_query routing for cl:* prefixes
# ===========================================================================

class TestTelegramClarifyCallback:
    """Verify clicking a button resolves the clarify primitive."""

    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_numeric_choice_resolves_with_choice_text(self):
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        # Pre-register a clarify entry so the callback can look up the choice text
        cm.register("cidA", "sk-cb", "Pick", ["red", "green", "blue"])
        adapter._clarify_state["cidA"] = "sk-cb"

        query = AsyncMock()
        query.data = "cl:cidA:1"  # green
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.text = "Pick"
        query.from_user = MagicMock()
        query.from_user.id = "777"
        query.from_user.first_name = "Tester"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)

        # State popped
        assert "cidA" not in adapter._clarify_state
        # Wait shouldn't be needed — resolve_gateway_clarify is sync.
        # The entry's response should be set.
        # We test by reading the entry's response directly.
        with cm._lock:
            entry = cm._entries.get("cidA")
        # Entry might be popped by wait_for_response, but here we never
        # called wait — so it's still in _entries with response set.
        assert entry is not None
        assert entry.response == "green"
        assert entry.event.is_set()
        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_persistence_failure_keeps_button_state_for_retry(self):
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        cm.register("cid-durable", "sk-cb", "Pick", ["red", "green"])
        cm.on_resolution("cid-durable", lambda response: (_ for _ in ()).throw(RuntimeError("locked")))
        adapter._clarify_state["cid-durable"] = "sk-cb"
        adapter._clarify_card_state["cid-durable"] = ("12345", "100")
        query = AsyncMock()
        query.data = "cl:cid-durable:1"
        query.message = MagicMock(chat_id=12345, text="Pick")
        query.from_user = MagicMock(id="777", first_name="Tester")
        update = MagicMock(callback_query=query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert adapter._clarify_state["cid-durable"] == "sk-cb"
        assert adapter._clarify_card_state["cid-durable"] == ("12345", "100")
        assert cm._entries["cid-durable"].event.is_set() is False

    @pytest.mark.asyncio
    async def test_restart_callback_uses_durable_choices_and_unblocks_replacement_waiter(self, tmp_path):
        from gateway.approval_card_ledger import ApprovalCardLedger
        from tools import clarify_gateway as cm

        binding = {
            "item_key": "owner/repo#1", "head_sha": "1" * 40,
            "contract_digest_sha256": "2" * 64, "scope": "merge",
            "decision_digest_sha256": "3" * 64,
        }
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path), "TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            ledger = ApprovalCardLedger(tmp_path / "approval_cards.db", instance_id="old")
            claim = ledger.claim(
                binding, platform="telegram", chat_id="12345", clarify_id="old-card",
                session_key="old-session", choices=["Approve", "Reject"],
            )
            ledger.activate(claim.card_id, "100")
            cm.register("replacement-wait", "new-session", "Approve?", ["Approve", "Reject"])
            cm.set_resolution_probe(
                "replacement-wait", lambda: ledger.response_for_binding(claim.binding_key))
            adapter = _make_adapter()
            adapter._hermes_profile_name = "coder"
            query = AsyncMock()
            query.data = "cl:old-card:0"
            query.message = MagicMock(chat_id=12345, text="Approve?")
            query.from_user = MagicMock(id="777", first_name="Tester")

            await adapter._handle_callback_query(MagicMock(callback_query=query), MagicMock())

            assert ledger.response_for_binding(claim.binding_key) == "Approve"
            assert cm.wait_for_response("replacement-wait", timeout=3.0) == "Approve"


    @pytest.mark.asyncio
    async def test_unauthorized_user_rejected(self):
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        cm.register("cidC", "sk-auth", "Pick", ["a", "b"])
        adapter._clarify_state["cidC"] = "sk-auth"

        # Hook up a runner that says NOT authorized
        class _DenyRunner:
            async def _handle_message(self, event):
                return None
            def _is_user_authorized(self, source):
                return False

        adapter._message_handler = _DenyRunner()._handle_message

        query = AsyncMock()
        query.data = "cl:cidC:0"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.chat.type = "private"
        query.message.text = "Pick"
        query.from_user = MagicMock()
        query.from_user.id = "999"
        query.from_user.first_name = "Mallory"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        await adapter._handle_callback_query(update, context)

        # Must not resolve, must answer with not-authorized message
        with cm._lock:
            entry = cm._entries.get("cidC")
        assert entry is not None
        assert not entry.event.is_set()
        query.answer.assert_called_once()
        assert query.answer.call_args[1]["text"] == unauthorized_action_notice("telegram")
        # State preserved
        assert adapter._clarify_state["cidC"] == "sk-auth"


# ===========================================================================
# Base adapter fallback render — text numbered list
# ===========================================================================

class TestBaseAdapterClarifyFallback:
    """Adapters without button overrides should render numbered text."""

    @pytest.mark.asyncio
    async def test_numbered_text_fallback(self):
        from gateway.platforms.base import BasePlatformAdapter, SendResult

        # Subclass just enough to instantiate
        class _Stub(BasePlatformAdapter):
            name = "stub"

            def __init__(self):
                # Skip base __init__ — we're not exercising it
                self.sent: list = []

            async def connect(self, *, is_reconnect: bool = False): pass
            async def disconnect(self): pass
            async def send(self, chat_id, content, **kw):
                self.sent.append({"chat_id": chat_id, "content": content})
                return SendResult(success=True, message_id="1")
            async def edit(self, *a, **k): return SendResult(success=False)
            async def get_history(self, *a, **k): return []
            async def get_chat_info(self, *a, **k): return {}

        adapter = _Stub()

        result = await adapter.send_clarify(
            chat_id="c",
            question="Pick a fruit",
            choices=["apple", "banana"],
            clarify_id="x",
            session_key="s",
        )
        assert result.success is True
        assert len(adapter.sent) == 1
        text = adapter.sent[0]["content"]
        assert "Pick a fruit" in text
        assert "1." in text and "apple" in text
        assert "2." in text and "banana" in text

