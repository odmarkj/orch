#!/usr/bin/env python3
"""
orch setup — run once after installing the package.

What this does:
  1. Copies profiles/orch-iterm2-profile.json into iTerm2's DynamicProfiles/
     (iTerm2 picks it up instantly, no restart needed)
  2. Checks for terminal-notifier and offers to install it
  3. Creates ~/.orch/config.toml with defaults if it doesn't exist
  4. Prints the CLAUDE.md snippet to add to each project
"""

import json
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
    step(1, 5, "Installing iTerm2 Dynamic Profile")

    if not ITERM_DYNAMIC_PROFILES.parent.parent.exists():
        print("  ✗ iTerm2 not found at ~/Library/Application Support/iTerm2")
        print("    Install iTerm2 from https://iterm2.com then re-run setup")
        return False

    ITERM_DYNAMIC_PROFILES.mkdir(parents=True, exist_ok=True)
    dest = ITERM_DYNAMIC_PROFILES / "orch-iterm2-profile.json"

    # Remove old symlink or back up existing file
    if dest.is_symlink():
        dest.unlink()
    elif dest.exists():
        dest.rename(dest.with_suffix(".json.bak"))
        print(f"  → Backed up existing profile to {dest.with_suffix('.json.bak')}")

    # Copy instead of symlink — iTerm2 does not follow symlinks in DynamicProfiles
    shutil.copy2(PROFILE_SRC, dest)
    print(f"  ✓ Installed: {PROFILE_SRC.name}")
    print(f"    → {dest}")
    print()
    print("  The profile is now live in iTerm2. Open Preferences → Profiles")
    print("  to verify 'orch' appears. To customize, edit the source file at")
    print(f"  {PROFILE_SRC} and re-run: orch setup")
    return True


def _brew_install(package: str, description: str) -> bool:
    """Offer to install a Homebrew package. Returns True if installed."""
    if shutil.which(package):
        print(f"  ✓ {package} already installed")
        return True

    print(f"  ✗ {package} not found")
    print(f"    {description}")

    try:
        answer = input("  Install now via Homebrew? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer in ("", "y", "yes"):
        result = subprocess.run(["brew", "install", package])
        if result.returncode == 0:
            print("  ✓ Installed")
            return True
        else:
            print(f"  ✗ Install failed — run: brew install {package}")
            return False
    else:
        print("  Skipped")
        return False


def check_terminal_notifier():
    step(2, 5, "Checking terminal-notifier")
    _brew_install(
        "terminal-notifier",
        "This enables macOS toast notifications when Claude needs input.",
    )


def check_devcontainer_cli():
    step(3, 5, "Checking devcontainer CLI")

    if shutil.which("devcontainer"):
        print("  ✓ devcontainer CLI already installed")
        return

    print("  ✗ devcontainer CLI not found")
    print("    This enables full devcontainer feature support (Python, Node, etc).")
    print("    Without it, orch falls back to raw docker with manual Claude install.")

    try:
        answer = input("  Install now via npm? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer in ("", "y", "yes"):
        result = subprocess.run(["npm", "install", "-g", "@devcontainers/cli"])
        if result.returncode == 0:
            print("  ✓ Installed")
        else:
            print("  ✗ Install failed — run: npm install -g @devcontainers/cli")
    else:
        print("  Skipped — orch will use raw docker (containers still work)")


def check_docker():
    step(4, 5, "Checking Docker")

    if not shutil.which("docker"):
        print("  ✗ Docker not found")
        print("    Docker is required for containerized Claude sessions.")
        print("    Install Docker Desktop from https://docker.com")
        return

    result = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"  ✓ Docker {result.stdout.strip()}")
    else:
        print("  ✗ Docker is installed but not running")
        print("    Start Docker Desktop and re-run setup")


def create_config():
    step(5, 5, "Creating ~/.orch/config.toml")
    ORCH_CONFIG_DIR.mkdir(exist_ok=True)

    if CONFIG_FILE.exists():
        # Check if [container] section is missing and append it
        existing = CONFIG_FILE.read_text()
        if "[container]" not in existing:
            container_section = """
[container]
# Enable containerized Claude sessions (requires Docker)
enabled = true

# Base devcontainer image (used when project has no .devcontainer/)
image = "mcr.microsoft.com/devcontainers/base:ubuntu"

# Memory limit per container
memory = "12g"

# Env vars to pass from host into containers (comma-separated)
passthrough_env = "ANTHROPIC_API_KEY,CLOUDFLARE_API_TOKEN,CLOUDFLARE_ACCOUNT_ID"

# Use devcontainer CLI if available (falls back to raw docker if false or missing)
prefer_devcontainer_cli = true
"""
            CONFIG_FILE.write_text(existing.rstrip() + "\n" + container_section)
            print(f"  ✓ Added [container] section to {CONFIG_FILE}")
        else:
            print(f"  ✓ Already exists at {CONFIG_FILE}")
        return

    config = """\
# orch configuration
# Edit freely — changes take effect on next orch launch

[iterm]
# Name of the iTerm2 profile to use for orch sessions.
# Must match the "Name" field in profiles/orch-iterm2-profile.json
profile = "orch"

# Whether all orch sessions live in a single dedicated window.
# true  → one "orch sessions" window, all projects as tabs
# false → new tab in whatever iTerm2 window is frontmost
dedicated_window = true

# Title of the dedicated orch window (used to find/create it)
window_title = "orch sessions"

[notifications]
# macOS notification sound. Set to "" to disable sound.
# Standard names: Glass, Blow, Bottle, Frog, Funk, Hero, Morse, Ping, Pop,
#                 Purr, Sosumi, Submarine, Tink
sound_input_needed = "Glass"
sound_resumed = "Pop"

# Show a "Claude resumed" notification when input has been given
notify_on_resume = true

[projects]
# Root directory to scan for projects (must contain .claude/ to be registered)
sites_root = "~/Sites"

[container]
# Enable containerized Claude sessions (requires Docker)
enabled = true

# Base devcontainer image (used when project has no .devcontainer/)
image = "mcr.microsoft.com/devcontainers/base:ubuntu"

# Memory limit per container
memory = "12g"

# Env vars to pass from host into containers (comma-separated)
passthrough_env = "ANTHROPIC_API_KEY,CLOUDFLARE_API_TOKEN,CLOUDFLARE_ACCOUNT_ID"

# Use devcontainer CLI if available (falls back to raw docker if false or missing)
prefer_devcontainer_cli = true
"""
    CONFIG_FILE.write_text(config)
    print(f"  ✓ Created {CONFIG_FILE}")


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

    install_iterm_profile()       # 1/5
    check_terminal_notifier()     # 2/5
    check_devcontainer_cli()      # 3/5
    check_docker()                # 4/5
    create_config()               # 5/5
    print_claude_snippet()

    print()
    hr()
    print("  Setup complete. Run: orch")
    hr()


if __name__ == "__main__":
    main()
