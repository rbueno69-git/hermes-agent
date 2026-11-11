import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform, PlatformConfig
from gateway.github_approval_broker import ApprovalProposal, GitHubApprovalBroker
from gateway.platforms.base import SendResult
from gateway.platforms.webhook import WebhookAdapter
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.telegram_clarify_binding import ClarifyPrompt


class BoundTelegram:
    def __init__(self, *, outcome="success"):
        self.outcome = outcome
        self.sent = []
        self.retired = []
        self._clarify_state = {}

    def approval_destination_identity(self, clarify_id=None):
        return "telegram-bot:501"

    async def send_clarify(self, **kwargs):
        self.sent.append(kwargs)
        if self.outcome == "raise":
            raise TimeoutError("unknown send outcome")
        if self.outcome == "failure":
            return SendResult(success=False, error="rejected before send")
        self._clarify_state[kwargs["clarify_id"]] = kwargs["session_key"]
        return SendResult(success=True, message_id="501")

    def approval_callback_binding(self, clarify_id, session_key):
        return self._clarify_state.get(clarify_id) == session_key

    async def retire_clarify_card(self, clarify_id, notice):
        self._clarify_state.pop(clarify_id, None)
        self.retired.append((clarify_id, notice))


def proposal(item_key="owner/repo#7", head="a" * 40, digest="b" * 64):
    return ApprovalProposal(item_key, head, digest)


@pytest.mark.parametrize("item_key", [
    "owner/repo#0", "owner/repo#-1", "owner/repo#1x", "owner/repo#1/2",
    "/repo#1", "owner/#1", "owner/repo# 1", "owner/repo#01", "owner/repo#1#2",
])
def test_item_key_requires_strict_owner_repo_and_positive_canonical_number(item_key):
    with pytest.raises(ValueError, match="owner/repo#number"):
        proposal(item_key=item_key).validate()


@pytest.mark.asyncio
async def test_unknown_provider_send_outcome_is_never_retried(tmp_path):
    path = tmp_path / "approval.db"
    broker = GitHubApprovalBroker(path, instance_id="one", delivery_lease_seconds=1)
    outcome = await broker.request(
        proposal(), adapter=BoundTelegram(outcome="raise"), chat_id="42", question="Approve?",
        session_key="webhook:1", authorized_user_id="7")

    assert outcome.action == "operator_reconciliation"
    assert broker.observe(proposal())["state"] == "delivery_unknown"
    assert await GitHubApprovalBroker(path, instance_id="two").retry_pending(adapter=BoundTelegram()) == []


@pytest.mark.asyncio
async def test_webhook_binding_is_checked_on_delegated_telegram_adapter(tmp_path):
    target = BoundTelegram()
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    adapter._delivery_info = {"delivery": {"deliver": "telegram", "deliver_extra": {"chat_id": "42"}}}
    broker = GitHubApprovalBroker(tmp_path / "approval.db", instance_id="webhook")

    outcome = await broker.request(
        proposal(), adapter=adapter, chat_id="delivery", question="Approve?",
        session_key="webhook:1", authorized_user_id="7")

    assert outcome.action == "sent"
    assert broker.observe(proposal())["state"] == "active"


@pytest.mark.asyncio
async def test_callback_is_bound_to_user_chat_thread_and_message(tmp_path):
    broker = GitHubApprovalBroker(tmp_path / "approval.db", instance_id="one")
    sent = await broker.request(
        proposal(), adapter=BoundTelegram(), chat_id="42", thread_id="9", question="Approve?",
        session_key="webhook:1", authorized_user_id="7")

    assert broker.resolve_callback(
        sent.clarify_id, 0, approver="telegram:7", user_id="7", chat_id="42",
        thread_id="9", message_id="wrong", destination_id="telegram-bot:501") is None
    assert broker.resolve_callback(
        sent.clarify_id, 0, approver="telegram:8", user_id="8", chat_id="42",
        thread_id="9", message_id="501", destination_id="telegram-bot:501") is None
    decision = broker.resolve_callback(
        sent.clarify_id, 0, approver="telegram:7", user_id="7", chat_id="42",
        thread_id="9", message_id="501", destination_id="telegram-bot:501")
    assert decision is not None and decision.response == "Approve"


