"""Durable, process-safe authority for interactive approval cards."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


RECOVERY_PREFIX = "approval-rcv-"
LEGACY_RECOVERY_PREFIX = "approval-recovery-"
_DEFAULT_EXPIRY = object()


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
    owner_instance_id: Optional[str] = None
    delivery_generation: Optional[str] = None
    expires_at: Optional[float] = None
    superseded: Tuple[ApprovalCard, ...] = ()


@dataclass(frozen=True)
class ApprovalObservation:
    status: str
    response: Optional[str] = None


def canonical_binding_key(binding: Dict[str, str]) -> str:
    required = {
        "item_key", "head_sha", "contract_digest_sha256", "scope",
        "decision_digest_sha256",
    }
    if type(binding) is not dict or set(binding) != required:
        raise ValueError("approval binding must contain the exact canonical fields")
    if any(not isinstance(binding[key], str) or not binding[key] for key in required):
        raise ValueError("approval binding fields must be non-empty strings")
    encoded = json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ApprovalCardLedger:
    """SQLite-backed approval/card lifecycle shared across sessions and restarts."""

    def __init__(self, path: Path, *, instance_id: str, recovery_lease_seconds: float = 30.0):
        self.path = Path(path)
        self.instance_id = str(instance_id)
        if not self.instance_id:
            raise ValueError("instance_id is required")
        self.recovery_lease_seconds = float(recovery_lease_seconds)
        if self.recovery_lease_seconds < 0:
            raise ValueError("recovery_lease_seconds must be non-negative")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self, *, timeout: float = 5.0) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect(timeout=30.0) as connection:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS approval_bindings (
                    binding_key TEXT PRIMARY KEY,
                    item_key TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    contract_digest_sha256 TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    decision_digest_sha256 TEXT NOT NULL,
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
                    expires_at REAL,
                    status TEXT NOT NULL CHECK(status IN ('delivering','active','answered','superseded','failed')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS approval_cards_binding_status
                    ON approval_cards(binding_key, status);
                CREATE INDEX IF NOT EXISTS approval_bindings_item_status
                    ON approval_bindings(item_key, status);
                CREATE TABLE IF NOT EXISTS approval_recoveries (
                    card_id INTEGER PRIMARY KEY REFERENCES approval_cards(card_id),
                    clarify_id TEXT NOT NULL UNIQUE,
                    instance_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT,
                    generation TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('delivering','active','answered','failed')),
                    response TEXT,
                    continuation_json TEXT,
                    expires_at REAL,
                    continuation_claim_id TEXT,
                    continuation_claimed_at REAL,
                    updated_at REAL NOT NULL
                );
                """
            )
            binding_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(approval_bindings)")
            }
            if "decision_digest_sha256" not in binding_columns:
                connection.execute(
                    "ALTER TABLE approval_bindings ADD COLUMN decision_digest_sha256 TEXT"
                )
            connection.execute(
                "UPDATE approval_bindings SET decision_digest_sha256 = 'legacy:' || binding_key "
                "WHERE decision_digest_sha256 IS NULL"
            )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(approval_cards)")}
            if "choices_json" not in columns:
                connection.execute("ALTER TABLE approval_cards ADD COLUMN choices_json TEXT")
            if "expires_at" not in columns:
                connection.execute("ALTER TABLE approval_cards ADD COLUMN expires_at REAL")
                connection.execute(
                    "UPDATE approval_cards SET status = 'failed' "
                    "WHERE expires_at IS NULL AND status IN ('delivering','active')"
                )
            recovery_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(approval_recoveries)")
            }
            if "generation" not in recovery_columns:
                connection.execute("ALTER TABLE approval_recoveries ADD COLUMN generation TEXT")
            missing_generations = connection.execute(
                "SELECT card_id, clarify_id FROM approval_recoveries WHERE generation IS NULL"
            ).fetchall()
            for row in missing_generations:
                persisted_id = str(row["clarify_id"])
                token = (
                    persisted_id[len(RECOVERY_PREFIX):]
                    if persisted_id.startswith(RECOVERY_PREFIX)
                    else ""
                )
                generation = (
                    token
                    if len(token) == 32 and all(char in "0123456789abcdef" for char in token)
                    else secrets.token_hex(16)
                )
                connection.execute(
                    "UPDATE approval_recoveries SET generation = ? "
                    "WHERE card_id = ? AND generation IS NULL",
                    (generation, int(row["card_id"])),
                )
            if "continuation_json" not in recovery_columns:
                connection.execute("ALTER TABLE approval_recoveries ADD COLUMN continuation_json TEXT")
            if "expires_at" not in recovery_columns:
                connection.execute("ALTER TABLE approval_recoveries ADD COLUMN expires_at REAL")
            if "continuation_claim_id" not in recovery_columns:
                connection.execute(
                    "ALTER TABLE approval_recoveries ADD COLUMN continuation_claim_id TEXT"
                )
            if "continuation_claimed_at" not in recovery_columns:
                connection.execute(
                    "ALTER TABLE approval_recoveries ADD COLUMN continuation_claimed_at REAL"
                )
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
        expires_at: object = _DEFAULT_EXPIRY,
    ) -> ApprovalClaim:
        binding_key = canonical_binding_key(binding)
        now = time.time()
        if expires_at is _DEFAULT_EXPIRY:
            normalized_expires_at: Optional[float] = now + 28800.0
        elif expires_at is None:
            normalized_expires_at = None
        else:
            normalized_expires_at = float(expires_at)
        expires_at = normalized_expires_at
        if choices is not None and (
            type(choices) is not list or not choices
            or any(not isinstance(choice, str) or not choice for choice in choices)
        ):
            raise ValueError("approval choices must be a non-empty list of strings")
        choices_json = json.dumps(choices, ensure_ascii=False, separators=(",", ":")) if choices else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exact = connection.execute(
                """
                SELECT item_key, head_sha, contract_digest_sha256, scope,
                       decision_digest_sha256, status, response
                FROM approval_bindings WHERE binding_key = ?
                """,
                (binding_key,),
            ).fetchone()
            if exact is not None and any(
                exact[field] != binding[field]
                for field in (
                    "item_key", "head_sha", "contract_digest_sha256", "scope",
                    "decision_digest_sha256",
                )
            ):
                connection.rollback()
                raise ValueError("persisted approval binding does not match canonical identity")
            if exact is not None and exact["status"] == "superseded":
                connection.commit()
                return ApprovalClaim(action="superseded", binding_key=binding_key)
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
            if (
                active is not None
                and active["expires_at"] is not None
                and float(active["expires_at"]) <= now
            ):
                connection.execute(
                    "UPDATE approval_cards SET status = 'failed', updated_at = ? WHERE card_id = ?",
                    (now, int(active["card_id"])),
                )
                active = None
            if active is not None:
                connection.commit()
                return ApprovalClaim(
                    action="reuse_active",
                    binding_key=binding_key,
                    card_id=int(active["card_id"]),
                    clarify_id=str(active["clarify_id"]),
                    message_id=str(active["message_id"]) if active["message_id"] is not None else None,
                    owner_instance_id=str(active["instance_id"]),
                )

            connection.execute(
                """
                UPDATE approval_cards SET status = 'failed', updated_at = ?
                WHERE card_id IN (
                    SELECT c.card_id FROM approval_cards c
                    JOIN approval_bindings b ON b.binding_key = c.binding_key
                    WHERE b.item_key = ? AND c.status IN ('delivering','active')
                      AND c.expires_at IS NOT NULL AND c.expires_at <= ?
                )
                """,
                (now, binding["item_key"], now),
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
                    owner_instance_id=str(delivering["instance_id"]),
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
                    binding_key, item_key, head_sha, contract_digest_sha256, scope,
                    decision_digest_sha256, status, response, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?)
                ON CONFLICT(binding_key) DO UPDATE SET
                    status = 'pending', response = NULL, updated_at = excluded.updated_at
                """,
                (
                    binding_key,
                    binding["item_key"],
                    binding["head_sha"],
                    binding["contract_digest_sha256"],
                    binding["scope"],
                    binding["decision_digest_sha256"],
                    now,
                ),
            )
            cursor = connection.execute(
                """
                INSERT INTO approval_cards(
                    binding_key, clarify_id, session_key, instance_id, platform, chat_id,
                    message_id, choices_json, expires_at, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 'delivering', ?, ?)
                """,
                (binding_key, clarify_id, session_key, self.instance_id, platform, chat_id,
                 choices_json, expires_at, now, now),
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
                expires_at=expires_at,
                superseded=tuple(self._card(row, status="superseded") for row in old_rows),
            )

    @staticmethod
    def _continuation_json(continuation: Optional[dict]) -> Optional[str]:
        if continuation is None:
            return None
        if type(continuation) is not dict:
            raise ValueError("approval recovery continuation must be a dictionary")
        session_chat_id = continuation.get("session_chat_id")
        if not isinstance(session_chat_id, str) or not session_chat_id:
            raise ValueError("approval recovery continuation requires session_chat_id")
        if type(continuation.get("delivery")) is not dict:
            raise ValueError("approval recovery continuation requires delivery")
        try:
            encoded = json.dumps(
                continuation, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("approval recovery continuation must be JSON-safe") from exc
        if len(encoded.encode("utf-8")) > 32768:
            raise ValueError("approval recovery continuation is too large")
        return encoded

    def claim_recovery(
        self,
        card_id: Optional[int],
        *,
        platform: str,
        chat_id: str,
        continuation: Optional[dict] = None,
        expires_at: Optional[float] = None,
    ) -> ApprovalClaim:
        """Elect exactly one publisher for a non-authorizing delivery recovery control."""
        if card_id is None:
            raise ValueError("card_id is required")
        generation = secrets.token_hex(16)
        clarify_id = f"{RECOVERY_PREFIX}{generation}"
        continuation_json = self._continuation_json(continuation)
        if expires_at is not None:
            expires_at = float(expires_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            card = connection.execute(
                "SELECT binding_key, instance_id, status FROM approval_cards WHERE card_id = ?",
                (int(card_id),),
            ).fetchone()
            if (
                card is None or card["status"] != "delivering"
                or str(card["instance_id"]) == self.instance_id
            ):
                connection.rollback()
                raise ValueError("approval card is not an orphaned foreign delivery")
            existing = connection.execute(
                "SELECT * FROM approval_recoveries WHERE card_id = ?", (int(card_id),)
            ).fetchone()
            if existing is not None:
                existing_expires_at = (
                    float(existing["expires_at"]) if existing["expires_at"] is not None else None
                )
                now = time.time()
                if (
                    existing["status"] == "failed"
                    and existing["response"] == "__expired__"
                ) or (
                    existing["status"] in ("delivering", "active", "failed")
                    and existing_expires_at is not None
                    and existing_expires_at <= now
                ):
                    connection.execute(
                        """
                        UPDATE approval_recoveries
                        SET status = 'failed', response = '__expired__', updated_at = ?
                        WHERE card_id = ? AND status IN ('delivering','active','failed')
                        """,
                        (now, int(card_id)),
                    )
                    connection.commit()
                    return ApprovalClaim(
                        action="expired", binding_key=str(card["binding_key"]),
                        card_id=int(card_id), clarify_id=str(existing["clarify_id"]),
                        message_id=(
                            str(existing["message_id"])
                            if existing["message_id"] is not None else None
                        ),
                        owner_instance_id=str(existing["instance_id"]),
                        delivery_generation=str(existing["generation"]),
                        expires_at=existing_expires_at,
                    )
                if existing["status"] == "failed":
                    persisted_continuation = (
                        str(existing["continuation_json"])
                        if existing["continuation_json"] is not None else continuation_json
                    )
                    persisted_expires_at = (
                        existing_expires_at if existing_expires_at is not None else expires_at
                    )
                    connection.execute(
                        """
                        UPDATE approval_recoveries SET instance_id = ?, platform = ?, chat_id = ?,
                            clarify_id = ?, message_id = NULL, generation = ?, status = 'delivering',
                            response = NULL, continuation_json = ?, expires_at = ?, updated_at = ?
                        WHERE card_id = ? AND status = 'failed'
                        """,
                        (self.instance_id, str(platform), str(chat_id), clarify_id, generation,
                         persisted_continuation, persisted_expires_at, now, int(card_id)),
                    )
                    connection.commit()
                    return ApprovalClaim(
                        action="send", binding_key=str(card["binding_key"]),
                        card_id=int(card_id), clarify_id=clarify_id,
                        owner_instance_id=self.instance_id,
                        delivery_generation=generation,
                        expires_at=persisted_expires_at,
                    )
                connection.commit()
                action = "reuse_answered" if existing["status"] == "answered" else "reuse_active"
                return ApprovalClaim(
                    action=action,
                    binding_key=str(card["binding_key"]),
                    card_id=int(card_id),
                    clarify_id=str(existing["clarify_id"]),
                    message_id=(str(existing["message_id"]) if existing["message_id"] is not None else None),
                    response=(str(existing["response"]) if existing["response"] is not None else None),
                    owner_instance_id=str(existing["instance_id"]),
                    delivery_generation=str(existing["generation"]),
                    expires_at=(float(existing["expires_at"]) if existing["expires_at"] is not None else None),
                )
            now = time.time()
            initial_status = (
                "failed" if expires_at is not None and expires_at <= now else "delivering"
            )
            connection.execute(
                """
                INSERT INTO approval_recoveries(
                    card_id, clarify_id, instance_id, platform, chat_id,
                    message_id, generation, status, response, continuation_json, expires_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (int(card_id), clarify_id, self.instance_id, str(platform), str(chat_id),
                 generation, initial_status,
                 "__expired__" if initial_status == "failed" else None,
                 continuation_json, expires_at, now),
            )
            connection.commit()
            return ApprovalClaim(
                action="expired" if initial_status == "failed" else "send", binding_key=str(card["binding_key"]),
                card_id=int(card_id), clarify_id=clarify_id,
                owner_instance_id=self.instance_id,
                delivery_generation=generation,
                expires_at=expires_at,
            )

    def activate_recovery(
        self, card_id: Optional[int], generation: Optional[str], message_id: str,
    ) -> bool:
        if card_id is None or not generation:
            return False
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE approval_recoveries
                SET status = 'failed', response = '__expired__', updated_at = ?
                WHERE card_id = ? AND generation = ?
                  AND status IN ('delivering','active')
                  AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (now, int(card_id), str(generation), now),
            )
            changed = connection.execute(
                """
                UPDATE approval_recoveries SET
                    status = CASE WHEN status = 'answered' THEN 'answered' ELSE 'active' END,
                    message_id = ?, updated_at = ?
                WHERE card_id = ? AND generation = ? AND status IN ('delivering','answered')
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (str(message_id), now, int(card_id), str(generation), now),
            ).rowcount
            connection.commit()
            return changed == 1

    def fail_recovery(self, card_id: Optional[int], generation: Optional[str]) -> None:
        if card_id is None or not generation:
            return
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE approval_recoveries SET status = 'failed', updated_at = ?
                WHERE card_id = ? AND generation = ? AND status IN ('delivering','active')
                """,
                (time.time(), int(card_id), str(generation)),
            )

    def expire_recovery(
        self, card_id: Optional[int], generation: Optional[str], *, now: Optional[float] = None,
    ) -> bool:
        if card_id is None or not generation:
            return False
        observed_at = time.time() if now is None else float(now)
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE approval_recoveries
                SET status = 'failed', response = '__expired__', updated_at = ?
                WHERE card_id = ? AND generation = ?
                  AND status IN ('delivering','active')
                  AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (observed_at, int(card_id), str(generation), observed_at),
            ).rowcount
        return changed == 1

    def recoverable_delivery_contexts(self, *, now: Optional[float] = None) -> List[dict]:
        observed_at = time.time() if now is None else float(now)
        contexts: List[dict] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE approval_recoveries SET status = 'failed', updated_at = ?
                WHERE status IN ('delivering','active')
                  AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (observed_at, observed_at),
            )
            rows = connection.execute(
                """
                SELECT continuation_json FROM approval_recoveries
                WHERE status IN ('delivering','active','answered')
                  AND continuation_json IS NOT NULL
                ORDER BY card_id
                """
            ).fetchall()
            connection.commit()
        for row in rows:
            try:
                context = json.loads(str(row["continuation_json"]))
                self._continuation_json(context)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            contexts.append(context)
        return contexts

    def claim_answered_recovery_continuations(
        self, *, now: Optional[float] = None, lease_seconds: float = 30.0, limit: int = 16,
    ) -> List[dict]:
        observed_at = time.time() if now is None else float(now)
        stale_before = observed_at - max(0.0, float(lease_seconds))
        claimed: List[dict] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT card_id, clarify_id, generation, response, continuation_json
                FROM approval_recoveries
                WHERE status = 'answered' AND continuation_json IS NOT NULL
                  AND (continuation_claim_id IS NULL OR continuation_claimed_at <= ?)
                ORDER BY card_id LIMIT ?
                """,
                (stale_before, max(1, int(limit))),
            ).fetchall()
            for row in rows:
                claim_id = secrets.token_hex(16)
                changed = connection.execute(
                    """
                    UPDATE approval_recoveries
                    SET continuation_claim_id = ?, continuation_claimed_at = ?
                    WHERE card_id = ? AND generation = ? AND status = 'answered'
                      AND continuation_json IS NOT NULL
                      AND (continuation_claim_id IS NULL OR continuation_claimed_at <= ?)
                    """,
                    (
                        claim_id, observed_at, int(row["card_id"]), str(row["generation"]),
                        stale_before,
                    ),
                ).rowcount
                if changed != 1:
                    continue
                try:
                    context = json.loads(str(row["continuation_json"]))
                    self._continuation_json(context)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                claimed.append({
                    "card_id": int(row["card_id"]),
                    "clarify_id": str(row["clarify_id"]),
                    "generation": str(row["generation"]),
                    "response": str(row["response"] or ""),
                    "claim_id": claim_id,
                    "context": context,
                })
            connection.commit()
        return claimed

    def complete_recovery_continuation(
        self, card_id: int, generation: str, claim_id: str,
    ) -> bool:
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE approval_recoveries
                SET continuation_json = NULL, continuation_claim_id = NULL,
                    continuation_claimed_at = NULL, updated_at = ?
                WHERE card_id = ? AND generation = ? AND continuation_claim_id = ?
                  AND status = 'answered'
                """,
                (time.time(), int(card_id), str(generation), str(claim_id)),
            ).rowcount
        return changed == 1

    def release_recovery_continuation_claim(
        self, card_id: int, generation: str, claim_id: str,
    ) -> bool:
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE approval_recoveries
                SET continuation_claim_id = NULL, continuation_claimed_at = NULL
                WHERE card_id = ? AND generation = ? AND continuation_claim_id = ?
                  AND status = 'answered'
                """,
                (int(card_id), str(generation), str(claim_id)),
            ).rowcount
        return changed == 1

    def observe_recovery(self, card_id: Optional[int]) -> ApprovalObservation:
        if card_id is None:
            return ApprovalObservation(status="missing")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, response FROM approval_recoveries WHERE card_id = ?",
                (int(card_id),),
            ).fetchone()
        if row is None:
            return ApprovalObservation(status="missing")
        return ApprovalObservation(
            status=str(row["status"]),
            response=(str(row["response"]) if row["response"] is not None else None),
        )

    def activate(self, card_id: Optional[int], message_id: str) -> bool:
        if card_id is None:
            raise ValueError("card_id is required")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE approval_cards SET status = 'failed', updated_at = ?
                WHERE card_id = ? AND status = 'delivering'
                  AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (now, int(card_id), now),
            )
            changed = connection.execute(
                """
                UPDATE approval_cards SET
                    status = CASE WHEN status = 'answered' THEN 'answered' ELSE 'active' END,
                    message_id = ?, updated_at = ?
                WHERE card_id = ?
                  AND (status = 'answered' OR (
                      status = 'delivering' AND (expires_at IS NULL OR expires_at > ?)
                  ))
                """,
                (str(message_id), now, int(card_id), now),
            ).rowcount
            connection.commit()
            return changed == 1

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
                "SELECT card_id, binding_key, status, expires_at FROM approval_cards WHERE clarify_id = ?", (clarify_id,)
            ).fetchone()
            if row is None or row["status"] not in ("delivering", "active"):
                connection.commit()
                return False
            now = time.time()
            if row["expires_at"] is not None and float(row["expires_at"]) <= now:
                connection.execute(
                    "UPDATE approval_cards SET status = 'failed', updated_at = ? WHERE card_id = ?",
                    (now, int(row["card_id"])),
                )
                connection.commit()
                return False
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
            matched_prefix = next(
                (prefix for prefix in (RECOVERY_PREFIX, LEGACY_RECOVERY_PREFIX)
                 if clarify_id.startswith(prefix)),
                None,
            )
            if matched_prefix is not None:
                recovery_token = clarify_id[len(matched_prefix):]
                token_parts = recovery_token.rsplit("-", 1)
                generation: Optional[str]
                card_token: Optional[str]
                is_hex_generation = (
                    len(recovery_token) == 32
                    and all(char in "0123456789abcdef" for char in recovery_token)
                )
                if matched_prefix == RECOVERY_PREFIX and is_hex_generation:
                    card_token = None
                    generation = recovery_token
                elif recovery_token.isdigit():
                    card_token = recovery_token
                    generation = None
                elif (
                    len(token_parts) == 2
                    and token_parts[0].isdigit()
                    and len(token_parts[1]) == 32
                    and all(char in "0123456789abcdef" for char in token_parts[1])
                ):
                    card_token, generation = token_parts
                else:
                    connection.commit()
                    return None
                if choice_index not in (0, 1):
                    connection.commit()
                    return None
                recovery = connection.execute(
                    """
                    SELECT r.card_id, r.generation, r.expires_at, c.binding_key,
                           c.status AS card_status, r.status AS recovery_status, r.response
                    FROM approval_recoveries r
                    JOIN approval_cards c ON c.card_id = r.card_id
                    WHERE r.clarify_id = ?
                      AND (? IS NULL OR r.card_id = ?)
                      AND (? IS NULL OR r.generation = ?)
                    """,
                    (
                        clarify_id,
                        card_token,
                        int(card_token) if card_token is not None else None,
                        generation,
                        generation,
                    ),
                ).fetchone()
                if recovery is not None:
                    generation = str(recovery["generation"])
                if recovery is None:
                    connection.commit()
                    return None
                if recovery["recovery_status"] == "answered":
                    connection.commit()
                    return str(recovery["response"] or "") or None
                if recovery["recovery_status"] not in ("delivering", "active"):
                    connection.commit()
                    return None
                now = time.time()
                if (
                    recovery["expires_at"] is not None
                    and float(recovery["expires_at"]) <= now
                ):
                    connection.execute(
                        """
                        UPDATE approval_recoveries
                        SET status = 'failed', response = '__expired__', updated_at = ?
                        WHERE card_id = ? AND generation = ? AND clarify_id = ?
                          AND status IN ('delivering','active')
                        """,
                        (now, int(recovery["card_id"]), generation, clarify_id),
                    )
                    connection.commit()
                    return None
                response = "Reemitir aprovação" if choice_index == 0 else "Manter bloqueado"
                changed = connection.execute(
                    """
                    UPDATE approval_recoveries SET status = 'answered', response = ?, updated_at = ?
                    WHERE card_id = ? AND generation = ? AND clarify_id = ?
                      AND status IN ('delivering','active')
                      AND (expires_at IS NULL OR expires_at > ?)
                    """,
                    (response, now, int(recovery["card_id"]), generation, clarify_id, now),
                ).rowcount
                if changed != 1:
                    connection.rollback()
                    return None
                if choice_index == 1:
                    connection.commit()
                    return response
                if recovery["card_status"] == "delivering":
                    connection.execute(
                        "UPDATE approval_cards SET status = 'failed', updated_at = ? WHERE card_id = ?",
                        (now, int(recovery["card_id"])),
                    )
                    connection.execute(
                        """
                        UPDATE approval_bindings SET status = 'failed', updated_at = ?
                        WHERE binding_key = ? AND status = 'pending'
                        """,
                        (now, str(recovery["binding_key"])),
                    )
                    connection.commit()
                    return response
                connection.rollback()
                return None
            row = connection.execute(
                "SELECT card_id, binding_key, status, choices_json, expires_at "
                "FROM approval_cards WHERE clarify_id = ?",
                (clarify_id,),
            ).fetchone()
            if row is None or row["status"] not in ("delivering", "active") or not row["choices_json"]:
                connection.commit()
                return None
            now = time.time()
            if row["expires_at"] is not None and float(row["expires_at"]) <= now:
                connection.execute(
                    "UPDATE approval_cards SET status = 'failed', updated_at = ? WHERE card_id = ?",
                    (now, int(row["card_id"])),
                )
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

    def observe_clarify(self, clarify_id: str) -> ApprovalObservation:
        """Return the durable binding state addressed by a rendered clarify card."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT b.status, b.response
                FROM approval_cards AS c
                JOIN approval_bindings AS b ON b.binding_key = c.binding_key
                WHERE c.clarify_id = ?
                """,
                (clarify_id,),
            ).fetchone()
        if row is None:
            return ApprovalObservation(status="missing")
        status = str(row["status"])
        response = str(row["response"] or "") if status == "answered" else None
        return ApprovalObservation(status=status, response=response)
