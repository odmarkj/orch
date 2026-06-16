"""
orch agent session management — tmux sessions and headless Claude runs.

Each project gets a named tmux session inside the Lima VM.  Automated
dispatch (headless) runs Claude as a direct subprocess without tmux.

Also contains worktree management (moved from container.py).
"""

from __future__ import annotations

import random
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .vm import vm_exec, vm_exec_sandboxed, vm_ensure_running, vm_is_running

if TYPE_CHECKING:
    from .models import Project


# ── session management ───────────────────────────────────────────────────────


def session_name(project: "Project") -> str:
    """Canonical session/scope name for a project."""
    return f"orch-{project.name}"


def session_exists(project: "Project") -> bool:
    """Check if a session is running for this project.

    Checks per-window PID files first (interactive SSH sessions), then
    falls back to tmux (headless/dispatch sessions). Each iTerm window
    writes its own /tmp/orch-{project}-{pid}.pid; the project is
    considered active if any such file has a live PID.
    """
    name = session_name(project)
    # Check per-window PID files (interactive sessions via SSH).
    # Any live PID means the project has at least one active window.
    result = vm_exec(
        f'for f in /tmp/{name}-*.pid; do '
        f'  [ -f "$f" ] || continue; '
        f'  p=$(cat "$f" 2>/dev/null); '
        f'  [ -n "$p" ] && kill -0 "$p" 2>/dev/null && exit 0; '
        f'done; exit 1',
        timeout=5,
    )
    if result.returncode == 0:
        return True
    # Fall back to tmux (headless sessions)
    result = vm_exec(
        f"tmux has-session -t {shlex.quote(name)} 2>/dev/null",
        timeout=10,
    )
    return result.returncode == 0


def start_session(project: "Project") -> str:
    """Start a Claude session in a new tmux session inside the VM.

    Returns the tmux session name.
    """
    from .vm import sandbox_cmd

    vm_ensure_running()
    name = session_name(project)

    # If session already exists, just return it
    if session_exists(project):
        return name

    # First session → fire hook before creating
    fire_first_session_hook(project)

    # Update stack detection if stale (cheap, local-only)
    _maybe_update_stack_detection(project)

    claude_cmd = _build_claude_cmd(project)
    project_dir = str(project.path)

    # Wrap in sandbox + systemd scope so cleanup kills everything
    sandboxed = sandbox_cmd(
        f"cd {shlex.quote(project_dir)} && {claude_cmd}",
        writable_dirs=[project_dir],
        scope=name,
    )

    # Create a new detached tmux session running sandboxed Claude
    vm_exec(
        f"tmux new-session -d -s {shlex.quote(name)} "
        f"-c {shlex.quote(project_dir)} "
        f"{shlex.quote(sandboxed)}",
        timeout=15,
    )

    return name


def _kill_session_tree(name: str) -> None:
    """Kill a session and everything it spawned.

    `name` can be either form:
      - orch-{project}          → kill ALL windows + systemd scope + tmux
      - orch-{project}-{pid}    → kill only that specific window

    For interactive sessions: kill the process from the PID file(s) and clean up.
    For headless sessions: stop the systemd scope and/or tmux session.
    """
    base, _, tail = name.rpartition("-")
    if tail.isdigit() and base.startswith("orch-"):
        # Per-window form — kill just this window.
        pid_file = f"/tmp/{name}.pid"
        wt_file = f"/tmp/{name}.worktree"
        vm_exec(
            f'kill -TERM {tail} 2>/dev/null; '
            f'rm -f {shlex.quote(pid_file)} {shlex.quote(wt_file)}',
            timeout=5,
        )
        return

    # Project-level form — kill all windows for this project, plus
    # any headless artifacts (systemd scope, tmux session).
    # 1. Kill every per-window interactive session.
    vm_exec(
        f'for f in /tmp/{name}-*.pid; do '
        f'  [ -f "$f" ] || continue; '
        f'  p=$(cat "$f" 2>/dev/null); '
        f'  [ -n "$p" ] && kill -TERM "$p" 2>/dev/null; '
        f'  rm -f "$f" "${{f%.pid}}.worktree"; '
        f'done',
        timeout=10,
    )
    # 2. Stop systemd scope (headless sessions)
    vm_exec(
        f"sudo systemctl stop {shlex.quote(name)}.scope 2>/dev/null",
        timeout=10,
    )
    # 3. Kill any tmux session (headless/legacy)
    vm_exec(f"tmux kill-session -t {shlex.quote(name)} 2>/dev/null", timeout=10)


