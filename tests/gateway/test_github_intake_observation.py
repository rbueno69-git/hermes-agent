"""Accepted GitHub webhook events remain durable when intake has no contract."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.github_approval_broker import GitHubApprovalBroker
from gateway.github_intake_continuation import IntakeContinuation
from gateway.platforms.base import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _explicit_merge_review_requested


def test_explicit_merge_review_markers_must_form_one_bounded_attribution_block():
    assert _explicit_merge_review_requested(
        "Acao pretendida: revisar e, se aprovado, mergear\nStatus: aguardando revisao") is True
    assert _explicit_merge_review_requested(
        "Acao pretendida: revisar e, se aprovado, mergear\n" + ("contexto\n" * 150)
        + "Status: aguardando revisao") is False


@pytest.mark.asyncio
async def test_accepted_pr_without_contract_persists_reason_and_exact_head(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "webhook_subscriptions.json").write_text(json.dumps({
        "github-hermes-intake": {
            "secret": "INSECURE_NO_AUTH",
            "events": ["pull_request"],
            "script": "github-intake.py",
            "deliver": "telegram",
            "deliver_extra": {"chat_id": "42"},
            "github_approval": {
                "schema": "hermes.github-intake.v2", "authorized_user_id": "7"},
        }
    }))
    broker = GitHubApprovalBroker(tmp_path / "approval.db", instance_id="endpoint")
    monkeypatch.setattr("gateway.github_approval_broker.default_broker", lambda **_kwargs: broker)
    runner = MagicMock()
    runner.config = SimpleNamespace(multiplex_profiles=False)
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0}))
    adapter.gateway_runner = runner
    adapter._route_processor.run_route_script = MagicMock(return_value=(False, None))
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    head = "247cbb18197811508a2d697c28a016ff755cead7"
    payload = {
        "action": "opened",
        "repository": {"full_name": "rbueno69-git/tutor-estudos"},
        "pull_request": {
            "number": 39, "state": "open", "title": "Tema columns",
            "head": {"sha": head},
        },
    }

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/webhooks/github-hermes-intake", json=payload,
            headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "pr-39"},
        )
        assert response.status == 202
        response_body = await response.json()

    assert response_body["status"] == "ineligible_recorded"
    row = IntakeContinuation(broker.path).observe("rbueno69-git/tutor-estudos#39", head)
    assert row["identity"] == f"rbueno69-git/tutor-estudos#39+{head}"
    assert row["reason_code"] == "missing_contract"
    assert row["state"] == "pending"


@pytest.mark.asyncio
async def test_pr_explicitly_awaiting_review_is_not_classified_as_ordinary_missing_contract(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "webhook_subscriptions.json").write_text(json.dumps({
        "github-hermes-intake": {
            "secret": "INSECURE_NO_AUTH", "events": ["pull_request"],
            "script": "github-intake.py", "deliver": "telegram",
            "deliver_extra": {"chat_id": "42"},
            "github_approval": {
                "schema": "hermes.github-intake.v2", "authorized_user_id": "7"},
        }
    }))
    broker = GitHubApprovalBroker(tmp_path / "approval.db", instance_id="endpoint")
    monkeypatch.setattr("gateway.github_approval_broker.default_broker", lambda **_kwargs: broker)
    runner = MagicMock()
    runner.config = SimpleNamespace(multiplex_profiles=False)
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0}))
    adapter.gateway_runner = runner
    adapter._route_processor.run_route_script = MagicMock(return_value=(False, None))
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    head = "247cbb18197811508a2d697c28a016ff755cead7"
    payload = {
        "action": "opened", "repository": {"full_name": "rbueno69-git/tutor-estudos"},
        "pull_request": {
            "number": 39, "state": "open", "title": "Tema columns", "head": {"sha": head},
            "body": "Acao pretendida: revisar e, se aprovado, mergear\nStatus: aguardando revisao",
        },
    }

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/webhooks/github-hermes-intake", json=payload,
            headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "pr-review-39"},
        )
        assert response.status == 202

    row = IntakeContinuation(broker.path).observe("rbueno69-git/tutor-estudos#39", head)
    assert row["reason_code"] == "review_requested"
    assert row["state"] == "pending"


@pytest.mark.asyncio
async def test_continuation_worker_runs_review_requests_when_approval_ledger_has_no_references(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0}))
    adapter._routes = {
        "github-hermes-intake": {
            "profile": None,
            "github_approval": {
                "schema": "hermes.github-intake.v2", "authorized_user_id": "7"},
            "deliver_extra": {"chat_id": "42"},
        }
    }
    calls = []
    monkeypatch.setattr(
        "gateway.github_intake_continuation.read_executable_references", lambda _path: [])
    monkeypatch.setattr(
        "gateway.github_intake_continuation.read_approved_tuples", lambda _path: set())
    monkeypatch.setattr(
        "gateway.github_intake_continuation.IntakeContinuation.reconcile",
        lambda _self, **kwargs: calls.append(kwargs) or [],
    )

    result = await adapter._reconcile_github_intake_continuations(
        profile=None,
        broker=SimpleNamespace(path=tmp_path / "approval.db"),
        target=SimpleNamespace(),
    )

    assert result == 0
    assert len(calls) == 1
    assert calls[0]["references"] == []
