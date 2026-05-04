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
credentials to the file. The polling loop does NOT call Anthropic's
/oauth/token endpoint itself — refresh remains the responsibility of real
Claude CLI processes.

There is also a manual `refresh_now()` (CLI: `orch credbroker refresh`,
TUI: `R`) that DOES call /oauth/token directly, using the keychain's current
refresh_token and Claude.app's public OAuth client_id. Use it when a VM
session is stuck on 401s and you don't want to wait for the host to rotate
the chain on its own. Successful refresh writes back to both Keychain and
file; a failed refresh_token means the chain has diverged past recovery —
run `claude login` on the host.
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
import urllib.error
import urllib.request

KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDS_FILE = pathlib.Path.home() / ".claude" / ".credentials.json"
DEFAULT_INTERVAL_SECONDS = 30

# Claude.app's public OAuth client_id and refresh endpoint, verified
# empirically: a stale refresh_token at this URL returns the OAuth-standard
# {"error":"invalid_grant"} response, while other Anthropic hosts reject the
# client_id outright.
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_TOKEN_URL = "https://claude.ai/v1/oauth/token"

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


def write_keychain(creds: dict) -> None:
    """
    Update the Keychain entry. macOS may prompt for permission the first time
    a non-Claude.app process writes to this entry; user can click "Always Allow"
    to suppress future prompts (caveat: ACL is bound to code signature, so an
    unsigned `python3` may re-prompt on each path/version change).
    Raises RuntimeError on failure.
    """
    payload = json.dumps(creds)
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    result = subprocess.run(
        [
            "security", "add-generic-password",
            "-U",
            "-s", KEYCHAIN_SERVICE,
            "-a", user,
            "-w", payload,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"keychain write failed (rc={result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def refresh_now() -> tuple[dict, str | None]:
    """
    Force-refresh OAuth tokens via Anthropic's /oauth/token endpoint.

    Reads the current refresh_token from Keychain (or file as fallback),
    POSTs to OAUTH_TOKEN_URL with the official client_id, then writes the
    new (accessToken, refreshToken, expiresAt) back to BOTH Keychain and
    file. Preserves scopes/subscriptionType/rateLimitTier from the existing
    creds.

    Returns (new_oauth_dict, keychain_warning_or_None). The warning is
    populated when the file write succeeded but the Keychain write did not
    — file is fresh (VM recovers) but host's Keychain is now stale (host's
    Claude.app will need to re-login on its next refresh attempt).

    Raises RuntimeError on hard failures (no creds, refresh_token rejected,
    network error). The caller should surface these to the user.
    """
    creds = read_keychain()
    if creds is None:
        creds = read_file()
    if creds is None:
        raise RuntimeError("no creds in Keychain or file; run `claude login` on host first")

    oauth = _oauth(creds)
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        raise RuntimeError("no refreshToken in current creds; run `claude login` on host")

    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID,
    }).encode("utf-8")

    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            # claude.ai is fronted by Cloudflare, which blocks the default
            # Python-urllib/X.Y UA with HTTP 403 (error 1010). Any non-default
            # UA passes; we mimic the official client for parity.
            "User-Agent": "claude-cli/2.1.126",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            response = json.load(resp)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace").strip()
        if e.code == 400 and "invalid_grant" in err_body:
            raise RuntimeError(
                "refresh_token rejected — chain has diverged past recovery. "
                "Run `claude login` on the host."
            ) from e
        raise RuntimeError(f"refresh failed [{e.code}]: {err_body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error reaching {OAUTH_TOKEN_URL}: {e}") from e

    new_oauth = dict(oauth)  # preserve scopes, subscriptionType, rateLimitTier
    new_oauth["accessToken"] = response["access_token"]
    new_oauth["refreshToken"] = response.get("refresh_token", refresh_token)
    new_oauth["expiresAt"] = int(time.time() * 1000) + int(response["expires_in"]) * 1000

    new_creds = {"claudeAiOauth": new_oauth}

    # Try Keychain first. If it fails, still propagate to file so the VM
    # session recovers — host's Claude.app falls out of sync but that's
    # recoverable with one re-login on host.
    keychain_warning: str | None = None
    try:
        write_keychain(new_creds)
    except Exception as e:
        keychain_warning = str(e)
        log.warning("keychain write failed (host out of sync): %s", e)

    atomic_write_file(new_creds)
    log.info(
        "refresh_now: wrote new tokens (expiresAt=%s, keychain_ok=%s)",
        new_oauth["expiresAt"],
        keychain_warning is None,
    )
    return new_oauth, keychain_warning


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
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="force-call /oauth/token now and propagate to Keychain + file",
    )
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

    if args.refresh:
        try:
            new_oauth, kc_warn = refresh_now()
        except RuntimeError as e:
            print(f"refresh failed: {e}", file=sys.stderr)
            return 1
        mins = (new_oauth["expiresAt"] - int(time.time() * 1000)) // 60000
        print(f"refreshed: new access token expires in ~{mins}m")
        if kc_warn:
            print(f"  warning (keychain): {kc_warn}", file=sys.stderr)
            print("  file is fresh (VM will recover); host may need `claude login`", file=sys.stderr)
        return 0

    if args.once:
        result = sync_once(dry_run=args.dry_run)
        print(result)
        return 0

    run_loop(interval=args.interval, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
