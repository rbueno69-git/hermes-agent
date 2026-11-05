"""Durable, process-safe authority for interactive approval cards."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class ApprovalCard:
    card_id: int
    binding_key: str
    clarify_id: str
    platform: str
    chat_id: str
    message_id: Optional[str]
    status: str


@dataclass(frozen=True)
class ApprovalClaim:
    action: str
    binding_key: str
    card_id: Optional[int] = None
    clarify_id: Optional[str] = None
    message_id: Optional[str] = None
    response: Optional[str] = None
    superseded: Tuple[ApprovalCard, ...] = ()


@dataclass(frozen=True)
class ApprovalObservation:
    status: str
    response: Optional[str] = None


def canonical_binding_key(binding: Dict[str, str]) -> str:
    required = {"item_key", "head_sha", "contract_digest_sha256", "scope"}
    if type(binding) is not dict or set(binding) != required:
        raise ValueError("approval binding must contain the exact canonical fields")
    if any(not isinstance(binding[key], str) or not binding[key] for key in required):
        raise ValueError("approval binding fields must be non-empty strings")
    encoded = json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ApprovalCardLedger:
    """SQLite-backed approval/card lifecycle shared across sessions and restarts."""

    def __init__(self, path: Path, *, instance_id: str):
        self.path = Path(path)
        self.instance_id = str(instance_id)
        if not self.instance_id:
            raise ValueError("instance_id is required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS approval_bindings (
                    binding_key TEXT PRIMARY KEY,
                    item_key TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    contract_digest_sha256 TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','answered','superseded','failed')),
                    response TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approval_cards (
                    card_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    binding_key TEXT NOT NULL REFERENCES approval_bindings(binding_key),
                    clarify_id TEXT NOT NULL UNIQUE,
                    session_key TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT,
                    choices_json TEXT,
                    status TEXT NOT NULL CHECK(status IN ('delivering','active','answered','superseded','failed')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS approval_cards_binding_status
                    ON approval_cards(binding_key, status);
                CREATE INDEX IF NOT EXISTS approval_bindings_item_status
                    ON approval_bindings(item_key, status);
                """
            )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(approval_cards)")}
            if "choices_json" not in columns:
                connection.execute("ALTER TABLE approval_cards ADD COLUMN choices_json TEXT")
        os.chmod(self.path, 0o600)

    @staticmethod
    def _card(row: sqlite3.Row, *, status: Optional[str] = None) -> ApprovalCard:
        return ApprovalCard(
            card_id=int(row["card_id"]),
            binding_key=str(row["binding_key"]),
            clarify_id=str(row["clarify_id"]),
            platform=str(row["platform"]),
            chat_id=str(row["chat_id"]),
            message_id=str(row["message_id"]) if row["message_id"] is not None else None,
            status=status or str(row["status"]),
        )

    def claim(
        self,
        binding: Dict[str, str],
        *,
        platform: str,
        chat_id: str,
        clarify_id: str,
        session_key: str,
        choices: Optional[List[str]] = None,
        is_active: Optional[Callable[[str], bool]] = None,
    ) -> ApprovalClaim:
        binding_key = canonical_binding_key(binding)
        if choices is not None and (
            type(choices) is not list or not choices
            or any(not isinstance(choice, str) or not choice for choice in choices)
        ):
            raise ValueError("approval choices must be a non-empty list of strings")
        choices_json = json.dumps(choices, ensure_ascii=False, separators=(",", ":")) if choices else None
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exact = connection.execute(
                "SELECT status, response FROM approval_bindings WHERE binding_key = ?", (binding_key,)
            ).fetchone()
            if exact is not None and exact["status"] == "answered":
                card = connection.execute(
                    "SELECT * FROM approval_cards WHERE binding_key = ? ORDER BY card_id DESC LIMIT 1",
                    (binding_key,),
                ).fetchone()
                connection.commit()
                return ApprovalClaim(
                    action="reuse_answered",
                    binding_key=binding_key,
                    clarify_id=str(card["clarify_id"]) if card is not None else None,
                    message_id=(str(card["message_id"]) if card is not None and card["message_id"] is not None else None),
                    response=str(exact["response"] or ""),
                )

            active = connection.execute(
                """
                SELECT * FROM approval_cards
                WHERE binding_key = ? AND status IN ('delivering','active')
                ORDER BY card_id DESC LIMIT 1
                """,
                (binding_key,),
            ).fetchone()
            if active is not None:
                connection.commit()
                return ApprovalClaim(
                    action="reuse_active",
                    binding_key=binding_key,
                    card_id=int(active["card_id"]),
                    clarify_id=str(active["clarify_id"]),
                    message_id=str(active["message_id"]) if active["message_id"] is not None else None,
                )

            old_rows = connection.execute(
                """
                SELECT c.* FROM approval_cards c
                JOIN approval_bindings b ON b.binding_key = c.binding_key
                WHERE b.item_key = ? AND c.status IN ('delivering','active')
                ORDER BY c.card_id
                """,
                (binding["item_key"],),
            ).fetchall()
            delivering = next((row for row in old_rows if row["status"] == "delivering"), None)
            if delivering is not None:
                connection.commit()
                return ApprovalClaim(
                    action="wait_predecessor",
                    binding_key=binding_key,
                    card_id=int(delivering["card_id"]),
                    clarify_id=str(delivering["clarify_id"]),
                )
            if old_rows:
                old_ids = [int(row["card_id"]) for row in old_rows]
                connection.executemany(
                    "UPDATE approval_cards SET status = 'superseded', updated_at = ? WHERE card_id = ?",
                    [(now, card_id) for card_id in old_ids],
                )
                old_bindings = {str(row["binding_key"]) for row in old_rows if row["binding_key"] != binding_key}
                connection.executemany(
                    "UPDATE approval_bindings SET status = 'superseded', updated_at = ? WHERE binding_key = ?",
                    [(now, old_binding) for old_binding in old_bindings],
                )

            connection.execute(
                """
                INSERT INTO approval_bindings(
                    binding_key, item_key, head_sha, contract_digest_sha256, scope, status, response, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?)
                ON CONFLICT(binding_key) DO UPDATE SET
                    status = 'pending', response = NULL, updated_at = excluded.updated_at
                """,
                (
                    binding_key,
                    binding["item_key"],
                    binding["head_sha"],
                    binding["contract_digest_sha256"],
                    binding["scope"],
                    now,
                ),
            )
            cursor = connection.execute(
                """
                INSERT INTO approval_cards(
                    binding_key, clarify_id, session_key, instance_id, platform, chat_id,
                    message_id, choices_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 'delivering', ?, ?)
                """,
                (binding_key, clarify_id, session_key, self.instance_id, platform, chat_id,
                 choices_json, now, now),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("approval card insert returned no identity")
            card_id = int(cursor.lastrowid)
            connection.commit()
            return ApprovalClaim(
                action="send",
                binding_key=binding_key,
                card_id=card_id,
                clarify_id=clarify_id,
                superseded=tuple(self._card(row, status="superseded") for row in old_rows),
            )

    def activate(self, card_id: Optional[int], message_id: str) -> None:
        if card_id is None:
            raise ValueError("card_id is required")
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE approval_cards SET
                    status = CASE WHEN status = 'answered' THEN 'answered' ELSE 'active' END,
                    message_id = ?, updated_at = ?
                WHERE card_id = ? AND status IN ('delivering', 'answered')
                """,
                (str(message_id), time.time(), int(card_id)),
            ).rowcount
            if changed != 1:
                raise RuntimeError("approval card is not deliverable")

    def fail(self, card_id: Optional[int]) -> None:
        if card_id is None:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT binding_key, status FROM approval_cards WHERE card_id = ?", (int(card_id),)
            ).fetchone()
            if row is None or row["status"] not in ("delivering", "active"):
                connection.commit()
                return
            now = time.time()
            changed = connection.execute(
                """
                UPDATE approval_cards SET status = 'failed', updated_at = ?
                WHERE card_id = ? AND status IN ('delivering', 'active')
                """,
                (now, int(card_id)),
            ).rowcount
            if changed == 1:
                connection.execute(
                    """
                    UPDATE approval_bindings SET status = 'failed', updated_at = ?
                    WHERE binding_key = ? AND status = 'pending'
                    """,
                    (now, str(row["binding_key"])),
                )
            connection.commit()

    def fail_clarify(self, clarify_id: str) -> bool:
        """Expire one active delivery so a later identical claim may safely publish again."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT card_id, binding_key, status FROM approval_cards WHERE clarify_id = ?", (clarify_id,)
            ).fetchone()
            if row is None or row["status"] not in ("delivering", "active"):
                connection.commit()
                return False
            now = time.time()
            connection.execute(
                "UPDATE approval_cards SET status = 'failed', updated_at = ? WHERE card_id = ?",
                (now, int(row["card_id"])),
            )
            connection.execute(
                """
                UPDATE approval_bindings SET status = 'failed', updated_at = ?
                WHERE binding_key = ? AND status = 'pending'
                """,
                (now, str(row["binding_key"])),
            )
            connection.commit()
            return True

    def answer(self, clarify_id: str, response: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT card_id, binding_key, status FROM approval_cards WHERE clarify_id = ?", (clarify_id,)
            ).fetchone()
            if row is None or row["status"] not in ("delivering", "active"):
                connection.commit()
                return False
            now = time.time()
            connection.execute(
                "UPDATE approval_cards SET status = 'answered', updated_at = ? WHERE card_id = ?",
                (now, int(row["card_id"])),
            )
            connection.execute(
                """
                UPDATE approval_bindings SET status = 'answered', response = ?, updated_at = ?
                WHERE binding_key = ?
                """,
                (str(response), now, str(row["binding_key"])),
            )
            connection.commit()
            return True

    def answer_choice(self, clarify_id: str, choice_index: int) -> Optional[str]:
        """Resolve an active card from its persisted choice list after a process restart."""
        if type(choice_index) is not int or choice_index < 0:
            return None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT card_id, binding_key, status, choices_json FROM approval_cards WHERE clarify_id = ?",
                (clarify_id,),
            ).fetchone()
            if row is None or row["status"] not in ("delivering", "active") or not row["choices_json"]:
                connection.commit()
                return None
            try:
                choices = json.loads(str(row["choices_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                connection.rollback()
                return None
            if type(choices) is not list or not (0 <= choice_index < len(choices)):
                connection.commit()
                return None
            response = choices[choice_index]
            if not isinstance(response, str) or not response:
                connection.rollback()
                return None
            now = time.time()
            connection.execute(
                "UPDATE approval_cards SET status = 'answered', updated_at = ? WHERE card_id = ?",
                (now, int(row["card_id"])),
            )
            connection.execute(
                "UPDATE approval_bindings SET status = 'answered', response = ?, updated_at = ? WHERE binding_key = ?",
                (response, now, str(row["binding_key"])),
            )
            connection.commit()
            return response

    def response_for_binding(self, binding_key: str) -> Optional[str]:
        """Return the submitted decision only after the binding is durably answered."""
        observation = self.observe(binding_key)
        if observation.status != "answered":
            return None
        return observation.response or ""

    def observe(self, binding_key: str) -> ApprovalObservation:
        """Return the durable typed lifecycle state for one canonical binding."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, response FROM approval_bindings WHERE binding_key = ?", (binding_key,)
            ).fetchone()
        if row is None:
            return ApprovalObservation(status="missing")
        status = str(row["status"])
        response = str(row["response"] or "") if status == "answered" else None
        return ApprovalObservation(status=status, response=response)
