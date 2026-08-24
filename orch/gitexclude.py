"""Keep orch's own runtime state out of the repos orch manages.

orch writes ``.orch/`` into every project it touches — session status, the
per-project config, bridge scratch. In a repo whose ``.gitignore`` says nothing
about it, that untracked directory alone makes the tree dirty: noise for
whoever reads ``git status``, and worse, ``_MainSync``'s dirty gate then skips
the project forever, so its local main never fast-forwards again.

``.git/info/exclude`` is the right home for the entry. It is local to the
clone, never committed, and needs no PR against a third-party repo that would
rightly refuse an orch-specific ``.gitignore`` line. Projects onboarded later
get it for free, and repos that already ignore ``.orch`` are left untouched.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("orch.gitexclude")

ENTRY = ".orch/"
MARKER = "# orch: runtime state, local to this clone"

# Repos this process has already handled. Purely an optimisation — the work
# below is idempotent — but discovery re-runs on every daemon sweep and every
# TUI refresh, and none of those need to pay for a git subprocess per project.
# Failures memoise too: a `.git` we cannot read or write now is overwhelmingly
# likely to stay that way, and retrying every 900s would only spam the log.
_checked: set[str] = set()


def is_orch_path(path: str) -> bool:
    """True for ``.orch`` itself and anything beneath it."""
    path = path.strip().strip('"')
    return path == ".orch" or path == ".orch/" or path.startswith(".orch/")


def ensure_orch_excluded(project_path: Path | str) -> bool:
    """Add ``.orch/`` to *project_path*'s ``.git/info/exclude``.

    Returns True only when this call wrote the entry. A repo where git already
    ignores ``.orch`` — its own ``.gitignore`` covers it, or a previous call
    wrote the exclude — is a no-op, so this is safe to call on every sweep.

    Fail-soft by contract: a missing, read-only, or otherwise unusual ``.git``
    must never break discovery, a session launch, or a sync sweep. Every
    failure is a logged False.
    """
    key = str(project_path)
    if key in _checked:
        return False
    try:
        wrote = _ensure(key)
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        log.debug("could not exclude %s in %s: %r", ENTRY, key, exc)
        wrote = False
    _checked.add(key)
    return wrote


def _ensure(path: str) -> bool:
    # Query ".orch/" with the trailing slash, not ".orch". A repo whose
    # .gitignore says ".orch/" has a directory-only pattern, and git will not
    # match it against a bare ".orch" that doesn't exist on disk yet — so the
    # slashless query reports "not ignored" for repos that plainly do ignore
    # it, and we'd append a redundant exclude line. ".orch" is always a
    # directory here, so asking about it as one is also just accurate.
    check = subprocess.run(
        ["git", "check-ignore", "-q", ENTRY],
        cwd=path, capture_output=True, timeout=10,
    )
    if check.returncode == 0:
        return False  # already ignored — nothing to do, nothing to conflict with
    if check.returncode != 1:
        # 128 = not a git repo / unreadable repo. Anything else is unexpected
        # enough that writing into its .git is the wrong move.
        log.debug(
            "check-ignore in %s exited %d; leaving .git/info/exclude alone",
            path, check.returncode,
        )
        return False

    common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=path, capture_output=True, text=True, timeout=10,
    )
    if common.returncode != 0 or not common.stdout.strip():
        return False
    # --git-common-dir resolves worktrees to the main repo's .git, which is
    # where the shared info/exclude lives; it may come back relative to cwd.
    git_dir = Path(common.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = Path(path) / git_dir

    exclude = git_dir / "info" / "exclude"
    existing = ""
    if exclude.exists():
        existing = exclude.read_text()
        if any(is_orch_path(line) for line in existing.splitlines()):
            # Listed already but check-ignore disagrees (a later negation, say).
            # Whatever the repo is doing, a duplicate line won't help.
            return False
    else:
        exclude.parent.mkdir(parents=True, exist_ok=True)

    if existing and not existing.endswith("\n"):
        existing += "\n"
    exclude.write_text(f"{existing}{MARKER}\n{ENTRY}\n")
    log.info("excluded %s via .git/info/exclude in %s", ENTRY, path)
    return True
