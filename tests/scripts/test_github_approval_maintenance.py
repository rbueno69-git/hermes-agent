"""Contract tests for the script-only GitHub approval maintenance job."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "github_approval_maintenance.py"
ITEM = "rbueno69-git/tutordigital-dashboard#519"
HEAD = "6" * 40
DIGEST = "d" * 64


def _load():
    spec = importlib.util.spec_from_file_location("github_approval_maintenance", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_snapshot_candidates_include_only_local_approval_observations(tmp_path):
    module = _load()
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"items": [
        {"id": ITEM, "observation": "unassigned_approval_candidate"},
        {"id": "rbueno69-git/pink-ops#1", "observation": "proposed_hermes_candidate"},
        {"id": "rbueno69-git/pink-ops#2", "observation": "proposed_foreign_candidate"},
        {"id": "rbueno69-git/pink-ops#3", "observation": "read_only_observed"},
    ]}), encoding="utf-8")

    assert module.snapshot_candidates(snapshot) == {
        ITEM, "rbueno69-git/pink-ops#1",
    }


def test_signed_local_request_has_deterministic_delivery_and_exact_event():
    module = _load()
    payload = {
        "action": "edited",
        "repository": {"full_name": "rbueno69-git/tutordigital-dashboard"},
        "pull_request": {"number": 519, "head": {"sha": HEAD}},
    }

    request = module.signed_local_request(
        payload=payload,
        url="http://127.0.0.1:8644/webhooks/github-hermes-intake",
        secret="test-secret",
    )
    body = request.data
    expected_delivery = hashlib.sha256(
        b"hermes-github-approval-watchdog-v1\0" + body
    ).hexdigest()

    assert request.full_url.startswith("http://127.0.0.1:")
    assert request.headers["X-github-event"] == "pull_request"
    assert request.headers["X-github-delivery"] == expected_delivery
    assert request.headers["X-hub-signature-256"] == (
        "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    )


def test_cursor_advances_only_after_every_candidate_is_processed(tmp_path, monkeypatch):
    module = _load()
    module.CURSOR = tmp_path / "cursor.json"
    module.CHANGES = tmp_path / "changes.jsonl"
    module.SNAPSHOT = tmp_path / "snapshot.json"
    module.BROKER = tmp_path / "broker.db"
    module.LEDGER = tmp_path / "approvals.jsonl"
    module.LOCK = tmp_path / "maintenance.lock"
    module.CHANGES.write_text(json.dumps({"id": ITEM}) + "\n", encoding="utf-8")
    module.SNAPSHOT.write_text(json.dumps({"items": []}), encoding="utf-8")
    monkeypatch.setattr(module, "project_pending_decisions", list)
    monkeypatch.setattr(module, "actionable_items", lambda: set())
    monkeypatch.setattr(module, "live_payload", lambda _item: {"action": "edited"})
    monkeypatch.setattr(module, "validated_envelope", lambda _payload: {
        "contract_accepted": True, "head_sha": HEAD, "digest": DIGEST,
    })
    monkeypatch.setattr(module, "has_current_tuple", lambda _item, _envelope: False)
    monkeypatch.setattr(module, "post_signed_local", lambda _payload: (_ for _ in ()).throw(RuntimeError("down")))

    with pytest.raises(RuntimeError, match="down"):
        module.run_once(dry_run=False)

    assert not module.CURSOR.exists()


def test_snapshot_candidate_rejected_by_intake_does_not_advance_cursor(tmp_path, monkeypatch):
    module = _load()
    module.CURSOR = tmp_path / "cursor.json"
    module.CHANGES = tmp_path / "changes.jsonl"
    module.SNAPSHOT = tmp_path / "snapshot.json"
    module.BROKER = tmp_path / "broker.db"
    module.LEDGER = tmp_path / "approvals.jsonl"
    module.LOCK = tmp_path / "maintenance.lock"
    module.CHANGES.write_text(json.dumps({"id": ITEM}) + "\n", encoding="utf-8")
    module.SNAPSHOT.write_text(json.dumps({"items": [
        {"id": ITEM, "observation": "unassigned_approval_candidate"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(module, "project_pending_decisions", list)
    monkeypatch.setattr(module, "actionable_items", lambda: set())
    monkeypatch.setattr(module, "live_payload", lambda _item: {"action": "edited"})
    monkeypatch.setattr(module, "validated_envelope", lambda _payload: None)

    with pytest.raises(RuntimeError, match="candidate item rejected by Hermes intake"):
        module.run_once(dry_run=False)

    assert not module.CURSOR.exists()


def test_rejected_candidate_does_not_block_later_valid_candidate(tmp_path, monkeypatch):
    module = _load()
    rejected = "rbueno69-git/Open-Loops#1"
    valid = ITEM
    module.CURSOR = tmp_path / "cursor.json"
    module.CHANGES = tmp_path / "changes.jsonl"
    module.SNAPSHOT = tmp_path / "snapshot.json"
    module.BROKER = tmp_path / "broker.db"
    module.LEDGER = tmp_path / "approvals.jsonl"
    module.LOCK = tmp_path / "maintenance.lock"
    module.CHANGES.write_text(
        json.dumps({"id": rejected}) + "\n" + json.dumps({"id": valid}) + "\n",
        encoding="utf-8",
    )
    module.SNAPSHOT.write_text(json.dumps({"items": [
        {"id": rejected, "observation": "unassigned_approval_candidate"},
        {"id": valid, "observation": "unassigned_approval_candidate"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(module, "project_pending_decisions", list)
    monkeypatch.setattr(module, "actionable_items", lambda: set())
    monkeypatch.setattr(module, "live_payload", lambda item: {"action": "edited", "item": item})
    monkeypatch.setattr(
        module,
        "validated_envelope",
        lambda payload: None if payload["item"] == rejected else {
            "contract_accepted": True, "head_sha": HEAD, "digest": DIGEST,
        },
    )
    monkeypatch.setattr(module, "has_current_tuple", lambda _item, _envelope: False)
    emitted = []
    monkeypatch.setattr(module, "post_signed_local", lambda payload: emitted.append(payload["item"]))

    with pytest.raises(RuntimeError, match=rejected):
        module.run_once(dry_run=False)

    assert emitted == [valid]
    assert not module.CURSOR.exists()


def test_snapshot_only_contract_is_not_replayed_without_new_change(tmp_path, monkeypatch):
    module = _load()
    module.CURSOR = tmp_path / "cursor.json"
    module.CHANGES = tmp_path / "changes.jsonl"
    module.SNAPSHOT = tmp_path / "snapshot.json"
    module.BROKER = tmp_path / "broker.db"
    module.LEDGER = tmp_path / "approvals.jsonl"
    module.LOCK = tmp_path / "maintenance.lock"
    module.SNAPSHOT.write_text(json.dumps({"items": [
        {"id": ITEM, "observation": "unassigned_approval_candidate"},
    ]}), encoding="utf-8")
    module.CURSOR.write_text('{"changes_lines":0}\n', encoding="utf-8")
    monkeypatch.setattr(module, "project_pending_decisions", list)
    monkeypatch.setattr(module, "actionable_items", lambda: set())
    monkeypatch.setattr(module, "live_payload", lambda _item: (_ for _ in ()).throw(
        AssertionError("snapshot-only candidate must not be replayed")
    ))

    result = module.run_once(dry_run=False)

    assert result["emitted"] == []
    assert result["candidates"] == []


def test_bind_internal_continuation_cli_persists_exact_explicit_lane(tmp_path, monkeypatch, capsys):
    module = _load()
    module.BROKER = tmp_path / "broker.db"
    kanban = tmp_path / "kanban.db"
    calls = []

    class BrokerStub:
        def __init__(self, path, *, instance_id):
            calls.append(("init", path, instance_id))

        def bind_internal_continuation(self, **kwargs):
            calls.append(("bind", kwargs))

    monkeypatch.setattr(module, "GitHubApprovalBroker", BrokerStub)
    monkeypatch.setattr(module, "kanban_db_path", lambda: kanban)
    monkeypatch.setattr(module, "run_once", lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("binding must not run ordinary maintenance")))

    assert module.main([
        "--bind-internal-continuation", "proposal-key", "t_blocked", "t_fix",
    ]) == 0

    assert calls == [
        ("init", module.BROKER, "github-approval-maintenance:continuation-binding"),
        ("bind", {
            "predecessor_proposal_key": "proposal-key",
            "predecessor_task_id": "t_blocked",
            "continuation_task_id": "t_fix",
            "kanban_path": kanban,
        }),
    ]
    assert json.loads(capsys.readouterr().out) == {
        "action": "internal_continuation_bound",
        "continuation_task_id": "t_fix",
        "predecessor_proposal_key": "proposal-key",
        "predecessor_task_id": "t_blocked",
    }


def test_bind_item_succession_cli_uses_live_broker_and_kanban_paths(tmp_path, monkeypatch, capsys):
    module = _load()
    module.BROKER = tmp_path / "broker.db"
    kanban = tmp_path / "kanban.db"
    calls = []

    class BrokerStub:
        def __init__(self, path, *, instance_id):
            calls.append(("init", path, instance_id))

        def bind_item_succession(self, **kwargs):
            calls.append(("bind", kwargs))

    monkeypatch.setattr(module, "GitHubApprovalBroker", BrokerStub)
    monkeypatch.setattr(module, "kanban_db_path", lambda: kanban)
    monkeypatch.setattr(module, "run_once", lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("binding must not run ordinary maintenance")))

    assert module.main([
        "--bind-item-succession", "owner/repo#2", "owner/repo#3", "t_successor",
    ]) == 0

    assert calls == [
        ("init", module.BROKER, "github-approval-maintenance:item-succession-binding"),
        ("bind", {
            "predecessor_item_key": "owner/repo#2",
            "successor_item_key": "owner/repo#3",
            "successor_task_id": "t_successor",
            "kanban_path": kanban,
        }),
    ]
    assert json.loads(capsys.readouterr().out) == {
        "action": "item_succession_bound",
        "predecessor_item_key": "owner/repo#2",
        "successor_item_key": "owner/repo#3",
        "successor_task_id": "t_successor",
    }