def test_replayed_opposite_choice_returns_original_durable_decision(tmp_path):
    broker = GitHubApprovalBroker(tmp_path / "approval.db", instance_id="one")
    sent = asyncio.run(broker.request(
        proposal(), adapter=BoundTelegram(), chat_id="42", question="Approve?",
        session_key="webhook:1", authorized_user_id="7"))
    first = broker.resolve_callback(
        sent.clarify_id, 0, approver="telegram:7", user_id="7", chat_id="42",
        thread_id=None, message_id="501", destination_id="telegram-bot:501")
    replay = broker.resolve_callback(
        sent.clarify_id, 1, approver="telegram:7", user_id="7", chat_id="42",
        thread_id=None, message_id="501", destination_id="telegram-bot:501")

    assert first.response == replay.response == "Approve"
    assert broker.callback_choice(sent.clarify_id, 1, "telegram-bot:501") == "Approve"


class ForbiddenExecutor:
    def __init__(self):
        self.calls = []

    async def execute_idempotent(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("approval broker invoked a product executor")


@pytest.mark.asyncio
async def test_telegram_retire_clarify_card_removes_binding_and_keyboard():
    adapter = object.__new__(TelegramAdapter)
    adapter._clarify_state = {"gha-id": "session"}
    adapter._approval_clarify_state = {
        "gha-id": ClarifyPrompt("session", "42", "501", None)}
    adapter._app = SimpleNamespace(bot=SimpleNamespace(edit_message_text=AsyncMock()))

    await adapter.retire_clarify_card("gha-id", "Superseded")

    assert "gha-id" not in adapter._clarify_state
    assert "gha-id" not in adapter._approval_clarify_state
    adapter._app.bot.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_retirement_failure_keeps_coordinates_for_verified_retry():
    adapter = object.__new__(TelegramAdapter)
    adapter._clarify_state = {"gha-id": "session"}
    adapter._approval_clarify_state = {
        "gha-id": ClarifyPrompt("session", "42", "501", None)}
    edit = AsyncMock(side_effect=[TimeoutError("telegram timeout"), None])
    adapter._app = SimpleNamespace(bot=SimpleNamespace(edit_message_text=edit))

    with pytest.raises(TimeoutError):
        await adapter.retire_clarify_card("gha-id", "Superseded")
    assert "gha-id" not in adapter._clarify_state
    assert "gha-id" in adapter._approval_clarify_state

    await adapter.retire_clarify_card("gha-id", "Superseded")
    assert "gha-id" not in adapter._approval_clarify_state
    assert edit.await_count == 2


@pytest.mark.asyncio
async def test_restart_rebuilds_terminal_retirement_coordinates_without_callback(tmp_path):
    broker = GitHubApprovalBroker(tmp_path / "approval.db", instance_id="one")
    target = BoundTelegram()
    sent = await broker.request(
        proposal(), adapter=target, chat_id="42", thread_id="9", question="Approve?",
        session_key="webhook:1", authorized_user_id="7")

    async def fail_retire(*_args):
        raise TimeoutError("edit failed")

    target.retire_clarify_card = fail_retire
    await broker.retire_item(proposal().item_key, status="closed", adapter=target)
    fresh = object.__new__(TelegramAdapter)
    fresh._bot = SimpleNamespace(id=501)
    fresh._clarify_state = {}
    fresh._approval_clarify_state = {}
    edit = AsyncMock()
    fresh._app = SimpleNamespace(bot=SimpleNamespace(edit_message_text=edit))

    assert await broker.retry_retirements(adapter=fresh) == 1
    edit.assert_awaited_once()
    assert sent.clarify_id not in fresh._clarify_state


@pytest.mark.asyncio
async def test_telegram_approval_prompt_has_no_free_text_other_choice(monkeypatch):
    adapter = object.__new__(TelegramAdapter)
    adapter._clarify_state = {}
    adapter._approval_clarify_state = {}
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardButton",
        lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data),
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardMarkup",
        lambda rows: SimpleNamespace(inline_keyboard=rows),
    )

    async def capture(_kind, _chat_id, _metadata, build, **_kwargs):
        _text, keyboard, remember = build()
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        assert callbacks == ["cl:gha-id:0", "cl:gha-id:1"]
        remember(SimpleNamespace(chat_id=42, message_id=501, message_thread_id=None))
        return SendResult(success=True, message_id="501")

    adapter._send_prompt = capture
    await adapter.send_clarify(
        "42", "Approve?", ["Approve", "Do not approve"], "gha-id", "session",
        {"is_approval_prompt": True})

    expected = ClarifyPrompt("session", "42", "501", None)
    assert adapter._clarify_state["gha-id"] == expected
    assert adapter._approval_clarify_state["gha-id"] == expected


