"""Task-input normalization helpers for :mod:`hermes_cli.kanban_db`."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Callable, Optional

from hermes_cli.profiles import normalize_profile_name
from toolsets import get_toolset_names

KNOWN_TOOLSET_NAMES = frozenset(name.casefold() for name in get_toolset_names())
VALID_WORKSPACE_KINDS = {"scratch", "worktree", "dir"}
logger = logging.getLogger(__name__)


def validate_model_override(
    model: Optional[str], provider: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Strip model/provider and reject a provider without a model."""
    model = (model or "").strip() or None
    provider = (provider or "").strip() or None
    if provider and not model:
        raise ValueError("provider_override requires a model_override")
    return model, provider


def canonical_assignee(assignee: Optional[str]) -> Optional[str]:
    """Return the canonical lower-case profile name for a Kanban row."""
    return None if assignee is None else normalize_profile_name(assignee)


def normalize_parents_and_continuation(
    parents: Iterable[str], continuation_parent_id: Optional[str],
) -> tuple[tuple[str, ...], Optional[str]]:
    """Normalize parent IDs and require the continuation anchor to be direct."""
    normalized_parents = tuple(parent for parent in parents if parent)
    continuation = (
        str(continuation_parent_id).strip() if continuation_parent_id is not None else None
    ) or None
    if continuation and continuation not in normalized_parents:
        raise ValueError("continuation parent must be a direct parent")
    return normalized_parents, continuation


def normalize_workspace_request(
    project_id: Optional[str],
    workspace_kind: Optional[str],
    branch_name: Optional[str],
    board_project_id: Callable[[], Optional[str]],
) -> tuple[Optional[str], str, Optional[str]]:
    """Apply board project defaults and validate workspace/branch coupling."""
    if project_id is None and workspace_kind != "scratch":
        try:
            project_id = (board_project_id() or "").strip() or None
        except Exception:
            logger.exception("failed to resolve the board project default")
    workspace_kind = workspace_kind or "scratch"
    if workspace_kind not in VALID_WORKSPACE_KINDS:
        raise ValueError(
            f"workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}, "
            f"got {workspace_kind!r}"
        )
    branch_name = str(branch_name).strip() or None if branch_name is not None else None
    if branch_name and workspace_kind != "worktree":
        raise ValueError("branch_name is only valid for worktree workspaces")
    return project_id, workspace_kind, branch_name


def normalize_task_skills(skills: Optional[Iterable[str]]) -> Optional[list[str]]:
    """Strip/dedupe skills and reject commas or toolset names."""
    if skills is None:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    toolset_typos: list[str] = []
    for skill in skills:
        if not skill:
            continue
        name = str(skill).strip()
        if not name:
            continue
        if "," in name:
            raise ValueError(
                f"skill name cannot contain comma: {name!r} "
                f"(pass a list of separate names instead of a comma-joined string)"
            )
        if name.casefold() in KNOWN_TOOLSET_NAMES:
            toolset_typos.append(name)
            continue
        if name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    if toolset_typos:
        quoted = ", ".join(repr(name) for name in toolset_typos)
        noun = "is a toolset name" if len(toolset_typos) == 1 else "are toolset names"
        raise ValueError(
            f"{quoted} {noun}, not skill name(s). "
            "Put toolsets in the assignee profile's `toolsets:` config "
            "instead of per-task skills. Skills are named skill bundles "
            "(e.g. `blogwatcher`, `github-code-review`); toolsets are runtime "
            "capabilities (e.g. `web`, `browser`, `terminal`)."
        )
    return cleaned
