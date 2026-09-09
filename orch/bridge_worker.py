"""
orch bridge worker — executes a single bridge end-to-end inside the daemon.

Replaces ``comm.handle_bridge_request``. Same pipeline (worktree, headless
Claude, optional clarification, intent post-processing, PR creation) but
talks to the SQLite state layer instead of files, and classifies failures
as transient vs permanent so the janitor can auto-retry the right ones.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
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
    regardless of outcome. Returns (stdout, stderr).

    Raises TransientBridgeError on subprocess error, timeout, or non-zero
    exit; the failure path includes the captured streams so callers don't
    need to unpack again. Raises PermanentBridgeError when the command
    never reached the VM because it was too large for the ssh control
    channel — that outcome is a property of the payload, so every retry
    would fail identically and calling it transient just burns the budget
    while telling the submitter to "try again later"."""
    from .agent import run_headless
    from .vm import CommandTooLargeError, is_ssh_undeliverable

    try:
        result = run_headless(target, prompt, **kwargs)
    except CommandTooLargeError as e:
        _record_headless_output(
            bid, phase, returncode=None, stdout=None, stderr=str(e),
        )
        raise PermanentBridgeError(
            f"headless {phase} could not be delivered ({len(prompt)}-byte "
            f"prompt): {e}"
        ) from e
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
        if is_ssh_undeliverable(result.returncode, err):
            # ssh died at session setup — the remote command never ran, so
            # this says nothing about the VM's health and everything about
            # the size of what we tried to send.
            raise PermanentBridgeError(
                f"headless {phase} was never delivered to the VM: ssh failed at "
                f"session setup (rc={result.returncode}) because the command "
                f"exceeded the ssh control-channel limit. An identical retry "
                f"cannot succeed — shrink the request (put long detail in a "
                f"file and reference the path from the bridge). "
                f"stderr tail: {_tail(err, 400)}"
            )
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


@dataclass
class CommitOutcome:
    """Structured result of the commit/push/PR stage.

    "The agent changed nothing" (changed=False) must be distinguishable
    from every other outcome — a bridge that produced work but shipped
    none of it silently destroyed an agent's commits once, precisely
    because both cases collapsed to the same return value.
    """
    changed: bool
    pushed: bool = False
    branch: str | None = None
    pr_url: str | None = None


# ── Prompt ─────────────────────────────────────────────────────────────────