def kill_session(project: "Project") -> bool:
    """Kill the tmux session for a project.

    Returns True if this was the last session (on_last_session hook fired).
    """
    was_running = session_exists(project)
    name = session_name(project)
    _kill_session_tree(name)

    if was_running:
        fire_last_session_hook(project)
        return True
    return False


def list_sessions() -> list[dict]:
    """List all orch sessions inside the VM.

    Checks per-window PID files (interactive SSH) and tmux sessions
    (headless/dispatch). Returns one entry per live window; a project
    with four iTerm windows produces four entries. Each dict has
    'name', 'project', 'created', 'attached' keys, where 'name' is
    unique per window (orch-{project}-{pid} for interactive, or
    orch-{project} for tmux).
    """
    sessions: list[dict] = []
    seen_tmux: set[str] = set()

    # Per-window PID files: /tmp/orch-{project}-{pid}.pid.
    # A window is "attached" if its process still has a controlling TTY
    # (meaning the SSH connection is alive).  No TTY = SSH dropped.
    # Gather everything in one shell pass to avoid N vm_exec round-trips.
    # The literal glob is harmless if no files match: cat fails silently
    # and $p is empty, so the continue below skips the iteration.
    result = vm_exec(
        'for f in /tmp/orch-*-*.pid; do '
        '  [ -f "$f" ] || continue; '
        '  p=$(cat "$f" 2>/dev/null); '
        '  [ -z "$p" ] && continue; '
        '  if ! kill -0 "$p" 2>/dev/null; then rm -f "$f" "${f%.pid}.worktree"; continue; fi; '
        '  t=$(ps -o tty= -p "$p" 2>/dev/null | tr -d " "); '
        '  w=""; '
        '  if [ -f "${f%.pid}.worktree" ]; then w=$(cat "${f%.pid}.worktree" 2>/dev/null); fi; '
        '  echo "$f|$p|$t|$w"; '
        'done',
        timeout=10,
    )
    if result.returncode == 0:
        for line in result.stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) < 3:
                continue
            pid_path = parts[0]
            pid = parts[1]
            tty = parts[2]
            wt_id = parts[3] if len(parts) > 3 else ""
            # /tmp/orch-{project}-{pid}.pid → orch-{project}-{pid}
            fname = pid_path.rsplit("/", 1)[-1].removesuffix(".pid")
            # Split trailing -{pid}
            base, _, tail = fname.rpartition("-")
            if not tail.isdigit() or not base.startswith("orch-"):
                continue
            attached = bool(tty) and tty != "?"
            sessions.append({
                "name": fname,
                "project": base.removeprefix("orch-"),
                "created": "",
                "attached": attached,
                "pid": pid,
                "worktree_id": wt_id,
            })

    # Also check tmux sessions (headless/dispatch)
    result = vm_exec(
        "tmux list-sessions -F '#{session_name}|#{session_created}|#{session_attached}' 2>/dev/null",
        timeout=10,
    )
    if result.returncode == 0:
        for line in result.stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) >= 3 and parts[0].startswith("orch-") and parts[0] not in seen_tmux:
                seen_tmux.add(parts[0])
                sessions.append({
                    "name": parts[0],
                    "project": parts[0].removeprefix("orch-"),
                    "created": parts[1],
                    "attached": parts[2] != "0",
                })

    return sessions


# ── Session lifecycle hooks ──────────────────────────────────────────────────

def run_session_hook(project: "Project", hook_cmd: str) -> None:
    """Execute a hook command inside the VM (unsandboxed).

    Errors are logged but never block session start/stop.
    """
    if not vm_is_running():
        return
    try:
        vm_exec(hook_cmd, cwd=str(project.path), timeout=60)
    except Exception:
        pass


def fire_first_session_hook(project: "Project") -> None:
    """Fire on_first_session hook if configured."""
    hook = project.on_first_session_hook
    if hook:
        run_session_hook(project, hook)


def fire_last_session_hook(project: "Project") -> None:
    """Fire on_last_session hook if configured."""
    hook = project.on_last_session_hook
    if hook:
        run_session_hook(project, hook)


