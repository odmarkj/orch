#!/usr/bin/env bash
# orch — VM-side shim, installed to ~/.local/bin/orch by lima/orch.yaml.
#
# The `orch` console script is not installed inside the Lima VM. The repo is
# virtiofs-mounted at /Users/joshuaodmark/Apps/orch and carries TWO venvs:
#
#   <repo>/          a macOS venv (bin/, lib/, pyvenv.cfg -> /opt/homebrew).
#                    This is the HOST's — it runs the daemon and the TUI.
#                    Its shebang is a macOS interpreter, so it cannot run here.
#   <repo>/.venv/    a Linux venv built inside the VM. This one works here.
#
# Without this shim, `orch` is simply not found, and the repo-root macOS venv
# is the first thing a VM session finds when it goes looking — which reads as
# "the orch CLI is host-only" and sends bridge submissions to the host by hand.
# It is not host-only: the bridge subtree is stdlib-only and talks to the
# daemon over HTTP via host.lima.internal.
#
# Command families that genuinely require the macOS host (launchd, limactl,
# Keychain) fail fast here with a pointer rather than a confusing traceback.

set -euo pipefail

ORCH_REPO="${ORCH_REPO:-/Users/joshuaodmark/Apps/orch}"

host_only() {
  cat >&2 <<EOF
orch $1: host-only — this command drives launchd / limactl / the macOS
Keychain, none of which exist inside the Lima VM.

Run it in a terminal on the macOS host instead.

VM-side commands that DO work here: bridge, logs, plan, stage, init, ignore.
EOF
  exit 2
}

case "${1:-}" in
  daemon|vm|credbroker|setup) host_only "$1" ;;
  "")
    echo "orch (TUI) is host-only; run it on the macOS host." >&2
    echo "From inside the VM, try: orch bridge list" >&2
    exit 2
    ;;
esac

# Prefer the VM's own venv (has the optional deps); fall back to the system
# interpreter with PYTHONPATH, which is enough for the stdlib-only bridge CLI.
# Never cd: source-project auto-detection reads the caller's cwd.
if [ -x "$ORCH_REPO/.venv/bin/python3" ] && "$ORCH_REPO/.venv/bin/python3" -c '' 2>/dev/null; then
  exec "$ORCH_REPO/.venv/bin/python3" -m orch "$@"
fi

exec env PYTHONPATH="$ORCH_REPO${PYTHONPATH:+:$PYTHONPATH}" python3 -m orch "$@"
