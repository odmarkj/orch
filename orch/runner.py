"""
Self-hosted GitHub Actions runner setup.

Automates what was previously done by hand for `racepace` (and originally
for `asha`): register a new self-hosted runner against a repo, install it
as a systemd service on this VM, and set the `RUNNER_PROBE_TOKEN` repo
secret the `pick-runner` fail-safe probe needs.

See `.claude-docs/patterns/self-hosted-ci-runners.md` (scaffolded by
`orch init`) for the full pattern this plugs into — the `pick-runner` job
that routes CI to the runner this sets up when it's online, and falls back
to `ubuntu-latest` otherwise.

This only touches the local VM (extracting a tarball, running a repo's own
config.sh, installing a systemd unit) and the target repo's own GitHub
settings (a registration token, a secret) — nothing in the k3s cluster, no
production deploy. `svc.sh install`/`start` need root, so this prompts for
`sudo` interactively; it is not meant to run unattended in CI.
"""

from __future__ import annotations

import getpass
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

GH_RUNNERS_DIR = Path("/home/joshuaodmark.guest/gh-runners")
RUNNER_TARBALL = GH_RUNNERS_DIR / "runner.tar.gz"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, **kwargs)


def _next_runner_index(repo: str) -> int:
    """First unused N for gh-runners/<repo>-runner-N."""
    n = 1
    while (GH_RUNNERS_DIR / f"{repo}-runner-{n}").exists():
        n += 1
    return n


