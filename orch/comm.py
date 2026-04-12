"""
Cross-project agent communication.

Simplified replacement for bridge_comm.py — same protocol, but uses
vm_exec / run_headless instead of Docker containers.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
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
    """Read and validate ``.orch/bridge_request``. Returns None if invalid."""
    req_file = project.bridge_request_file
    if not req_file.exists():
        return None

    try:
        data = json.loads(req_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    for key in ("target", "intent", "summary", "context", "request"):
        if key not in data or not str(data[key]).strip():
            return None

    if data["intent"] not in ("fix", "review", "query", "inform"):
        return None

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


# ── Prompt building ──────────────────────────────────────────────────────────

def _build_bridge_prompt(request: BridgeRequest) -> str:
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

    intent_instructions = {
        "fix": [
            "- Make the requested code changes in this project",
            "- Save all changes. Do not commit or push.",
            "- Write a brief summary of what you changed to .orch/bridge_result",
        ],
        "review": [
            "- Review the relevant code and provide feedback",
            "- Write your review to .orch/bridge_result",
        ],
        "query": [
            "- Answer the question based on this project's code",
            "- Write your answer to .orch/bridge_result",
        ],
        "inform": [
            "- Read and acknowledge the information provided",
            "- If any action is warranted, note it in .orch/bridge_result",
        ],
    }

    lines += ["## Instructions", *intent_instructions.get(request.intent, []), ""]
    lines += [
        f"If you cannot complete the request without more information from the",
        f"source project, start your final output with {CLARIFICATION_MARKER}",
        "followed by your specific question on the next line.",
    ]

    return "\n".join(lines)


# ── Main pipeline ────────────────────────────────────────────────────────────

def handle_bridge_request(
    request: BridgeRequest,
    all_projects: list["Project"],
) -> BridgeResponse:
    """Execute the full bridge pipeline.

    1. Locate target project
    2. Create worktree
    3. Run Claude subagent in VM
    4. Handle optional clarification
    5. Post-process by intent (commit/push/PR for fix)
    6. Deliver response and archive request
    """
    from .agent import (
        create_worktree, remove_worktree, run_headless,
        _run_git, _create_pr,
    )
    from .vm import vm_ensure_running

    # 1. Find target
    target = None
    for p in all_projects:
        if p.name == request.target:
            target = p
            break

    if target is None:
        resp = BridgeResponse(
            id=request.id, source=request.source_project,
            target=request.target, intent=request.intent,
            summary=request.summary, status="failed",
            result=f"Target project '{request.target}' not found.",
        )
        _deliver_response(request, resp)
        _archive_request_file(request)
        return resp

    worktree_path = None
    branch_name = ""

    try:
        # 2. Create worktree
        worktree_path, branch_name = create_worktree(
            target, request.summary, branch_prefix="bridge",
        )
        vm_ensure_running()

        # Write bridge depth
        depth_dir = worktree_path / ".orch"
        depth_dir.mkdir(parents=True, exist_ok=True)
        (depth_dir / "_bridge_depth").write_text(str(request.depth + 1))

        # 3. Run subagent
        prompt = _build_bridge_prompt(request)
        result = run_headless(
            target, prompt, workdir=worktree_path,
            allowed_dirs=[str(worktree_path), str(request.source_path)],
            timeout=600,
        )
        output = result.stdout.strip()

        # 4. Clarification round
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
                result = run_headless(
                    target, followup, workdir=worktree_path, timeout=600,
                )
                output = result.stdout.strip()

        # Read bridge_result file
        result_file = worktree_path / ".orch" / "bridge_result"
        result_text = ""
        if result_file.exists():
            result_text = result_file.read_text().strip()
        if not result_text:
            result_text = output

        # 5. Post-process
        pr_url = None
        if request.intent == "fix":
            _run_git(target, worktree_path, ["add", "-A"], timeout=10)
            status_check = _run_git(
                target, worktree_path, ["status", "--porcelain"], timeout=10,
            )
            if status_check.stdout.strip():
                _run_git(
                    target, worktree_path,
                    ["commit", "-m", f"bridge: {request.summary[:60]}"],
                    timeout=30,
                )
                delays = [2, 4, 8, 16]
                for attempt in range(5):
                    push = _run_git(
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
            id=request.id, source=request.source_project,
            target=request.target, intent=request.intent,
            summary=request.summary, status="completed",
            result=result_text, pr_url=pr_url,
            branch=branch_name if request.intent == "fix" else None,
        )

    except Exception as exc:
        resp = BridgeResponse(
            id=request.id, source=request.source_project,
            target=request.target, intent=request.intent,
            summary=request.summary, status="failed",
            result=str(exc),
        )

    finally:
        if worktree_path is not None:
            try:
                remove_worktree(target, worktree_path, branch_name)
            except Exception:
                pass

    _deliver_response(request, resp)
    _archive_request_file(request)
    return resp


# ── Clarification ────────────────────────────────────────────────────────────

def _run_clarification(request: BridgeRequest, question: str) -> str:
    """Ask the source project to answer the subagent's clarification."""
    from .agent import run_headless
    from .models import Project

    prompt = (
        f"A bridge subagent working on project \"{request.target}\" needs "
        f"clarification to complete this task:\n\n"
        f"## Original request\n{request.request}\n\n"
        f"## Their question\n{question}\n\n"
        "Answer concisely based on this project's code. "
        "Write your answer to stdout — it will be forwarded."
    )
    source = Project(path=request.source_path)
    result = run_headless(source, prompt, timeout=120)
    return result.stdout.strip() if result.returncode == 0 else "(no answer)"


# ── Response delivery ────────────────────────────────────────────────────────

def _deliver_response(request: BridgeRequest, response: BridgeResponse) -> None:
    """Write response JSON to the source project's bridge_responses/."""
    source_dir = request.source_path / ".orch" / "bridge_responses"
    source_dir.mkdir(parents=True, exist_ok=True)
    out = {
        "id": response.id, "source": response.source,
        "target": response.target, "intent": response.intent,
        "summary": response.summary, "status": response.status,
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
    """Move bridge_request to bridge_requests/<id>.json for audit."""
    req_file = request.source_path / ".orch" / "bridge_request"
    if not req_file.exists():
        return
    archive_dir = request.source_path / ".orch" / "bridge_requests"
    archive_dir.mkdir(parents=True, exist_ok=True)
    req_file.rename(archive_dir / f"{request.id}.json")
