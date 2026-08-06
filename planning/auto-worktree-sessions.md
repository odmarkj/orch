# Auto-Worktree Sessions

Goal: add a new `w` shortcut that launches a Claude session inside a fresh
git worktree (auto-named branch off main, automatic cleanup). The existing
`c` shortcut is unchanged — it still runs in the project root and preserves
`/resume` history. `w` is the explicit gesture for "I want concurrency."

Hard requirement: many concurrent `w` sessions on the same codebase without
conflicts.

## Design Decisions

- **D1. Worktree location:** `<project_parent>/.orch-worktrees/<project>/<id>/`.
  Parity with the existing bridge_worker location (`~/Apps/.orch-worktrees/`).
  Virtiofs-visible inside the VM, already gitignored.
- **D2. Branch naming:** `claude/<unix-ms>-<hex4>`. Opaque, never surfaced.
- **D3. Base branch detection:** `origin/HEAD` → `origin/main` → `origin/master`
  → current HEAD. Pass `origin/<base>` to `git worktree add` so we branch off
  fresh remote tip.
- **D4. State in SQLite, not files:** add a `worktrees` table to the daemon's
  `state.db`. Daemon already owns the DB and runs the GC. Survives orch restart.
- **D5. PID↔worktree correlation:** session spawn writes a `correlation_id`
  env var into the shell. Inner cmd writes both `/tmp/orch-{proj}-{pid}.pid`
  AND `/tmp/orch-{proj}-{pid}.worktree` containing the ID. `list_sessions`
  reads both. Race-free.
- **D6. Cleanup trigger:** `_do_refresh_session_cache` already detects
  detached sessions every 15s and calls `_kill_session_tree`. Same code path
  fires `cleanup_closed_session(project, correlation_id)`.
- **D7. Keep-vs-remove at close:**
  - 0 commits beyond base AND clean → remove worktree + delete branch
  - ≥1 commit → keep (GC handles via merged-PR check)
  - dirty + 0 commits → keep (GC handles after 7d)
- **D8. PR-merged GC:** daily, `gh pr list --head <branch> --state merged`
  inside the VM (gh is already auth'd). If merged → remove. Throttled to
  once/24h.
- **D9. Age GC:** dirty-only >7d removed; commits-no-PR >30d marked
  `abandoned`, surfaced via `orch worktrees list`, NOT auto-deleted.
- **D10. `.orch/` lives in worktree** (so Claude can write `status` etc.
  via its cwd). Watcher is extended to schedule worktree `.orch/` dirs +
  JSONL dirs, and remap events back to the original project so UI stays
  unchanged.
- **D11. No `--resume`** for `w` sessions. Every `w` press is a fresh
  Claude session in a fresh worktree. `c` keeps its current resume behavior.
- **D12. Two shortcuts, one project:** `c` runs Claude in the project root
  (today's behavior, has `/resume` history). `w` runs Claude in a worktree
  (no `/resume` history for that worktree, but supports concurrency). Both
  can be open at the same time. The `w`-session worktree row tracks
  correlation_id; `c` sessions have no correlation_id and skip cleanup.

## Phased Plan

### Phase 1 — `w` shortcut spawns worktree session (~250 LoC, 4-6h)
- `orch/state.py`: add `worktrees` table + CRUD helpers, bump SCHEMA_VERSION.
- `orch/agent.py`: add `detect_main_branch()`, extend `create_worktree(base_ref=)`,
  add `create_session_worktree(project)`.
- `orch/iterm.py`: add `open_vm_session_in_worktree(project, wt_path, corr_id)`.
  cd to worktree, write pid+worktree tmp files, skip `--resume`.
- `orch/app.py`: add new `Binding("w", "session_start_worktree", ...)` near
  the existing `c` binding (~line 606). Add `action_session_start_worktree`
  modeled on `action_session_start` (~line 1346) — creates worktree, inserts
  SQLite row, launches via the new spawn function. If the project is not a
  git repo, surface a notify ("w requires a git repo") and abort. `c` and
  `action_session_start` are NOT touched.

### Phase 2 — UI continuity (~150 LoC, 3h)
- Extend `_start_watcher` to schedule active worktree `.orch/` + JSONL dirs.
- Modify `_handle_file_change`, `_check_journal_state`, `_project_for_path`
  to map worktree paths back to original Project. Writes to status etc.
  land on the original project's `.orch/status` (where the UI reads).

### Phase 3 — Session-close cleanup (~200 LoC, 3h)
- Extend `list_sessions` to return `correlation_id` (empty string for `c`
  sessions, real id for `w` sessions).
- New module `orch/worktrees.py` with `cleanup_closed_session()`.
- Hook into `_do_refresh_session_cache` (15s tick) and `action_quit`.
  Cleanup only fires when `correlation_id` is present — `c` sessions are
  left alone.

### Phase 4 — Daily GC + CLI (~250 LoC, 3-4h)
- Extend `_Janitor._tick` with throttled (24h) worktree GC.
- Add `orch worktrees {list,gc,rm}` subcommands.

### Phase 5 — PR flow polish (~80 LoC, 1.5h)
- Append instruction to `~/.orch/system-prompt.md`: "If user asks for a PR,
  `git push -u origin HEAD` + `gh pr create --fill`. Do not mention the
  branch name."
- Export `ORCH_BRANCH`, `ORCH_BASE_BRANCH` in spawn env.

## Total: ~930 LoC, 15-18 hours

Recommended ship order: Phase 1+2+3 together (otherwise UI goes dead during
sessions or worktrees accumulate). Phase 4 a week later. Phase 5 anytime.

## Concurrent-Session Correctness

For two `w` presses on the same project (or a mix of `c` + multiple `w`):
1. Two `git worktree add` calls produce distinct dirs + branches (4 hex bytes
   of entropy per id).
2. File edits land in distinct on-disk checkouts (git worktree inode isolation).
3. Each session has its own `.orch/` → no contention on status/wfi/task files.
4. PIDs are unique per process → distinct tmp pidfiles, no collision.
5. JSONL logs go to distinct `~/.claude/projects/-...worktrees-<corr>/` dirs.
6. PR push uses `HEAD` → only that session's branch is pushed.

## Risks / Edge Cases

- **Project dirty at "c" time**: those changes aren't in the new worktree.
  Surface a notify; don't block.
- **Non-git project**: `w` shows a notify and aborts. `c` is unaffected.
- **Crashed Claude leaves orphan worktree row**: daemon fallback sweep
  (probe pid in VM) catches it.
- **`.orch/pending_task` was for original project**: on `w` spawn, copy the
  file (if present) into the new worktree's `.orch/pending_task` so the
  "task" feature still reaches Claude.
- **gh pr list cost**: ~N calls per day, throttled to 24h. Bounded.
- **Disk usage**: 50 worktrees × 500MB = 25GB. 30d abandonment + merged-PR
  GC keep it in check.

## Files touched
- `orch/state.py` (schema + CRUD)
- `orch/agent.py` (worktree primitives, main detection)
- `orch/iterm.py` (spawn-in-worktree variant)
- `orch/app.py` (action_session_start, watcher fan-out, status routing)
- `orch/daemon.py` (Janitor GC tick)
- `orch/__main__.py` (`orch worktrees` CLI)
- new: `orch/worktrees.py` (cleanup logic)
- `~/.orch/system-prompt.md` (PR-flow instruction)
