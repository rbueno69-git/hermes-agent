#!/usr/bin/env python3
"""Project GitHub decisions and recover missed approval events without an agent."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Iterable

from gateway.github_approval_broker import GitHubApprovalBroker
from gateway.github_approval_reconciler import ApprovalDecisionReconciler, IntakeScriptLiveReader
from hermes_cli.kanban_db import kanban_db_path
from hermes_cli.webhook import _load_subscriptions, _route_url


HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
BROKER = HOME / "state" / "github-approval-broker.db"
LEDGER = HOME / "github-intake" / "approvals.jsonl"
CHANGES = HOME / "github-intake" / "changes.jsonl"
SNAPSHOT = HOME / "github-intake" / "snapshot.json"
CURSOR = HOME / "github-intake" / "approval-maintenance-cursor.json"
INTAKE = HOME / "scripts" / "github-hermes-intake.py"
LOCK = HOME / "state" / "github-approval-maintenance.lock"
SUBSCRIPTION = "github-hermes-intake"
_CANDIDATE_OBSERVATIONS = frozenset({
    "unassigned_approval_candidate",
    "proposed_hermes_candidate",
})
_ACTIONABLE_STATES = (
    "pending_notification", "dispatching", "delivery_unknown", "active", "answered",
)


def _github(endpoint: str) -> dict:
    import subprocess

    result = subprocess.run(
        ["gh", "api", endpoint], capture_output=True, text=True,
        encoding="utf-8", errors="strict", timeout=45, check=False,
    )
    if result.returncode != 0 or len(result.stdout) > 1_048_576:
        raise RuntimeError("GitHub metadata read failed")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("GitHub metadata has the wrong shape")
    return value


def live_payload(item_key: str) -> dict:
    repo, raw_number = item_key.rsplit("#", 1)
    number = int(raw_number)
    issue = _github(f"repos/{repo}/issues/{number}")
    action = "edited" if issue.get("state") == "open" else "closed"
    common = {
        "action": action,
        "repository": {"full_name": repo},
        "sender": {"login": "github-approval-watchdog"},
    }
    if isinstance(issue.get("pull_request"), dict):
        return {**common, "pull_request": _github(f"repos/{repo}/pulls/{number}")}
    return {**common, "issue": issue}


def validated_envelope(payload: dict) -> dict | None:
    import subprocess

    result = subprocess.run(
        [os.fspath(INTAKE)], input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        capture_output=True, text=True, encoding="utf-8", errors="strict",
        timeout=90, check=False,
    )
    if result.returncode not in (0, 2) or len(result.stdout) > 1_048_576:
        raise RuntimeError("pinned GitHub intake validator failed")
    if not result.stdout.strip():
        return None
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("pinned GitHub intake validator returned the wrong shape")
    return value


def actionable_items() -> set[str]:
    if not BROKER.exists():
        return set()
    placeholders = ",".join("?" for _ in _ACTIONABLE_STATES)
    with sqlite3.connect(f"file:{BROKER}?mode=ro", uri=True) as db:
        rows = db.execute(
            f"SELECT DISTINCT item_key FROM github_approval_proposals WHERE state IN ({placeholders})",
            _ACTIONABLE_STATES,
        ).fetchall()
    return {str(row[0]) for row in rows}


def has_current_tuple(item_key: str, envelope: dict) -> bool:
    if not BROKER.exists():
        return False
    head, digest = envelope.get("head_sha"), envelope.get("digest")
    with sqlite3.connect(f"file:{BROKER}?mode=ro", uri=True) as db:
        row = db.execute(
            """SELECT 1 FROM github_approval_proposals
               WHERE item_key=? AND head_sha=? AND contract_digest_sha256=? LIMIT 1""",
            (item_key, head, digest),
        ).fetchone()
    return row is not None


def snapshot_candidates(path: Path | None = None) -> set[str]:
    path = SNAPSHOT if path is None else path
    if not path.exists():
        return set()
    value = json.loads(path.read_text(encoding="utf-8"))
    items = value.get("items") if isinstance(value, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("invalid GitHub intake snapshot")
    candidates: set[str] = set()
    for row in items:
        if not isinstance(row, dict):
            raise RuntimeError("invalid GitHub intake snapshot item")
        item_key = row.get("id")
        if row.get("observation") in _CANDIDATE_OBSERVATIONS and isinstance(item_key, str):
            candidates.add(item_key)
    return candidates


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def _read_cursor() -> int:
    if not CURSOR.exists():
        return max(0, _line_count(CHANGES) - 200)
    value = json.loads(CURSOR.read_text(encoding="utf-8"))
    count = value.get("changes_lines") if isinstance(value, dict) else None
    if type(count) is not int or count < 0:
        raise RuntimeError("invalid GitHub approval maintenance cursor")
    return count


def _changed_items(start: int) -> tuple[set[str], int]:
    if not CHANGES.exists():
        return set(), 0
    items: set[str] = set()
    total = 0
    with CHANGES.open(encoding="utf-8") as handle:
        for total, line in enumerate(handle, 1):
            if total <= start:
                continue
            value = json.loads(line)
            item_key = value.get("id") if isinstance(value, dict) else None
            if isinstance(item_key, str):
                items.add(item_key)
    return items, total


def _write_cursor(count: int) -> None:
    CURSOR.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".approval-maintenance-", dir=CURSOR.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"changes_lines": count}, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, CURSOR)
        directory = os.open(CURSOR.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _event_name(payload: dict) -> str:
    if isinstance(payload.get("pull_request"), dict):
        return "pull_request"
    if isinstance(payload.get("comment"), dict):
        return "issue_comment"
    if isinstance(payload.get("issue"), dict):
        return "issues"
    raise RuntimeError("unsupported GitHub watchdog payload")


def signed_local_request(*, payload: dict, url: str, secret: str) -> urllib.request.Request:
    if not url.startswith(("http://127.0.0.1:", "http://[::1]:", "http://localhost:")):
        raise RuntimeError("GitHub approval watchdog route must be loopback")
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    delivery = hashlib.sha256(b"hermes-github-approval-watchdog-v1\0" + body).hexdigest()
    return urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": _event_name(payload),
            "X-GitHub-Delivery": delivery,
        },
    )


def post_signed_local(payload: dict) -> None:
    subscriptions = _load_subscriptions()
    route = subscriptions.get(SUBSCRIPTION)
    if not isinstance(route, dict):
        raise RuntimeError("GitHub Hermes intake subscription is unavailable")
    secret = route.get("secret")
    if not isinstance(secret, str) or not secret:
        raise RuntimeError("GitHub Hermes intake subscription secret is unavailable")
    request = signed_local_request(payload=payload, url=_route_url(SUBSCRIPTION, route), secret=secret)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(1_048_577)
        if response.status < 200 or response.status >= 300 or len(body) > 1_048_576:
            raise RuntimeError("signed local GitHub intake rejected the watchdog event")


def project_pending_decisions() -> list[dict]:
    reconciler = ApprovalDecisionReconciler(
        broker_path=BROKER,
        ledger_path=LEDGER,
        kanban_path=kanban_db_path(),
        live_reader=IntakeScriptLiveReader(intake_script=INTAKE),
        assignee="default",
        lock_path=Path(f"{LEDGER}.lock"),
    )
    return [result.__dict__ for result in reconciler.reconcile(limit=32)]


def _candidate_items() -> tuple[set[str], int, int]:
    start = _read_cursor()
    changed, total = _changed_items(start)
    # The durable changes cursor is the missed-event authority. Replaying every
    # snapshot candidate on each tick would regenerate issue approvals whenever
    # an unrelated default-branch commit changes the issue's synthetic head.
    return changed | actionable_items(), start, total


def run_once(*, dry_run: bool) -> dict:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        projected = [] if dry_run else project_pending_decisions()
        candidates, start, total = _candidate_items()
        actionable = actionable_items()
        approval_candidates = snapshot_candidates()
        emitted: list[str] = []
        rejected: list[str] = []
        for item_key in sorted(candidates):
            payload = live_payload(item_key)
            terminal = payload.get("action") == "closed"
            if terminal:
                if item_key not in actionable:
                    continue
            else:
                envelope = validated_envelope(payload)
                if envelope is None:
                    if item_key in approval_candidates:
                        rejected.append(item_key)
                    continue
                if envelope.get("contract_accepted") is not True:
                    continue
                if has_current_tuple(item_key, envelope):
                    continue
            if not dry_run:
                post_signed_local(payload)
            emitted.append(item_key)
        if rejected:
            raise RuntimeError(
                "candidate item rejected by Hermes intake: " + ", ".join(rejected)
            )
        if not dry_run:
            _write_cursor(total)
        return {
            "cursor_from": start,
            "cursor_to": total,
            "candidates": sorted(candidates),
            "emitted": emitted,
            "projected": projected,
        }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument(
        "--bind-internal-continuation", nargs=3,
        metavar=("PREDECESSOR_PROPOSAL_KEY", "PREDECESSOR_TASK_ID", "CONTINUATION_TASK_ID"),
    )
    args = parser.parse_args(argv)
    if args.bind_internal_continuation is not None:
        predecessor_proposal_key, predecessor_task_id, continuation_task_id = (
            args.bind_internal_continuation
        )
        broker = GitHubApprovalBroker(
            BROKER, instance_id="github-approval-maintenance:continuation-binding")
        broker.bind_internal_continuation(
            predecessor_proposal_key=predecessor_proposal_key,
            predecessor_task_id=predecessor_task_id,
            continuation_task_id=continuation_task_id,
            kanban_path=kanban_db_path(),
        )
        print(json.dumps({
            "action": "internal_continuation_bound",
            "predecessor_proposal_key": predecessor_proposal_key,
            "predecessor_task_id": predecessor_task_id,
            "continuation_task_id": continuation_task_id,
        }, sort_keys=True, separators=(",", ":")))
        return 0
    result = run_once(dry_run=args.dry_run)
    if args.dry_run or result["emitted"] or result["projected"]:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