def _maybe_update_stack_detection(project: "Project") -> None:
    """Write .claude-docs/project-stack.md if missing or stale (>24h).

    This is a cheap local-only operation (file reads), safe to run on
    every session start. It tells Claude which best-practices files to
    prioritize based on the project's detected tech stack.
    """
    import os
    from datetime import datetime, timedelta

    stack_file = project.path / ".claude-docs" / "project-stack.md"
    docs_dir = project.path / ".claude-docs"

    # Skip if .claude-docs/ doesn't exist (project not init'd with orch)
    if not docs_dir.is_dir():
        return

    # Only regenerate if missing or older than 24 hours
    if stack_file.exists():
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(stack_file))
            if datetime.now() - mtime < timedelta(hours=24):
                return
        except OSError:
            pass

    try:
        from .stack import generate_project_stack_md
        content = generate_project_stack_md(project.path)
        if content:
            stack_file.write_text(content)
    except Exception:
        pass  # Never let detection failure block a session


# ── Headless Claude execution ────────────────────────────────────────────────

def run_headless(
    project: "Project",
    prompt: str,
    *,
    workdir: str | Path | None = None,
    timeout: int = 600,
    allowed_dirs: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run Claude headlessly with a prompt, capturing output.

    Used by auto-dispatch and bridge communication. Filesystem writes
    are sandboxed to the project directory (and any extra allowed_dirs).
    """
    vm_ensure_running()

    if workdir is None:
        workdir = str(project.path)

    safe_prompt = prompt.replace("'", "'\\''")
    dirs_flag = ""
    if allowed_dirs:
        dirs_flag = " ".join(
            f"--add-dir {shlex.quote(d)}" for d in allowed_dirs
        )

    cmd = f"claude --dangerously-skip-permissions {dirs_flag} -p '{safe_prompt}'"

    writable = [str(project.path)]
    if allowed_dirs:
        writable.extend(allowed_dirs)

    return vm_exec_sandboxed(
        cmd, cwd=workdir, writable_dirs=writable, timeout=timeout,
    )


# ── Worktree management ─────────────────────────────────────────────────────

def _slugify(text: str, max_len: int = 30) -> str:
    """Turn a todo description into a safe branch/directory name slug."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len]


def _ensure_worktrees_gitignored(project: "Project") -> None:
    """Add .orch-worktrees to the project's .gitignore if not already present."""
    gitignore = project.path / ".gitignore"
    entry = ".orch-worktrees"
    try:
        if gitignore.exists():
            content = gitignore.read_text()
            if entry in content.splitlines():
                return
            if content and not content.endswith("\n"):
                content += "\n"
            content += f"{entry}\n"
            gitignore.write_text(content)
        else:
            gitignore.write_text(f"{entry}\n")
    except OSError:
        pass


def create_worktree(
    project: "Project", todo_text: str, *, branch_prefix: str = "auto",
) -> tuple[Path, str]:
    """Create a git worktree for the given task.

    Returns (worktree_path, branch_name).
    """
    _ensure_worktrees_gitignored(project)

    slug = _slugify(todo_text)
    suffix = random.randint(1000, 9999)
    branch_name = f"{branch_prefix}/{slug}-{suffix}"
    worktree_dir = project.path.parent / ".orch-worktrees" / f"{project.name}-{slug}-{suffix}"

    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_dir)],
        capture_output=True, text=True,
        cwd=str(project.path), timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr}")

    return worktree_dir, branch_name


def detect_main_branch(project: "Project") -> str:
    """Return the project's main branch name (e.g. 'main' or 'master').

    Tries origin/HEAD → origin/main → origin/master → current HEAD.
    """
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True,
        cwd=str(project.path), timeout=10,
    )
    if result.returncode == 0 and result.stdout.strip():
        ref = result.stdout.strip()
        if "/" in ref:
            return ref.split("/", 1)[1]
        return ref

    for candidate in ("main", "master"):
        chk = subprocess.run(
            ["git", "rev-parse", "--verify", f"origin/{candidate}"],
            capture_output=True, text=True,
            cwd=str(project.path), timeout=10,
        )
        if chk.returncode == 0:
            return candidate

    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
        cwd=str(project.path), timeout=10,
    )
    if head.returncode == 0 and head.stdout.strip() not in ("HEAD", ""):
        return head.stdout.strip()
    return "main"


