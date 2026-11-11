import sys
import types

from agent.agent_runtime_helpers_placeholders import _INTERRUPTED_PLACEHOLDER
from agent.turn_iteration_prep import _neutralise_replay_echo_ghosts


_REPRODUCED_PLACEHOLDER = "[interrupt: no assistant output for this turn]"


def _stub_conversation_loop(monkeypatch):
    stub = types.ModuleType("agent.conversation_loop")
    stub._INTERRUPT_SCAFFOLD_MARKER = "[interrupted before tool follow-up]"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent.conversation_loop", stub)


def test_replayed_current_interrupt_phrase_is_neutralised_before_provider_call(monkeypatch):
    _stub_conversation_loop(monkeypatch)
    row = {
        "role": "assistant",
        "content": "",
        "display_kind": "hidden",
        "api_content": _REPRODUCED_PLACEHOLDER,
    }

    prepared, count = _neutralise_replay_echo_ghosts([row])

    assert count == 1
    assert prepared[0]["content"] == ""
    assert prepared[0]["api_content"] == _INTERRUPTED_PLACEHOLDER
    assert _INTERRUPTED_PLACEHOLDER != _REPRODUCED_PLACEHOLDER
    assert prepared[0] is not row


def test_user_quote_of_interrupt_phrase_is_not_rewritten(monkeypatch):
    _stub_conversation_loop(monkeypatch)
    row = {"role": "user", "content": _REPRODUCED_PLACEHOLDER}

    prepared, count = _neutralise_replay_echo_ghosts([row])

    assert count == 0
    assert prepared is not None
    assert prepared[0] == row
