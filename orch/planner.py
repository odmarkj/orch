"""
Day planner for orch.

Gathers context from every project (lifecycle, todos, git activity, claude
status) and makes a single Claude API call that returns a structured plan.
The plan is displayed in the TUI and can be printed to the terminal.

Finish-what-you-started bias:
  Projects closer to launch (mvp, staging) get a priority boost.
  Stalled projects get surfaced regardless of stage.
  Projects at maintaining are deprioritized unless explicitly flagged.
  New project ideas are always lowest priority.

One call, no streaming, structured JSON back.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Project

from .lifecycle import (
    STAGE_EMOJI, STAGES, ProjectLifecycle,
    load as load_lifecycle,
    ensure_initialized,
)


# ── Project snapshot (what we feed to Claude) ────────────────────────────────

@dataclass
class ProjectSnapshot:
    name:             str
    stage:            str
    stage_emoji:      str
    description:      str
    days_in_stage:    int
    stall_score:      float
    is_stalled:       bool
    launch_debt:      int
    pending_todos:    int
    todo_sample:      list[str]     # first 5 pending items
    claude_status:    str           # current .claude/status
    recent_commits:   list[str]     # last 3 git log lines
    avg_days_stage:   float | None

    def to_dict(self) -> dict:
        return {
            "name":           self.name,
            "stage":          f"{self.stage_emoji} {self.stage}",
            "description":    self.description,
            "days_in_stage":  self.days_in_stage,
            "stall_score":    round(self.stall_score, 2),
            "is_stalled":     self.is_stalled,
            "launch_debt_days": self.launch_debt,
            "pending_todos":  self.pending_todos,
            "todo_sample":    self.todo_sample,
            "claude_status":  self.claude_status,
            "recent_commits": self.recent_commits,
        }


def _git_log(project: "Project", n: int = 3) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "log", f"-{n}", "--oneline", "--no-decorate"],
            cwd=str(project.path),
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            return [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return []


def _todo_sample(project: "Project", n: int = 5) -> tuple[int, list[str]]:
    try:
        text = (project.path / "TODOS.md").read_text()
        lines = [
            l.strip().lstrip("- [ ]").strip()
            for l in text.splitlines()
            if l.strip().startswith("- [ ]")
        ]
        return len(lines), lines[:n]
    except FileNotFoundError:
        return 0, []


def snapshot(project: "Project") -> ProjectSnapshot:
    lc = load_lifecycle(project)
    pending, sample = _todo_sample(project)
    return ProjectSnapshot(
        name           = project.name,
        stage          = lc.stage,
        stage_emoji    = lc.stage_emoji,
        description    = lc.description,
        days_in_stage  = lc.days_in_current_stage,
        stall_score    = lc.stall_score,
        is_stalled     = lc.is_stalled,
        launch_debt    = lc.launch_debt,
        pending_todos  = pending,
        todo_sample    = sample,
        claude_status  = project.current_status,
        recent_commits = _git_log(project),
        avg_days_stage = lc.average_days_per_stage,
    )


# ── Priority scoring (pre-AI, deterministic) ─────────────────────────────────

STAGE_WEIGHT = {
    "idea":        0.2,
    "building":    0.6,
    "mvp":         0.9,
    "staging":     1.0,
    "live":        0.3,   # already shipped — maintaining mode
    "maintaining": 0.1,
}


def priority_score(snap: ProjectSnapshot) -> float:
    """
    Deterministic score used to order the Claude prompt and as a tiebreaker.
    Higher = more urgent.
    """
    base    = STAGE_WEIGHT.get(snap.stage, 0.5)
    stall   = min(snap.stall_score * 0.4, 0.4)    # stall adds up to 0.4
    todos   = min(snap.pending_todos * 0.01, 0.15) # more todos = slight boost
    debt    = min(snap.launch_debt * 0.001, 0.2)   # launch debt adds up to 0.2
    return round(base + stall + todos + debt, 3)


# ── Claude API call ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are the orch day planner. Your job is to help an indie developer who uses
AI coding agents (Claude Code) to build and ship software products. They
orchestrate multiple projects simultaneously and need a focused, opinionated
daily plan.

Core philosophy: finish what you started before starting something new.
Projects closer to launch always take priority over early-stage projects.
Stalled projects need attention regardless of their stage.
Projects at "maintaining" are shipped — only surface them if they have a
specific blocker or are explicitly flagged.

You will receive a JSON array of project snapshots. Return ONLY valid JSON
with this exact structure — no markdown, no explanation, just the JSON:

{
  "date": "YYYY-MM-DD",
  "focus_projects": ["project-a", "project-b", "project-c"],
  "plan": [
    {
      "project": "project-name",
      "priority": 1,
      "stage": "mvp",
      "headline": "One sentence: what to accomplish today",
      "rationale": "One sentence: why this project, why now",
      "suggested_tasks": ["specific task 1", "specific task 2"],
      "flag": null
    }
  ],
  "stalled": ["project-name"],
  "launch_debt_warning": ["project-name"],
  "skip_today": ["project-name"],
  "one_liner": "Single motivating sentence for the day"
}

Rules:
- focus_projects: max 4 projects for the day (the ones to actually work on)
- plan: include all projects, sorted by priority (1 = highest)
- flag: null, "stalled", "launch_debt", "no_todos", or "needs_stage_update"
- suggested_tasks: pulled from their todo_sample, reworded as actionable steps
- Be direct and opinionated. The developer trusts your judgment.
- Never suggest starting new features on a project with launch_debt > 30 days.
"""


