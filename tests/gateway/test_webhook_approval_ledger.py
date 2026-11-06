import multiprocessing
import sqlite3
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import pytest

from gateway.approval_card_ledger import ApprovalCardLedger


def _binding(digest="2" * 64, head="1" * 40, decision="4" * 64):
    return {
        "item_key": "rbueno69-git/carteira-investimentos#160",
        "head_sha": head,
        "contract_digest_sha256": digest,
        "scope": "Somente leitura; merge e deploy autorizados.",
        "decision_digest_sha256": decision,
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


def test_changed_decision_semantics_never_reuses_answered_binding(tmp_path):
    ledger = ApprovalCardLedger(tmp_path / "approval-cards.db", instance_id="gateway-a")
    first = ledger.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-a", session_key="session-a", choices=["Approve", "Reject"],
    )
    assert ledger.activate(first.card_id, "501") is True
    assert ledger.answer_choice("clarify-a", 0) == "Approve"

    changed = ledger.claim(
        _binding(decision="5" * 64), platform="telegram", chat_id="327791380",
        clarify_id="clarify-b", session_key="session-b", choices=["Approve", "Reject"],
    )

    assert changed.action == "send"
    assert changed.response is None


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


def test_stale_recovery_delivery_is_never_automatically_reissued(tmp_path):
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

    replay = ApprovalCardLedger(
        path, instance_id="gateway-c", recovery_lease_seconds=0
    ).claim_recovery(old.card_id, platform="telegram", chat_id="327791380")

    assert replay.action == "reuse_active"
    assert replay.owner_instance_id == "gateway-b"
    assert replay.delivery_generation == first_recovery.delivery_generation
    assert replay.clarify_id == first_recovery.clarify_id
    assert crashed.answer_choice(
        first_recovery.clarify_id or "", 0
    ) == "Reemitir aprovação"
    assert crashed.observe(old.binding_key).status == "failed"
    assert crashed.activate_recovery(
        old.card_id, first_recovery.delivery_generation, "600"
    ) is True
    assert crashed.observe(old.binding_key).status == "failed"


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


def test_recovery_continuation_survives_restart_for_delivery_hydration(tmp_path):
    path = tmp_path / "approval-cards.db"
    crashed = ApprovalCardLedger(path, instance_id="gateway-a")
    old = crashed.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-old", session_key="session-old", choices=["Approve", "Reject"],
    )
    continuation = {
        "session_chat_id": "webhook-delivery",
        "delivery": {
            "deliver": "telegram",
            "deliver_extra": {"chat_id": "327791380"},
            "approval_binding": {
                "item_key": "owner/repo#161",
                "head_sha": "1" * 40,
                "contract_digest_sha256": "2" * 64,
                "scope": "merge",
            },
        },
        "question": "Approve?",
        "choices": ["Approve", "Reject"],
        "clarify_id": "clarify-new",
        "session_key": "session-new",
    }
    deadline = time.time() + 60.0
    recovery = ApprovalCardLedger(path, instance_id="gateway-b").claim_recovery(
        old.card_id,
        platform="telegram",
        chat_id="327791380",
        continuation=continuation,
        expires_at=deadline,
    )

    contexts = ApprovalCardLedger(
        path, instance_id="gateway-c"
    ).recoverable_delivery_contexts(now=time.time())

    assert recovery.action == "send"
    assert contexts == [continuation]


def test_recovery_expiry_is_durable_and_generation_fenced(tmp_path):
    path = tmp_path / "approval-cards.db"
    crashed = ApprovalCardLedger(path, instance_id="gateway-a")
    old = crashed.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-old", session_key="session-old", choices=["Approve", "Reject"],
    )
    recovery_ledger = ApprovalCardLedger(path, instance_id="gateway-b")
    deadline = time.time() + 60.0
    recovery = recovery_ledger.claim_recovery(
        old.card_id,
        platform="telegram",
        chat_id="327791380",
        continuation={"session_chat_id": "delivery", "delivery": {}},
        expires_at=deadline,
    )
    assert recovery_ledger.activate_recovery(
        old.card_id, recovery.delivery_generation, "600") is True

    assert recovery_ledger.expire_recovery(
        old.card_id, "0" * 32, now=deadline
    ) is False
    assert recovery_ledger.expire_recovery(
        old.card_id, recovery.delivery_generation, now=deadline
    ) is True
    assert recovery_ledger.observe_recovery(old.card_id).status == "failed"

    reclaimed = ApprovalCardLedger(path, instance_id="gateway-c").claim_recovery(
        old.card_id,
        platform="telegram",
        chat_id="327791380",
        continuation={"session_chat_id": "delivery", "delivery": {}},
        expires_at=deadline + 900.0,
    )
    assert reclaimed.action == "expired"
    assert reclaimed.expires_at == deadline
    assert recovery_ledger.observe_recovery(old.card_id).status == "failed"