@pytest.mark.asyncio
async def test_repository_dispatch_producer_creates_card_and_never_admits_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gateway.platforms.webhook.web",
        SimpleNamespace(json_response=lambda body, status=200: SimpleNamespace(status=status, body=body)))
    target = BoundTelegram()
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    adapter._delivery_info = {}
    adapter._delivery_info_created = {}
    adapter._delivery_info_order = []
    route = {
        "deliver": "telegram",
        "deliver_extra": {"chat_id": "42", "message_thread_id": "9"},
        "github_approval": {"authorized_user_id": "7"},
    }
    payload = {
        "action": "hermes_contract_approval",
        "repository": {"full_name": "owner/repo"},
        "client_payload": {
            "pull_request_number": 7,
            "head_sha": "a" * 40,
            "contract_digest_sha256": "b" * 64,
            "executor": "hermes",
        },
    }
    broker = GitHubApprovalBroker(tmp_path / "approval.db", instance_id="producer")
    monkeypatch.setattr("gateway.github_approval_broker.default_broker", lambda **kwargs: broker)
    adapter._admit_agent_run = AsyncMock(side_effect=AssertionError("approval producer executed agent"))

    response = await adapter._handle_github_approval_contract(
        route, payload, "repository_dispatch", "route", None, "delivery-1")

    assert response.status == 202
    assert len(target.sent) == 1
    assert broker.observe(proposal())["state"] == "active"
    adapter._admit_agent_run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("merged, expected", [(False, "closed"), (True, "merged")])
async def test_pull_request_terminal_event_retires_existing_card(tmp_path, monkeypatch, merged, expected):
    monkeypatch.setattr(
        "gateway.platforms.webhook.web",
        SimpleNamespace(json_response=lambda body, status=200: SimpleNamespace(status=status, body=body)))
    target = BoundTelegram()
    broker = GitHubApprovalBroker(tmp_path / "approval.db", instance_id="producer")
    await broker.request(
        proposal(), adapter=target, chat_id="42", question="Approve?",
        session_key="webhook:1", authorized_user_id="7")
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    route = {
        "deliver": "telegram",
        "deliver_extra": {"chat_id": "42"},
        "github_approval": {"authorized_user_id": "7"},
    }
    payload = {
        "action": "closed",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 7, "merged": merged},
    }
    monkeypatch.setattr("gateway.github_approval_broker.default_broker", lambda **kwargs: broker)

    response = await adapter._handle_github_approval_contract(
        route, payload, "pull_request", "route", None, "delivery-close")

    assert response.status == 202
    assert broker.observe(proposal())["state"] == "superseded"
    assert target.retired == [("gha-" + proposal().canonical_key()[:24], f"GitHub item {expected}")]


@pytest.mark.asyncio
async def test_approval_route_rejects_unrecognized_github_contract_before_agent(monkeypatch):
    monkeypatch.setattr(
        "gateway.platforms.webhook.web",
        SimpleNamespace(json_response=lambda body, status=200: SimpleNamespace(status=status, body=body)))
    adapter = object.__new__(WebhookAdapter)
    response = await adapter._handle_github_approval_contract(
        {"github_approval": {"authorized_user_id": "7"}}, {"action": "opened"},
        "pull_request", "route", None, "delivery-1")
    assert response.status == 400


