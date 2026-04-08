"""
Cross-project agent communication bridge.

Allows a Claude session in one project to request work from another project.
The bridge spawns a subagent on a worktree of the target project inside its
container, with read-only access to the source project for context.

Protocol:
  1. Agent A writes ``.claude/bridge_request`` (JSON) in its project
  2. Orch watchdog picks it up, validates, and dispatches
  3. A worktree is created for the target project
  4. Claude runs inside the target container at the worktree path
  5. Response is delivered to source project's ``.claude/bridge_responses/``
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Project


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class BridgeRequest:
    id: str
    source_project: str
    source_path: Path
    target: str
    intent: str           # fix | review | query | inform
    summary: str
    context: str
    request: str
    relevant_files: list[str] = field(default_factory=list)
    depth: int = 0


@dataclass
class BridgeResponse:
    id: str
    source: str
    target: str
    intent: str
    summary: str
    status: str           # completed | failed | clarification_timeout
    result: str
    pr_url: str | None = None
    branch: str | None = None


MAX_BRIDGE_DEPTH = 1
CLARIFICATION_MARKER = "[NEEDS_CLARIFICATION]"


# ── Parsing ──────────────────────────────────────────────────────────────────

def parse_bridge_request(project: "Project") -> BridgeRequest | None:
    """Read and validate ``.claude/bridge_request``.  Returns *None* if
    the file is missing or has invalid content."""
    req_file = project.bridge_request_file
    if not req_file.exists():
        return None

    try:
        data = json.loads(req_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    # Require mandatory fields
    for key in ("target", "intent", "summary", "context", "request"):
        if key not in data or not str(data[key]).strip():
            return None

    if data["intent"] not in ("fix", "review", "query", "inform"):
        return None

    import random
    req_id = f"bridge-{int(time.time())}-{random.randint(1000, 9999)}"

    return BridgeRequest(
        id=req_id,
        source_project=project.name,
        source_path=project.path,
        target=data["target"],
        intent=data["intent"],
        summary=data["summary"],
        context=data["context"],
        request=data["request"],
        relevant_files=data.get("relevant_files", []),
        depth=int(data.get("depth", 0)),
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _find_target_project(
    target_name: str, projects: list["Project"],
) -> "Project | None":
    """Find a project by name among discovered projects."""
    for p in projects:
        if p.name == target_name:
            return p
    return None


def _build_bridge_prompt(request: BridgeRequest, source_readable: bool) -> str:
    """Construct the prompt for the bridge subagent."""
    lines = [
        f'You are handling a bridge request from project "{request.source_project}".',
        "",
        "## What they need",
        request.request,
        "",
        f"## Context from {request.source_project}",
        request.context,
        "",
    ]

    if source_readable:
        lines += [
            "## Source project code (read-only)",
            f"The source project is available at: {request.source_path}",
            "You may read files there for context but do NOT modify them.",
            "",
        ]

    if request.relevant_files:
        lines += [
            "## Relevant files in this project",
            *(f"- {f}" for f in request.relevant_files),
            "",
        ]

    # Intent-specific instructions
    if request.intent == "fix":
        lines += [
            "## Instructions",
            "- Make the requested code changes in this project",
            "- Save all changes. Do not commit or push.",
            "- Write a brief summary of what you changed to .claude/bridge_result",
        ]
    elif request.intent == "review":
        lines += [
            "## Instructions",
            "- Review the relevant code and provide feedback",
            "- Write your review to .claude/bridge_result",
        ]
    elif request.intent == "query":
        lines += [
            "## Instructions",
            "- Answer the question based on this project's code",
            "- Write your answer to .claude/bridge_result",
        ]
    elif request.intent == "inform":
        lines += [
            "## Instructions",
            "- Read and acknowledge the information provided",
            "- If any action is warranted, note it in .claude/bridge_result",
        ]

    lines += [
        "",
        "If you cannot complete the request without more information from the",
        f"source project, start your final output with {CLARIFICATION_MARKER}",
        "followed by your specific question on the next line.",
    ]

    return "\n".join(lines)


def _build_clarification_prompt(
    request: BridgeRequest, question: str,
) -> str:
    """Build a prompt for the source project to answer a clarification."""
    return (
        f"A bridge subagent working on project \"{request.target}\" needs "
        f"clarification to complete this task:\n\n"
        f"## Original request\n{request.request}\n\n"
        f"## Their question\n{question}\n\n"
        "Answer concisely based on this project's code. "
        "Write your answer to stdout — it will be forwarded."
    )


# ── Main pipeline ────────────────────────────────────────────────────────────

def handle_bridge_request(
    request: BridgeRequest,
    all_projects: list["Project"],
) -> BridgeResponse:
    """Execute the full bridge pipeline.

    1. Locate target project
    2. Create worktree + ensure container
    3. Run Claude subagent inside container
    4. Handle optional clarification round
    5. Post-process by intent (commit/push/PR for ``fix``)
    6. Deliver response to source project
    7. Clean up worktree
    """
    from .container import (
        create_worktree,
        remove_worktree,
        ensure_running,
        run_claude_in_container,
        worktree_container_path,
        _run_git_in_container,
        _create_pr,
    )

    # 1. Find target
    target = _find_target_project(request.target, all_projects)
    if target is None:
        resp = BridgeResponse(
            id=request.id,
            source=request.source_project,
            target=request.target,
            intent=request.intent,
            summary=request.summary,
            status="failed",
            result=f"Target project '{request.target}' not found.",
        )
        _deliver_response(request, resp)
        _archive_request_file(request)
        return resp

    worktree_path = None
    branch_name = ""

    try:
        # 2. Create worktree and start container
        worktree_path, branch_name = create_worktree(
            target, request.summary, branch_prefix="bridge",
        )
        container_wt = worktree_container_path(target, worktree_path)
        ensure_running(target)

        # Write bridge depth so outbound requests from the subagent can
        # detect chain depth.
        depth_dir = worktree_path / ".claude"
        depth_dir.mkdir(parents=True, exist_ok=True)
        (depth_dir / "_bridge_depth").write_text(str(request.depth + 1))

        # Check if the source project is readable from the target container
        # (it is if it's under a reference_dirs mount).
        source_readable = request.source_path.is_dir()

        # 3. Run subagent
        prompt = _build_bridge_prompt(request, source_readable)
        result = run_claude_in_container(
            target, prompt, workdir=container_wt, timeout=600,
        )
        output = result.stdout.strip()

        # 4. Clarification round (one attempt)
        if CLARIFICATION_MARKER in output:
            question = output.split(CLARIFICATION_MARKER, 1)[1].strip()
            if question:
                answer = _run_clarification(request, question)
                followup = (
                    f"{prompt}\n\n"
                    f"## Clarification\n"
                    f"**Question**: {question}\n"
                    f"**Answer**: {answer}\n\n"
                    f"Continue with your task now."
                )
                result = run_claude_in_container(
                    target, followup, workdir=container_wt, timeout=600,
                )
                output = result.stdout.strip()

        # Read the bridge_result file if the subagent wrote one
        result_file = worktree_path / ".claude" / "bridge_result"
        result_text = ""
        if result_file.exists():
            result_text = result_file.read_text().strip()
        if not result_text:
            result_text = output  # fall back to stdout

        # 5. Post-process by intent
        pr_url = None
        if request.intent == "fix":
            _run_git_in_container(target, worktree_path, ["add", "-A"], timeout=10)
            status_check = _run_git_in_container(
                target, worktree_path, ["status", "--porcelain"], timeout=10,
            )
            if status_check.stdout.strip():
                _run_git_in_container(
                    target, worktree_path,
                    ["commit", "-m", f"bridge: {request.summary[:60]}"],
                    timeout=30,
                )
                # Push with retries
                delays = [2, 4, 8, 16]
                for attempt in range(5):
                    push = _run_git_in_container(
                        target, worktree_path,
                        ["push", "-u", "origin", branch_name],
                        timeout=60,
                    )
                    if push.returncode == 0:
                        break
                    if attempt < len(delays):
                        time.sleep(delays[attempt])

                pr_url = _create_pr(
                    target, worktree_path, branch_name,
                    request.summary, result_text,
                    title_prefix="bridge",
                )

        resp = BridgeResponse(
            id=request.id,
            source=request.source_project,
            target=request.target,
            intent=request.intent,
            summary=request.summary,
            status="completed",
            result=result_text,
            pr_url=pr_url,
            branch=branch_name if request.intent == "fix" else None,
        )

    except Exception as exc:
        resp = BridgeResponse(
            id=request.id,
            source=request.source_project,
            target=request.target,
            intent=request.intent,
            summary=request.summary,
            status="failed",
            result=str(exc),
        )

    finally:
        # 7. Clean up worktree
        if worktree_path is not None:
            try:
                remove_worktree(target, worktree_path, branch_name)
            except Exception:
                pass

    # 6. Deliver and archive
    _deliver_response(request, resp)
    _archive_request_file(request)
    return resp


# ── Clarification ────────────────────────────────────────────────────────────

def _run_clarification(request: BridgeRequest, question: str) -> str:
    """Run a quick Claude instance in the source project container to answer
    the subagent's clarification question."""
    from .container import run_claude_in_container

    prompt = _build_clarification_prompt(request, question)
    result = run_claude_in_container(
        # We need a Project object — reconstruct a minimal one
        _project_from_path(request.source_path),
        prompt,
        timeout=120,
    )
    return result.stdout.strip() if result.returncode == 0 else "(no answer)"


