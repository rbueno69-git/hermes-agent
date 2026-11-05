from gateway.approval_card_ledger import ApprovalCardLedger


def _binding(digest="2" * 64, head="1" * 40):
    return {
        "item_key": "rbueno69-git/carteira-investimentos#160",
        "head_sha": head,
        "contract_digest_sha256": digest,
        "scope": "Somente leitura; merge e deploy autorizados.",
    }


def test_equivalent_claim_reuses_active_card_in_same_gateway(tmp_path):
    ledger = ApprovalCardLedger(tmp_path / "approval-cards.db", instance_id="gateway-a")
    first = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-a", session_key="session-a",
    )
    assert first.action == "send"
    ledger.activate(first.card_id, "501")

    duplicate = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-b", session_key="session-b",
    )

    assert duplicate.action == "reuse_active"
    assert duplicate.clarify_id == "clarify-a"
    assert duplicate.message_id == "501"


def test_answered_binding_is_reused_after_gateway_restart(tmp_path):
    path = tmp_path / "approval-cards.db"
    first_gateway = ApprovalCardLedger(path, instance_id="gateway-a")
    first = first_gateway.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-a", session_key="session-a",
    )
    first_gateway.activate(first.card_id, "501")
    first_gateway.answer("clarify-a", "Aprovar")

    restarted = ApprovalCardLedger(path, instance_id="gateway-b")
    replay = restarted.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-b", session_key="session-b",
    )

    assert replay.action == "reuse_answered"
    assert replay.response == "Aprovar"
    assert replay.message_id == "501"


def test_material_change_supersedes_active_card_before_replacement(tmp_path):
    ledger = ApprovalCardLedger(tmp_path / "approval-cards.db", instance_id="gateway-a")
    old = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-old", session_key="session-old",
    )
    ledger.activate(old.card_id, "501")

    replacement = ledger.claim(
        _binding(digest="3" * 64), platform="telegram", chat_id="327791380",
        clarify_id="clarify-new", session_key="session-new",
    )

    assert replacement.action == "send"
    assert [(card.clarify_id, card.message_id, card.status) for card in replacement.superseded] == [
        ("clarify-old", "501", "superseded")
    ]


def test_fast_answer_before_send_returns_is_persisted_with_message_id(tmp_path):
    ledger = ApprovalCardLedger(tmp_path / "approval-cards.db", instance_id="gateway-a")
    claim = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-fast", session_key="session-fast",
    )

    assert ledger.answer("clarify-fast", "Aprovar") is True
    ledger.activate(claim.card_id, "501")
    replay = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-replay", session_key="session-replay",
    )

    assert replay.action == "reuse_answered"
    assert replay.response == "Aprovar"
    assert replay.message_id == "501"


def test_expired_in_memory_wait_is_atomically_replaced(tmp_path):
    ledger = ApprovalCardLedger(tmp_path / "approval-cards.db", instance_id="gateway-a")
    old = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-expired", session_key="session-expired",
    )
    ledger.activate(old.card_id, "501")

    replacement = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-new", session_key="session-new",
        is_active=lambda clarify_id: False,
    )

    assert replacement.action == "send"
    assert [(card.clarify_id, card.message_id) for card in replacement.superseded] == [
        ("clarify-expired", "501")
    ]
