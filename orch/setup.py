#!/usr/bin/env python3
"""
orch setup — run once after installing the package.

What this does:
  1. Copies profiles/orch-iterm2-profile.json into iTerm2's DynamicProfiles/
     (iTerm2 picks it up instantly, no restart needed)
  2. Verifies macOS notification support (built-in osascript)
  3. Checks for Lima and offers to install it
  4. Creates the Lima VM if it doesn't exist
  5. Creates ~/.orch/config.toml with defaults if it doesn't exist
  6. Prints the CLAUDE.md snippet to add to each project
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent.parent  # repo root, not the orch/ package dir
PROFILE_SRC = HERE / "profiles" / "orch-iterm2-profile.json"
ITERM_DYNAMIC_PROFILES = Path.home() / "Library" / "Application Support" / "iTerm2" / "DynamicProfiles"
ORCH_CONFIG_DIR = Path.home() / ".orch"
CONFIG_FILE = ORCH_CONFIG_DIR / "config.toml"


def hr(char="─", width=60):
    print(char * width)


def step(n, total, msg):
    print(f"\n[{n}/{total}] {msg}")


def install_iterm_profile():
    step(1, 6, "Installing iTerm2 Dynamic Profile")

    if not ITERM_DYNAMIC_PROFILES.parent.parent.exists():
        print("  ✗ iTerm2 not found at ~/Library/Application Support/iTerm2")
        print("    Install iTerm2 from https://iterm2.com then re-run setup")
        return False

    ITERM_DYNAMIC_PROFILES.mkdir(parents=True, exist_ok=True)
    dest = ITERM_DYNAMIC_PROFILES / "orch-iterm2-profile.json"

    if dest.exists() or dest.is_symlink():
        dest.unlink()
    bak = dest.with_suffix(".json.bak")
    if bak.exists():
        bak.unlink()

    shutil.copy2(PROFILE_SRC, dest)
    print(f"  ✓ Installed: {PROFILE_SRC.name}")
    print(f"    → {dest}")
    return True


def check_notifications():
    step(2, 6, "Checking notifications")
    print("  ✓ Using built-in macOS notifications (osascript)")
    print("    Ensure Focus / Do Not Disturb is off to receive notifications.")


def check_lima():
    step(3, 6, "Checking Lima")

    if shutil.which("limactl"):
        result = subprocess.run(
            ["limactl", "--version"],
            capture_output=True, text=True,
        )
        version = result.stdout.strip() if result.returncode == 0 else "unknown"
        print(f"  ✓ Lima {version}")
        return True

    print("  ✗ Lima not found")
    print("    Lima provides a lightweight Linux VM for running Claude sessions.")

    try:
        answer = input("  Install now via Homebrew? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer in ("", "y", "yes"):
        result = subprocess.run(["brew", "install", "lima"])
        if result.returncode == 0:
            print("  ✓ Installed")
            return True
        else:
            print("  ✗ Install failed — run: brew install lima")
            return False
    else:
        print("  Skipped — run: brew install lima")
        return False


def create_vm():
    step(4, 6, "Creating Lima VM")

    from .vm import vm_status, vm_create, LIMA_YAML

    status = vm_status()
    if status != "NotCreated":
        print(f"  ✓ VM already exists (status: {status})")
        if status == "Stopped":
            try:
                answer = input("  Start VM now? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            if answer in ("", "y", "yes"):
                from .vm import vm_start
                print("  Starting VM…")
                vm_start()
                print("  ✓ VM running")
        return

    print(f"  Creating VM from {LIMA_YAML}…")
    print("  This downloads an Ubuntu image and installs tools (5-10 minutes).")

    try:
        answer = input("  Create VM now? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer in ("", "y", "yes"):
        try:
            vm_create()
            print("  ✓ VM created")
            print("  Start with: orch vm start")
        except subprocess.CalledProcessError as e:
            print(f"  ✗ VM creation failed: {e}")
    else:
        print("  Skipped — create later with: orch vm create")


def check_gh_cli():
    step(5, 6, "Checking GitHub CLI")

    if shutil.which("gh"):
        print("  ✓ gh CLI installed")
        return True

    print("  ✗ gh CLI not found")
    print("    Required for auto-dispatch PR creation and code review comments.")

    try:
        answer = input("  Install now via Homebrew? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer in ("", "y", "yes"):
        result = subprocess.run(["brew", "install", "gh"])
        if result.returncode == 0:
            print("  ✓ Installed")
            return True
        else:
            print("  ✗ Install failed — run: brew install gh")
            return False
    else:
        print("  Skipped — run: brew install gh")
        return False


def _prompt_sites_root() -> str:
    """Ask the user where their projects live, defaulting to cwd."""
    default = os.getcwd()
    print(f"  Where are your projects located?")
    print(f"  orch scans this directory for subdirectories containing .claude/")
    try:
        answer = input(f"  Projects root [{default}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    chosen = answer if answer else default
    resolved = Path(chosen).expanduser().resolve()
    if not resolved.is_dir():
        print(f"  ⚠ {resolved} does not exist yet — it will be used once created")
    return str(resolved)


def create_config():
    step(6, 6, "Creating ~/.orch/config.toml")
    ORCH_CONFIG_DIR.mkdir(exist_ok=True)

    if CONFIG_FILE.exists():
        print(f"  ✓ Already exists at {CONFIG_FILE}")
        return

    sites_root = _prompt_sites_root()

    config = f"""\
# orch configuration
# Edit freely — changes take effect on next orch launch

[iterm]
# Name of the iTerm2 profile to use for orch sessions.
profile = "orch"

# Whether all orch sessions live in a single dedicated window.
dedicated_window = true

# Title of the dedicated orch window
window_title = "orch sessions"

[notifications]
# macOS notification sounds. Set to "" to disable.
sound_input_needed = "Glass"
sound_resumed = "Pop"
notify_on_resume = true

[projects]
# Root directory to scan for projects (must contain .claude/ to be registered)
sites_root = "{sites_root}"

[vm]
# Lima VM name (matches lima/orch.yaml template)
name = "orch"

[dispatch]
# Maximum number of Claude instances to run in parallel per project
max_parallel = 3

[bridge]
# Mobile web bridge port
port = 7777

[planner]
# Model used for day planning
model = "claude-sonnet-4-20250514"
"""
    CONFIG_FILE.write_text(config)
    print(f"  ✓ Created {CONFIG_FILE}")
    print(f"    Projects root: {sites_root}")


def print_claude_snippet():
    print()
    print("  CLAUDE.md snippet")
    print()
    print("  Add this block to CLAUDE.md in every project you want orch to track.")
    print("  Claude will maintain these files automatically.")
    print()
    hr("  ─")
    snippet = (HERE / "CLAUDE_SNIPPET.md").read_text()
    for line in snippet.splitlines():
        print(f"  {line}")
    hr("  ─")


def main():
    hr("═")
    print("  orch setup")
    hr("═")

    install_iterm_profile()       # 1/6
    check_notifications()          # 2/6
    check_lima()                   # 3/6
    create_vm()                    # 4/6
    check_gh_cli()                 # 5/6
    create_config()                # 6/6
    print_claude_snippet()

    print()
    hr()
    print("  Setup complete. Run: orch vm start && orch")
    hr()


if __name__ == "__main__":
    main()
