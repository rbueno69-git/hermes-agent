"""Project durable Telegram GitHub decisions into the canonical ledger and Kanban."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional


@dataclass(frozen=True)
class LiveContract:
    item_key: str
    state: str
    head_sha: str
    contract_digest_sha256: str
    accepted: bool
    title: str = ""


_ITEM_RE = re.compile(r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)\Z")
_SHA40_RE = re.compile(r"[0-9a-f]{40}\Z")
_SHA64_RE = re.compile(r"[0-9a-f]{64}\Z")


class ContractRejected(RuntimeError):
    """The pinned validator rejected the current contract as non-executable."""


class IntakeScriptLiveReader:
    """Re-read GitHub metadata and pass the live item through the pinned intake validator."""

    def __init__(
        self,
        *,
        github_reader: Optional[Callable[[str], object]] = None,
        intake_runner: Optional[Callable[[dict], dict]] = None,
        intake_script: Optional[Path] = None,
        continuation_path: Optional[Path] = None,
    ):
        self.github_reader = github_reader or self._github_api
        self.intake_script = Path(intake_script) if intake_script else None
        self.continuation_path = Path(continuation_path) if continuation_path else None
        self.intake_runner = intake_runner or self._run_intake

    @staticmethod
    def _github_api(endpoint: str) -> object:
        result = subprocess.run(
            ["gh", "api", endpoint], capture_output=True, text=True,
            encoding="utf-8", errors="strict", timeout=30, check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 1_048_576:
            raise RuntimeError("GitHub live metadata read failed")
        value = json.loads(result.stdout)
        if not isinstance(value, (dict, list)):
            raise RuntimeError("GitHub live metadata has the wrong shape")
        return value

    @staticmethod
    def _from_envelope(item_key: str, title: str, envelope: dict) -> LiveContract:
        required = {
            "schema", "item_key", "head_sha", "contract_accepted", "digest",
            "scope", "limits", "risk", "binding_status",
        }
        if set(envelope) != required or envelope.get("schema") != "hermes.github-intake.v2":
            raise RuntimeError("pinned GitHub intake validator returned an unexpected envelope")
        if (not isinstance(envelope.get("scope"), list)
                or any(not isinstance(value, str) for value in envelope["scope"])
                or not isinstance(envelope.get("limits"), dict)
                or any(not isinstance(key, str) or not isinstance(value, str)
                       for key, value in envelope["limits"].items())
                or not isinstance(envelope.get("risk"), str)):
            raise RuntimeError("pinned GitHub intake validator returned malformed authority fields")
        head_sha, digest = envelope.get("head_sha"), envelope.get("digest")
        accepted = envelope.get("contract_accepted") is True and envelope.get("binding_status") == "pending"
        if (envelope.get("item_key") != item_key or not isinstance(head_sha, str)
                or not _SHA40_RE.fullmatch(head_sha) or not isinstance(digest, str)
                or not _SHA64_RE.fullmatch(digest)):
            raise RuntimeError("pinned GitHub intake validator returned a mismatched tuple")
        return LiveContract(item_key, "open", head_sha, digest, accepted, title)

    def _run_intake(self, payload: dict) -> dict:
        script = self.intake_script
        if script is None:
            from hermes_constants import get_process_hermes_home

            script = get_process_hermes_home() / "scripts" / "github-hermes-intake.py"
        if not script.is_file():
            raise RuntimeError("pinned GitHub intake validator is unavailable")
        result = subprocess.run(
            [os.fspath(script)], input=json.dumps(payload), capture_output=True, text=True,
            encoding="utf-8", errors="strict", timeout=60, check=False,
        )
        if result.returncode == 2 or (result.returncode == 0 and not result.stdout):
            raise ContractRejected("pinned GitHub intake validator rejected the live contract")
        if result.returncode != 0 or len(result.stdout) > 1_048_576:
            raise RuntimeError("pinned GitHub intake validator rejected the live item")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise RuntimeError("pinned GitHub intake validator returned the wrong shape")
        return value

    def __call__(self, item_key: str) -> LiveContract:
        match = _ITEM_RE.fullmatch(item_key)
        if match is None:
            raise ValueError("item_key must be owner/repo#number")
        repo, number = match.group("repo"), int(match.group("number"))
        issue = self.github_reader(f"repos/{repo}/issues/{number}")
        if not isinstance(issue, dict):
            raise RuntimeError("GitHub live issue metadata has the wrong shape")
        if issue.get("number") not in (None, number):
            raise RuntimeError("GitHub live item identity changed")
        state = issue.get("state")
        raw_title = issue.get("title")
        title: str = raw_title if isinstance(raw_title, str) else ""
        if state != "open":
            return LiveContract(item_key, str(state or "unknown"), "", "", False, title)
        common = {
            "action": "edited", "repository": {"full_name": repo},
            "sender": {"login": "github-approval-reconciler"},
        }
        head_sha = ""
        if isinstance(issue.get("pull_request"), dict):
            pull = self.github_reader(f"repos/{repo}/pulls/{number}")
            if not isinstance(pull, dict):
                raise RuntimeError("GitHub live PR metadata has the wrong shape")
            raw_head = pull.get("head")
            head = raw_head if isinstance(raw_head, dict) else {}
            head_sha = head.get("sha")
            if pull.get("state") != "open" or not isinstance(head_sha, str) or not _SHA40_RE.fullmatch(head_sha):
                return LiveContract(item_key, str(pull.get("state") or "unknown"), "", "", False, title)
            payload = {
                **common,
                "pull_request": {
                    "number": number, "state": "open", "title": title,
                    "html_url": issue.get("html_url", ""), "user": issue.get("user", {}),
                    "labels": issue.get("labels", []), "head": {"sha": head_sha},
                },
            }
        else:
            payload = {
                **common,
                "issue": {
                    "number": number, "state": "open", "title": title,
                    "html_url": issue.get("html_url", ""), "user": issue.get("user", {}),
                    "labels": issue.get("labels", []),
                },
            }
        try:
            envelope = self.intake_runner(payload)
        except ContractRejected:
            return LiveContract(item_key, "open", head_sha, "", False, title)
        return self._from_envelope(item_key, title, envelope)

    def for_digest(self, item_key: str, expected_digest: str) -> LiveContract:
        """Revalidate the exact approved contract, including comment-hosted contracts."""
        if not _SHA64_RE.fullmatch(expected_digest):
            raise ValueError("expected_digest must be sha256")
        current = self(item_key)
        if current.accepted and current.contract_digest_sha256 == expected_digest:
            return current
        match = _ITEM_RE.fullmatch(item_key)
        if match is None or current.state != "open":
            return current
        repo, number = match.group("repo"), int(match.group("number"))
        issue = self.github_reader(f"repos/{repo}/issues/{number}")
        comments = self.github_reader(f"repos/{repo}/issues/{number}/comments?per_page=100")
        if not isinstance(issue, dict) or not isinstance(comments, list):
            raise RuntimeError("GitHub comment metadata has the wrong shape")
        common_issue = {
            "number": number, "state": "open", "title": current.title,
            "html_url": issue.get("html_url", ""), "user": issue.get("user", {}),
            "labels": issue.get("labels", []),
        }
        if isinstance(issue.get("pull_request"), dict):
            common_issue["pull_request"] = issue["pull_request"]
        for comment in reversed(comments):
            if not isinstance(comment, dict) or type(comment.get("id")) is not int:
                continue
            payload = {
                "action": "edited", "repository": {"full_name": repo},
                "sender": {"login": "github-approval-reconciler"},
                "issue": common_issue,
                "comment": {
                    "id": comment["id"], "body": comment.get("body", ""),
                    "html_url": comment.get("html_url", ""), "user": comment.get("user", {}),
                },
            }
            try:
                candidate = self._from_envelope(
                    item_key, current.title, self.intake_runner(payload)
                )
            except ContractRejected:
                continue
            if candidate.accepted and candidate.contract_digest_sha256 == expected_digest:
                return candidate
        if self.continuation_path is not None:
            from gateway.github_intake_continuation import (
                IntakeObservation, MissingMaterial, build_clarification_contract,
                build_merge_only_contract,
            )

            with sqlite3.connect(self.continuation_path) as db:
                db.row_factory = sqlite3.Row
                row = db.execute(
                    """SELECT item_key,head_sha,reason_code,title,state,detail
                       FROM github_intake_observations
                       WHERE item_key=? AND head_sha=?
                         AND state IN ('approval_proposed','clarification_requested')""",
                    (item_key, current.head_sha),
                ).fetchone()
            if row is not None and str(row["detail"]) == expected_digest:
                observation = IntakeObservation(
                    item_key=str(row["item_key"]), head_sha=str(row["head_sha"]),
                    reason_code=str(row["reason_code"]), title=str(row["title"]),
                )
                if str(row["state"]) == "clarification_requested":
                    proposal = build_clarification_contract(observation, "")
                else:
                    try:
                        proposal = build_merge_only_contract(
                            observation, "", github_reader=self.github_reader)
                    except MissingMaterial:
                        return current
                if proposal["contract_digest_sha256"] == expected_digest:
                    return LiveContract(
                        item_key, "open", current.head_sha, expected_digest, True, current.title)
        return current


class ApprovalWatchdog:
    """Recover missed accepted contracts and retire terminal cards through signed local events."""

    _ACTIONABLE = ("pending_notification", "dispatching", "delivery_unknown", "active", "answered")

    def __init__(
        self,
        *,
        broker_path: Path,
        candidate_items: Callable[[], Iterable[str]],
        live_reader: Callable[[str], LiveContract],
        event_sender: Callable[[str, LiveContract, str], None],
    ):
        self.broker_path = Path(broker_path)
        self.candidate_items = candidate_items
        self.live_reader = live_reader
        self.event_sender = event_sender

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(f"file:{self.broker_path}?mode=ro", uri=True, timeout=30.0)
        db.row_factory = sqlite3.Row
        return db

    def _actionable_items(self) -> set[str]:
        if not self.broker_path.exists():
            return set()
        placeholders = ",".join("?" for _ in self._ACTIONABLE)
        with self._connect() as db:
            rows = db.execute(
                f"SELECT DISTINCT item_key FROM github_approval_proposals WHERE state IN ({placeholders})",
                self._ACTIONABLE,
            ).fetchall()
        return {str(row["item_key"]) for row in rows}

    def _has_tuple(self, live: LiveContract) -> bool:
        if not self.broker_path.exists():
            return False
        from gateway.github_approval_broker import ApprovalProposal

        key = ApprovalProposal(
            live.item_key, live.head_sha, live.contract_digest_sha256, "hermes"
        ).canonical_key()
        with self._connect() as db:
            return db.execute(
                "SELECT 1 FROM github_approval_proposals WHERE proposal_key=?", (key,)
            ).fetchone() is not None

    def run(self) -> list[dict[str, str]]:
        actionable = self._actionable_items()
        items = set(str(item) for item in self.candidate_items()) | actionable
        emitted: list[dict[str, str]] = []
        for item_key in sorted(items):
            live = self.live_reader(item_key)
            if live.state == "open" and live.accepted:
                if self._has_tuple(live):
                    continue
                action = "recover"
            elif item_key in actionable and live.state != "open":
                action = "retire"
            else:
                continue
            self.event_sender(item_key, live, action)
            emitted.append({"item_key": item_key, "action": action})
        return emitted


@dataclass(frozen=True)
class ProjectionResult:
    decision_id: int
    outcome: str
    reason: Optional[str] = None
    task_id: Optional[str] = None


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            with contextlib.suppress(OSError):
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ApprovalDecisionReconciler:
    """Single-writer projector; product effects remain exclusively in the created task."""

    def __init__(
        self,
        *,
        broker_path: Path,
        ledger_path: Path,
        kanban_path: Path,
        live_reader: Callable[[str], LiveContract],
        assignee: str = "default",
        lock_path: Optional[Path] = None,
    ):
        self.broker_path = Path(broker_path)
        self.ledger_path = Path(ledger_path)
        self.kanban_path = Path(kanban_path)
        self.live_reader = live_reader
        self.assignee = assignee
        self.lock_path = Path(lock_path) if lock_path else Path(f"{self.ledger_path}.lock")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.broker_path, timeout=30.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS github_approval_reconciliations (
                    decision_id INTEGER PRIMARY KEY REFERENCES github_approval_decisions(decision_id),
                    proposal_key TEXT NOT NULL UNIQUE,
                    outcome TEXT NOT NULL CHECK(outcome IN ('approved','rejected','stale')),
                    reason TEXT,
                    approval_request_id TEXT,
                    task_id TEXT,
                    completed_at REAL NOT NULL
                )"""
            )

    def _pending(self, limit: int) -> list[sqlite3.Row]:
        with self._connect() as db:
            return db.execute(
                """SELECT d.decision_id,d.proposal_key,d.response,d.approver,d.state,d.decided_at,
                          p.item_key,p.head_sha,p.contract_digest_sha256,p.executor,p.question,
                          p.message_id,p.chat_id,p.authorized_user_id,p.state AS proposal_state,
                          i.predecessor_proposal_key,i.task_id AS inherited_task_id
                   FROM github_approval_decisions d
                   JOIN github_approval_proposals p USING(proposal_key)
                   LEFT JOIN github_approval_reconciliations r USING(decision_id)
                   LEFT JOIN github_approval_inheritances i USING(proposal_key)
                   WHERE d.state='pending' AND r.decision_id IS NULL
                   ORDER BY d.decision_id LIMIT ?""",
                (max(1, int(limit)),),
            ).fetchall()

    @staticmethod
    def _request_id(row: sqlite3.Row) -> str:
        return (
            f"hermes:github:{row['item_key']}:{str(row['head_sha'])[:12]}:"
            f"{str(row['contract_digest_sha256'])[:16]}"
        )

    @staticmethod
    def _idempotency_key(row: sqlite3.Row) -> str:
        canonical = json.dumps(
            {
                "contract_digest_sha256": row["contract_digest_sha256"],
                "executor": row["executor"],
                "head_sha": row["head_sha"],
                "item_key": row["item_key"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return f"github-approval:{hashlib.sha256(canonical).hexdigest()}"

    def _iter_ledger(self) -> Iterator[dict]:
        if not self.ledger_path.exists():
            return
        raw = self.ledger_path.read_text(encoding="utf-8-sig")
        decoder = json.JSONDecoder()
        offset = 0
        while offset < len(raw):
            while offset < len(raw) and raw[offset].isspace():
                offset += 1
            if offset >= len(raw):
                break
            try:
                value, offset = decoder.raw_decode(raw, offset)
            except json.JSONDecodeError as exc:
                raise RuntimeError("canonical approval ledger contains malformed JSON") from exc
            if not isinstance(value, dict):
                raise RuntimeError("canonical approval ledger contains a non-object entry")
            yield value

    def _ledger_has(self, request_id: str) -> bool:
        return any(
            value.get("approval_request_id") == request_id
            for value in self._iter_ledger()
        )

    def _ledger_entry(self, request_id: str) -> Optional[dict]:
        matches = [
            value for value in self._iter_ledger()
            if value.get("approval_request_id") == request_id
        ]
        if not matches:
            return None
        if any(value != matches[0] for value in matches[1:]):
            raise RuntimeError("canonical approval ledger contains conflicting request entries")
        return matches[0]

    def _append_ledger(self, row: sqlite3.Row, request_id: str) -> None:
        if self._ledger_has(request_id):
            return
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        created = not self.ledger_path.exists()
        record = {
            "schema": "hermes.github-approval.v1",
            "approval_request_id": request_id,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "status": ("approved_terminal_successor_contract"
                       if row["predecessor_proposal_key"] is not None
                       else "approved_terminal"),
            "approver": row["approver"],
            "platform": "telegram",
            "work_item": row["item_key"],
            "head_sha": row["head_sha"],
            "contract_digest_sha256": row["contract_digest_sha256"],
            "executor": "Hermes",
            "decision_id": row["decision_id"],
            "proposal_key": row["proposal_key"],
            "decision_message_id": row["message_id"],
            "decision_chat_id": row["chat_id"],
            "scope": row["question"],
            "decision": row["response"],
        }
        if row["predecessor_proposal_key"] is not None:
            record["predecessor_proposal_key"] = row["predecessor_proposal_key"]
            record["inherited_task_id"] = row["inherited_task_id"]
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if created:
            _fsync_directory(self.ledger_path.parent)

    def _create_task(self, row: sqlite3.Row, ledger_entry: Optional[dict] = None) -> str:
        from hermes_cli.kanban_db import create_task
        from hermes_cli.kanban_db_connect import connect

        body = "\n".join(
            (
                "# Approved GitHub execution",
                "",
                f"Item: `{row['item_key']}`",
                f"Approved head: `{row['head_sha']}`",
                f"Contract digest: `{row['contract_digest_sha256']}`",
                f"Proposal key: `{row['proposal_key']}`",
                f"Telegram message: `{row['message_id']}`",
                "",
                "## Approved bounded proposal",
                str(row["question"]),
                "",
                "Revalidate the exact live head/digest before every effect. Execute only effects "
                "explicitly authorized by the approved proposal; run local tests and review as applicable. "
                "GitHub compute is prohibited. Completion contract is local-only.",
            )
        )
        conn = connect(self.kanban_path)
        try:
            if (ledger_entry is not None
                    and row["inherited_task_id"] is None
                    and ledger_entry.get("inherited_from_approval_request_id") is not None):
                expected = {
                    "work_item": str(row["item_key"]),
                    "head_sha": str(row["head_sha"]),
                    "contract_digest_sha256": str(row["contract_digest_sha256"]),
                }
                predecessor_request = str(ledger_entry.get("inherited_from_approval_request_id", ""))
                original_decision = ledger_entry.get("original_decision_id")
                if (ledger_entry.get("schema") != "hermes.github-approval.v1"
                        or ledger_entry.get("status") != "approved_terminal_successor_contract"
                        or any(str(ledger_entry.get(key, "")) != value
                               for key, value in expected.items())
                        or str(ledger_entry.get("executor", "")).lower() != str(row["executor"]).lower()
                        or not predecessor_request.startswith(
                            f"hermes:github:{row['item_key']}:"
                        )
                        or not isinstance(original_decision, int) or original_decision <= 0
                        or not str(ledger_entry.get("reconciliation_reason", "")).strip()):
                    raise RuntimeError("canonical approval ledger binding conflicts with pending decision")
                bound_task_id = ledger_entry.get("inherited_task_id")
                if bound_task_id is not None:
                    bound = conn.execute(
                        "SELECT id,body,idempotency_key,completion_contract FROM tasks "
                        "WHERE id=? AND status IN ('ready','in_progress','running')",
                        (str(bound_task_id),),
                    ).fetchone()
                    if bound is not None:
                        body = str(bound["body"] or "")
                        idempotency_key = str(bound["idempotency_key"] or "")
                        if (str(row["item_key"]) not in body
                                or str(row["contract_digest_sha256"]) not in body
                                or not idempotency_key.startswith("github-")
                                or str(row["item_key"]) not in idempotency_key
                                or str(row["contract_digest_sha256"]) not in idempotency_key
                                or str(bound["completion_contract"]) != "local-only"):
                            raise RuntimeError(
                                "canonical approval ledger task conflicts with successor binding"
                            )
                        return str(bound["id"])
            inherited_task_id = row["inherited_task_id"]
            if inherited_task_id is not None:
                inherited = conn.execute(
                    "SELECT id FROM tasks WHERE id=? AND status IN ('ready','in_progress','running')",
                    (str(inherited_task_id),),
                ).fetchone()
                if inherited is not None:
                    return str(inherited["id"])
            idempotency_key = self._idempotency_key(row)
            existing = conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key=? AND status!='archived' "
                "ORDER BY created_at LIMIT 2",
                (idempotency_key,),
            ).fetchall()
            if len(existing) > 1:
                raise RuntimeError("duplicate Kanban idempotency key")
            if existing:
                return str(existing[0]["id"])
            return create_task(
                conn,
                title=f"Execute approved GitHub contract — {row['item_key']}",
                body=body,
                assignee=self.assignee,
                created_by="github-approval-reconciler",
                workspace_kind="scratch",
                priority=100,
                idempotency_key=self._idempotency_key(row),
                completion_contract="local-only",
                skills=("systematic-debugging", "test-driven-development", "github-pr-workflow"),
            )
        finally:
            conn.close()

    def _finish(
        self,
        row: sqlite3.Row,
        outcome: str,
        *,
        reason: Optional[str] = None,
        request_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> ProjectionResult:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT OR IGNORE INTO github_approval_reconciliations(
                    decision_id,proposal_key,outcome,reason,approval_request_id,task_id,completed_at)
                    VALUES(?,?,?,?,?,?,?)""",
                (row["decision_id"], row["proposal_key"], outcome, reason, request_id, task_id, time.time()),
            )
            if outcome == "stale":
                db.execute(
                    "UPDATE github_approval_decisions SET state='stale' WHERE decision_id=? AND state='pending'",
                    (row["decision_id"],),
                )
                db.execute(
                    """UPDATE github_approval_proposals SET state='superseded',callback_bound=0,
                       retirement_pending=CASE WHEN message_id IS NOT NULL THEN 1 ELSE 0 END,updated_at=?
                       WHERE proposal_key=? AND state='answered'""",
                    (time.time(), row["proposal_key"]),
                )
            db.commit()
        return ProjectionResult(int(row["decision_id"]), outcome, reason, task_id)

    @staticmethod
    def _stale_reason(row: sqlite3.Row, live: LiveContract) -> Optional[str]:
        if live.state != "open" or not live.accepted:
            return "terminal_item"
        if live.item_key != row["item_key"]:
            return "item_drift"
        if live.head_sha != row["head_sha"]:
            return "head_drift"
        if live.contract_digest_sha256 != row["contract_digest_sha256"]:
            return "contract_drift"
        return None

    def reconcile(self, *, limit: int = 16, stop_after: Optional[str] = None) -> list[ProjectionResult]:
        if stop_after not in {None, "ledger"}:
            raise ValueError("stop_after must be None or ledger")
        results: list[ProjectionResult] = []
        with _exclusive_lock(self.lock_path):
            for row in self._pending(limit):
                if row["response"] == "Do not approve":
                    results.append(self._finish(row, "rejected", reason="owner_rejected"))
                    continue
                if row["response"] != "Approve":
                    results.append(self._finish(row, "stale", reason="unknown_decision"))
                    continue
                digest_reader = getattr(self.live_reader, "for_digest", None)
                if callable(digest_reader):
                    live = digest_reader(
                        str(row["item_key"]), str(row["contract_digest_sha256"])
                    )
                else:
                    live = self.live_reader(str(row["item_key"]))
                if not isinstance(live, LiveContract):
                    raise RuntimeError("live approval reader returned the wrong type")
                reason = self._stale_reason(row, live)
                if reason is not None:
                    results.append(self._finish(row, "stale", reason=reason))
                    continue
                request_id = self._request_id(row)
                ledger_entry = self._ledger_entry(request_id)
                self._append_ledger(row, request_id)
                if stop_after == "ledger":
                    break
                task_id = self._create_task(row, ledger_entry=ledger_entry)
                results.append(self._finish(row, "approved", request_id=request_id, task_id=task_id))
        return results
