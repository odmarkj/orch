"""
orch bridge worker — executes a single bridge end-to-end inside the daemon.

Replaces ``comm.handle_bridge_request``. Same pipeline (worktree, headless
Claude, optional clarification, intent post-processing, PR creation) but
talks to the SQLite state layer instead of files, and classifies failures
as transient vs permanent so the janitor can auto-retry the right ones.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import state
from .discovery import discover_projects
from .models import Project


CLARIFICATION_MARKER = "[NEEDS_CLARIFICATION]"

# Cap per-stream byte length stored in the event log so a runaway Claude
# can't bloat the SQLite DB. Tail-bias because errors are usually at the
# bottom of stdout/stderr.
_OUTPUT_TAIL_BYTES = 16_000


def _tail(text: str | None, limit: int = _OUTPUT_TAIL_BYTES) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return "…[truncated " + str(len(text) - limit) + " chars]…\n" + text[-limit:]


def _record_headless_output(
    bid: str,
    phase: str,
    *,
    returncode: int | None,
    stdout: str | None,
    stderr: str | None,
    timed_out: bool = False,
) -> None:
    """Persist captured headless output as a bridge_event so it survives the
    worktree teardown. Always emitted — failure or success."""
    state.add_event(bid, "headless_output", {
        "phase": phase,
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout": _tail(stdout),
        "stderr": _tail(stderr),
    })


def _run_headless_capture(
    target: Project, prompt: str, *, bid: str, phase: str, **kwargs,
) -> tuple[str, str]:
    """Run a headless Claude turn, persisting both streams to the event log
    regardless of outcome. Returns (stdout, stderr). Raises TransientBridgeError
    on subprocess error, timeout, or non-zero exit; the failure path includes
    the captured streams so callers don't need to unpack again."""
    from .agent import run_headless

    try:
        result = run_headless(target, prompt, **kwargs)
    except subprocess.TimeoutExpired as e:
        # TimeoutExpired carries whatever streams were collected before the
        # timeout fired. Capture them, log, then surface as transient.
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        _record_headless_output(
            bid, phase, returncode=None, stdout=out, stderr=err, timed_out=True,
        )
        raise TransientBridgeError(
            f"headless {phase} timed out after {e.timeout}s"
            + (f"; stderr tail: {_tail(err, 400)}" if err.strip() else "")
        ) from e
    except Exception as e:
        _record_headless_output(
            bid, phase, returncode=None, stdout=None, stderr=repr(e),
        )
        raise TransientBridgeError(f"headless {phase} errored: {e}") from e

    out = result.stdout or ""
    err = result.stderr or ""
    _record_headless_output(
        bid, phase, returncode=result.returncode, stdout=out, stderr=err,
    )
    if result.returncode != 0:
        raise TransientBridgeError(
            f"headless {phase} exited {result.returncode}"
            + (f"; stderr tail: {_tail(err, 400)}" if err.strip() else "")
            + (f"; stdout tail: {_tail(out, 400)}" if not err.strip() and out.strip() else "")
        )
    return out, err


# ── Errors ─────────────────────────────────────────────────────────────────

class PermanentBridgeError(Exception):
    """Raised when a bridge cannot succeed regardless of retries."""


class TransientBridgeError(Exception):
    """Raised when a bridge failed but a retry might succeed (VM down,
    network blip, git push race, Claude API rate-limit, etc)."""


# ── Prompt ─────────────────────────────────────────────────────────────────