def test_recovery_answer_and_activation_expire_atomically_at_deadline(tmp_path):
    path = tmp_path / "approval-cards.db"
    crashed = ApprovalCardLedger(path, instance_id="gateway-a")
    old = crashed.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-old", session_key="session-old", choices=["Approve", "Reject"],
    )
    ledger = ApprovalCardLedger(path, instance_id="gateway-b")
    recovery = ledger.claim_recovery(
        old.card_id, platform="telegram", chat_id="327791380",
        continuation={"session_chat_id": "delivery", "delivery": {}},
        expires_at=time.time() + 60.0,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE approval_recoveries SET expires_at = ? WHERE card_id = ?",
            (time.time() - 1.0, old.card_id),
        )

    assert ledger.activate_recovery(
        old.card_id, recovery.delivery_generation, "600"
    ) is False
    assert ledger.answer_choice(recovery.clarify_id, 0) is None
    assert ledger.observe_recovery(old.card_id).status == "failed"
    assert ledger.observe(old.binding_key).status == "pending"


def test_nullable_existing_recovery_generation_is_repaired_before_callback(tmp_path):
    path = tmp_path / "approval-cards.db"
    crashed = ApprovalCardLedger(path, instance_id="gateway-a")
    old = crashed.claim(
        _binding(), platform="telegram", chat_id="327791380",
        clarify_id="clarify-old", session_key="session-old", choices=["Approve", "Reject"],
    )
    ledger = ApprovalCardLedger(path, instance_id="gateway-b")
    recovery = ledger.claim_recovery(
        old.card_id, platform="telegram", chat_id="327791380",
        expires_at=time.time() + 60.0,
    )
    legacy_id = f"approval-recovery-{old.card_id}"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            ALTER TABLE approval_recoveries RENAME TO approval_recoveries_current;
            CREATE TABLE approval_recoveries (
                card_id INTEGER PRIMARY KEY REFERENCES approval_cards(card_id),
                clarify_id TEXT NOT NULL UNIQUE,
                instance_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                message_id TEXT,
                generation TEXT,
                status TEXT NOT NULL,
                response TEXT,
                continuation_json TEXT,
                expires_at REAL,
                updated_at REAL NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO approval_recoveries(
                card_id, clarify_id, instance_id, platform, chat_id, message_id,
                generation, status, response, continuation_json, expires_at, updated_at
            )
            SELECT card_id, ?, instance_id, platform, chat_id, message_id,
                   NULL, status, response, continuation_json, expires_at, updated_at
            FROM approval_recoveries_current
            """,
            (legacy_id,),
        )
        connection.execute("DROP TABLE approval_recoveries_current")

    upgraded = ApprovalCardLedger(path, instance_id="gateway-c")

    assert upgraded.answer_choice(legacy_id, 1) == "Manter bloqueado"
    with sqlite3.connect(path) as connection:
        generation, status = connection.execute(
            "SELECT generation, status FROM approval_recoveries WHERE card_id = ?",
            (old.card_id,),
        ).fetchone()
    assert generation is not None
    assert status == "answered"


def test_superseded_binding_cannot_be_resurrected_by_stale_replay(tmp_path):
    ledger = ApprovalCardLedger(tmp_path / "approval-cards.db", instance_id="gateway-a")
    old_binding = _binding()
    old = ledger.claim(
        old_binding, platform="telegram", chat_id="1", clarify_id="old",
        session_key="old-session", choices=["Approve", "Reject"],
    )
    assert ledger.activate(old.card_id, "10")
    new_binding = dict(old_binding, head_sha="9" * 40)
    new = ledger.claim(
        new_binding, platform="telegram", chat_id="1", clarify_id="new",
        session_key="new-session", choices=["Approve", "Reject"],
    )
    assert ledger.activate(new.card_id, "11")

    stale = ledger.claim(
        old_binding, platform="telegram", chat_id="1", clarify_id="stale",
        session_key="stale-session", choices=["Approve", "Reject"],
    )

    assert stale.action == "superseded"
    assert ledger.observe(old.binding_key).status == "superseded"
    assert ledger.observe(new.binding_key).status == "pending"
    assert ledger.answer_choice("old", 0) is None


def test_primary_card_deadline_is_atomic_and_survives_restart(tmp_path):
    path = tmp_path / "approval-cards.db"
    ledger = ApprovalCardLedger(path, instance_id="gateway-a")
    claim = ledger.claim(
        _binding(), platform="telegram", chat_id="1", clarify_id="deadline-card",
        session_key="deadline-session", choices=["Approve", "Reject"],
        expires_at=time.time() - 1.0,
    )

    assert ledger.activate(claim.card_id, "10") is False
    restarted = ApprovalCardLedger(path, instance_id="gateway-b")
    assert restarted.answer_choice("deadline-card", 0) is None
    assert restarted.observe(claim.binding_key).status == "pending"



def test_nullable_legacy_decision_digest_cannot_reuse_answered_binding(tmp_path):
    path = tmp_path / "approval-cards.db"
    binding = _binding()
    from gateway.approval_card_ledger import canonical_binding_key

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE approval_bindings (
                binding_key TEXT PRIMARY KEY,
                item_key TEXT NOT NULL,
                head_sha TEXT NOT NULL,
                contract_digest_sha256 TEXT NOT NULL,
                scope TEXT NOT NULL,
                decision_digest_sha256 TEXT,
                status TEXT NOT NULL,
                response TEXT,
                updated_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO approval_bindings(
                binding_key, item_key, head_sha, contract_digest_sha256, scope,
                decision_digest_sha256, status, response, updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, 'answered', 'APPROVE-OLD', 1.0)
            """,
            (
                canonical_binding_key(binding), binding["item_key"], binding["head_sha"],
                binding["contract_digest_sha256"], binding["scope"],
            ),
        )

    ledger = ApprovalCardLedger(path, instance_id="gateway-b")
    with pytest.raises(ValueError, match="canonical identity"):
        ledger.claim(
            binding, platform="telegram", chat_id="327791380",
            clarify_id="clarify-new", session_key="session-new",
            choices=["Approve", "Reject"],
        )


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


