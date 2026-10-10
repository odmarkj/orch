"""`python -m orch.serialsvc` — also what the launchd agent invokes.

Dispatch is split deliberately. The management verbs drive launchd and the
tty, so they refuse to run anywhere but the macOS host. Everything else is an
HTTP call to the control plane and behaves identically on the host and in the
VM, which is the property that lets a VM session do the whole job.
"""

from __future__ import annotations

import sys

USAGE = """\
orch serial — host-owned USB serial device, driven from anywhere

  Host-only (manages the launchd agent and the tty):
    orch serial install [--upgrade]   Provision venv + load launchd agent
    orch serial uninstall             Unload + remove the agent
    orch serial restart | stop        Kickstart / unload the agent
    orch serial logs [-f]             Tail the service log

  Shipping a merged serialsvc source fix to the live process is: pull the
  merge into the host's checkout of this repo, then `orch serial restart`.
  `install --upgrade` only upgrades the service's own pyserial/esptool venv
  -- it does not redeploy this project's code, and restarting without first
  pulling just re-runs the same old source.

  Works on the host and inside the VM:
    orch serial status                Service + device + reset budget
    orch serial doctor                Check every link in the chain
    orch serial devices               List attached serial devices
    orch serial chip                  Identify chip + flash (costs 1 reset)
    orch serial monitor [--baud N] [--seconds N]
    orch serial flash <img> [--addr 0x10000] [--baud N] [--erase] [--force]
    orch serial flash 0x1000:boot.bin 0x10000:app.bin
    orch serial backup <dest.bin> [--addr 0x0] [--size 0x400000]
    orch serial reset [--bootloader] [--force]

Firmware paths must live under the shared root (~/Apps), which is mounted at
the same absolute path on the host and in the VM — so a VM build is flashed
with no copying.

Every reset is rate-limited (default 60s); on hardware that attaches to a
cellular network a reset is expensive. --force overrides.
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    sub, rest = argv[0], argv[1:]

    if sub == "run":
        from . import require_macos
        require_macos("run")
        from .server import run
        return run()

    from . import manage

    if sub == "install":
        return manage.install(rest)
    if sub == "uninstall":
        return manage.uninstall(rest)
    if sub == "restart":
        return manage.restart(rest)
    if sub == "stop":
        return manage.stop(rest)
    if sub == "logs":
        return manage.logs(rest)
    if sub == "status":
        return manage.status(rest)
    if sub == "doctor":
        return manage.doctor(rest)

    from . import client

    routes = {
        "devices": client.cmd_devices,
        "reset": client.cmd_reset,
        "flash": client.cmd_flash,
        "backup": client.cmd_backup,
        "chip": client.cmd_chip,
        "monitor": client.cmd_monitor,
    }
    if sub in routes:
        return routes[sub](rest)

    print(f"unknown command: serial {sub}\n", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
