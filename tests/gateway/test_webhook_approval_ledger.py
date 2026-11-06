import multiprocessing
import sqlite3
from concurrent.futures import ProcessPoolExecutor

from gateway.approval_card_ledger import ApprovalCardLedger


def _binding(digest="2" * 64, head="1" * 40):
    return {
        "item_key": "rbueno69-git/carteira-investimentos#160",
        "head_sha": head,
        "contract_digest_sha256": digest,
        "scope": "Somente leitura; merge e deploy autorizados.",
    }


def _claim_in_process(path, instance_id, clarify_id):
    ledger = ApprovalCardLedger(path, instance_id=instance_id)
    claim = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id=clarify_id, session_key=f"session-{clarify_id}", choices=["Approve", "Reject"],
    )
    return claim.action


def _claim_recovery_in_process(path, instance_id, card_id):
    ledger = ApprovalCardLedger(path, instance_id=instance_id)
    return ledger.claim_recovery(
        card_id, platform="telegram", chat_id="327791380"
    ).action


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


def test_equivalent_claim_reuses_active_card_from_another_gateway_instance(tmp_path):
    path = tmp_path / "approval-cards.db"
    first_gateway = ApprovalCardLedger(path, instance_id="gateway-a")
    first = first_gateway.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-a", session_key="session-a", choices=["Approve", "Reject"],
    )
    first_gateway.activate(first.card_id, "501")

    second_gateway = ApprovalCardLedger(path, instance_id="gateway-b")
    duplicate = second_gateway.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-b", session_key="session-b", choices=["Approve", "Reject"],
        is_active=lambda clarify_id: False,
    )

    assert duplicate.action == "reuse_active"
    assert duplicate.clarify_id == "clarify-a"
    assert duplicate.message_id == "501"


def test_concurrent_gateway_processes_publish_exactly_one_card(tmp_path):
    path = tmp_path / "approval-cards.db"
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
        futures = [
            pool.submit(_claim_in_process, path, "gateway-a", "clarify-a"),
            pool.submit(_claim_in_process, path, "gateway-b", "clarify-b"),
        ]
        actions = [future.result(timeout=20) for future in futures]

    assert sorted(actions) == ["reuse_active", "send"]


def test_unknown_delivery_is_not_republished_after_gateway_restart(tmp_path):
    path = tmp_path / "approval-cards.db"
    first_gateway = ApprovalCardLedger(path, instance_id="gateway-a")
    first = first_gateway.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-a", session_key="session-a", choices=["Approve", "Reject"],
    )
    assert first.action == "send"

    restarted = ApprovalCardLedger(path, instance_id="gateway-b")
    replay = restarted.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-b", session_key="session-b", choices=["Approve", "Reject"],
    )

    assert replay.action == "reuse_active"
    assert replay.clarify_id == "clarify-a"
    assert replay.message_id is None


def test_timed_out_card_can_be_safely_reissued(tmp_path):
    ledger = ApprovalCardLedger(tmp_path / "approval-cards.db", instance_id="gateway-a")
    first = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-a", session_key="session-a", choices=["Approve", "Reject"],
    )
    ledger.activate(first.card_id, "501")

    assert ledger.fail_clarify("clarify-a") is True
    replacement = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-b", session_key="session-b", choices=["Approve", "Reject"],
    )

    assert replacement.action == "send"
    assert replacement.clarify_id == "clarify-b"


def test_late_send_ack_cannot_reactivate_expired_card(tmp_path):
    ledger = ApprovalCardLedger(tmp_path / "approval-cards.db", instance_id="gateway-a")
    claim = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-a", session_key="session-a", choices=["Approve", "Reject"],
    )

    assert ledger.fail_clarify("clarify-a") is True
    assert ledger.activate(claim.card_id, "501") is False
    assert ledger.observe(claim.binding_key).status == "failed"
    assert ledger.observe_clarify("clarify-a").status == "failed"


def test_stale_send_failure_cannot_erase_answered_decision(tmp_path):
    ledger = ApprovalCardLedger(tmp_path / "approval-cards.db", instance_id="gateway-a")
    claim = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-a", session_key="session-a", choices=["Approve", "Reject"],
    )

    assert ledger.answer("clarify-a", "Approve") is True
    ledger.fail(claim.card_id)

    observation = ledger.observe(claim.binding_key)
    assert observation.status == "answered"
    assert observation.response == "Approve"
    replay = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-b", session_key="session-b", choices=["Approve", "Reject"],
    )
    assert replay.action == "reuse_answered"
    assert replay.response == "Approve"


