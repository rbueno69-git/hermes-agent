"""Gateway dispatcher ownership survives overlapping gateway restarts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from gateway.config import Platform
from gateway.run import GatewayRunner


def test_contended_dispatcher_boot_retries_until_lock_is_available(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: object()}
    runner._kanban_dispatcher_lock_handle = None
    boots = []

    fake_kb = SimpleNamespace(DEFAULT_FAILURE_LIMIT=2)

    def boot():
        boots.append(len(boots))
        if len(boots) == 1:
            runner._kanban_dispatcher_lock_contended = True
            return None
        runner._kanban_dispatcher_lock_contended = False
        return (lambda: {"kanban": {}}, fake_kb, {"dispatch_interval_seconds": 1})

    class FakeDispatcher:
        def __init__(self, kb, settings):
            pass

        def tick_once(self):
            runner._running = False
            return []

        def ready_nonempty(self):
            return False

    async def immediate_sleep(_delay):
        return None

    async def direct(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(runner, "_kanban_dispatcher_boot", boot)
    monkeypatch.setattr(runner, "_sleep_between_ticks", immediate_sleep)
    monkeypatch.setattr("gateway.kanban_watchers.asyncio.sleep", immediate_sleep)
    monkeypatch.setattr("gateway.kanban_watchers._to_thread_process_service", direct)
    monkeypatch.setattr("gateway.kanban_watchers._resolve_dispatcher_settings", lambda cfg, kb: SimpleNamespace(
        interval=1, max_spawn=None, max_in_progress=1, failure_limit=2,
        stale_timeout_seconds=0, reconcile_orphans=True,
        default_assignee=None, max_in_progress_per_profile=None,
    ))
    monkeypatch.setattr("gateway.kanban_watchers._KanbanDispatcher", FakeDispatcher)
    monkeypatch.setattr("gateway.kanban_watchers._kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr("gateway.kanban_watchers._resolve_auto_decompose_settings", lambda load: (False, 0))
    monkeypatch.setattr("hermes_cli.kanban_db_dispatch.reap_worker_zombies", list)

    asyncio.run(runner._kanban_dispatcher_watcher())

    assert len(boots) == 2