def test_concurrent_legacy_schema_migration_is_serialized(tmp_path, monkeypatch):
    path = tmp_path / "approval-cards.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE approval_bindings (
                binding_key TEXT PRIMARY KEY,
                item_key TEXT NOT NULL,
                head_sha TEXT NOT NULL,
                contract_digest_sha256 TEXT NOT NULL,
                scope TEXT NOT NULL,
                status TEXT NOT NULL,
                response TEXT,
                updated_at REAL NOT NULL
            )
            """
        )

    real_connect = sqlite3.connect
    racers = 8
    inspection_barrier = threading.Barrier(racers)

    class RacingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if (
                sql.strip().startswith("PRAGMA table_info(approval_bindings)")
                and not self.in_transaction
            ):
                inspection_barrier.wait(timeout=10)
            return cursor

    def racing_connect(*args, **kwargs):
        return real_connect(*args, factory=RacingConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", racing_connect)
    with ThreadPoolExecutor(max_workers=racers) as pool:
        futures = [
            pool.submit(ApprovalCardLedger, path, instance_id=f"gateway-{index}")
            for index in range(racers)
        ]
    failures = [future.exception() for future in futures if future.exception() is not None]

    assert failures == []
    with real_connect(path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(approval_bindings)")
        }
    assert "decision_digest_sha256" in columns


def test_startup_migration_waits_through_normal_writer_contention(tmp_path):
    path = tmp_path / "approval-cards.db"
    ApprovalCardLedger(path, instance_id="gateway-a")
    writer = sqlite3.connect(path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "UPDATE approval_bindings SET updated_at = updated_at WHERE 0"
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ApprovalCardLedger, path, instance_id="gateway-b")
        time.sleep(5.25)
        writer.commit()
        restarted = future.result(timeout=10)
    writer.close()

    assert restarted.instance_id == "gateway-b"


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