def test_material_change_waits_for_ambiguous_delivery_before_replacement(tmp_path):
    ledger = ApprovalCardLedger(tmp_path / "approval-cards.db", instance_id="gateway-a")
    old = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-old", session_key="session-old", choices=["Approve", "Reject"],
    )

    deferred = ApprovalCardLedger(
        tmp_path / "approval-cards.db", instance_id="gateway-b"
    ).claim(
        _binding(digest="3" * 64), platform="telegram", chat_id="327791380",
        clarify_id="clarify-new", session_key="session-new", choices=["Approve", "Reject"],
    )

    assert deferred.action == "wait_predecessor"
    assert deferred.clarify_id == "clarify-old"
    ledger.activate(old.card_id, "501")
    replacement = ApprovalCardLedger(
        tmp_path / "approval-cards.db", instance_id="gateway-b"
    ).claim(
        _binding(digest="3" * 64), platform="telegram", chat_id="327791380",
        clarify_id="clarify-new", session_key="session-new", choices=["Approve", "Reject"],
    )
    assert replacement.action == "send"
    assert [card.clarify_id for card in replacement.superseded] == ["clarify-old"]


def test_explicit_recovery_choice_unblocks_crashed_delivering_predecessor(tmp_path):
    path = tmp_path / "approval-cards.db"
    crashed = ApprovalCardLedger(path, instance_id="gateway-a")
    old = crashed.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-old", session_key="session-old", choices=["Approve", "Reject"],
    )
    restarted = ApprovalCardLedger(path, instance_id="gateway-b")
    waiting = restarted.claim(
        _binding(digest="3" * 64), platform="telegram", chat_id="327791380",
        clarify_id="clarify-new", session_key="session-new", choices=["Approve", "Reject"],
    )

    assert waiting.action == "wait_predecessor"
    assert waiting.owner_instance_id == "gateway-a"
    recovery = restarted.claim_recovery(
        old.card_id, platform="telegram", chat_id="327791380"
    )
    assert recovery.action == "send"
    recovery_id = recovery.clarify_id or ""
    assert restarted.answer_choice(recovery_id, 0) == "Reemitir aprovação"
    assert restarted.observe(old.binding_key).status == "failed"
    replacement = restarted.claim(
        _binding(digest="3" * 64), platform="telegram", chat_id="327791380",
        clarify_id="clarify-new", session_key="session-new", choices=["Approve", "Reject"],
    )
    assert replacement.action == "send"


def test_concurrent_gateways_publish_exactly_one_recovery_control(tmp_path):
    path = tmp_path / "approval-cards.db"
    crashed = ApprovalCardLedger(path, instance_id="gateway-a")
    old = crashed.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-old", session_key="session-old", choices=["Approve", "Reject"],
    )

    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
        actions = [
            future.result(timeout=20)
            for future in (
                pool.submit(_claim_recovery_in_process, path, "gateway-b", old.card_id),
                pool.submit(_claim_recovery_in_process, path, "gateway-c", old.card_id),
            )
        ]

    assert sorted(actions) == ["reuse_active", "send"]


def test_stale_recovery_delivery_is_safely_reissued_without_authorizing_item(tmp_path):
    path = tmp_path / "approval-cards.db"
    crashed = ApprovalCardLedger(path, instance_id="gateway-a")
    old = crashed.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-old", session_key="session-old", choices=["Approve", "Reject"],
    )
    first_recovery = ApprovalCardLedger(path, instance_id="gateway-b").claim_recovery(
        old.card_id, platform="telegram", chat_id="327791380"
    )
    assert first_recovery.action == "send"

    takeover = ApprovalCardLedger(
        path, instance_id="gateway-c", recovery_lease_seconds=0
    ).claim_recovery(old.card_id, platform="telegram", chat_id="327791380")

    assert takeover.action == "send"
    assert takeover.owner_instance_id == "gateway-c"
    assert takeover.delivery_generation != first_recovery.delivery_generation
    assert takeover.clarify_id != first_recovery.clarify_id
    assert crashed.answer_choice(first_recovery.clarify_id or "", 0) is None
    assert crashed.observe(old.binding_key).status == "pending"
    assert crashed.activate_recovery(
        old.card_id, first_recovery.delivery_generation, "600"
    ) is False
    assert crashed.activate_recovery(
        old.card_id, takeover.delivery_generation, "601"
    ) is True
    assert crashed.observe(old.binding_key).status == "pending"


