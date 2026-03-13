"""
Project lifecycle tracking.

Each project keeps a .orch/project.toml that is committed to the repo.
The ledger is append-only — stages are never edited, only new entries added.
This file is readable by Claude Code, so Claude can update status and note
blockers without any orch-specific tooling.

Stages (in order):
  idea       → Conceived, not yet in active development
  building   → Active development, pre-functional
  mvp        → Core loop works, usable by you
  staging    → Live on real infrastructure, pre-public
  live       → Deployed, in use (moves to maintaining immediately on deploy)
  maintaining → Shipped. Stable. Investment is incremental.

Stall detection:
  Compares average days between past transitions against days since last
  transition. If current gap > 1.5x the project's own average, it's stalled.
  Projects with only one ledger entry use a global default (14 days).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Project

# ── Constants ─────────────────────────────────────────────────────────────────

STAGES = ["idea", "building", "mvp", "staging", "live", "maintaining"]
STAGE_EMOJI = {
    "idea":        "💡",
    "building":    "🔨",
    "mvp":         "🎯",
    "staging":     "🚀",
    "live":        "✅",
    "maintaining": "🔧",
}
DEFAULT_STALL_DAYS = 14   # used when there's only one ledger entry
STALL_MULTIPLIER  = 1.5   # gap > avg * this = stalled


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class LedgerEntry:
    date:  date
    stage: str
    note:  str = ""

    def __str__(self) -> str:
        note = f" — {self.note}" if self.note else ""
        return f"{self.date}  {self.stage}{note}"


@dataclass
class ProjectLifecycle:
    project_name: str
    stage:        str = "building"
    description:  str = ""
    ignored:      bool = False
    ledger:       list[LedgerEntry] = field(default_factory=list)

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def stage_index(self) -> int:
        try:
            return STAGES.index(self.stage)
        except ValueError:
            return 1

    @property
    def stage_emoji(self) -> str:
        return STAGE_EMOJI.get(self.stage, "")

    @property
    def days_in_current_stage(self) -> int:
        if not self.ledger:
            return 0
        return (date.today() - self.ledger[-1].date).days

    @property
    def average_days_per_stage(self) -> float | None:
        """Average days between ledger transitions. None if < 2 entries."""
        if len(self.ledger) < 2:
            return None
        gaps = [
            max(0, (self.ledger[i].date - self.ledger[i - 1].date).days)
            for i in range(1, len(self.ledger))
        ]
        positive = [g for g in gaps if g > 0]
        if not positive:
            return None
        return sum(positive) / len(positive)

    @property
    def is_stalled(self) -> bool:
        avg = self.average_days_per_stage
        threshold = (avg * STALL_MULTIPLIER) if avg else DEFAULT_STALL_DAYS
        return self.days_in_current_stage > threshold

    @property
    def stall_score(self) -> float:
        """
        0.0 = on pace, 1.0 = exactly at stall threshold, >1.0 = increasingly overdue.
        Used by the day planner for weighting.
        """
        avg = self.average_days_per_stage
        threshold = (avg * STALL_MULTIPLIER) if avg else DEFAULT_STALL_DAYS
        if threshold == 0:
            return 0.0
        return self.days_in_current_stage / threshold

    @property
    def launch_debt(self) -> int:
        """
        Days a project has been in a shippable-but-not-shipped state.
        Counts days spent in mvp + staging without moving to live/maintaining.
        Zero once at live/maintaining.
        """
        if self.stage in ("live", "maintaining"):
            return 0
        debt = 0
        for i, entry in enumerate(self.ledger):
            if entry.stage in ("mvp", "staging"):
                end = (
                    self.ledger[i + 1].date
                    if i + 1 < len(self.ledger)
                    else date.today()
                )
                debt += (end - entry.date).days
        return debt


# ── TOML serialisation (no dep — hand-rolled for this simple schema) ──────────

def _to_toml(lc: ProjectLifecycle) -> str:
    ignored_line = f'ignored     = true' if lc.ignored else f'ignored     = false'
    lines = [
        "# orch project lifecycle — committed with the repo",
        "# Edit freely. Claude can update this file.",
        "# Stages: idea → building → mvp → staging → live → maintaining",
        "# Set ignored = true to hide this project from orch",
        "",
        "[project]",
        f'name        = "{lc.project_name}"',
        f'description = "{lc.description}"',
        f'stage       = "{lc.stage}"',
        ignored_line,
        "",
        "# Ledger is append-only. Add entries at the bottom.",
        "# Claude will add entries automatically on stage transitions.",
    ]
    for entry in lc.ledger:
        lines += [
            "",
            "[[ledger]]",
            f'date  = "{entry.date}"',
            f'stage = "{entry.stage}"',
            f'note  = "{entry.note}"',
        ]
    return "\n".join(lines) + "\n"


def _from_toml(text: str, project_name: str) -> ProjectLifecycle:
    """
    Minimal TOML parser for our specific schema.
    Handles [project] section and [[ledger]] array-of-tables.
    """
    lc = ProjectLifecycle(project_name=project_name)
    section = None
    current_entry: dict | None = None

    def _unquote(s: str) -> str:
        return s.strip().strip('"').strip("'")

    def _flush_entry():
        nonlocal current_entry
        if current_entry is not None:
            try:
                lc.ledger.append(LedgerEntry(
                    date=date.fromisoformat(current_entry.get("date", str(date.today()))),
                    stage=current_entry.get("stage", "building"),
                    note=current_entry.get("note", ""),
                ))
            except (ValueError, KeyError):
                pass
        current_entry = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if line == "[project]":
            _flush_entry()
            section = "project"
            continue

        if line == "[[ledger]]":
            _flush_entry()
            section = "ledger"
            current_entry = {}
            continue

        if "=" not in line:
            continue

        key, _, val = line.partition("=")
        key = key.strip()
        val = _unquote(val)

        if section == "project":
            if key == "name":        lc.project_name = val
            elif key == "description": lc.description  = val
            elif key == "stage":     lc.stage         = val
            elif key == "ignored":   lc.ignored       = val.lower() == "true"
        elif section == "ledger" and current_entry is not None:
            current_entry[key] = val

    _flush_entry()
    return lc


# ── File I/O ──────────────────────────────────────────────────────────────────

def _lifecycle_path(project: "Project") -> Path:
    return project.path / ".orch" / "project.toml"


def load(project: "Project") -> ProjectLifecycle:
    path = _lifecycle_path(project)
    if not path.exists():
        return ProjectLifecycle(project_name=project.name)
    return _from_toml(path.read_text(), project.name)


def save(project: "Project", lc: ProjectLifecycle) -> None:
    path = _lifecycle_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_to_toml(lc))


def advance_stage(project: "Project", new_stage: str, note: str = "") -> ProjectLifecycle:
    """
    Move the project to a new stage and append a ledger entry.
    Raises ValueError if new_stage is not a recognised stage.
    """
    if new_stage not in STAGES:
        raise ValueError(f"Unknown stage '{new_stage}'. Valid: {STAGES}")

    lc = load(project)
    lc.stage = new_stage
    lc.ledger.append(LedgerEntry(date=date.today(), stage=new_stage, note=note))
    save(project, lc)
    return lc


def ignore_project(project: "Project") -> ProjectLifecycle:
    """Mark a project as ignored. It won't appear in orch until manually un-ignored."""
    lc = load(project)
    lc.ignored = True
    lc.ledger.append(LedgerEntry(date=date.today(), stage=lc.stage, note="Ignored"))
    save(project, lc)
    return lc


def unignore_project(project: "Project") -> ProjectLifecycle:
    """Un-ignore a project so it appears in orch again."""
    lc = load(project)
    lc.ignored = False
    lc.ledger.append(LedgerEntry(date=date.today(), stage=lc.stage, note="Un-ignored"))
    save(project, lc)
    return lc


def ensure_initialized(project: "Project") -> ProjectLifecycle:
    """
    Create .orch/project.toml if it doesn't exist, with a single
    opening ledger entry dated today at 'building'.
    """
    lc = load(project)
    if not lc.ledger:
        lc.stage = "building"
        lc.ledger = [LedgerEntry(
            date=date.today(),
            stage="building",
            note="Project registered with orch",
        )]
        save(project, lc)
    return lc