def _build_prompt(b: dict) -> str:
    lines = [
        f'You are handling a bridge request from project "{b["source_project"]}".',
        "",
        "## What they need",
        b["request"],
        "",
        f"## Context from {b['source_project']}",
        b["context"],
        "",
        "## Source project code (read-only)",
        f"The source project is available at: {b['source_path']}",
        "You may read files there for context but do NOT modify them.",
        "",
    ]
    if b.get("relevant_files"):
        lines += [
            "## Relevant files in this project",
            *(f"- {f}" for f in b["relevant_files"]),
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
    lines += ["## Instructions", *intent_instructions.get(b["intent"], []), ""]
    lines += [
        f"If you cannot complete the request without more information from the",
        f"source project, start your final output with {CLARIFICATION_MARKER}",
        "followed by your specific question on the next line.",
    ]
    return "\n".join(lines)


# ── Main entry point ───────────────────────────────────────────────────────

def run_bridge(bridge: dict) -> None:
    """Execute one bridge to completion. Updates SQLite directly.

    On success: marks completed with result/pr_url/branch.
    On permanent failure: marks rejected.
    On transient failure: marks failed with error_class=transient and
    next_retry_at populated, so the janitor will requeue.
    """
    bid = bridge["id"]

    try:
        from .agent import (
            create_worktree, remove_worktree, run_headless,
            _run_git, _create_pr,
        )
        from .vm import vm_ensure_running

        target = _find_target(bridge["target_project"])
        if target is None:
            raise PermanentBridgeError(
                f"target project {bridge['target_project']!r} not found"
            )

        if _project_disabled_as_target(target):
            raise PermanentBridgeError(
                f"target project {target.name!r} has bridges disabled"
            )

        try:
            vm_ensure_running()
        except Exception as e:
            raise TransientBridgeError(f"VM not available: {e}") from e

        worktree_path: Path | None = None
        branch_name = ""
        try:
            try:
                worktree_path, branch_name = create_worktree(
                    target, bridge["summary"], branch_prefix="bridge",
                )
            except RuntimeError as e:
                raise TransientBridgeError(f"worktree creation failed: {e}") from e

            state.set_inflight_meta(
                bid,
                worker_pid=os.getpid(),
                worktree_path=str(worktree_path),
                branch=branch_name,
            )

            prompt = _build_prompt(bridge)
            state.add_event(bid, "prompt_sent")
            stdout, _stderr = _run_headless_capture(
                target, prompt, bid=bid, phase="initial",
                workdir=worktree_path,
                allowed_dirs=[str(worktree_path), bridge["source_path"]],
                timeout=600,
            )
            output = stdout.strip()

            if CLARIFICATION_MARKER in output:
                question = output.split(CLARIFICATION_MARKER, 1)[1].strip()
                if question:
                    state.add_event(bid, "clarification_sent", {"question": question[:500]})
                    answer = _run_clarification(bridge, question)
                    state.add_event(bid, "clarification_received", {"answer": answer[:500]})
                    followup = (
                        f"{prompt}\n\n## Clarification\n"
                        f"**Question**: {question}\n"
                        f"**Answer**: {answer}\n\n"
                        f"Continue with your task now."
                    )
                    stdout, _stderr = _run_headless_capture(
                        target, followup, bid=bid, phase="clarification",
                        workdir=worktree_path, timeout=600,
                    )
                    output = stdout.strip()

            result_file = worktree_path / ".orch" / "bridge_result"
            result_text = ""
            if result_file.exists():
                try:
                    result_text = result_file.read_text().strip()
                except OSError:
                    pass
            if not result_text:
                result_text = output

            pr_url = None
            if bridge["intent"] == "fix":
                pr_url = _commit_and_pr(
                    target, worktree_path, branch_name,
                    bridge["summary"], result_text,
                )

            state.mark_completed(
                bid,
                result=result_text,
                pr_url=pr_url,
                branch=branch_name if bridge["intent"] == "fix" else None,
            )
            if pr_url:
                state.add_event(bid, "pr_created", {"pr_url": pr_url})

        finally:
            if worktree_path is not None:
                try:
                    remove_worktree(target, worktree_path, branch_name)
                except Exception:
                    pass

    except PermanentBridgeError as e:
        state.mark_rejected(bid, reason=str(e))
    except TransientBridgeError as e:
        _schedule_retry(bid, str(e))
    except Exception as e:
        # Unknown errors are conservatively transient — better to retry once
        # than silently drop work. The retry budget caps runaway loops.
        _schedule_retry(bid, f"unhandled: {e!r}")


# ── Helpers ─────────────────────────────────────────────────────────────────

def _find_target(name: str) -> Project | None:
    for p in discover_projects():
        if p.name == name:
            return p
    return None


def _project_disabled_as_target(project: Project) -> bool:
    return project._read_orch_config_section_str("bridge", "disabled") == "true"


def _max_retries_for_target(project: Project | None) -> int:
    from .config import bridge_max_retries
    if project is not None:
        val = project._read_orch_config_section_str("bridge", "max_retries")
        if val:
            try:
                return int(val)
            except ValueError:
                pass
    return bridge_max_retries()


def _schedule_retry(bid: str, error: str) -> None:
    """Mark a bridge as transient-failed with an exponential-backoff
    next_retry_at. The janitor requeues when the time arrives."""
    from datetime import datetime, timedelta, timezone
    from .config import bridge_retry_backoff_seconds, bridge_max_retries

    bridge = state.get_bridge(bid)
    if bridge is None:
        return

    target = _find_target(bridge["target_project"])
    max_retries = _max_retries_for_target(target)
    backoff = bridge_retry_backoff_seconds()

    if bridge["retry_count"] >= max_retries:
        # Out of budget — mark as a hard failure (no next_retry_at).
        state.mark_failed(
            bid,
            error=f"out of retries ({max_retries}): {error}",
            error_class=state.ERROR_PERMANENT,
            next_retry_at=None,
        )
        return

    idx = min(bridge["retry_count"], len(backoff) - 1)
    delay = backoff[idx] if backoff else 60
    next_at = (
        datetime.now(timezone.utc) + timedelta(seconds=delay)
    ).isoformat(timespec="microseconds")
    state.mark_failed(
        bid, error=error, error_class=state.ERROR_TRANSIENT, next_retry_at=next_at,
    )


def _run_clarification(bridge: dict, question: str) -> str:
    """Ask the source project for clarification. Best-effort: any failure
    produces "(no answer)" rather than raising — the parent run can still
    finish, just with less information.

    Captures both streams to a `headless_output` event so a confused
    clarification turn is debuggable after the fact.
    """
    from .agent import run_headless

    prompt = (
        f"A bridge subagent working on project \"{bridge['target_project']}\" "
        f"needs clarification to complete this task:\n\n"
        f"## Original request\n{bridge['request']}\n\n"
        f"## Their question\n{question}\n\n"
        "Answer concisely based on this project's code. "
        "Write your answer to stdout — it will be forwarded."
    )
    source = Project(path=Path(bridge["source_path"]))
    try:
        result = run_headless(source, prompt, timeout=120)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        _record_headless_output(
            bridge["id"], "clarification_source",
            returncode=None, stdout=out, stderr=err, timed_out=True,
        )
        return "(no answer)"
    except Exception as e:
        _record_headless_output(
            bridge["id"], "clarification_source",
            returncode=None, stdout=None, stderr=repr(e),
        )
        return "(no answer)"

    _record_headless_output(
        bridge["id"], "clarification_source",
        returncode=result.returncode,
        stdout=result.stdout, stderr=result.stderr,
    )
    if result.returncode == 0:
        return (result.stdout or "").strip() or "(no answer)"
    return "(no answer)"


def _commit_and_pr(
    target: Project, worktree_path: Path, branch_name: str,
    summary: str, result_text: str,
) -> str | None:
    from .agent import _run_git, _create_pr
    import time

    _run_git(target, worktree_path, ["add", "-A"], timeout=10)
    status_check = _run_git(
        target, worktree_path, ["status", "--porcelain"], timeout=10,
    )
    if not status_check.stdout.strip():
        return None

    _run_git(
        target, worktree_path,
        ["commit", "-m", f"bridge: {summary[:60]}"],
        timeout=30,
    )

    delays = [2, 4, 8, 16]
    pushed = False
    for attempt in range(5):
        push = _run_git(
            target, worktree_path,
            ["push", "-u", "origin", branch_name],
            timeout=60,
        )
        if push.returncode == 0:
            pushed = True
            break
        if attempt < len(delays):
            time.sleep(delays[attempt])

    if not pushed:
        raise TransientBridgeError("git push failed after retries")

    return _create_pr(
        target, worktree_path, branch_name,
        summary, result_text, title_prefix="bridge",
    )