def create_session_worktree(project: "Project") -> tuple[Path, str, str, str]:
    """Create a worktree for a `w` session, branched off the project's main.

    Returns (worktree_path, branch_name, base_branch, wt_id).
    The wt_id is also the SQLite row id and the correlation_id written to
    /tmp/orch-{project}-{pid}.worktree so list_sessions can pair pids to
    worktrees.
    """
    from .state import new_worktree_id

    _ensure_worktrees_gitignored(project)

    wt_id = new_worktree_id()
    # branch name strips the "wt_" prefix for aesthetics
    branch_name = f"claude/{wt_id[3:]}"
    worktree_dir = project.path.parent / ".orch-worktrees" / project.name / wt_id
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    base = detect_main_branch(project)
    base_ref = f"origin/{base}"
    if subprocess.run(
        ["git", "rev-parse", "--verify", base_ref],
        capture_output=True, cwd=str(project.path), timeout=10,
    ).returncode != 0:
        base_ref = base

    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_dir), base_ref],
        capture_output=True, text=True,
        cwd=str(project.path), timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")

    # Pre-create .orch/ so Claude can write status etc. without racing.
    (worktree_dir / ".orch").mkdir(parents=True, exist_ok=True)

    return worktree_dir, branch_name, base, wt_id


def remove_worktree(
    project: "Project", worktree_path: Path, branch_name: str = "",
    *, force_delete_branch: bool = False,
) -> None:
    """Remove a git worktree and (optionally) delete the local branch.

    By default the branch is only deleted if it was pushed (preserves
    bridge_worker behavior). Pass force_delete_branch=True to always
    drop the local branch — used by session worktree cleanup where the
    branch may never have been pushed.
    """
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        capture_output=True, text=True,
        cwd=str(project.path), timeout=30,
    )

    if not branch_name:
        return

    should_delete = force_delete_branch
    if not should_delete:
        check = subprocess.run(
            ["git", "branch", "-r", "--list", f"origin/{branch_name}"],
            capture_output=True, text=True,
            cwd=str(project.path), timeout=10,
        )
        should_delete = bool(check.stdout.strip())

    if should_delete:
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            capture_output=True, text=True,
            cwd=str(project.path), timeout=10,
        )


# ── Git operations (run inside VM for direnv/mise toolchain) ─────────────────