@pytest.mark.asyncio
async def test_dynamic_subscription_v2_endpoint_durably_acks_without_waiting_for_telegram(
        tmp_path, monkeypatch):
    """The public endpoint and hot-reloaded subscription, not a private handler, own admission."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    subscription = {
        "github-hermes-intake": {
            "secret": "INSECURE_NO_AUTH",
            "events": ["issues", "pull_request", "issue_comment"],
            "script": "github-intake.py",
            "deliver": "telegram",
            "deliver_extra": {"chat_id": "42"},
            "github_approval": {
                "schema": "hermes.github-intake.v2",
                "authorized_user_id": "7",
            },
        }
    }
    (tmp_path / "webhook_subscriptions.json").write_text(json.dumps(subscription))
    broker = GitHubApprovalBroker(tmp_path / "approval.db", instance_id="endpoint")
    monkeypatch.setattr("gateway.github_approval_broker.default_broker", lambda **_kwargs: broker)
    transformed = {
        "schema": "hermes.github-intake.v2",
        "item_key": "owner/repo#7",
        "head_sha": "a" * 40,
        "contract_accepted": True,
        "digest": "b" * 64,
        "scope": ["tests"],
        "limits": {"files": 3},
        "risk": "low",
        "binding_status": "pending",
    }
    target = BoundTelegram()
    gate = asyncio.Event()

    async def blocked_send(**kwargs):
        target.sent.append(kwargs)
        await gate.wait()
        return SendResult(success=True, message_id="501")

    target.send_clarify = blocked_send
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    runner.config = SimpleNamespace(multiplex_profiles=False)
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0}))
    adapter.gateway_runner = runner
    adapter._route_processor.run_route_script = MagicMock(return_value=(True, transformed))
    adapter._admit_agent_run = AsyncMock(side_effect=AssertionError("LLM approval producer ran"))
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)

    started = time.monotonic()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/webhooks/github-hermes-intake", json={"action": "opened"},
            headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "delivery-v2"})
        elapsed = time.monotonic() - started
        assert response.status == 202
        assert (await response.json())["status"] == "approval_admitted"

    assert elapsed < 0.5
    assert target.sent == []
    assert broker.observe(proposal())["state"] == "pending_notification"
    adapter._admit_agent_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_v2_approval_card_binds_bounded_contract_details(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gateway.platforms.webhook.web",
        SimpleNamespace(json_response=lambda body, status=200: SimpleNamespace(status=status, body=body)))
    target = BoundTelegram()
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    broker = GitHubApprovalBroker(tmp_path / "approval.db", instance_id="producer")
    monkeypatch.setattr("gateway.github_approval_broker.default_broker", lambda **_kwargs: broker)
    payload = {
        "schema": "hermes.github-intake.v2",
        "item_key": "owner/repo#7",
        "head_sha": "a" * 40,
        "contract_accepted": True,
        "digest": "b" * 64,
        "scope": ["tests", "deploy", "x" * 5000],
        "limits": {"files": 3, "note": "y" * 5000},
        "risk": "high\x00" + "z" * 5000,
        "binding_status": "pending",
    }

    response = await adapter._handle_github_approval_contract(
        {
            "deliver": "telegram",
            "deliver_extra": {"chat_id": "42"},
            "github_approval": {
                "schema": "hermes.github-intake.v2", "authorized_user_id": "7"},
        },
        payload, "issues", "route", None, "delivery-v2")

    assert response.status == 202
    recorded = broker.observe(proposal())
    question = recorded["question"]
    assert question.startswith(
        "Approve GitHub execution?\n"
        "Item: owner/repo#7\n"
        f"Head: {'a' * 40}\n"
        f"Digest: {'b' * 64}\n"
        "Risk: high ")
    assert "\nScope: [\"tests\",\"deploy\",\"" in question
    assert "\nLimits: {\"files\":3,\"note\":\"" in question
    assert "\x00" not in question
    assert len(question) <= 2600


@pytest.mark.asyncio
async def test_dynamic_v2_endpoint_retires_transformed_terminal_item(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "webhook_subscriptions.json").write_text(json.dumps({
        "github-hermes-intake": {
            "secret": "INSECURE_NO_AUTH", "events": ["pull_request"],
            "script": "github-intake.py", "deliver": "telegram",
            "deliver_extra": {"chat_id": "42"},
            "github_approval": {
                "schema": "hermes.github-intake.v2", "authorized_user_id": "7"},
        }}))
    target = BoundTelegram()
    broker = GitHubApprovalBroker(tmp_path / "approval.db", instance_id="endpoint")
    await broker.request(
        proposal(), adapter=target, chat_id="42", question="Approve?",
        session_key="webhook:1", authorized_user_id="7")
    monkeypatch.setattr("gateway.github_approval_broker.default_broker", lambda **_kwargs: broker)
    terminal = {
        "schema": "hermes.github-intake.v2", "item_key": "owner/repo#7",
        "head_sha": "a" * 40, "contract_accepted": False, "digest": "b" * 64,
        "scope": [], "limits": {}, "risk": "none", "binding_status": "closed",
    }
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    runner.config = SimpleNamespace(multiplex_profiles=False)
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1"}))
    adapter.gateway_runner = runner
    adapter._route_processor.run_route_script = MagicMock(return_value=(True, terminal))
    adapter._admit_agent_run = AsyncMock(side_effect=AssertionError("agent ran"))
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/webhooks/github-hermes-intake", json={"action": "closed"},
            headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "close-v2"})
        assert response.status == 202
        assert (await response.json())["status"] == "approval_retired"
    assert broker.observe(proposal())["state"] == "superseded"
    adapter._admit_agent_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_v2_endpoint_rejects_real_script_empty_object_instead_of_raw_payload(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "tools.environments.local.build_subprocess_env", lambda: dict(os.environ))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "empty.py"
    script.write_text("import json\nprint(json.dumps({}))\n")
    (tmp_path / "webhook_subscriptions.json").write_text(json.dumps({
        "github-hermes-intake": {
            "secret": "INSECURE_NO_AUTH", "events": ["issues"],
            "script": "empty.py", "deliver": "telegram",
            "deliver_extra": {"chat_id": "42"},
            "github_approval": {
                "schema": "hermes.github-intake.v2", "authorized_user_id": "7"},
        }}))
    target = BoundTelegram()
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    runner.config = SimpleNamespace(multiplex_profiles=False)
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1"}))
    adapter.gateway_runner = runner
    adapter._admit_agent_run = AsyncMock(side_effect=AssertionError("agent ran"))
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    raw_authority_envelope = {
        "schema": "hermes.github-intake.v2", "item_key": "owner/repo#7",
        "head_sha": "a" * 40, "contract_accepted": True, "digest": "b" * 64,
        "scope": ["tests"], "limits": {"files": 3}, "risk": "low",
        "binding_status": "pending",
    }

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/webhooks/github-hermes-intake", json=raw_authority_envelope,
            headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "empty-v2"})

    assert response.status == 400
    assert target.sent == []
    adapter._admit_agent_run.assert_not_awaited()


def test_dynamic_github_approval_requires_exact_explicit_schema_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    base = {
        "secret": "INSECURE_NO_AUTH", "deliver": "telegram",
        "github_approval": {"authorized_user_id": "7"},
    }
    (tmp_path / "webhook_subscriptions.json").write_text(json.dumps({
        "missing": base,
        "foreign": {**base, "github_approval": {
            "authorized_user_id": "7", "schema": "hermes.github-intake.v1"}},
        "valid": {**base, "github_approval": {
            "authorized_user_id": "7", "schema": "hermes.github-intake.v2"}},
    }))
    adapter = WebhookAdapter(PlatformConfig(
        enabled=True, extra={"host": "127.0.0.1", "secret": "fallback"}))
    adapter._reload_dynamic_routes()
    assert set(adapter._dynamic_routes) == {"valid"}


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [None, "superseded", "closed"])
async def test_approval_lifecycle_and_restart_never_invoke_product_executor(
        tmp_path, monkeypatch, terminal):
    path = tmp_path / "approval.db"
    broker = GitHubApprovalBroker(path, instance_id="startup")
    target = BoundTelegram()
    broker.admit(
        proposal(), adapter=target, chat_id="42", question="Approve?",
        session_key="webhook:1", authorized_user_id="7")
    runner = MagicMock()
    runner._authorization_adapter.return_value = target
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={}))
    adapter.gateway_runner = runner
    adapter._routes = {"approval": {
        "deliver": "telegram",
        "github_approval": {
            "schema": "hermes.github-intake.v2", "authorized_user_id": "7"},
    }}
    monkeypatch.setattr("gateway.github_approval_broker.default_broker", lambda **_kwargs: broker)

    await asyncio.gather(
        adapter._github_approval_worker_once(),
        adapter._github_approval_worker_once(),
    )
    assert len(target.sent) == 1
    active = broker.observe(proposal())
    assert active is not None
    assert active["state"] == "active"

    executor = ForbiddenExecutor()
    runner.github_approval_runtime = SimpleNamespace(executor=executor)
    broker.resolve_callback(
        active["clarify_id"], 0, approver="telegram:7", user_id="7", chat_id="42",
        thread_id=None, message_id="501", destination_id="telegram-bot:501")
    if terminal == "superseded":
        await broker.request(
            proposal(head="c" * 40), adapter=target, chat_id="42", question="Approve?",
            session_key="webhook:2", authorized_user_id="7")
    elif terminal == "closed":
        await broker.retire_item(proposal().item_key, status="closed", adapter=target)

    restarted = GitHubApprovalBroker(path, instance_id="restart")
    monkeypatch.setattr("gateway.github_approval_broker.default_broker", lambda **_kwargs: restarted)
    runner._authorization_adapter.return_value = BoundTelegram()
    await asyncio.gather(
        adapter._github_approval_worker_once(),
        adapter._github_approval_worker_once(),
    )

    assert executor.calls == []
    recorded = restarted.observe(proposal())
    assert recorded is not None
    assert recorded["response"] == "Approve"
