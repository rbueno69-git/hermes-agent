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
from gateway.platforms.webhook import WebhookAdapter


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
