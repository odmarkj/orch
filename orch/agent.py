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


# ── tmux session management ──────────────────────────────────────────────────

def session_name(project: "Project") -> str:
    """Canonical tmux session name for a project."""
    return f"orch-{project.name}"


def session_exists(project: "Project") -> bool:
    """Check if a tmux session exists for this project."""
    result = vm_exec(
        f"tmux has-session -t {shlex.quote(session_name(project))} 2>/dev/null",
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
    """Kill a tmux session and everything it spawned.

    The primary mechanism is stopping the systemd scope that sandbox_cmd()
    creates — this kills every process in the scope, even daemonized ones.
    The pane-PID cleanup is a fallback for sessions started before scopes
    were introduced or if the scope somehow fails.
    """
    # 1. Stop the systemd scope — kills everything the session spawned
    vm_exec(
        f"sudo systemctl stop {shlex.quote(name)}.scope 2>/dev/null",
        timeout=10,
    )

    # 2. Get pane PIDs before destroying the tmux session (fallback)
    result = vm_exec(
        f"tmux list-panes -t {shlex.quote(name)} -F '#{{pane_pid}}' 2>/dev/null",
        timeout=5,
    )
    pane_pids = []
    if result.returncode == 0 and result.stdout.strip():
        pane_pids = result.stdout.strip().splitlines()

    # 3. Kill the tmux session
    vm_exec(f"tmux kill-session -t {shlex.quote(name)} 2>/dev/null", timeout=10)

    # 4. Kill any surviving processes from the pane tree (fallback)
    for pid in pane_pids:
        pid = pid.strip()
        if pid.isdigit():
            vm_exec(
                f"sudo kill -- -$(ps -o pgid= -p {pid} | tr -d ' ') 2>/dev/null; "
                f"sudo pkill -TERM -P {pid} 2>/dev/null; "
                f"sudo kill -TERM {pid} 2>/dev/null",
                timeout=5,
            )


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
    """List all orch tmux sessions inside the VM.

    Returns a list of dicts with 'name', 'created', 'attached' keys.
    """
    result = vm_exec(
        "tmux list-sessions -F '#{session_name}|#{session_created}|#{session_attached}' 2>/dev/null",
        timeout=10,
    )
    if result.returncode != 0:
        return []

    sessions = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) >= 3 and parts[0].startswith("orch-"):
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


def remove_worktree(
    project: "Project", worktree_path: Path, branch_name: str = "",
) -> None:
    """Remove a git worktree and delete the local branch if pushed."""
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        capture_output=True, text=True,
        cwd=str(project.path), timeout=30,
    )

    if branch_name:
        check = subprocess.run(
            ["git", "branch", "-r", "--list", f"origin/{branch_name}"],
            capture_output=True, text=True,
            cwd=str(project.path), timeout=10,
        )
        if check.stdout.strip():
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
                review_file = worktree_path / ".claude" / "last_review.md"
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
    sessions_file = project.claude_dir / "sessions.json"
    if sessions_file.exists():
        try:
            data = json.loads(sessions_file.read_text())
            session_id = data.get("active")
            if session_id:
                return f"{base} --resume {session_id}"
        except (json.JSONDecodeError, KeyError):
            pass

    return base
