"""
orch credential broker — mirrors macOS Keychain Claude credentials to
~/.claude/.credentials.json so the Lima VM (which reads the file) stays in
sync with the host (which reads Keychain).

Background: macOS Claude Code stores OAuth credentials in the login Keychain
("Claude Code-credentials"); Linux Claude Code stores them in
$CLAUDE_CONFIG_DIR/.credentials.json. The orch VM bind-mounts the host's
~/.claude/ via virtiofs and points $CLAUDE_CONFIG_DIR there, so host and VM
share the file path — but they do NOT share the Keychain. When host Claude
refreshes its token (transparently, every ~8h), it writes new tokens to
Keychain; the file is left with the now-stale, server-invalidated
refresh_token. The next VM session that tries to refresh fails and prompts
for login.

This broker fixes that by polling Keychain for changes and mirroring fresh
credentials to the file. It does NOT call Anthropic's /oauth/token endpoint
itself — refresh remains the responsibility of real Claude CLI processes.

v0: one-way mirror only (Keychain → file). The reverse direction
(VM-driven refresh → propagate back to Keychain) is not yet implemented;
it would require Keychain write access which prompts the user.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time

KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDS_FILE = pathlib.Path.home() / ".claude" / ".credentials.json"
DEFAULT_INTERVAL_SECONDS = 30

LAUNCHD_LABEL = "com.orch.credbroker"
LAUNCHD_PLIST = pathlib.Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
LOG_DIR = pathlib.Path.home() / ".orch"
LOG_FILE = LOG_DIR / "credbroker.log"

log = logging.getLogger("orch.credbroker")


def read_keychain() -> dict | None:
    """Return Keychain Claude creds as a parsed dict, or None if unavailable."""
    try:
        out = subprocess.check_output(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        log.warning("Keychain payload is not valid JSON: %s", e)
        return None


def read_file() -> dict | None:
    if not CREDS_FILE.exists():
        return None
    try:
        return json.loads(CREDS_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Reading %s failed: %s", CREDS_FILE, e)
        return None


def atomic_write_file(creds: dict) -> None:
    """Write creds to .credentials.json atomically with mode 0600."""
    CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(creds, indent=2)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".credentials.json.",
        dir=str(CREDS_FILE.parent),
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, CREDS_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _oauth(creds: dict) -> dict:
    return creds.get("claudeAiOauth", {}) or {}


def in_sync(kc: dict, fl: dict) -> bool:
    a, b = _oauth(kc), _oauth(fl)
    return (
        a.get("accessToken") == b.get("accessToken")
        and a.get("refreshToken") == b.get("refreshToken")
        and a.get("expiresAt") == b.get("expiresAt")
    )


def sync_once(dry_run: bool = False) -> str:
    kc = read_keychain()
    if kc is None:
        return "no_keychain"
    sub = _oauth(kc).get("subscriptionType")
    if sub != "max":
        # Defense against the dual-access OAuth bug: if Keychain ever holds a
        # Console-billed token (e.g. setup-token used the wrong client_id),
        # propagating it to file would silently route VM usage to API billing.
        # Refuse to write — operator should re-login and verify the banner.
        log.warning("refusing to mirror non-max creds (subscriptionType=%r)", sub)
        return f"non_max_skip:{sub}"
    fl = read_file()
    if fl is not None and in_sync(kc, fl):
        return "in_sync"
    if dry_run:
        return "would_write" if fl is not None else "would_write_new"
    atomic_write_file(kc)
    return "wrote" if fl is not None else "wrote_new"


def run_loop(interval: int = DEFAULT_INTERVAL_SECONDS, dry_run: bool = False) -> None:
    log.info(
        "credbroker starting (interval=%ds, dry_run=%s, file=%s)",
        interval,
        dry_run,
        CREDS_FILE,
    )
    last = None
    while True:
        try:
            result = sync_once(dry_run=dry_run)
            if result != last:
                log.info("sync: %s", result)
                last = result
        except Exception:
            log.exception("sync_once raised")
        time.sleep(interval)


def install_launchd() -> int:
    """Generate launchd plist, load it, return 0 on success."""
    if platform.system() != "Darwin":
        print("credbroker only runs on macOS", file=sys.stderr)
        return 2

    orch_bin = shutil.which("orch")
    if not orch_bin:
        print("orch not found on PATH; install with `pip install -e .`", file=sys.stderr)
        return 1

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [orch_bin, "credbroker", "run"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(LOG_FILE),
        "StandardErrorPath": str(LOG_FILE),
        "ProcessType": "Background",
    }
    with open(LAUNCHD_PLIST, "wb") as f:
        plistlib.dump(plist, f)

    # Idempotent: unload first if already loaded, then load fresh.
    subprocess.run(
        ["launchctl", "unload", str(LAUNCHD_PLIST)],
        capture_output=True,
    )
    result = subprocess.run(
        ["launchctl", "load", str(LAUNCHD_PLIST)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"launchctl load failed: {result.stderr}", file=sys.stderr)
        return 1

    print(f"installed: {LAUNCHD_PLIST}")
    print(f"log: {LOG_FILE}")
    return 0


def uninstall_launchd() -> int:
    if not LAUNCHD_PLIST.exists():
        print("no plist installed")
        return 0
    subprocess.run(
        ["launchctl", "unload", str(LAUNCHD_PLIST)],
        capture_output=True,
    )
    LAUNCHD_PLIST.unlink()
    print(f"removed: {LAUNCHD_PLIST}")
    return 0


def status_launchd() -> int:
    if not LAUNCHD_PLIST.exists():
        print(f"plist not installed at {LAUNCHD_PLIST}")
        return 1
    result = subprocess.run(
        ["launchctl", "list", LAUNCHD_LABEL],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"plist installed but not loaded: {LAUNCHD_PLIST}")
        return 1
    print(result.stdout.rstrip())
    print(f"plist: {LAUNCHD_PLIST}")
    print(f"log:   {LOG_FILE}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orch-credbroker")
    parser.add_argument("--once", action="store_true", help="run a single sync cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="don't write anything; report what would happen")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"polling interval in seconds (default: {DEFAULT_INTERVAL_SECONDS})",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if platform.system() != "Darwin":
        log.error("credbroker only runs on macOS (host with Keychain)")
        return 2

    if args.once:
        result = sync_once(dry_run=args.dry_run)
        print(result)
        return 0

    run_loop(interval=args.interval, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
