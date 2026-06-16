"""orch worktree lifecycle — cleanup logic for `w` sessions.

Used by:
  - OrchApp._do_refresh_session_cache (every 15s) — when a `w` session
    detaches (iTerm window closes), classify the worktree and either
    remove it cleanly or mark it kept for the daemon GC.
  - The daemon Janitor (Phase 4) — daily sweep for merged PRs and aged-out
    abandoned worktrees.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from . import state

if TYPE_CHECKING:
    from .models import Project


def _git_count_commits(worktree_path: str, base_branch: str) -> int | None:
    """Commits in worktree HEAD not in <base_branch>. None if git fails."""
    for base_ref in (f"origin/{base_branch}", base_branch):
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{base_ref}..HEAD"],
            capture_output=True, text=True,
            cwd=worktree_path, timeout=15,
        )
        if result.returncode == 0:
            try:
                return int(result.stdout.strip())
            except ValueError:
                pass
    return None


def _git_is_dirty(worktree_path: str) -> bool | None:
    """True if the working tree has uncommitted changes. None if git fails."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True,
        cwd=worktree_path, timeout=15,
    )
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def cleanup_closed_session(project: "Project", wt_id: str) -> str:
    """Decide what to do with a worktree whose session just ended.

    Outcomes:
      kept-clean      no commits + clean → kept on disk so the session stays
                      resumable via the `v` picker; daemon GC ages it out
      kept            ≥1 commit → leave for daemon GC (checks merged PRs)
      kept-dirty      0 commits but dirty → leave for daemon GC (age-out)
      unknown         row missing or git probe failed

    Best-effort: all errors are swallowed.
    """
    row = state.get_worktree(wt_id)
    if row is None:
        return "unknown"
    if row["status"] != state.WT_ACTIVE:
        return row["status"]

    wt_path = row["worktree_path"]
    base_branch = row["base_branch"]
    branch = row["branch"]

    if not Path(wt_path).is_dir():
        # Disk already gone (manual rm, etc.) — just close the row.
        state.mark_worktree_closed(wt_id, state.WT_REMOVED_CLEAN)
        return state.WT_REMOVED_CLEAN

    commits = _git_count_commits(wt_path, base_branch)
    dirty = _git_is_dirty(wt_path)

    if commits is None or dirty is None:
        # Don't risk a wrong decision — keep it, let GC re-evaluate later.
        state.mark_worktree_closed(wt_id, state.WT_KEPT)
        return "unknown"

    if commits == 0 and not dirty:
        # Keep the worktree on disk: its conversation is still resumable from
        # the `v` picker (Claude keys --resume on cwd, which must survive). The
        # daemon GC ages these out; we don't delete eagerly anymore.
        state.mark_worktree_closed(wt_id, state.WT_KEPT_CLEAN)
        return state.WT_KEPT_CLEAN

    if commits and commits > 0:
        state.mark_worktree_closed(wt_id, state.WT_KEPT)
        return state.WT_KEPT

    # commits == 0, dirty
    state.mark_worktree_closed(wt_id, state.WT_KEPT_DIRTY)
    return state.WT_KEPT_DIRTY
