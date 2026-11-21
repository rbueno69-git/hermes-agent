"""Durable policy continuation for accepted but ineligible GitHub events."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


_ITEM_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*\Z")
_SHA40_RE = re.compile(r"[0-9a-f]{40}\Z")
_REASON_CODES = frozenset({"missing_contract", "ineligible_contract"})
_TERMINAL_STATES = frozenset({
    "approval_proposed", "approval_reconciled", "clarification_requested", "ignored_by_policy",
})


@dataclass(frozen=True)
class IntakeObservation:
    item_key: str
    head_sha: str
    reason_code: str
    title: str = ""

    @property
    def identity(self) -> str:
        return f"{self.item_key}+{self.head_sha}"

    def validate(self) -> None:
        if not isinstance(self.item_key, str) or _ITEM_RE.fullmatch(self.item_key) is None:
            raise ValueError("item_key must be owner/repo#number")
        if not isinstance(self.head_sha, str) or _SHA40_RE.fullmatch(self.head_sha) is None:
            raise ValueError("head_sha must be lowercase sha40")
        if self.reason_code not in _REASON_CODES:
            raise ValueError("unsupported GitHub intake reason code")
        if not isinstance(self.title, str):
            raise ValueError("title must be text")


class MissingMaterial(RuntimeError):
    """A referenced PR needs an operator choice before a bounded contract exists."""


def _iter_ledger(path: Path):
    path = Path(path)
    if not path.exists():
        return
    raw = path.read_text(encoding="utf-8-sig")
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


def read_executable_references(path: Path) -> list[str]:
    """Read authority text only from approvals that can represent executable contracts."""
    references: list[str] = []
    for value in _iter_ledger(path):
        status, scope = value.get("status"), value.get("scope")
        if (isinstance(status, str) and status.startswith("approved_terminal")
                and isinstance(scope, str) and scope):
            references.append(scope)
    return references


def read_approved_tuples(path: Path) -> set[tuple[str, str]]:
    """Return exact approved identities; a prose reference is never approval."""
    approved = set()
    for value in _iter_ledger(path):
        status = value.get("status")
        item_key, head_sha = value.get("work_item"), value.get("head_sha")
        if (isinstance(status, str) and status.startswith("approved_terminal")
                and isinstance(item_key, str) and _ITEM_RE.fullmatch(item_key)
                and isinstance(head_sha, str) and _SHA40_RE.fullmatch(head_sha)):
            approved.add((item_key, head_sha))
    return approved


def build_clarification_contract(observation: IntakeObservation, reason: str) -> dict[str, str]:
    """Build the safe bounded choice used when file-level material is unavailable."""
    scope = (
        f"Clarify continuation for {observation.item_key} at exact head "
        f"{observation.head_sha}: approve the complete PR diff for merge-only execution, "
        "with no deploy, release, workflow, Actions, checks, or hosted compute; or reject it."
    )
    digest = hashlib.sha256(json.dumps({
        "item_key": observation.item_key,
        "head_sha": observation.head_sha,
        "mode": "exact-head-complete-diff-merge-only",
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"question": f"{scope}\nMaterial unavailable: {reason}",
            "contract_digest_sha256": digest}


def build_merge_only_contract(
    observation: IntakeObservation,
    reference: str,
    *,
    github_reader: Callable[[str], object],
) -> dict[str, str]:
    """Bound an expected ordinary PR to its exact head/files and repository-only merge."""
    observation.validate()
    repo, raw_number = observation.item_key.rsplit("#", 1)
    number = int(raw_number)
    pull = github_reader(f"repos/{repo}/pulls/{number}")
    files = github_reader(f"repos/{repo}/pulls/{number}/files?per_page=100")
    if not isinstance(pull, dict) or not isinstance(files, list):
        raise MissingMaterial("GitHub did not return the live PR and exact file inventory")
    head = pull.get("head")
    live_head = head.get("sha") if isinstance(head, dict) else None
    if (pull.get("state") != "open" or live_head != observation.head_sha
            or not files or len(files) > 60):
        raise MissingMaterial("The PR is not open at the observed head or its file inventory is not bounded")
    filenames = []
    for row in files:
        filename = row.get("filename") if isinstance(row, dict) else None
        if not isinstance(filename, str) or not filename or len(filename) > 500:
            raise MissingMaterial("The PR file inventory is incomplete")
        filenames.append(filename)
    scope = ", ".join(filenames)
    if len(scope) > 1600:
        raise MissingMaterial("The PR file inventory is too large for an automatic bounded contract")
    contract = {
        "action": "validate locally and squash merge the exact PR head",
        "head_sha": observation.head_sha,
        "item_key": observation.item_key,
        "limits": "No deploy, no persistent-state write, no GitHub-hosted computation; stop on diff drift.",
        "scope": filenames,
    }
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    question = (
        "Approve bounded GitHub execution?\n"
        f"Item: {observation.item_key}\nHead: {observation.head_sha}\nDigest: {digest}\n"
        f"Scope: validate locally and squash merge only these files: {scope}\n"
        "Limits: No deploy; no persistent-state write; no GitHub Actions/checks/runners/Codespaces; "
        "stop if the live head or diff changes.\n"
        f"Expected by executable contract: {reference[:500]}"
    )
    return {
        "item_key": observation.item_key,
        "head_sha": observation.head_sha,
        "contract_digest_sha256": digest,
        "question": question,
    }


class IntakeContinuation:
    """Idempotently classify durable ineligible events and emit their one continuation."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS github_intake_observations (
                    identity TEXT PRIMARY KEY,
                    item_key TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    reason_code TEXT NOT NULL CHECK(reason_code IN ('missing_contract','ineligible_contract')),
                    title TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'pending','approval_proposed','approval_reconciled',
                        'clarification_requested','ignored_by_policy')),
                    detail TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(item_key,head_sha)
                )"""
            )
            table_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='github_intake_observations'"
            ).fetchone()[0]
            if "approval_reconciled" not in table_sql:
                db.executescript(
                    """
                    ALTER TABLE github_intake_observations RENAME TO github_intake_observations_v1;
                    CREATE TABLE github_intake_observations (
                        identity TEXT PRIMARY KEY,
                        item_key TEXT NOT NULL,
                        head_sha TEXT NOT NULL,
                        reason_code TEXT NOT NULL CHECK(reason_code IN (
                            'missing_contract','ineligible_contract')),
                        title TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN (
                            'pending','approval_proposed','approval_reconciled',
                            'clarification_requested','ignored_by_policy')),
                        detail TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(item_key,head_sha)
                    );
                    INSERT INTO github_intake_observations
                        (identity,item_key,head_sha,reason_code,title,state,detail,created_at,updated_at)
                    SELECT identity,item_key,head_sha,reason_code,title,state,detail,created_at,updated_at
                    FROM github_intake_observations_v1;
                    DROP TABLE github_intake_observations_v1;
                    """
                )

    def record(self, observation: IntakeObservation) -> bool:
        observation.validate()
        now = time.time()
        with self._connect() as db:
            changed = db.execute(
                """INSERT INTO github_intake_observations(
                       identity,item_key,head_sha,reason_code,title,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,'pending',?,?)
                   ON CONFLICT(identity) DO UPDATE SET
                       reason_code=excluded.reason_code,title=excluded.title,updated_at=excluded.updated_at
                   WHERE github_intake_observations.state='pending'""",
                (observation.identity, observation.item_key, observation.head_sha,
                 observation.reason_code, observation.title[:500], now, now),
            ).rowcount
        return changed == 1

    def observe(self, item_key: str, head_sha: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM github_intake_observations WHERE item_key=? AND head_sha=?",
                (item_key, head_sha),
            ).fetchone()
        return dict(row) if row is not None else None

    def _pending(self, limit: int) -> list[sqlite3.Row]:
        with self._connect() as db:
            return db.execute(
                "SELECT * FROM github_intake_observations WHERE state='pending' "
                "ORDER BY created_at,identity LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()

    @staticmethod
    def _reference_for(item_key: str, references: Iterable[str]) -> str | None:
        token = re.compile(rf"(?<![A-Za-z0-9_.#/-]){re.escape(item_key)}(?![0-9])")
        for reference in references:
            if isinstance(reference, str) and token.search(reference):
                return reference
        return None

    def _finish(self, identity: str, state: str, detail: str | None = None) -> bool:
        if state not in _TERMINAL_STATES:
            raise ValueError("invalid terminal intake state")
        with self._connect() as db:
            changed = db.execute(
                "UPDATE github_intake_observations SET state=?,detail=?,updated_at=? "
                "WHERE identity=? AND state='pending'",
                (state, detail, time.time(), identity),
            ).rowcount
        return changed == 1

    def reconcile(
        self,
        *,
        references: Sequence[str],
        approved_tuples: set[tuple[str, str]] | None = None,
        contract_builder: Callable[[IntakeObservation, str], Mapping[str, str]],
        approval_sender: Callable[[Mapping[str, str]], object],
        clarification_sender: Callable[[IntakeObservation, str, list[str]], object],
        limit: int = 16,
    ) -> list[dict[str, str]]:
        emitted: list[dict[str, str]] = []
        approved_tuples = approved_tuples or set()
        for row in self._pending(limit):
            observation = IntakeObservation(
                item_key=str(row["item_key"]), head_sha=str(row["head_sha"]),
                reason_code=str(row["reason_code"]), title=str(row["title"]),
            )
            if (observation.item_key, observation.head_sha) in approved_tuples:
                action = "approval_reconciled"
                if self._finish(observation.identity, action):
                    emitted.append({"identity": observation.identity, "action": action})
                continue
            reference = self._reference_for(observation.item_key, references)
            if reference is None:
                action = "ignored_by_policy"
                if self._finish(observation.identity, action):
                    emitted.append({"identity": observation.identity, "action": action})
                continue
            try:
                proposal = dict(contract_builder(observation, reference))
            except MissingMaterial as exc:
                question = f"{observation.item_key} at {observation.head_sha}: {exc}"
                choices = ["Prepare merge-only contract", "Ignore this PR"]
                detail = clarification_sender(observation, question, choices) or str(exc)
                action = "clarification_requested"
                if self._finish(observation.identity, action, str(detail)):
                    emitted.append({"identity": observation.identity, "action": action})
                continue
            required = {"item_key", "head_sha", "contract_digest_sha256", "question"}
            if set(proposal) != required:
                raise RuntimeError("bounded contract builder returned an unexpected proposal")
            if proposal["item_key"] != observation.item_key or proposal["head_sha"] != observation.head_sha:
                raise RuntimeError("bounded contract builder changed the observed identity")
            approval_sender(proposal)
            action = "approval_proposed"
            if self._finish(observation.identity, action, proposal["contract_digest_sha256"]):
                emitted.append({"identity": observation.identity, "action": action})
        return emitted