@dataclass
class DayPlan:
    date:                 str
    focus_projects:       list[str]
    plan:                 list[dict]
    stalled:              list[str]
    launch_debt_warning:  list[str]
    skip_today:           list[str]
    one_liner:            str
    raw_response:         str = ""


def _planner_config() -> dict:
    """Read [planner] section from ~/.orch/config.toml."""
    from pathlib import Path
    cfg = {"model": "claude-sonnet-4-20250514"}
    config_file = Path.home() / ".orch" / "config.toml"
    if not config_file.exists():
        return cfg
    section = None
    for raw in config_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section == "planner" and "=" in line:
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def generate(
    projects: "list[Project]",
    api_key: str | None = None,
    model: str | None = None,
) -> DayPlan:
    """
    Build snapshots for all projects, call Claude once, return a DayPlan.
    Raises RuntimeError if the API call fails.
    """
    import os
    pcfg = _planner_config()
    model = model or pcfg.get("model", "claude-sonnet-4-20250514")
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Add it to ~/.orch/config.toml or "
            "export it in your shell profile."
        )

    # Build and sort snapshots by priority score
    snapshots = [snapshot(p) for p in projects]
    snapshots.sort(key=priority_score, reverse=True)

    payload = json.dumps([s.to_dict() for s in snapshots], indent=2)

    import urllib.request
    body = json.dumps({
        "model":      model,
        "max_tokens": 2000,
        "system":     SYSTEM_PROMPT,
        "messages":   [{"role": "user", "content": payload}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        raise RuntimeError(f"Claude API call failed: {e}") from e

    raw = data["content"][0]["text"].strip()

    # Strip markdown fences if Claude wrapped it anyway
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse Claude response as JSON: {e}\n\nRaw:\n{raw}") from e

    return DayPlan(
        date                = parsed.get("date", str(date.today())),
        focus_projects      = parsed.get("focus_projects", []),
        plan                = parsed.get("plan", []),
        stalled             = parsed.get("stalled", []),
        launch_debt_warning = parsed.get("launch_debt_warning", []),
        skip_today          = parsed.get("skip_today", []),
        one_liner           = parsed.get("one_liner", ""),
        raw_response        = raw,
    )


def format_plan(plan: DayPlan) -> str:
    """Plain text rendering for terminal output."""
    import re as _re
    lines = [
        f"  Day plan — {plan.date}",
        f"  {plan.one_liner}",
        "",
        f"  Focus today: {', '.join(plan.focus_projects)}",
        "",
    ]

    if plan.launch_debt_warning:
        lines += [
            f"  ⚠  Launch debt: {', '.join(plan.launch_debt_warning)}",
            "     These are near-done. Ship them before building new features.",
            "",
        ]
    if plan.stalled:
        lines += [
            f"  ⏸  Stalled: {', '.join(plan.stalled)}",
            "",
        ]

    for item in plan.plan:
        emoji = STAGE_EMOJI.get(item.get("stage", ""), "")
        flag  = f"  [{item['flag']}]" if item.get("flag") else ""
        lines += [
            f"  {item['priority']}. {item['project']} {emoji}{flag}",
            f"     {item['headline']}",
            f"     {item['rationale']}",
        ]
        for task in item.get("suggested_tasks", []):
            lines.append(f"       • {task}")
        lines.append("")

    return "\n".join(lines)


# re import needed for strip at module level
import re
