"""Durable, fail-closed Telegram authority for GitHub approvals."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


_CHOICES = ("Approve", "Do not approve")
_ITEM_KEY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*\Z")
_NONTERMINAL_TASK_STATUSES = frozenset({
    "triage", "todo", "scheduled", "ready", "running", "blocked", "review",
})
_TERMINAL_TASK_STATUSES = frozenset({"done", "archived"})
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApprovalProposal:
    item_key: str
    head_sha: str
    contract_digest_sha256: str
    executor: str = "hermes"

    def canonical_key(self) -> str:
        self.validate()
        body = json.dumps(
            {"contract_digest_sha256": self.contract_digest_sha256, "executor": self.executor,
             "head_sha": self.head_sha, "item_key": self.item_key},
            sort_keys=True, separators=(",", ":"),
        ).encode()
        return hashlib.sha256(body).hexdigest()

    def validate(self) -> None:
        if not isinstance(self.item_key, str) or _ITEM_KEY_RE.fullmatch(self.item_key) is None:
            raise ValueError("item_key must be owner/repo#number with a positive canonical number")
        for name, value, length in (
            ("head_sha", self.head_sha, 40),
            ("contract_digest_sha256", self.contract_digest_sha256, 64),
        ):
            if not isinstance(value, str) or len(value) != length or any(c not in "0123456789abcdef" for c in value):
                raise ValueError(f"{name} must be lowercase hexadecimal ({length} chars)")
        if self.executor != "hermes":
            raise ValueError("GitHub approval executor must be hermes")


@dataclass(frozen=True)
class RequestOutcome:
    action: str
    proposal_key: str
    clarify_id: Optional[str] = None
    message_id: Optional[str] = None
    response: Optional[str] = None
    retired_clarify_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApprovalDecision:
    decision_id: int
    proposal_key: str
    response: str
    approver: str
    resolution_original_text: Optional[str] = None
    resolution_decision_text: Optional[str] = None
    resolution_parse_mode: Optional[str] = None


async def _await(value):
    return await value if inspect.isawaitable(value) else value


class GitHubApprovalBroker:
    """SQLite-backed authority. Telegram and executor ambiguity always stop closed."""

    def __init__(self, path: Path, *, instance_id: str, delivery_lease_seconds: float = 30.0):
        self.path = Path(path)
        self.instance_id = str(instance_id)
        if not self.instance_id:
            raise ValueError("instance_id is required")
        self.delivery_lease_seconds = max(1.0, float(delivery_lease_seconds))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS github_approval_proposals (
                    proposal_key TEXT PRIMARY KEY,
                    item_key TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    contract_digest_sha256 TEXT NOT NULL,
                    executor TEXT NOT NULL CHECK(executor = 'hermes'),
                    state TEXT NOT NULL CHECK(state IN (
                        'pending_notification','dispatching','delivery_unknown','active','answered','superseded')),
                    clarify_id TEXT NOT NULL UNIQUE,
                    session_key TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    thread_id TEXT,
                    authorized_user_id TEXT NOT NULL,
                    destination_id TEXT,
                    question TEXT NOT NULL,
                    message_id TEXT,
                    callback_bound INTEGER NOT NULL DEFAULT 0 CHECK(callback_bound IN (0,1)),
                    owner_instance_id TEXT,
                    lease_expires_at REAL,
                    response TEXT,
                    approver TEXT,
                    origin_status TEXT,
                    last_error TEXT,
                    retirement_pending INTEGER NOT NULL DEFAULT 0 CHECK(retirement_pending IN (0,1)),
                    retirement_retry_after REAL,
                    retirement_action TEXT CHECK(retirement_action IN ('resolve','delete')),
                    resolution_original_text TEXT,
                    resolution_decision_text TEXT,
                    resolution_parse_mode TEXT,
                    delivery_retry_after REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS github_approval_item_state
                    ON github_approval_proposals(item_key, state);
                CREATE TABLE IF NOT EXISTS github_approval_decisions (
                    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_key TEXT NOT NULL UNIQUE REFERENCES github_approval_proposals(proposal_key),
                    response TEXT NOT NULL,
                    approver TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','stale')),
                    decided_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS github_approval_inheritances (
                    proposal_key TEXT PRIMARY KEY REFERENCES github_approval_proposals(proposal_key),
                    predecessor_proposal_key TEXT NOT NULL REFERENCES github_approval_proposals(proposal_key),
                    task_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS github_approval_continuations (
                    predecessor_proposal_key TEXT PRIMARY KEY
                        REFERENCES github_approval_proposals(proposal_key),
                    item_key TEXT NOT NULL,
                    contract_digest_sha256 TEXT NOT NULL,
                    predecessor_task_id TEXT NOT NULL,
                    continuation_task_id TEXT NOT NULL UNIQUE,
                    continuation_idempotency_key TEXT NOT NULL UNIQUE,
                    blocker_class TEXT NOT NULL CHECK(blocker_class IN ('internal','external')),
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS github_approval_item_successions (
                    predecessor_item_key TEXT PRIMARY KEY,
                    successor_item_key TEXT NOT NULL,
                    successor_proposal_key TEXT NOT NULL
                        REFERENCES github_approval_proposals(proposal_key),
                    successor_task_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                COMMIT;
            """)
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(github_approval_proposals)")}
            if "destination_id" not in columns:
                db.execute("ALTER TABLE github_approval_proposals ADD COLUMN destination_id TEXT")
            if "retirement_pending" not in columns:
                db.execute("ALTER TABLE github_approval_proposals ADD COLUMN retirement_pending INTEGER NOT NULL DEFAULT 0")
            if "retirement_retry_after" not in columns:
                db.execute("ALTER TABLE github_approval_proposals ADD COLUMN retirement_retry_after REAL")
            if "retirement_action" not in columns:
                db.execute("ALTER TABLE github_approval_proposals ADD COLUMN retirement_action TEXT "
                           "CHECK(retirement_action IN ('resolve','delete'))")
            if "delivery_retry_after" not in columns:
                db.execute("ALTER TABLE github_approval_proposals ADD COLUMN delivery_retry_after REAL")
            if "resolution_original_text" not in columns:
                db.execute("ALTER TABLE github_approval_proposals ADD COLUMN resolution_original_text TEXT")
            if "resolution_decision_text" not in columns:
                db.execute("ALTER TABLE github_approval_proposals ADD COLUMN resolution_decision_text TEXT")
            if "resolution_parse_mode" not in columns:
                db.execute("ALTER TABLE github_approval_proposals ADD COLUMN resolution_parse_mode TEXT")

        try:
            self.path.chmod(0o600)
        except OSError:
            pass


    @staticmethod
    def _callback_is_bound(adapter: Any, clarify_id: str, session_key: str) -> bool:
        probe = getattr(adapter, "approval_callback_binding", None)
        if callable(probe):
            return bool(probe(clarify_id, session_key))
        state = getattr(adapter, "_clarify_state", None)
        return isinstance(state, dict) and state.get(clarify_id) == session_key

    @staticmethod
    def _destination_identity(adapter: Any, clarify_id: str) -> Optional[str]:
        identity = getattr(adapter, "approval_destination_identity", None)
        value = identity(clarify_id) if callable(identity) else None
        return value if isinstance(value, str) and value else None

    def _claim(self, proposal: ApprovalProposal, *, chat_id: str, question: str, session_key: str,
               thread_id: Optional[str], authorized_user_id: str,
               destination_id: Optional[str], dispatch: bool = True) -> tuple[RequestOutcome, list[str]]:
        key, now = proposal.canonical_key(), time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM github_approval_item_successions WHERE predecessor_item_key=?",
                (proposal.item_key,),
            ).fetchone() is not None:
                db.commit()
                return RequestOutcome("superseded", key), []
            exact = db.execute("SELECT * FROM github_approval_proposals WHERE proposal_key=?", (key,)).fetchone()
            if exact is not None:
                requested_binding = (chat_id, thread_id, authorized_user_id, question)
                stored_binding = (
                    str(exact["chat_id"]),
                    str(exact["thread_id"]) if exact["thread_id"] is not None else None,
                    str(exact["authorized_user_id"]),
                    str(exact["question"]),
                )
                if requested_binding != stored_binding:
                    db.commit()
                    return RequestOutcome(
                        "binding_conflict", key, str(exact["clarify_id"]), exact["message_id"]), []
                stored_destination = exact["destination_id"]
                if stored_destination is not None and destination_id != str(stored_destination):
                    db.commit()
                    return RequestOutcome(
                        "binding_conflict", key, str(exact["clarify_id"]), exact["message_id"]), []
                if exact["state"] == "superseded":
                    db.commit()
                    return RequestOutcome("superseded", key, str(exact["clarify_id"])), []
            if exact is not None and exact["state"] == "answered":
                db.commit()
                return RequestOutcome("answered", key, str(exact["clarify_id"]), exact["message_id"],
                                      str(exact["response"])), []
            if exact is not None and exact["state"] == "active":
                db.commit()
                return RequestOutcome("reused", key, str(exact["clarify_id"]), str(exact["message_id"])), []
            if exact is not None and exact["state"] in {"dispatching", "delivery_unknown"}:
                db.commit()
                return RequestOutcome("operator_reconciliation", key, str(exact["clarify_id"])), []
            old_active = db.execute(
                "SELECT clarify_id FROM github_approval_proposals WHERE item_key=? AND proposal_key!=? "
                "AND state='active' AND message_id IS NOT NULL",
                (proposal.item_key, key),
            ).fetchall()
            retired = [str(row["clarify_id"]) for row in old_active]
            db.execute("UPDATE github_approval_proposals SET state='superseded',callback_bound=0,"
                       "retirement_pending=0,retirement_retry_after=NULL,updated_at=? "
                       "WHERE item_key=? AND proposal_key!=? AND state IN "
                       "('pending_notification','dispatching','active','answered')", (now, proposal.item_key, key))
            db.execute("UPDATE github_approval_decisions SET state='stale' "
                       "WHERE state IN ('pending','claimed') AND proposal_key IN "
                       "(SELECT proposal_key FROM github_approval_proposals WHERE item_key=? "
                       "AND proposal_key!=? AND state='superseded')", (proposal.item_key, key))
            clarify_id = str(exact["clarify_id"]) if exact else f"gha-{key[:24]}"
            initial_state = "dispatching" if dispatch else "pending_notification"
            db.execute("""INSERT INTO github_approval_proposals(
                    proposal_key,item_key,head_sha,contract_digest_sha256,executor,state,clarify_id,
                    session_key,chat_id,thread_id,authorized_user_id,question,message_id,callback_bound,
                    destination_id,owner_instance_id,lease_expires_at,last_error,created_at,updated_at)
                VALUES(?,?,?,?,?,?, ?,?,?,?,?,?,NULL,0,?,?,?,?,?,?)
                ON CONFLICT(proposal_key) DO UPDATE SET state=excluded.state,session_key=excluded.session_key,
                    chat_id=excluded.chat_id,thread_id=excluded.thread_id,
                    authorized_user_id=excluded.authorized_user_id,question=excluded.question,message_id=NULL,
                    callback_bound=0,owner_instance_id=excluded.owner_instance_id,
                    lease_expires_at=excluded.lease_expires_at,
                    last_error=NULL,updated_at=excluded.updated_at""",
                (key, proposal.item_key, proposal.head_sha, proposal.contract_digest_sha256, proposal.executor,
                 initial_state,
                 clarify_id, session_key, chat_id, thread_id, authorized_user_id, question,
                 destination_id, self.instance_id if dispatch else None,
                 now + self.delivery_lease_seconds if dispatch else None, None, now, now))
            db.commit()
        return RequestOutcome("send", key, clarify_id), retired

    @staticmethod
    def _authority_question(question: str) -> str:
        """Remove only the SHA binding; all authority-bearing prose must stay byte-identical."""
        return "\n".join(
            "Head: <successor>" if line.startswith("Head: ") else line
            for line in str(question).splitlines()
        )

    @staticmethod
    def _matches_external_blocked_task(
        *, block_kind: object, idempotency_key: object, body: object,
        proposal_key: str, item_key: str, contract_digest_sha256: str,
    ) -> bool:
        """Verify the producer-owned task that is parked on one human dependency."""
        lines = set(str(body or "").splitlines())
        return (
            str(block_kind) == "needs_input"
            and str(idempotency_key) == f"github-approval:{proposal_key}"
            and f"Item: `{item_key}`" in lines
            and f"Contract digest: `{contract_digest_sha256}`" in lines
            and f"Proposal key: `{proposal_key}`" in lines
        )

    @staticmethod
    def _body_contains_item_key(body: object, item_key: str) -> bool:
        return re.search(
            rf"(?<![A-Za-z0-9_.#/-]){re.escape(item_key)}(?![A-Za-z0-9_.#/-])",
            str(body or ""),
        ) is not None

    def _item_succession(self, item_key: str) -> Optional[sqlite3.Row]:
        with self._connect() as db:
            return db.execute(
                "SELECT * FROM github_approval_item_successions WHERE predecessor_item_key=?",
                (item_key,),
            ).fetchone()

    def bind_item_succession(
        self,
        *,
        predecessor_item_key: str,
        successor_item_key: str,
        successor_task_id: str,
        kanban_path: Path,
    ) -> None:
        """Bind one retired GitHub item to an independently approved durable successor."""
        for name, value in (
            ("predecessor_item_key", predecessor_item_key),
            ("successor_item_key", successor_item_key),
        ):
            if not isinstance(value, str) or _ITEM_KEY_RE.fullmatch(value) is None:
                raise ValueError(f"{name} must be owner/repo#number with a positive canonical number")
        if predecessor_item_key == successor_item_key:
            raise ValueError("item succession requires distinct predecessor and successor item keys")
        if not isinstance(successor_task_id, str) or not successor_task_id:
            raise ValueError("successor_task_id is required")
        existing = self._item_succession(predecessor_item_key)
        if existing is not None and (
                str(existing["successor_item_key"]), str(existing["successor_task_id"])) != (
                successor_item_key, successor_task_id):
            raise ValueError("predecessor item already has a different successor mapping")

        with self._connect() as db:
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='github_approval_reconciliations'"
            ).fetchone() is None:
                raise ValueError("successor has no reconciled approved proposal")
            authority = db.execute(
                """SELECT p.proposal_key,r.task_id AS latest_task_id
                   FROM github_approval_proposals p
                   JOIN github_approval_decisions d USING(proposal_key)
                   JOIN github_approval_reconciliations r USING(decision_id)
                   WHERE p.item_key=? AND p.state='answered'
                     AND p.response='Approve' AND d.response='Approve'
                     AND COALESCE(p.origin_status,'') NOT IN ('closed','merged','superseded')
                     AND r.outcome='approved'
                     AND (r.task_id=? OR EXISTS(
                         SELECT 1 FROM github_approval_proposals canonical_p
                         JOIN github_approval_decisions canonical_d
                           ON canonical_d.proposal_key=canonical_p.proposal_key
                         JOIN github_approval_reconciliations canonical_r
                           ON canonical_r.decision_id=canonical_d.decision_id
                         WHERE canonical_p.item_key=p.item_key
                           AND canonical_p.contract_digest_sha256=p.contract_digest_sha256
                           AND canonical_p.executor=p.executor
                           AND canonical_p.response='Approve'
                           AND canonical_d.response='Approve'
                           AND canonical_r.outcome='approved'
                           AND canonical_r.task_id=?))
                     AND p.rowid=(SELECT newest.rowid FROM github_approval_proposals newest
                                  WHERE newest.item_key=p.item_key
                                  ORDER BY newest.created_at DESC,newest.rowid DESC LIMIT 1)""",
                (successor_item_key, successor_task_id, successor_task_id),
            ).fetchone()
        if authority is None:
            raise ValueError("successor task is not the reconciled approved proposal task")
        successor_proposal_key = str(authority["proposal_key"])
        latest_successor_task_id = str(authority["latest_task_id"])

        kanban_guard = sqlite3.connect(Path(kanban_path), timeout=30.0)
        try:
            kanban_guard.execute("BEGIN IMMEDIATE")
            task = kanban_guard.execute(
                "SELECT status,body FROM tasks WHERE id=?", (successor_task_id,)
            ).fetchone()
            if task is None or str(task[0]) not in _NONTERMINAL_TASK_STATUSES:
                raise ValueError("successor task must be durable and nonterminal")
            if not all(self._body_contains_item_key(task[1], item_key) for item_key in (
                    predecessor_item_key, successor_item_key)):
                raise ValueError("successor task body must include both exact item keys")
            if latest_successor_task_id != successor_task_id:
                latest_task = kanban_guard.execute(
                    "SELECT status FROM tasks WHERE id=?", (latest_successor_task_id,)
                ).fetchone()
                if latest_task is None or str(latest_task[0]) not in _TERMINAL_TASK_STATUSES:
                    raise ValueError("newer reconciled successor task is not terminal")

            now = time.time()
            record = (
                predecessor_item_key, successor_item_key,
                successor_proposal_key, successor_task_id,
            )
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    """SELECT p.proposal_key,r.task_id AS latest_task_id
                       FROM github_approval_proposals p
                       JOIN github_approval_decisions d USING(proposal_key)
                       JOIN github_approval_reconciliations r USING(decision_id)
                       WHERE p.proposal_key=? AND p.item_key=?
                         AND p.state='answered'
                         AND p.response='Approve' AND d.response='Approve'
                         AND COALESCE(p.origin_status,'') NOT IN ('closed','merged','superseded')
                         AND r.outcome='approved' AND r.task_id=?
                         AND EXISTS(
                             SELECT 1 FROM github_approval_proposals canonical_p
                             JOIN github_approval_decisions canonical_d
                               ON canonical_d.proposal_key=canonical_p.proposal_key
                             JOIN github_approval_reconciliations canonical_r
                               ON canonical_r.decision_id=canonical_d.decision_id
                             WHERE canonical_p.item_key=p.item_key
                               AND canonical_p.contract_digest_sha256=p.contract_digest_sha256
                               AND canonical_p.executor=p.executor
                               AND canonical_p.response='Approve'
                               AND canonical_d.response='Approve'
                               AND canonical_r.outcome='approved'
                               AND canonical_r.task_id=?)
                         AND p.rowid=(SELECT newest.rowid FROM github_approval_proposals newest
                                     WHERE newest.item_key=p.item_key
                                     ORDER BY newest.created_at DESC,newest.rowid DESC LIMIT 1)""",
                    (successor_proposal_key, successor_item_key,
                     latest_successor_task_id, successor_task_id),
                ).fetchone()
                if current is None:
                    db.rollback()
                    raise ValueError("successor approval changed during succession binding")
                predecessor_task_ids = [str(row["task_id"]) for row in db.execute(
                    """SELECT r.task_id
                       FROM github_approval_proposals p
                       JOIN github_approval_decisions d USING(proposal_key)
                       JOIN github_approval_reconciliations r USING(decision_id)
                       WHERE p.item_key=? AND r.outcome='approved' AND r.task_id IS NOT NULL""",
                    (predecessor_item_key,),
                ).fetchall()]
                for predecessor_task_id in predecessor_task_ids:
                    predecessor_task = kanban_guard.execute(
                        "SELECT status FROM tasks WHERE id=?", (predecessor_task_id,)
                    ).fetchone()
                    if predecessor_task is None:
                        db.rollback()
                        raise ValueError("reconciled predecessor task state is unavailable")
                    predecessor_status = str(predecessor_task[0])
                    if predecessor_status in _NONTERMINAL_TASK_STATUSES:
                        db.rollback()
                        raise ValueError("reconciled predecessor task is nonterminal")
                    if predecessor_status not in _TERMINAL_TASK_STATUSES:
                        db.rollback()
                        raise ValueError("reconciled predecessor task has an unknown status")
                db.execute(
                    """INSERT OR IGNORE INTO github_approval_item_successions(
                        predecessor_item_key,successor_item_key,successor_proposal_key,
                        successor_task_id,created_at) VALUES(?,?,?,?,?)""",
                    (*record, now),
                )
                persisted = db.execute(
                    """SELECT predecessor_item_key,successor_item_key,successor_proposal_key,
                              successor_task_id
                       FROM github_approval_item_successions WHERE predecessor_item_key=?""",
                    (predecessor_item_key,),
                ).fetchone()
                if persisted is None or tuple(persisted) != record:
                    db.rollback()
                    raise ValueError("predecessor item already has a different successor mapping")
                db.execute(
                    """UPDATE github_approval_proposals
                       SET state='superseded',callback_bound=0,owner_instance_id=NULL,
                           lease_expires_at=NULL,
                           retirement_pending=CASE WHEN message_id IS NOT NULL THEN 1 ELSE 0 END,
                           retirement_action=CASE WHEN message_id IS NOT NULL THEN 'delete' ELSE NULL END,
                           retirement_retry_after=NULL,updated_at=?
                       WHERE item_key=?""",
                    (now, predecessor_item_key),
                )
                db.execute(
                    """UPDATE github_approval_decisions SET state='stale'
                       WHERE state='pending' AND proposal_key IN (
                           SELECT proposal_key FROM github_approval_proposals
                           WHERE item_key=? AND state='superseded')""",
                    (predecessor_item_key,),
                )
                db.commit()
            kanban_guard.commit()
        except sqlite3.Error as exc:
            raise ValueError("Kanban successor task state is unavailable") from exc
        finally:
            kanban_guard.close()

    def bind_internal_continuation(
        self,
        *,
        predecessor_proposal_key: str,
        predecessor_task_id: str,
        continuation_task_id: str,
        kanban_path: Path,
    ) -> None:
        """Persist the diagnosed corrective lane for one reconciled approval.

        This binding is intentionally explicit and immutable. A blocked task is not authority;
        inheritance may use only the exact continuation recorded here and later proven live in
        Kanban. The caller diagnoses the blocker as internal, while this method verifies all
        machine-checkable authorities: the approved predecessor is blocked, the exact
        corrective task is live, and its idempotency key binds the same item, head, and
        contract digest.  External blockers therefore have no binding to inherit.
        """
        values = (
            predecessor_proposal_key,
            predecessor_task_id,
            continuation_task_id,
        )
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError("continuation binding requires complete durable identities")
        with self._connect() as db:
            authority = db.execute(
                """SELECT p.item_key,p.head_sha,p.contract_digest_sha256,r.task_id
                   FROM github_approval_proposals p
                   JOIN github_approval_decisions d USING(proposal_key)
                   JOIN github_approval_reconciliations r USING(decision_id)
                   WHERE p.proposal_key=? AND p.state IN ('answered','superseded')
                     AND p.response='Approve' AND r.outcome='approved'
                     AND COALESCE(p.origin_status,'') NOT IN ('closed','merged','superseded')""",
                (predecessor_proposal_key,),
            ).fetchone()
            if authority is None or str(authority["task_id"]) != predecessor_task_id:
                raise ValueError("continuation predecessor is not the reconciled approved task")
        expected_key = (
            f"github-corrective:{authority['item_key']}:{authority['head_sha']}:"
            f"{authority['contract_digest_sha256']}"
        )
        kanban_guard = sqlite3.connect(Path(kanban_path), timeout=30.0)
        try:
            kanban_guard.execute("BEGIN IMMEDIATE")
            predecessor = kanban_guard.execute(
                "SELECT status FROM tasks WHERE id=?", (predecessor_task_id,)
            ).fetchone()
            continuation = kanban_guard.execute(
                "SELECT status,idempotency_key FROM tasks WHERE id=?", (continuation_task_id,)
            ).fetchone()
            if predecessor is None or str(predecessor[0]) != "blocked":
                raise ValueError("approved predecessor must be blocked before binding a continuation")
            if continuation is None or str(continuation[0]) not in {"ready", "in_progress", "running"}:
                raise ValueError("internal continuation must be live")
            if str(continuation[1]) != expected_key:
                raise ValueError("internal continuation lacks the canonical idempotency binding")
            continuation_idempotency_key = expected_key
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    """SELECT r.task_id,p.origin_status
                       FROM github_approval_proposals p
                       JOIN github_approval_decisions d USING(proposal_key)
                       JOIN github_approval_reconciliations r USING(decision_id)
                       WHERE p.proposal_key=? AND p.response='Approve' AND r.outcome='approved'""",
                    (predecessor_proposal_key,),
                ).fetchone()
                if (current is None or str(current["task_id"]) != predecessor_task_id
                        or current["origin_status"] in {"closed", "merged", "superseded"}):
                    db.rollback()
                    raise ValueError("continuation predecessor approval changed during binding")
                record = (
                    predecessor_proposal_key,
                    str(authority["item_key"]),
                    str(authority["contract_digest_sha256"]),
                    predecessor_task_id,
                    continuation_task_id,
                    continuation_idempotency_key,
                    "internal",
                )
                db.execute(
                    """INSERT OR IGNORE INTO github_approval_continuations(
                        predecessor_proposal_key,item_key,contract_digest_sha256,
                        predecessor_task_id,continuation_task_id,continuation_idempotency_key,
                        blocker_class,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (*record, time.time()),
                )
                persisted = db.execute(
                    """SELECT predecessor_proposal_key,item_key,contract_digest_sha256,
                              predecessor_task_id,continuation_task_id,continuation_idempotency_key,
                              blocker_class
                       FROM github_approval_continuations WHERE predecessor_proposal_key=?""",
                    (predecessor_proposal_key,),
                ).fetchone()
                if persisted is None or tuple(persisted) != record:
                    db.rollback()
                    raise ValueError("approved predecessor already has a different continuation binding")
                db.commit()
            kanban_guard.commit()
        except sqlite3.Error as exc:
            raise ValueError("Kanban continuation state is unavailable") from exc
        finally:
            kanban_guard.close()


    def try_inherit_successor(
        self,
        proposal: ApprovalProposal,
        *,
        question: str,
        session_key: str,
        chat_id: str,
        authorized_user_id: str,
        destination_id: str,
        thread_id: Optional[str],
        kanban_path: Path,
        ancestry_checker: Callable[[str, str, str], bool],
    ) -> Optional[RequestOutcome]:
        """Project a proven internal descendant onto the existing approved task.

        Inheritance is deliberately narrow: same item/digest/executor and authority text, an
        approved reconciliation whose task is still live, and a caller-proved descendant head.
        Any uncertainty returns ``None`` so the normal human approval path remains fail-closed.
        """
        proposal.validate()
        required = (question, session_key, chat_id, authorized_user_id, destination_id)
        if not all(isinstance(value, str) and value for value in required):
            raise ValueError("successor inheritance requires complete durable bindings")
        key = proposal.canonical_key()
        if self._item_succession(proposal.item_key) is not None:
            return None
        with self._connect() as db:
            exact = db.execute(
                "SELECT * FROM github_approval_proposals WHERE proposal_key=?", (key,)
            ).fetchone()
            if exact is not None:
                expected = (
                    question, session_key, chat_id, thread_id, authorized_user_id, destination_id,
                )
                observed = (
                    str(exact["question"]), str(exact["session_key"]), str(exact["chat_id"]),
                    str(exact["thread_id"]) if exact["thread_id"] is not None else None,
                    str(exact["authorized_user_id"]),
                    str(exact["destination_id"]) if exact["destination_id"] is not None else None,
                )
                if str(exact["state"]) not in {"pending_notification", "active"} or observed != expected:
                    return None
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='github_approval_reconciliations'"
            ).fetchone() is None:
                return None
            candidates = db.execute(
                """SELECT p.*,r.task_id,d.decision_id
                   FROM github_approval_proposals p
                   JOIN github_approval_decisions d USING(proposal_key)
                   JOIN github_approval_reconciliations r USING(decision_id)
                   WHERE p.item_key=? AND p.contract_digest_sha256=? AND p.executor=?
                     AND p.head_sha!=? AND p.response='Approve'
                     AND (p.state='answered' OR EXISTS (
                         SELECT 1 FROM github_approval_continuations c
                         WHERE c.predecessor_proposal_key=p.proposal_key
                           AND c.blocker_class='internal'))
                     AND COALESCE(p.origin_status,'') NOT IN ('closed','merged','superseded')
                     AND r.outcome='approved'
                     AND r.task_id IS NOT NULL
                   ORDER BY d.decision_id DESC""",
                (proposal.item_key, proposal.contract_digest_sha256,
                 proposal.executor, proposal.head_sha),
            ).fetchall()
        normalized = self._authority_question(question)
        for candidate in candidates:
            if self._authority_question(str(candidate["question"])) != normalized:
                continue
            predecessor_task_id = str(candidate["task_id"])
            task_id = predecessor_task_id
            continuation_key: Optional[str] = None
            external_blocked_anchor = False
            try:
                with sqlite3.connect(f"file:{Path(kanban_path)}?mode=ro", uri=True) as kanban:
                    task = kanban.execute(
                        "SELECT status FROM tasks WHERE id=?", (predecessor_task_id,)
                    ).fetchone()
                    if task is not None and str(task[0]) == "blocked":
                        with self._connect() as db:
                            binding = db.execute(
                                """SELECT continuation_task_id,continuation_idempotency_key
                                   FROM github_approval_continuations
                                   WHERE predecessor_proposal_key=? AND predecessor_task_id=?
                                     AND item_key=? AND contract_digest_sha256=?
                                     AND blocker_class='internal'""",
                                (str(candidate["proposal_key"]), predecessor_task_id,
                                 proposal.item_key, proposal.contract_digest_sha256),
                            ).fetchone()
                        if binding is not None:
                            continuation = kanban.execute(
                                "SELECT status,idempotency_key FROM tasks WHERE id=?",
                                (str(binding["continuation_task_id"]),),
                            ).fetchone()
                            if (continuation is not None
                                    and str(continuation[0]) in {"ready", "in_progress", "running"}
                                    and str(continuation[1]) == str(binding["continuation_idempotency_key"])):
                                task_id = str(binding["continuation_task_id"])
                                continuation_key = str(binding["continuation_idempotency_key"])
                        if continuation_key is None:
                            anchored = kanban.execute(
                                "SELECT block_kind,idempotency_key,body FROM tasks WHERE id=?",
                                (predecessor_task_id,),
                            ).fetchone()
                            external_blocked_anchor = bool(
                                anchored is not None
                                and self._matches_external_blocked_task(
                                    block_kind=anchored[0], idempotency_key=anchored[1],
                                    body=anchored[2], proposal_key=str(candidate["proposal_key"]),
                                    item_key=proposal.item_key,
                                    contract_digest_sha256=proposal.contract_digest_sha256,
                                )
                            )
            except sqlite3.Error:
                return None
            if (task is None or (
                    str(task[0]) not in {"ready", "in_progress", "running"}
                    and task_id == predecessor_task_id
                    and not external_blocked_anchor)):
                continue
            try:
                descendant = ancestry_checker(
                    proposal.item_key, str(candidate["head_sha"]), proposal.head_sha
                )
            except Exception:
                logger.exception("Could not verify GitHub approval successor ancestry")
                return None
            if not descendant:
                continue
            predecessor_key = str(candidate["proposal_key"])
            kanban_guard = sqlite3.connect(Path(kanban_path), timeout=30.0)
            try:
                kanban_guard.execute("BEGIN IMMEDIATE")
                live_predecessor = kanban_guard.execute(
                    "SELECT status FROM tasks WHERE id=?", (predecessor_task_id,)
                ).fetchone()
                if continuation_key is not None:
                    live_task = kanban_guard.execute(
                        "SELECT status,idempotency_key FROM tasks WHERE id=?", (task_id,)
                    ).fetchone()
                elif external_blocked_anchor:
                    live_task = kanban_guard.execute(
                        "SELECT status,block_kind,idempotency_key,body FROM tasks WHERE id=?",
                        (task_id,),
                    ).fetchone()
                else:
                    live_task = kanban_guard.execute(
                        "SELECT status FROM tasks WHERE id=?", (task_id,)
                    ).fetchone()
                external_anchor_still_valid = bool(
                    external_blocked_anchor
                    and live_task is not None
                    and str(live_task[0]) == "blocked"
                    and self._matches_external_blocked_task(
                        block_kind=live_task[1], idempotency_key=live_task[2],
                        body=live_task[3], proposal_key=str(candidate["proposal_key"]),
                        item_key=proposal.item_key,
                        contract_digest_sha256=proposal.contract_digest_sha256,
                    )
                )
                if (live_task is None
                        or (str(live_task[0]) not in {"ready", "in_progress", "running"}
                            and not external_anchor_still_valid)
                        or (continuation_key is not None
                            and (live_predecessor is None
                                 or str(live_predecessor[0]) != "blocked"
                                 or str(live_task[1]) != continuation_key))):
                    kanban_guard.rollback()
                    continue
                now = time.time()
                with self._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    exact = db.execute(
                        "SELECT * FROM github_approval_proposals WHERE proposal_key=?", (key,)
                    ).fetchone()
                    if exact is not None:
                        expected = (
                            question, session_key, chat_id, thread_id,
                            authorized_user_id, destination_id,
                        )
                        observed = (
                            str(exact["question"]), str(exact["session_key"]),
                            str(exact["chat_id"]),
                            str(exact["thread_id"]) if exact["thread_id"] is not None else None,
                            str(exact["authorized_user_id"]),
                            (str(exact["destination_id"])
                             if exact["destination_id"] is not None else None),
                        )
                        if (str(exact["state"]) not in {"pending_notification", "active"}
                                or observed != expected):
                            db.rollback()
                            return None
                    predecessor = db.execute(
                        """SELECT p.state,p.response,p.approver,p.origin_status,r.outcome,r.task_id,
                                  c.continuation_task_id,c.continuation_idempotency_key,
                                  c.blocker_class
                           FROM github_approval_proposals p
                           JOIN github_approval_decisions d USING(proposal_key)
                           JOIN github_approval_reconciliations r USING(decision_id)
                           LEFT JOIN github_approval_continuations c
                             ON c.predecessor_proposal_key=p.proposal_key
                           WHERE p.proposal_key=?""", (predecessor_key,)
                    ).fetchone()
                    succession = db.execute(
                        "SELECT 1 FROM github_approval_item_successions "
                        "WHERE predecessor_item_key=?",
                        (proposal.item_key,),
                    ).fetchone()
                    if (succession is not None
                            or predecessor is None
                            or (predecessor["state"] != "answered"
                                and continuation_key is None)
                            or predecessor["response"] != "Approve"
                            or predecessor["origin_status"] in {"closed", "merged", "superseded"}
                            or predecessor["outcome"] != "approved"
                            or str(predecessor["task_id"]) != predecessor_task_id
                            or (continuation_key is not None and (
                                predecessor["blocker_class"] != "internal"
                                or str(predecessor["continuation_task_id"]) != task_id
                                or str(predecessor["continuation_idempotency_key"])
                                != continuation_key))):
                        db.rollback()
                        return None
                    retired = [str(row["clarify_id"]) for row in db.execute(
                        """SELECT clarify_id FROM github_approval_proposals
                           WHERE item_key=? AND proposal_key!=?
                             AND state IN ('dispatching','delivery_unknown','active')
                             AND message_id IS NOT NULL""",
                        (proposal.item_key, key),
                    ).fetchall()]
                    if (exact is not None and exact["state"] == "active"
                            and exact["message_id"] is not None):
                        retired.append(str(exact["clarify_id"]))
                    db.execute(
                        "UPDATE github_approval_proposals SET state='superseded',callback_bound=0,"
                        "retirement_pending=CASE WHEN message_id IS NOT NULL THEN 1 ELSE 0 END,"
                        "retirement_action=CASE WHEN message_id IS NOT NULL THEN 'delete' ELSE NULL END,"
                        "retirement_retry_after=NULL,updated_at=? "
                        "WHERE item_key=? AND proposal_key!=? AND state IN "
                        "('pending_notification','dispatching','delivery_unknown','active')",
                        (now, proposal.item_key, key),
                    )
                    if exact is None:
                        clarify_id = f"gha-{key[:24]}"
                        message_id = None
                        db.execute(
                            """INSERT INTO github_approval_proposals(
                                proposal_key,item_key,head_sha,contract_digest_sha256,executor,state,
                                clarify_id,session_key,chat_id,thread_id,authorized_user_id,destination_id,
                                question,message_id,callback_bound,response,approver,created_at,updated_at)
                               VALUES(?,?,?,?,?,'answered',?,?,?,?,?,?,?,NULL,0,'Approve',?,?,?)""",
                            (key, proposal.item_key, proposal.head_sha,
                             proposal.contract_digest_sha256, proposal.executor, clarify_id,
                             session_key, chat_id, thread_id, authorized_user_id, destination_id,
                             question, str(predecessor["approver"]), now, now),
                        )
                    else:
                        clarify_id = str(exact["clarify_id"])
                        message_id = (str(exact["message_id"])
                                      if exact["message_id"] is not None else None)
                        db.execute(
                            """UPDATE github_approval_proposals
                               SET state='answered',callback_bound=0,response='Approve',approver=?,
                                   owner_instance_id=NULL,lease_expires_at=NULL,last_error=NULL,
                                   retirement_pending=CASE WHEN message_id IS NOT NULL THEN 1 ELSE 0 END,
                                   retirement_action=CASE WHEN message_id IS NOT NULL THEN 'delete' ELSE NULL END,
                                   retirement_retry_after=NULL,updated_at=?
                               WHERE proposal_key=? AND state IN ('pending_notification','active')""",
                            (str(predecessor["approver"]), now, key),
                        )
                    db.execute(
                        """INSERT INTO github_approval_decisions(
                            proposal_key,response,approver,state,decided_at)
                           VALUES(?,'Approve',?,'pending',?) ON CONFLICT(proposal_key) DO NOTHING""",
                        (key, str(predecessor["approver"]), now),
                    )
                    db.execute(
                        "INSERT INTO github_approval_inheritances VALUES(?,?,?,?)",
                        (key, predecessor_key, task_id, now),
                    )
                    db.commit()
                kanban_guard.commit()
                return RequestOutcome(
                    "inherited", key, clarify_id, message_id=message_id, response="Approve",
                    retired_clarify_ids=tuple(dict.fromkeys(retired)),
                )
            finally:
                kanban_guard.close()
        return None

    async def retire_inherited_cards(self, outcome: RequestOutcome, *, adapter: Any) -> int:
        """Remove human cards made redundant by a committed inherited decision."""
        if outcome.action != "inherited" or not outcome.retired_clarify_ids:
            return 0
        delete = getattr(adapter, "delete_clarify_card", None)
        if not callable(delete):
            logger.warning("Approval adapter cannot retire inherited GitHub approval cards")
            return 0
        retired = 0
        for clarify_id in outcome.retired_clarify_ids:
            try:
                deleted = await _await(delete(clarify_id))
            except Exception:
                logger.exception("Could not delete GitHub approval card retired by inheritance")
                continue
            if deleted is False:
                logger.warning("Telegram did not delete inherited GitHub approval card %s", clarify_id)
                continue
            with self._connect() as db:
                retired += db.execute(
                    """UPDATE github_approval_proposals
                       SET retirement_pending=0,retirement_action=NULL,
                           retirement_retry_after=NULL,updated_at=?
                       WHERE clarify_id=? AND retirement_pending=1
                         AND retirement_action='delete'""",
                    (time.time(), clarify_id),
                ).rowcount
        return retired

    def admit(self, proposal: ApprovalProposal, *, adapter: Any, chat_id: str, question: str,
              session_key: str, authorized_user_id: str,
              thread_id: Optional[str] = None) -> RequestOutcome:
        """Commit an approval to the durable delivery outbox without transport I/O."""
        required = (chat_id, question, session_key, authorized_user_id)
        if not all(isinstance(value, str) and value for value in required):
            raise ValueError("chat_id, question, session_key, and authorized_user_id are required")
        key = proposal.canonical_key()
        if self._item_succession(proposal.item_key) is not None:
            return RequestOutcome("superseded", key)
        destination_id = self._destination_identity(adapter, f"gha-{key[:24]}")
        if destination_id is None:
            raise ValueError("approval destination identity is unavailable")
        claim, _retired = self._claim(
            proposal, chat_id=chat_id, question=question, session_key=session_key,
            thread_id=str(thread_id) if thread_id is not None else None,
            authorized_user_id=authorized_user_id, destination_id=destination_id, dispatch=False)
        return RequestOutcome("admitted" if claim.action == "send" else claim.action,
                              claim.proposal_key, claim.clarify_id, claim.message_id, claim.response)

    async def request(self, proposal: ApprovalProposal, *, adapter: Any, chat_id: str, question: str,
                      session_key: str, authorized_user_id: str, thread_id: Optional[str] = None) -> RequestOutcome:
        required = (chat_id, question, session_key, authorized_user_id)
        if not all(isinstance(value, str) and value for value in required):
            raise ValueError("chat_id, question, session_key, and authorized_user_id are required")
        key = proposal.canonical_key()
        if self._item_succession(proposal.item_key) is not None:
            return RequestOutcome("superseded", key)
        destination_id = self._destination_identity(adapter, f"gha-{key[:24]}")
        claim, retired = self._claim(proposal, chat_id=chat_id, question=question, session_key=session_key,
                                     thread_id=str(thread_id) if thread_id is not None else None,
                                     authorized_user_id=authorized_user_id, destination_id=destination_id)
        if claim.action != "send":
            return claim
        delete = getattr(adapter, "delete_clarify_card", None)
        if callable(delete):
            for clarify_id in retired:
                try:
                    deleted = await _await(delete(clarify_id))
                except Exception:
                    logger.exception("Could not delete superseded GitHub approval card")
                    continue
                if deleted is False:
                    logger.warning("Telegram did not delete superseded GitHub approval card %s", clarify_id)
        return await self._deliver_claim(claim.proposal_key, adapter)

    async def _deliver_claim(self, proposal_key: str, adapter: Any) -> RequestOutcome:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            reserved = db.execute(
                "SELECT state,owner_instance_id FROM github_approval_proposals WHERE proposal_key=?",
                (proposal_key,)).fetchone()
            if (reserved is not None and reserved["state"] == "pending_notification"
                    and reserved["owner_instance_id"] == self.instance_id):
                db.execute("UPDATE github_approval_proposals SET state='dispatching',updated_at=? "
                           "WHERE proposal_key=? AND state='pending_notification' AND owner_instance_id=?",
                           (time.time(), proposal_key, self.instance_id))
            db.commit()
            row = db.execute("SELECT * FROM github_approval_proposals WHERE proposal_key=? AND state='dispatching'",
                             (proposal_key,)).fetchone()
        if row is None:
            observed = self.observe_key(proposal_key)
            return RequestOutcome("reused", proposal_key, observed.get("clarify_id") if observed else None,
                                  observed.get("message_id") if observed else None)
        destination_id = str(row["destination_id"]) if row["destination_id"] is not None else None
        metadata = {"is_approval_prompt": True, "github_approval": True,
                    "authorized_user_id": str(row["authorized_user_id"])}
        if row["thread_id"] is not None:
            metadata["thread_id"] = str(row["thread_id"])
        explicit_failure = False
        delivered = False
        delivery_retry_after: Optional[float] = None
        try:
            result = await adapter.send_clarify(
                chat_id=str(row["chat_id"]), question=str(row["question"]), choices=list(_CHOICES),
                clarify_id=str(row["clarify_id"]), session_key=str(row["session_key"]), metadata=metadata)
            if not result.success:
                explicit_failure, message_id = True, None
                error = result.error or "provider rejected delivery"
                state = "pending_notification"
                raw_retry_after = getattr(result, "retry_after", None)
                if raw_retry_after is not None:
                    try:
                        delivery_retry_after = time.time() + max(1.0, float(raw_retry_after))
                    except (TypeError, ValueError):
                        pass
            else:
                delivered = True
                message_id = str(result.message_id) if result.message_id is not None else None
                bound = self._callback_is_bound(adapter, str(row["clarify_id"]), str(row["session_key"]))
                destination_id = self._destination_identity(adapter, str(row["clarify_id"]))
                if message_id and bound and destination_id:
                    state, error = "active", None
                else:
                    state, error = "delivery_unknown", "successful send lacked durable destination/message/callback binding"
        except Exception as exc:
            logger.exception("GitHub approval card delivery outcome is ambiguous")
            state, message_id, error = "delivery_unknown", None, f"ambiguous delivery: {type(exc).__name__}"
        now = time.time()
        with self._connect() as db:
            changed = db.execute("""UPDATE github_approval_proposals SET state=?,message_id=?,callback_bound=?,destination_id=?,
                    owner_instance_id=?,last_error=?,lease_expires_at=NULL,delivery_retry_after=?,updated_at=?
                    WHERE proposal_key=? AND state='dispatching' AND owner_instance_id=?""",
                (state, message_id if delivered else None, 1 if state == "active" else 0,
                 destination_id if state == "active" else row["destination_id"],
                 None if state == "pending_notification" else self.instance_id,
                 error, delivery_retry_after, now, proposal_key, self.instance_id)).rowcount
        if changed != 1:
            observed = self.observe_key(proposal_key)
            if delivered and observed is not None and observed.get("state") == "superseded":
                with self._connect() as db:
                    db.execute("UPDATE github_approval_proposals SET message_id=?,"
                               "retirement_pending=CASE WHEN ? IS NOT NULL THEN 1 ELSE 0 END,"
                               "retirement_action=CASE WHEN ? IS NOT NULL THEN 'delete' ELSE NULL END,"
                               "retirement_retry_after=NULL,updated_at=? "
                               "WHERE proposal_key=? AND state='superseded'",
                               (message_id, message_id, message_id, time.time(), proposal_key))
                delete = getattr(adapter, "delete_clarify_card", None)
                if callable(delete):
                    try:
                        deleted = await _await(delete(str(row["clarify_id"])))
                    except Exception:
                        logger.exception("Could not delete GitHub approval card superseded during delivery")
                    else:
                        if deleted is False:
                            logger.warning(
                                "Telegram did not delete GitHub approval card superseded during delivery %s",
                                row["clarify_id"])
                        else:
                            with self._connect() as db:
                                db.execute(
                                    """UPDATE github_approval_proposals
                                       SET retirement_pending=0,retirement_action=NULL,
                                           retirement_retry_after=NULL,updated_at=?
                                       WHERE proposal_key=? AND state='superseded'
                                         AND retirement_action='delete'""",
                                    (time.time(), proposal_key),
                                )
                return RequestOutcome("superseded", proposal_key, str(row["clarify_id"]), message_id)
            return RequestOutcome("operator_reconciliation", proposal_key, str(row["clarify_id"]))
        action = "sent" if state == "active" else ("pending_retry" if explicit_failure else "operator_reconciliation")
        return RequestOutcome(action, proposal_key, str(row["clarify_id"]), message_id if state == "active" else None)

    async def retry_pending(self, *, adapter: Any, limit: int = 16) -> list[RequestOutcome]:
        destination_id = self._destination_identity(adapter, "")
        if destination_id is None:
            return []
        now, claimed = time.time(), []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT p.proposal_key FROM github_approval_proposals p "
                              "WHERE p.state='pending_notification' "
                              "AND (p.owner_instance_id IS NULL OR p.lease_expires_at<=?) "
                              "AND (p.delivery_retry_after IS NULL OR p.delivery_retry_after<=?) "
                              "AND p.destination_id=? AND NOT EXISTS (SELECT 1 FROM github_approval_proposals old "
                              "WHERE old.item_key=p.item_key AND old.proposal_key!=p.proposal_key "
                              "AND old.state='superseded' AND old.retirement_pending=1) "
                              "ORDER BY p.updated_at LIMIT ?",
                              (now, now, destination_id, max(1, int(limit)))).fetchall()
            for row in rows:
                key = str(row["proposal_key"])
                if db.execute("UPDATE github_approval_proposals SET owner_instance_id=?,"
                              "lease_expires_at=?,updated_at=? WHERE proposal_key=? AND "
                              "state='pending_notification' AND (owner_instance_id IS NULL OR lease_expires_at<=?)",
                              (self.instance_id, now + self.delivery_lease_seconds, now, key, now)).rowcount == 1:
                    claimed.append(key)
            db.commit()
        return [await self._deliver_claim(key, adapter) for key in claimed]

    async def retry_retirements(self, *, adapter: Any, limit: int = 16) -> int:
        """Retry stale-card keyboard removal; success is durably verified by clearing the marker."""
        # Historical cosmetic supersession edits remain non-retryable. Inherited approvals use
        # an explicit delete action because their redundant authority card is not merely cosmetic.
        with self._connect() as db:
            db.execute(
                "UPDATE github_approval_proposals SET retirement_pending=0,"
                "retirement_retry_after=NULL,updated_at=? "
                "WHERE state='superseded' AND retirement_pending=1 "
                "AND COALESCE(retirement_action,'')!='delete'",
                (time.time(),),
            )
        destination_id = self._destination_identity(adapter, "")
        if destination_id is None:
            return 0
        now = time.time()
        with self._connect() as db:
            rows = db.execute(
                "SELECT proposal_key,clarify_id,session_key,chat_id,message_id,thread_id,state,response,question,"
                "resolution_original_text,resolution_decision_text,resolution_parse_mode,retirement_action "
                "FROM github_approval_proposals "
                "WHERE state IN ('answered','superseded') AND retirement_pending=1 "
                "AND message_id IS NOT NULL AND destination_id=? "
                "AND (retirement_retry_after IS NULL OR retirement_retry_after<=?) "
                "ORDER BY updated_at LIMIT ?", (destination_id, now, max(1, int(limit)))).fetchall()
        retired = 0
        resolve = getattr(adapter, "resolve_clarify_card", None)
        retire = getattr(adapter, "retire_clarify_card", None)
        delete = getattr(adapter, "delete_clarify_card", None)
        if not callable(resolve) and not callable(retire) and not callable(delete):
            return 0
        for row in rows:
            restore = getattr(adapter, "recover_approval_retirement", None)
            if callable(restore):
                restore(
                    str(row["clarify_id"]), str(row["session_key"]), str(row["chat_id"]),
                    str(row["message_id"]),
                    str(row["thread_id"]) if row["thread_id"] is not None else None)
            try:
                if row["retirement_action"] == "delete":
                    if not callable(delete):
                        continue
                    deleted = await _await(delete(str(row["clarify_id"])))
                    if deleted is False:
                        continue
                elif callable(resolve) and row["state"] == "answered":
                    original = row["resolution_original_text"]
                    decision = row["resolution_decision_text"]
                    await _await(resolve(
                        str(row["clarify_id"]),
                        str(original) if original is not None else str(row["question"]),
                        str(decision) if decision is not None else f"GitHub approval recorded: {row['response']}",
                        parse_mode=(str(row["resolution_parse_mode"])
                                    if row["resolution_parse_mode"] is not None else None)))
                else:
                    notice = (f"GitHub approval recorded: {row['response']}"
                              if row["state"] == "answered" else "Superseded GitHub approval")
                    await _await(retire(str(row["clarify_id"]), notice))
            except Exception as exc:
                raw_retry_after = getattr(exc, "retry_after", None)
                total_seconds = getattr(raw_retry_after, "total_seconds", None)
                if callable(total_seconds):
                    raw_retry_after = total_seconds()
                retry_after: Optional[float] = None
                if raw_retry_after is not None:
                    try:
                        retry_after = max(1.0, float(raw_retry_after))
                    except (TypeError, ValueError):
                        pass
                if retry_after is not None:
                    cooldown_started = time.time()
                    with self._connect() as db:
                        db.execute(
                            "UPDATE github_approval_proposals SET retirement_retry_after=?,updated_at=? "
                            "WHERE state IN ('answered','superseded') AND retirement_pending=1 "
                            "AND message_id IS NOT NULL AND destination_id=? "
                            "AND (retirement_retry_after IS NULL OR retirement_retry_after<=?)",
                            (cooldown_started + retry_after, cooldown_started,
                             destination_id, cooldown_started))
                logger.exception("Could not retry stale GitHub approval retirement")
                if retry_after is not None:
                    break
                continue
            with self._connect() as db:
                retired += db.execute(
                    "UPDATE github_approval_proposals SET retirement_pending=0,retirement_action=NULL,"
                    "retirement_retry_after=NULL,updated_at=? "
                    "WHERE proposal_key=? AND state IN ('answered','superseded') "
                    "AND retirement_pending=1",
                    (time.time(), str(row["proposal_key"]))).rowcount
        return retired

    def mark_card_retired(self, clarify_id: str) -> bool:
        """Durably acknowledge that Telegram removed an answered card's keyboard."""
        with self._connect() as db:
            changed = db.execute(
                "UPDATE github_approval_proposals SET retirement_pending=0,updated_at=? "
                "WHERE clarify_id=? AND state='answered' AND retirement_pending=1",
                (time.time(), str(clarify_id)),
            ).rowcount
        return changed == 1

    def recover_callbacks(self, adapter: Any) -> list[dict[str, str]]:
        restore = getattr(adapter, "recover_approval_callback", None)
        state = getattr(adapter, "_clarify_state", None)
        if not callable(restore) and not isinstance(state, dict):
            return []
        destination_id = self._destination_identity(adapter, "")
        if destination_id is None:
            return []
        with self._connect() as db:
            rows = db.execute("SELECT clarify_id,session_key,message_id,chat_id,thread_id FROM github_approval_proposals "
                              "WHERE state='active' AND callback_bound=1 AND message_id IS NOT NULL "
                              "AND destination_id=?", (destination_id,)).fetchall()
        recovered = []
        for row in rows:
            if callable(restore):
                restore(
                    str(row["clarify_id"]), str(row["session_key"]), str(row["chat_id"]),
                    str(row["message_id"]), str(row["thread_id"]) if row["thread_id"] is not None else None)
            else:
                state[str(row["clarify_id"])] = str(row["session_key"])
            recovered.append({key: str(row[key]) for key in ("clarify_id", "session_key", "message_id")})
        return recovered

    def has_callback(self, clarify_id: str, destination_id: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM github_approval_proposals WHERE clarify_id=? AND destination_id=?",
                              (str(clarify_id), str(destination_id))).fetchone() is not None

    def callback_state(self, clarify_id: str, destination_id: str) -> Optional[str]:
        """Return the durable lifecycle state behind a Telegram approval button."""
        with self._connect() as db:
            row = db.execute(
                "SELECT state FROM github_approval_proposals WHERE clarify_id=? AND destination_id=?",
                (str(clarify_id), str(destination_id)),
            ).fetchone()
        return str(row["state"]) if row is not None else None

    def callback_choice(self, clarify_id: str, choice_index: int, destination_id: str) -> Optional[str]:
        if type(choice_index) is not int or not 0 <= choice_index < len(_CHOICES):
            return None
        with self._connect() as db:
            row = db.execute("SELECT state,response FROM github_approval_proposals "
                             "WHERE clarify_id=? AND destination_id=?",
                             (str(clarify_id), str(destination_id))).fetchone()
        if row is None or row["state"] not in {"active", "answered"}:
            return None
        return str(row["response"]) if row["state"] == "answered" else _CHOICES[choice_index]

    def resolve_callback(self, clarify_id: str, choice_index: int, *, approver: str, user_id: str,
                         chat_id: str, thread_id: Optional[str], message_id: str,
                         destination_id: str, resolution_original_text: Optional[str] = None,
                         resolution_decision_text: Optional[str] = None,
                         resolution_parse_mode: Optional[str] = None) -> Optional[ApprovalDecision]:
        if type(choice_index) is not int or not 0 <= choice_index < len(_CHOICES):
            return None
        response, now = _CHOICES[choice_index], time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM github_approval_proposals WHERE clarify_id=? AND destination_id=?",
                             (str(clarify_id), str(destination_id))).fetchone()
            context = (str(user_id), str(chat_id), str(thread_id) if thread_id is not None else None, str(message_id))
            expected = ((str(row["authorized_user_id"]), str(row["chat_id"]),
                         str(row["thread_id"]) if row["thread_id"] is not None else None,
                         str(row["message_id"]))) if row is not None else None
            if row is None or row["state"] not in {"active", "answered"} or context != expected:
                db.commit()
                return None
            if row["state"] == "active":
                if approver != f"telegram:{user_id}":
                    db.commit()
                    return None
                db.execute("UPDATE github_approval_proposals SET state='answered',response=?,approver=?,"
                           "callback_bound=0,retirement_pending=CASE WHEN message_id IS NOT NULL THEN 1 ELSE 0 END,"
                           "retirement_action=CASE WHEN message_id IS NOT NULL THEN 'resolve' ELSE NULL END,"
                           "resolution_original_text=?,resolution_decision_text=?,resolution_parse_mode=?,"
                           "updated_at=? WHERE clarify_id=? AND state='active'",
                           (response, approver, resolution_original_text, resolution_decision_text,
                            resolution_parse_mode, now, str(clarify_id)))
                db.execute("INSERT INTO github_approval_decisions(proposal_key,response,approver,state,decided_at) "
                           "VALUES(?,?,?,'pending',?) ON CONFLICT(proposal_key) DO NOTHING",
                           (str(row["proposal_key"]), response, approver, now))
            decision = db.execute(
                "SELECT d.decision_id,d.proposal_key,d.response,d.approver,"
                "p.resolution_original_text,p.resolution_decision_text,p.resolution_parse_mode "
                "FROM github_approval_decisions d JOIN github_approval_proposals p "
                "ON p.proposal_key=d.proposal_key WHERE d.proposal_key=?",
                (str(row["proposal_key"]),)).fetchone()
            db.commit()
        return ApprovalDecision(int(decision["decision_id"]), str(decision["proposal_key"]),
                                str(decision["response"]), str(decision["approver"]),
                                (str(decision["resolution_original_text"])
                                 if decision["resolution_original_text"] is not None else None),
                                (str(decision["resolution_decision_text"])
                                 if decision["resolution_decision_text"] is not None else None),
                                (str(decision["resolution_parse_mode"])
                                 if decision["resolution_parse_mode"] is not None else None)) if decision else None


    def record_origin_status(self, proposal: ApprovalProposal, status: str) -> bool:
        with self._connect() as db:
            changed = db.execute("UPDATE github_approval_proposals SET origin_status=?,updated_at=? WHERE proposal_key=?",
                                 (str(status), time.time(), proposal.canonical_key())).rowcount
        return changed == 1

    async def retire_item(self, item_key: str, *, status: str, adapter: Any) -> int:
        """Atomically fail-close cards for a terminal item without cosmetic Telegram edits."""
        if status not in {"closed", "merged", "superseded"}:
            raise ValueError("status must be closed, merged, or superseded")
        if not isinstance(item_key, str) or _ITEM_KEY_RE.fullmatch(item_key) is None:
            raise ValueError("item_key must be owner/repo#number with a positive canonical number")
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE github_approval_proposals SET origin_status=?,updated_at=? WHERE item_key=?",
                (status, now, item_key),
            )
            changed = db.execute(
                "UPDATE github_approval_proposals SET state='superseded',callback_bound=0,"
                "lease_expires_at=NULL,retirement_pending=0,"
                "retirement_retry_after=NULL,updated_at=? WHERE item_key=? "
                "AND state IN ('pending_notification','dispatching','delivery_unknown','active','answered')",
                (now, item_key),
            ).rowcount
            db.execute("UPDATE github_approval_decisions SET state='stale' "
                       "WHERE state IN ('pending','claimed') AND proposal_key IN "
                       "(SELECT proposal_key FROM github_approval_proposals WHERE item_key=? AND state='superseded')",
                       (item_key,))
            db.commit()
        return int(changed)

    def observe_key(self, proposal_key: str) -> Optional[dict]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM github_approval_proposals WHERE proposal_key=?", (str(proposal_key),)).fetchone()
        return dict(row) if row is not None else None

    def observe(self, proposal: ApprovalProposal) -> Optional[dict]:
        return self.observe_key(proposal.canonical_key())


def default_broker(*, instance_id: str, create: bool = True) -> Optional[GitHubApprovalBroker]:
    from hermes_constants import get_process_hermes_home
    path = get_process_hermes_home() / "state" / "github-approval-broker.db"
    if not create and not path.exists():
        return None
    return GitHubApprovalBroker(path, instance_id=instance_id)
