"""``python -m orch.fetchsvc <action>`` — what the systemd unit invokes.

Also the escape hatch when the orch CLI is not on PATH inside the VM:

    PYTHONPATH=/Users/joshuaodmark/Apps/orch python3 -m orch.fetchsvc doctor
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    action = args[0] if args else "doctor"
    rest = args[1:]

    if action == "serve":
        from .server import serve

        return serve(rest)

    from . import manage

    if action in ("install", "provision"):
        return manage.install()
    if action == "adopt":
        return manage.adopt()
    if action == "uninstall":
        return manage.uninstall()
    if action in ("start", "stop", "restart"):
        return manage.simple_action(action)
    if action == "status":
        return manage.show_status()
    if action == "doctor":
        return manage.doctor()
    if action == "logs":
        return manage.logs(rest)
    if action in ("sync-credentials", "credentials"):
        report = manage.sync_credentials()
        print(f"  wrote {report['path']}")
        print(f"  configured: {', '.join(report['found']) or 'none'}")
        for item in report["missing"]:
            print(f"  not configured: {item}")
        return 0

    print(
        f"unknown action {action!r}. Use: serve, install, adopt, uninstall, "
        "start, stop, restart, status, doctor, logs, sync-credentials",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