def _project_from_path(path: Path) -> "Project":
    """Create a minimal Project for a known path (used for clarification)."""
    from .models import Project
    return Project(path=path)


# ── Response delivery ────────────────────────────────────────────────────────

def _deliver_response(request: BridgeRequest, response: BridgeResponse) -> None:
    """Write response JSON to the source project's ``.claude/bridge_responses/``."""
    source_dir = request.source_path / ".claude" / "bridge_responses"
    source_dir.mkdir(parents=True, exist_ok=True)
    out = {
        "id": response.id,
        "source": response.source,
        "target": response.target,
        "intent": response.intent,
        "summary": response.summary,
        "status": response.status,
        "result": response.result,
    }
    if response.pr_url:
        out["pr_url"] = response.pr_url
    if response.branch:
        out["branch"] = response.branch

    (source_dir / f"{response.id}.json").write_text(
        json.dumps(out, indent=2) + "\n"
    )


def _archive_request_file(request: BridgeRequest) -> None:
    """Move the bridge_request file to bridge_requests/<id>.json for audit."""
    req_file = request.source_path / ".claude" / "bridge_request"
    if not req_file.exists():
        return
    archive_dir = request.source_path / ".claude" / "bridge_requests"
    archive_dir.mkdir(parents=True, exist_ok=True)
    req_file.rename(archive_dir / f"{request.id}.json")