def _build_prompt(b: dict, branch_name: str = "") -> str:
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
    branch_display = branch_name or "the bridge branch"
    intent_instructions = {
        "fix": [
            "- Make the requested code changes in this project",
            "- Save all changes. Do not commit or push.",
            f"- After you exit, the orchestrator commits everything on branch "
            f"`{branch_display}`, pushes it, and opens a PR. Stay on that "
            "branch — do not switch branches, commit, or push yourself.",
            "- Write a brief summary of what you changed to .orch/bridge_result. "
            "It becomes the PR body, so write it as a description of the change.",
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
        from .agent import create_worktree, worktree_head
        from .config import bridge_worker_timeout_seconds
        from .vm import vm_ensure_running

        work_timeout = bridge_worker_timeout_seconds()

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
        base_commit = ""
        try:
            try:
                worktree_path, branch_name = create_worktree(
                    target, bridge["summary"], branch_prefix="bridge", bid=bid,
                )
            except RuntimeError as e:
                raise TransientBridgeError(f"worktree creation failed: {e}") from e

            # HEAD at creation time — lets the teardown guard tell "no
            # commits ever landed here" apart from "local-only commits".
            base_commit = worktree_head(worktree_path)

            state.set_inflight_meta(
                bid,
                worker_pid=os.getpid(),
                worktree_path=str(worktree_path),
                branch=branch_name,
            )

            prompt = _build_prompt(bridge, branch_name=branch_name)
            state.add_event(bid, "prompt_sent")
            stdout, _stderr = _run_headless_capture(
                target, prompt, bid=bid, phase="initial",
                workdir=worktree_path,
                allowed_dirs=[str(worktree_path), bridge["source_path"]],
                timeout=work_timeout,
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
                        workdir=worktree_path, timeout=work_timeout,
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
            pushed_branch = None
            if bridge["intent"] == "fix":
                outcome = _commit_and_pr(
                    target, worktree_path, branch_name,
                    bridge["summary"], result_text,
                    bid=bid, base_commit=base_commit,
                )
                pr_url = outcome.pr_url
                pushed_branch = outcome.branch
                if not outcome.changed:
                    state.add_event(bid, "no_changes", {
                        "note": "agent made no changes; nothing was committed or pushed",
                    })
                elif outcome.pushed and not pr_url:
                    state.add_event(bid, "pr_missing", {
                        "branch": pushed_branch,
                        "warning": "changes were pushed but no PR URL was captured",
                    })

            state.mark_completed(
                bid,
                result=result_text,
                pr_url=pr_url,
                branch=pushed_branch,
            )
            if pr_url:
                state.add_event(bid, "pr_created", {"pr_url": pr_url})

        finally:
            if worktree_path is not None:
                _teardown_worktree(
                    bid, target, worktree_path, branch_name, base_commit,
                )

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
    summary: str, result_text: str, *, bid: str, base_commit: str = "",
) -> CommitOutcome:
    """Commit whatever the agent produced, push it, and open a PR.

    Pushes the branch HEAD is actually on — never a possibly-stale planned
    ref. If the agent moved to another branch, `git push origin <planned>`
    exits 0 while shipping nothing, and the teardown then destroys the only
    copy of the commit. That exact sequence lost real work once.
    """
    from .agent import _run_git, _create_pr, head_on_any_remote, worktree_head
    import time

    _run_git(target, worktree_path, ["add", "-A"], timeout=10)
    status_check = _run_git(
        target, worktree_path, ["status", "--porcelain"], timeout=10,
    )
    dirty = bool(status_check.stdout.strip())

    if not dirty:
        # Nothing to commit — but that is only a genuine no-op if HEAD holds
        # no new local-only commits (an agent that committed its own work
        # must not be mistaken for one that did nothing).
        head_sha = worktree_head(worktree_path)
        if base_commit and head_sha == base_commit:
            return CommitOutcome(changed=False)
        if head_on_any_remote(worktree_path):
            return CommitOutcome(changed=False)
        # Local-only commits with a clean tree: fall through and push them.

    if dirty:
        commit = _run_git(
            target, worktree_path,
            ["commit", "-m", f"bridge: {summary[:60]}"],
            timeout=30,
        )
        if commit.returncode != 0:
            raise TransientBridgeError(
                "git commit failed: "
                + (commit.stderr or commit.stdout or "").strip()[:400]
            )

    head = _run_git(
        target, worktree_path, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=10,
    )
    current = head.stdout.strip()
    if not current:
        raise TransientBridgeError(
            "could not determine checked-out branch before push"
        )

    if current == "HEAD":
        # Detached HEAD: no branch to push by name, so ship the commit to
        # the planned branch ref explicitly. Never push the bare planned
        # branch here — its ref was left behind at the base commit and the
        # push would "succeed" while shipping nothing.
        push_branch = branch_name
        push_args = ["push", "origin", f"HEAD:refs/heads/{branch_name}"]
        state.add_event(bid, "branch_mismatch", {
            "planned": branch_name,
            "actual": "(detached HEAD)",
            "action": f"pushed HEAD to {branch_name}",
        })
    elif current != branch_name:
        # The agent moved to another branch; the work lives there. Push
        # what HEAD actually is and open the PR against it.
        push_branch = current
        push_args = ["push", "-u", "origin", current]
        state.add_event(bid, "branch_mismatch", {
            "planned": branch_name,
            "actual": current,
            "action": f"pushed {current} instead",
        })
        state.set_inflight_meta(bid, branch=current)
    else:
        push_branch = branch_name
        push_args = ["push", "-u", "origin", branch_name]

    delays = [2, 4, 8, 16]
    pushed = False
    for attempt in range(5):
        push = _run_git(target, worktree_path, push_args, timeout=60)
        if push.returncode == 0:
            pushed = True
            break
        if attempt < len(delays):
            time.sleep(delays[attempt])

    if not pushed:
        raise TransientBridgeError(
            f"git push of {push_branch} failed after retries"
        )

    pr_url = _create_pr(
        target, worktree_path, push_branch,
        summary, result_text, title_prefix="bridge",
    )
    if pr_url is None:
        # `gh pr create` fails when the branch already has an open PR — in
        # that case the push above just updated it, which is a success
        # worth recording, not a missing PR.
        pr_url = _existing_pr_url(worktree_path, push_branch)
    return CommitOutcome(changed=True, pushed=True, branch=push_branch, pr_url=pr_url)


def _existing_pr_url(worktree_path: Path, branch: str) -> str | None:
    """URL of an already-open PR for *branch*, or None."""
    from .vm import vm_exec

    try:
        result = vm_exec(
            f"gh pr view {shlex.quote(branch)} --json url --jq .url",
            cwd=str(worktree_path), timeout=30,
        )
    except Exception:
        return None
    url = (result.stdout or "").strip()
    if result.returncode == 0 and url.startswith("http"):
        return url
    return None


def _teardown_worktree(
    bid: str, target: Project, worktree_path: Path,
    branch_name: str, base_commit: str,
) -> None:
    """Remove the bridge worktree — unless it still holds work that never
    reached a remote, in which case leave it in place and make that loud.

    This is a safety net, not a feature: if the push logic is correct it
    should almost never trigger. When it does, the work still exists and
    `orch bridge status` says where. Never raises (runs in a finally).
    """
    from .agent import remove_worktree, worktree_branch, worktree_unpushed_reason

    try:
        reason = worktree_unpushed_reason(worktree_path, base_commit)
    except Exception as e:
        reason = f"could not verify worktree state: {e!r}"

    if reason:
        try:
            state.add_event(bid, "worktree_preserved", {
                "reason": reason,
                "worktree_path": str(worktree_path),
                "planned_branch": branch_name,
                "checked_out_branch": worktree_branch(worktree_path) or "(unknown)",
                "note": "worktree left in place so unpushed work can be recovered",
            })
        except Exception:
            pass
        return

    try:
        remove_worktree(target, worktree_path, branch_name)
    except Exception:
        pass