def _registration_token(owner: str, repo: str) -> str:
    result = _run(
        ["gh", "api", "-X", "POST", f"repos/{owner}/{repo}/actions/runners/registration-token"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get registration token: {result.stderr.strip()}")
    return json.loads(result.stdout)["token"]


def _extract_runner(dest: Path) -> None:
    if not RUNNER_TARBALL.exists():
        raise RuntimeError(
            f"{RUNNER_TARBALL} not found — download a runner release from "
            "https://github.com/actions/runner/releases and place it there "
            "as runner.tar.gz, then retry."
        )
    dest.mkdir(parents=True, exist_ok=False)
    print(f"  Extracting {RUNNER_TARBALL} -> {dest}")
    with tarfile.open(RUNNER_TARBALL) as tf:
        tf.extractall(dest)  # noqa: S202 — trusted local tarball we control


def _configure(dest: Path, owner: str, repo: str, name: str, label: str, token: str) -> None:
    config_sh = dest / "config.sh"
    result = _run(
        [
            str(config_sh),
            "--unattended",
            "--url", f"https://github.com/{owner}/{repo}",
            "--token", token,
            "--name", name,
            "--labels", label,
        ],
        cwd=dest,
    )
    if result.returncode != 0:
        raise RuntimeError("config.sh failed — see output above")


def _install_service(dest: Path, run_as_user: str) -> None:
    svc_sh = dest / "svc.sh"
    print(f"  Installing systemd service (needs sudo)…")
    result = _run(["sudo", str(svc_sh), "install", run_as_user], cwd=dest)
    if result.returncode != 0:
        raise RuntimeError("svc.sh install failed — see output above")

    result = _run(["sudo", str(svc_sh), "start"], cwd=dest)
    if result.returncode != 0:
        raise RuntimeError("svc.sh start failed — see output above")


def _set_probe_secret(owner: str, repo: str, probe_token: str | None) -> None:
    if not probe_token:
        print()
        print(
            "  RUNNER_PROBE_TOKEN needs a classic GitHub PAT with `repo` scope.\n"
            "  A classic PAT isn't repo-scoped on a personal account, so the\n"
            "  same one already used for odmarkj/asha and odmarkj/racepace's\n"
            "  RUNNER_PROBE_TOKEN secret works here too — paste that value,\n"
            "  or a fresh PAT with the same scope."
        )
        probe_token = getpass.getpass("  Paste PAT (input hidden): ").strip()
    if not probe_token:
        print("  No token entered — skipping RUNNER_PROBE_TOKEN. Set it manually with:")
        print(f"  gh secret set RUNNER_PROBE_TOKEN --repo {owner}/{repo}")
        return

    result = subprocess.run(
        ["gh", "secret", "set", "RUNNER_PROBE_TOKEN", "--repo", f"{owner}/{repo}"],
        input=probe_token, text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to set RUNNER_PROBE_TOKEN: {result.stderr.strip()}")
    print(f"  ✓ RUNNER_PROBE_TOKEN set on {owner}/{repo}")


def setup_runner(
    owner: str,
    repo: str,
    *,
    probe_token: str | None = None,
    run_as_user: str | None = None,
    skip_secret: bool = False,
) -> None:
    """Register + install a self-hosted runner for owner/repo, end to end."""
    run_as_user = run_as_user or getpass.getuser()
    label = f"{repo}-vm"
    index = _next_runner_index(repo)
    name = f"{label}-{index}"
    dest = GH_RUNNERS_DIR / f"{repo}-runner-{index}"

    print(f"\n  Setting up self-hosted runner for {owner}/{repo}")
    print(f"  name: {name}   label: {label}   dir: {dest}\n")

    print("  Requesting registration token…")
    token = _registration_token(owner, repo)

    _extract_runner(dest)
    _configure(dest, owner, repo, name, label, token)
    _install_service(dest, run_as_user)

    if not skip_secret:
        _set_probe_secret(owner, repo, probe_token)

    print(f"\n  Runner {name} installed and running.")
    print(f"  Next: copy the pick-runner job from")
    print(f"  .claude-docs/patterns/self-hosted-ci-runners.md into the")
    print(f"  project's workflow, using the \"{label}\" label.\n")


def cmd_runner(argv: list[str]) -> None:
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: orch runner setup <owner>/<repo> [options]")
        print()
        print("  Register a self-hosted GitHub Actions runner for <owner>/<repo>")
        print("  on this VM, install it as a systemd service, and set the")
        print("  RUNNER_PROBE_TOKEN repo secret the pick-runner probe needs.")
        print()
        print("  See .claude-docs/patterns/self-hosted-ci-runners.md for the")
        print("  workflow-side pick-runner snippet this pairs with.")
        print()
        print("Options:")
        print("  --probe-token TOKEN   RUNNER_PROBE_TOKEN value (else prompts)")
        print("  --run-as USER         systemd service user (default: current user)")
        print("  --skip-secret         Don't set RUNNER_PROBE_TOKEN at all")
        return

    action = argv[0]
    rest = argv[1:]

    if action != "setup":
        print(f"Unknown action '{action}'. Use: setup")
        sys.exit(1)

    if not rest or rest[0].startswith("-"):
        print("Usage: orch runner setup <owner>/<repo> [options]")
        sys.exit(1)

    owner_repo = rest[0]
    if "/" not in owner_repo:
        print(f"Expected <owner>/<repo>, got '{owner_repo}'")
        sys.exit(1)
    owner, repo = owner_repo.split("/", 1)

    probe_token = None
    run_as_user = None
    skip_secret = False

    i = 1
    while i < len(rest):
        arg = rest[i]
        if arg == "--probe-token" and i + 1 < len(rest):
            i += 1
            probe_token = rest[i]
        elif arg == "--run-as" and i + 1 < len(rest):
            i += 1
            run_as_user = rest[i]
        elif arg == "--skip-secret":
            skip_secret = True
        i += 1

    if not shutil.which("gh"):
        print("gh CLI not found on PATH — required to register the runner and set secrets.")
        sys.exit(1)

    try:
        setup_runner(
            owner, repo,
            probe_token=probe_token,
            run_as_user=run_as_user,
            skip_secret=skip_secret,
        )
    except RuntimeError as exc:
        print(f"\n  Error: {exc}", file=sys.stderr)
        sys.exit(1)
