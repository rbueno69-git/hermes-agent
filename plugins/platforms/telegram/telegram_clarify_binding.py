"""Bind a Telegram clarify prompt to the message carrying its buttons."""

from __future__ import annotations

import contextlib
import dataclasses
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _ident(value: Any) -> Optional[str]:
    """Normalize a Telegram id (int on the wire, str in state)."""
    return None if value is None else str(value)


@dataclasses.dataclass(frozen=True)
class ClarifyPrompt:
    """Message identity used to fail closed on mismatched button taps."""

    session_key: str
    chat_id: Optional[str] = None
    message_id: Optional[str] = None
    thread_id: Optional[str] = None

    @classmethod
    def from_sent(cls, session_key: str, msg: Any) -> ClarifyPrompt:
        return cls(
            session_key,
            _ident(getattr(msg, "chat_id", None)),
            _ident(getattr(msg, "message_id", None)),
            _ident(getattr(msg, "message_thread_id", None)),
        )


async def tap_matches_prompt(query: Any, clarify_id: str, prompt: ClarifyPrompt, denial: str) -> bool:
    """Accept only a tap from the exact message carrying the prompt."""
    if prompt.message_id is None:
        logger.warning("Telegram clarify callback unverifiable: no prompt message bound (id=%s)", clarify_id)
        return True
    message = getattr(query, "message", None)
    got_chat = _ident(getattr(message, "chat_id", None))
    got_message = _ident(getattr(message, "message_id", None))
    if (got_chat, got_message) == (prompt.chat_id, prompt.message_id):
        return True
    logger.warning(
        "Telegram clarify callback rejected: prompt binding mismatch "
        "(id=%s, expected chat=%s message=%s thread=%s, got chat=%s message=%s thread=%s, "
        "callback_query=%s, caller_id=%s)",
        clarify_id,
        prompt.chat_id,
        prompt.message_id,
        prompt.thread_id,
        got_chat,
        got_message,
        _ident(getattr(message, "message_thread_id", None)),
        getattr(query, "id", None),
        _ident(getattr(getattr(query, "from_user", None), "id", None)),
    )
    with contextlib.suppress(Exception):
        await query.answer(text=denial)
    return False


def log_resolved(query: Any, clarify_id: str, prompt: ClarifyPrompt, choice: str, user_display: str) -> None:
    """Log non-secret callback provenance for a resolved clarify."""
    logger.info(
        "Telegram clarify button resolved "
        "(id=%s, choice=%r, user=%s, caller_id=%s, callback_query=%s, chat=%s, message=%s)",
        clarify_id,
        choice,
        user_display,
        _ident(getattr(getattr(query, "from_user", None), "id", None)),
        getattr(query, "id", None),
        prompt.chat_id,
        prompt.message_id,
    )
