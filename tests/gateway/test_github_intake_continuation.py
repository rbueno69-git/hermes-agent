"""Regression coverage for durable continuation of ineligible GitHub intake."""

from __future__ import annotations

from gateway.github_intake_continuation import (
    IntakeContinuation,
    IntakeObservation,
    MissingMaterial,
    build_merge_only_contract,
    read_executable_references,
)


ITEM = "rbueno69-git/tutor-estudos#39"
HEAD = "247cbb18197811508a2d697c28a016ff755cead7"
DIGEST = "a545ed2e897d8373f849260739f63cb11aa1f6a5802b74fd420d7d07c47c2520"


def _observation() -> IntakeObservation:
    return IntakeObservation(
        item_key=ITEM,
        head_sha=HEAD,
        reason_code="missing_contract",
        title="docs(historia): add Tema columns",
    )


def test_referenced_open_pr_cannot_disappear_and_proposes_once(tmp_path):
    continuation = IntakeContinuation(tmp_path / "broker.db")
    continuation.record(_observation())
    sent = []

    def build(observation, reference):
        assert observation.item_key == ITEM
        assert ITEM in reference
        return {
            "item_key": ITEM,
            "head_sha": HEAD,
            "contract_digest_sha256": DIGEST,
            "question": "Validate exact diff locally; squash merge; no deploy.",
        }

    first = continuation.reconcile(
        references=[f"Executable contract consumes {ITEM} as its source."],
        contract_builder=build,
        approval_sender=lambda proposal: sent.append(proposal),
        clarification_sender=lambda *_args: None,
    )
    second = continuation.reconcile(
        references=[f"Executable contract consumes {ITEM} as its source."],
        contract_builder=build,
        approval_sender=lambda proposal: sent.append(proposal),
        clarification_sender=lambda *_args: None,
    )

    assert first == [{"identity": f"{ITEM}+{HEAD}", "action": "approval_proposed"}]
    assert second == []
    assert len(sent) == 1
    assert sent[0]["contract_digest_sha256"] == DIGEST
    row = continuation.observe(ITEM, HEAD)
    assert row["reason_code"] == "missing_contract"
    assert row["state"] == "approval_proposed"


def test_unreferenced_common_pr_is_terminally_ignored_without_card(tmp_path):
    continuation = IntakeContinuation(tmp_path / "broker.db")
    continuation.record(_observation())
    cards = []

    result = continuation.reconcile(
        references=[],
        contract_builder=lambda *_args: (_ for _ in ()).throw(AssertionError("not expected")),
        approval_sender=lambda proposal: cards.append(proposal),
        clarification_sender=lambda question, choices: cards.append((question, choices)),
    )

    assert result == [{"identity": f"{ITEM}+{HEAD}", "action": "ignored_by_policy"}]
    assert cards == []
    assert continuation.observe(ITEM, HEAD)["state"] == "ignored_by_policy"


def test_expected_pr_with_missing_material_requests_native_clarification_once(tmp_path):
    continuation = IntakeContinuation(tmp_path / "broker.db")
    continuation.record(_observation())
    cards = []

    def missing(_observation, _reference):
        raise MissingMaterial("Choose whether production deployment is in scope")

    first = continuation.reconcile(
        references=[f"Depends on {ITEM}"],
        contract_builder=missing,
        approval_sender=lambda _proposal: None,
        clarification_sender=lambda question, choices: cards.append((question, choices)),
    )
    second = continuation.reconcile(
        references=[f"Depends on {ITEM}"],
        contract_builder=missing,
        approval_sender=lambda _proposal: None,
        clarification_sender=lambda question, choices: cards.append((question, choices)),
    )

    assert first == [{"identity": f"{ITEM}+{HEAD}", "action": "clarification_requested"}]
    assert second == []
    assert cards == [(
        f"{ITEM} at {HEAD}: Choose whether production deployment is in scope",
        ["Prepare merge-only contract", "Ignore this PR"],
    )]
    assert continuation.observe(ITEM, HEAD)["state"] == "clarification_requested"


def test_builder_derives_bounded_merge_only_contract_from_exact_live_diff():
    def github(endpoint):
        if endpoint.endswith("/pulls/39"):
            return {"number": 39, "state": "open", "head": {"sha": HEAD}}
        if endpoint.endswith("/pulls/39/files?per_page=100"):
            return [
                {"filename": "historia-5ano/INDICE.md"},
                {"filename": "historia-8ano/INDICE.md"},
            ]
        raise AssertionError(endpoint)

    proposal = build_merge_only_contract(
        _observation(), f"Executable contract consumes {ITEM}.", github_reader=github)

    assert proposal["item_key"] == ITEM
    assert proposal["head_sha"] == HEAD
    assert len(proposal["contract_digest_sha256"]) == 64
    assert "historia-5ano/INDICE.md" in proposal["question"]
    assert "No deploy" in proposal["question"]


def test_reference_reader_uses_only_executable_approval_rows(tmp_path):
    ledger = tmp_path / "approvals.jsonl"
    ledger.write_text(
        '{"status":"ignored_by_policy","scope":"mentions ' + ITEM + '"}\n'
        '{"status":"approved_terminal","scope":"Consumes ' + ITEM + '"}\n',
        encoding="utf-8",
    )

    assert read_executable_references(ledger) == [f"Consumes {ITEM}"]