def _run_git(
    project: "Project",
    worktree_path: Path,
    git_args: list[str],
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """Run a git command inside the VM at *worktree_path*."""
    cmd_str = " ".join(shlex.quote(a) for a in ["git"] + git_args)
    return vm_exec(cmd_str, cwd=str(worktree_path), timeout=timeout)


# ── Test / review / commit / PR pipeline ─────────────────────────────────────

def _run_tests(
    project: "Project", worktree_path: Path, test_cmd: str, timeout: int = 300,
) -> tuple[bool, str]:
    """Run the project's test command inside the VM.

    Returns (passed, output).
    """
    result = vm_exec(test_cmd, cwd=str(worktree_path), timeout=timeout)
    output = ""
    if result.stdout:
        output += result.stdout
    if result.stderr:
        output += "\n" + result.stderr
    output = output.strip()
    if len(output) > 8000:
        output = output[-8000:]
    return result.returncode == 0, output


def _run_code_review(
    project: "Project", worktree_path: Path, branch_name: str,
) -> str:
    """Run Claude code review on worktree changes. Returns review text."""
    diff_result = _run_git(project, worktree_path, ["diff", "HEAD~1"], timeout=30)
    diff_text = diff_result.stdout.strip()
    if not diff_text:
        return ""

    review_prompt = (
        "Review the following code changes. Be concise. "
        "Flag any bugs, security issues, or significant problems. "
        "If the changes look good, say so briefly.\n\n"
        f"```diff\n{diff_text[:8000]}\n```"
    )

    result = run_headless(project, review_prompt, workdir=worktree_path, timeout=120)
    return result.stdout.strip() if result.returncode == 0 else ""


def _commit_and_push(
    project: "Project", worktree_path: Path, branch_name: str, todo_text: str,
) -> None:
    """Stage, commit, and push the worktree branch."""
    _run_git(project, worktree_path, ["add", "-A"], timeout=30)

    status = _run_git(project, worktree_path, ["status", "--porcelain"], timeout=10)
    if not status.stdout.strip():
        return

    safe_msg = todo_text[:72]
    _run_git(project, worktree_path, ["commit", "-m", f"auto: {safe_msg}"], timeout=30)

    delays = [2, 4, 8, 16]
    for attempt in range(5):
        result = _run_git(
            project, worktree_path,
            ["push", "-u", "origin", branch_name],
            timeout=60,
        )
        if result.returncode == 0:
            return
        if attempt < len(delays):
            time.sleep(delays[attempt])


def _create_pr(
    project: "Project",
    worktree_path: Path,
    branch_name: str,
    todo_text: str,
    review_text: str = "",
    title_prefix: str = "auto",
) -> str | None:
    """Create a PR via gh CLI inside the VM. Returns the PR URL or None."""
    body = f"## Auto-dispatched task\n\n{todo_text}\n"
    if review_text:
        body += f"\n## Code Review\n\n{review_text}\n"

    safe_title = f"{title_prefix}: {todo_text[:60]}"
    safe_body = body.replace("'", "'\\''")
    safe_title_sh = safe_title.replace("'", "'\\''")

    gh_cmd = (
        f"gh pr create --title '{safe_title_sh}' "
        f"--body '{safe_body}' --head '{branch_name}'"
    )
    result = vm_exec(gh_cmd, cwd=str(worktree_path), timeout=30)
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def run_task_in_worktree(project: "Project", todo_text: str) -> dict:
    """Full pipeline: worktree -> Claude -> test-fix -> review -> commit -> push -> PR.

    Returns a dict: {branch, worktree, pr_url, review, test_attempts, tests_passed}.
    """
    worktree_path, branch_name = create_worktree(project, todo_text)
    results = {
        "branch": branch_name,
        "worktree": str(worktree_path),
        "pr_url": None,
        "review": "",
        "test_attempts": 0,
        "tests_passed": None,
    }

    test_cmd = project.test_cmd
    max_fix = project.max_fix_attempts if test_cmd else 0

    try:
        vm_ensure_running()

        # ── Initial Claude run ──
        task_prompt = (
            f"Work on this task: {todo_text}\n\n"
            f"When done, make sure all changes are saved. Do not commit or push."
        )
        run_headless(project, task_prompt, workdir=worktree_path, timeout=600)

        # ── Test-fix loop ──
        if test_cmd:
            for attempt in range(1, max_fix + 2):
                results["test_attempts"] = attempt
                passed, test_output = _run_tests(project, worktree_path, test_cmd)

                if passed:
                    results["tests_passed"] = True
                    break

                if attempt > max_fix:
                    results["tests_passed"] = False
                    break

                fix_prompt = (
                    f"The tests failed (attempt {attempt}/{max_fix}). "
                    f"Fix the failing tests and make sure all changes are saved. "
                    f"Do not commit or push.\n\n"
                    f"Test command: {test_cmd}\n\n"
                    f"Test output:\n```\n{test_output}\n```"
                )
                run_headless(project, fix_prompt, workdir=worktree_path, timeout=600)

        # ── Code review (if enabled) ──
        if project.code_review_enabled:
            _run_git(project, worktree_path, ["add", "-A"], timeout=10)
            _run_git(
                project, worktree_path,
                ["commit", "-m", f"wip: {todo_text[:50]}"],
                timeout=10,
            )
            review = _run_code_review(project, worktree_path, branch_name)
            results["review"] = review

            if review:
                review_file = worktree_path / ".orch" / "last_review.md"
                review_file.parent.mkdir(parents=True, exist_ok=True)
                review_file.write_text(review)

            _run_git(
                project, worktree_path,
                ["commit", "--amend", "-m", f"auto: {todo_text[:72]}"],
                timeout=10,
            )
            delays = [2, 4, 8, 16]
            for attempt in range(5):
                result = _run_git(
                    project, worktree_path,
                    ["push", "-u", "origin", branch_name, "--force-with-lease"],
                    timeout=60,
                )
                if result.returncode == 0:
                    break
                if attempt < len(delays):
                    time.sleep(delays[attempt])
        else:
            _commit_and_push(project, worktree_path, branch_name, todo_text)

        # ── Create PR ──
        pr_url = _create_pr(
            project, worktree_path, branch_name, todo_text, results.get("review", ""),
        )
        results["pr_url"] = pr_url

    except Exception:
        try:
            remove_worktree(project, worktree_path, branch_name)
        except Exception:
            pass
        raise

    return results


# ── Helpers ──────────────────────────────────────────────────────────────────

def _build_claude_cmd(project: "Project") -> str:
    """Build the claude CLI invocation string for interactive sessions."""
    import json

    base = "claude --dangerously-skip-permissions"

    # Resume active session if available
    sessions_file = project.orch_dir / "sessions.json"
    if sessions_file.exists():
        try:
            data = json.loads(sessions_file.read_text())
            session_id = data.get("active")
            if session_id:
                return f"{base} --resume {session_id}"
        except (json.JSONDecodeError, KeyError):
            pass

    return base
