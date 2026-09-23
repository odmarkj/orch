"""Locate the ``~/.claude/projects/<dir>`` that Claude Code uses for a cwd.

Claude Code names a project's transcript dir by replacing every
non-alphanumeric character of the cwd with ``-`` — not just ``/``. Orch
worktree paths contain ``.`` (``.orch-worktrees``) and ``_`` (``wt_<id>``), so
a ``/``-only encoding never matched them, and worktree resume and worktree
JSONL watching both silently found nothing.

``encode_cwd`` is that rule (verified against 156/158 real dirs; the two
misses were sessions whose first recorded cwd wasn't the launch dir).
``jsonl_dir_for`` layers a fallback on top: if the encoded dir holds no
transcripts, look the cwd up in an index built from the ``cwd`` field Claude
records in each transcript, which survives a future change to the encoding
(or its long-path truncation). Every path-to-dir computation in orch goes
through here.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

# Claude Code truncates very long encoded names and appends a hash; we can't
# reproduce the hash, so those are left to the cwd index.
_MAX_ENCODED_LEN = 200
_CWD_SCAN_LINES = 20


def projects_base() -> Path:
    """Root of Claude Code's per-cwd transcript dirs.

    ``ORCH_CLAUDE_PROJECTS_DIR`` overrides it outright; otherwise honour
    ``CLAUDE_CONFIG_DIR`` the way Claude Code does, then ``~/.claude``. The
    override matters in the Lima VM, where ``$HOME`` is
    ``/home/<user>.guest`` but transcripts live under the host's home.
    """
    override = os.environ.get("ORCH_CLAUDE_PROJECTS_DIR")
    if override:
        return Path(override).expanduser()
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir).expanduser() / "projects"
    return Path.home() / ".claude" / "projects"


def encode_cwd(cwd: Path | str) -> str:
    """Claude Code's project-dir name for a cwd: non-alphanumerics → ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def encoded_jsonl_dir(cwd: Path | str) -> Path:
    """The dir Claude Code will create for ``cwd`` (may not exist yet)."""
    return projects_base() / encode_cwd(cwd)


def _has_transcripts(d: Path) -> bool:
    try:
        return any(p.is_file() for p in d.glob("*.jsonl"))
    except OSError:
        return False


def _first_cwd(jsonl_path: Path) -> str | None:
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= _CWD_SCAN_LINES:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("cwd"), str):
                    return obj["cwd"]
    except OSError:
        pass
    return None


def cwd_index() -> dict[str, Path]:
    """Map each recorded cwd to the transcript dir holding it.

    Reads the first few lines of one transcript per dir, so it is cheap
    enough to build on demand when the encoded dir misses.
    """
    index: dict[str, Path] = {}
    base = projects_base()
    try:
        dirs = [d for d in base.iterdir() if d.is_dir()]
    except OSError:
        return index
    for d in dirs:
        try:
            files = sorted(
                (p for p in d.glob("*.jsonl") if p.is_file()),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
        except OSError:
            continue
        for f in files[:3]:
            cwd = _first_cwd(f)
            if cwd:
                index.setdefault(cwd, d)
                break
    return index


class JsonlDirResolver:
    """Resolve cwds to transcript dirs, building the cwd index at most once.

    Per cwd: the encoded dir if it holds transcripts; else whatever dir the
    cwd index says recorded this cwd; else the encoded dir (so callers can
    watch or create it before Claude writes its first turn).
    """

    def __init__(self) -> None:
        self._index: dict[str, Path] | None = None

    def __call__(self, cwd: Path | str) -> Path:
        encoded = encoded_jsonl_dir(cwd)
        if len(encoded.name) <= _MAX_ENCODED_LEN and _has_transcripts(encoded):
            return encoded
        if self._index is None:
            self._index = cwd_index()
        return self._index.get(str(cwd), encoded)


def jsonl_dir_for(cwd: Path | str) -> Path:
    """Best-known transcript dir for one cwd (see ``JsonlDirResolver``)."""
    return JsonlDirResolver()(cwd)