def test_recovery_keep_blocked_choice_preserves_unknown_delivery(tmp_path):
    path = tmp_path / "approval-cards.db"
    crashed = ApprovalCardLedger(path, instance_id="gateway-a")
    old = crashed.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-old", session_key="session-old", choices=["Approve", "Reject"],
    )
    restarted = ApprovalCardLedger(path, instance_id="gateway-b")
    recovery = restarted.claim_recovery(
        old.card_id, platform="telegram", chat_id="327791380"
    )

    assert restarted.answer_choice(
        recovery.clarify_id or "", 1
    ) == "Manter bloqueado"
    assert restarted.observe(old.binding_key).status == "pending"
    assert restarted.claim(
        _binding(digest="3" * 64), platform="telegram", chat_id="327791380",
        clarify_id="clarify-new", session_key="session-new", choices=["Approve", "Reject"],
    ).action == "wait_predecessor"


def test_observe_exposes_terminal_supersession(tmp_path):
    ledger = ApprovalCardLedger(tmp_path / "approval-cards.db", instance_id="gateway-a")
    old = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-old", session_key="session-old", choices=["Approve", "Reject"],
    )
    ledger.activate(old.card_id, "501")
    replacement = ledger.claim(
        _binding(digest="3" * 64), platform="telegram", chat_id="327791380",
        clarify_id="clarify-new", session_key="session-new", choices=["Approve", "Reject"],
    )

    assert replacement.action == "send"
    observation = ledger.observe(old.binding_key)
    assert observation.status == "superseded"
    assert observation.response is None


def test_persisted_choice_can_answer_card_after_gateway_restart(tmp_path):
    path = tmp_path / "approval-cards.db"
    first_gateway = ApprovalCardLedger(path, instance_id="gateway-a")
    first = first_gateway.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-a", session_key="session-a", choices=["Approve", "Reject"],
    )
    first_gateway.activate(first.card_id, "501")

    restarted = ApprovalCardLedger(path, instance_id="gateway-b")
    assert restarted.answer_choice("clarify-a", 0) == "Approve"
    assert restarted.response_for_binding(first.binding_key) == "Approve"


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


def test_expired_in_memory_wait_reuses_durable_card_instead_of_duplicating_it(tmp_path):
    ledger = ApprovalCardLedger(tmp_path / "approval-cards.db", instance_id="gateway-a")
    old = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-expired", session_key="session-expired",
    )
    ledger.activate(old.card_id, "501")

    duplicate = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-new", session_key="session-new",
        is_active=lambda clarify_id: False,
    )

    assert duplicate.action == "reuse_active"
    assert duplicate.clarify_id == "clarify-expired"
    assert duplicate.message_id == "501"


def test_predecessor_schema_active_recovery_remains_answerable_after_upgrade(tmp_path):
    path = tmp_path / "approval-cards.db"
    ledger = ApprovalCardLedger(path, instance_id="gateway-a")
    old = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-old", session_key="session-old", choices=["Approve", "Reject"],
    )
    recovery_ledger = ApprovalCardLedger(path, instance_id="gateway-b")
    recovery = recovery_ledger.claim_recovery(
        old.card_id, platform="telegram", chat_id="327791380")
    assert recovery_ledger.activate_recovery(
        old.card_id, recovery.delivery_generation, "600") is True
    legacy_id = f"approval-recovery-{old.card_id}"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE approval_recoveries SET clarify_id = ? WHERE card_id = ?",
            (legacy_id, old.card_id),
        )
        connection.execute("ALTER TABLE approval_recoveries DROP COLUMN generation")

    upgraded = ApprovalCardLedger(path, instance_id="gateway-b")
    reused = upgraded.claim_recovery(
        old.card_id, platform="telegram", chat_id="327791380")

    assert reused.action == "reuse_active"
    assert reused.clarify_id == legacy_id
    assert upgraded.answer_choice(legacy_id, 1) == "Manter bloqueado"


def test_recovery_callback_data_fits_telegram_limit_at_maximum_card_id(tmp_path):
    path = tmp_path / "approval-cards.db"
    ledger = ApprovalCardLedger(path, instance_id="gateway-a")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO sqlite_sequence(name, seq) VALUES('approval_cards', 9223372036854775806)"
        )
    old = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-old", session_key="session-old", choices=["Approve", "Reject"],
    )
    recovery_ledger = ApprovalCardLedger(path, instance_id="gateway-b")
    recovery = recovery_ledger.claim_recovery(
        old.card_id, platform="telegram", chat_id="327791380")

    assert old.card_id == 9223372036854775807
    callback = f"cl:{recovery.clarify_id}:other".encode("utf-8")
    assert len(callback) <= 64
