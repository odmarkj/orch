"""Provisioning and supervision for serialsvc — orch's side of it.

Everything the service needs is created here and nowhere else: the venv that
carries pyserial/esptool, the launchd agent, the state directory. The agent
runs with ``KeepAlive`` and ``RunAtLoad`` so the device is reachable from the
VM after a reboot or a replug with nobody driving it -- the whole point is
that no human is in the loop.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import (
    LAUNCHD_LABEL,
    LAUNCHD_PLIST,
    LOG_FILE,
    STATE_DIR,
    VENV_DIR,
    VENV_PACKAGES,
    bind,
    control_url,
    data_url,
    orch_pythonpath,
    reset_cooldown,
    require_macos,
    service_python,
    shared_roots,
)


def _echo(message: str) -> None:
    print(f"  {message}")


def _venv_python_candidates() -> list[str]:
    """Interpreters that can build the service venv.

    esptool 5.x needs >= 3.10. Apple's /usr/bin/python3 is still 3.9 on this
    machine, which silently resolves to esptool 4.x and a different CLI, so
    prefer an explicit modern interpreter and fall back deliberately.
    """
    found = [sys.executable]
    for name in ("python3.13", "python3.12", "python3.14", "python3"):
        path = f"/opt/homebrew/bin/{name}"
        if os.path.exists(path):
            found.append(path)
    found.append("/usr/bin/python3")
    return found


def _usable_interpreter() -> str | None:
    for candidate in _venv_python_candidates():
        try:
            out = subprocess.run(
                [candidate, "-c",
                 "import sys; print('%d.%d' % sys.version_info[:2])"],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode != 0:
            continue
        try:
            major, minor = (int(x) for x in out.stdout.strip().split("."))
        except ValueError:
            continue
        if (major, minor) >= (3, 10):
            return candidate
    return None


def provision_venv(upgrade: bool = False) -> int:
    """Create/refresh the service's own venv.

    ``upgrade`` only reaches ``VENV_PACKAGES`` (pyserial, esptool) -- it is
    not how a fix to this project's own source code (server.py, client.py,
    this file, ...) reaches the live process. The launchd agent imports
    ``orch.serialsvc`` fresh from ``orch_pythonpath()`` (the host's checkout
    of this repo) every time it starts, so a source fix needs the host
    checkout updated (e.g. ``git pull``) and then the agent restarted --
    ``orch serial restart`` (or ``install`` again) on the host -- not
    ``install --upgrade``. See ``restart()`` below.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not service_python().exists():
        interpreter = _usable_interpreter()
        if interpreter is None:
            print(
                "no Python >= 3.10 found; esptool 5.x requires it.\n"
                "Install one with: brew install python@3.13",
                file=sys.stderr,
            )
            return 1
        _echo(f"creating venv with {interpreter}")
        result = subprocess.run(
            [interpreter, "-m", "venv", str(VENV_DIR)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"venv creation failed: {result.returncode}\n{result.stderr}",
                  file=sys.stderr)
            return 1

    cmd = [str(service_python()), "-m", "pip", "install", "-q",
           "--disable-pip-version-check"]
    if upgrade:
        cmd.append("--upgrade")
    cmd.extend(VENV_PACKAGES)
    _echo(f"installing {', '.join(VENV_PACKAGES)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"pip install failed:\n{result.stdout}\n{result.stderr}",
              file=sys.stderr)
        return 1
    return 0


def install(argv: list[str]) -> int:
    require_macos("install")
    if provision_venv(upgrade="--upgrade" in argv) != 0:
        return 1

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
    host, data_port, control_port = bind()

    env = {
        "PATH": os.environ.get(
            "PATH", "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"),
        "PYTHONPATH": orch_pythonpath(),
        "PYTHONUNBUFFERED": "1",
    }
    # Carry only deliberate overrides into the agent. launchd jobs inherit
    # almost nothing, so anything the service should honour has to be named.
    for var in (
        "ORCH_SERIAL_DEVICE", "ORCH_SERIAL_VIDPID", "ORCH_SERIAL_BAUD",
        "ORCH_SERIAL_HOST", "ORCH_SERIAL_DATA_PORT",
        "ORCH_SERIAL_CONTROL_PORT", "ORCH_SERIAL_RESET_COOLDOWN",
        "ORCH_SERIAL_STALE_READ_GRACE",
        "ORCH_SERIAL_IMAGE_ROOTS", "ORCH_SITES_ROOT",
    ):
        value = os.environ.get(var, "").strip()
        if value:
            env[var] = value

    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [str(service_python()), "-m", "orch.serialsvc", "run"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(LOG_FILE),
        "StandardErrorPath": str(LOG_FILE),
        "ProcessType": "Background",
        "EnvironmentVariables": env,
    }
    with open(LAUNCHD_PLIST, "wb") as f:
        plistlib.dump(plist, f)

    subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)],
                   capture_output=True)
    result = subprocess.run(["launchctl", "load", str(LAUNCHD_PLIST)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"launchctl load failed: {result.stderr}", file=sys.stderr)
        return 1

    _echo(f"installed: {LAUNCHD_PLIST}")
    _echo(f"log:       {LOG_FILE}")
    _echo(f"control:   http://{host}:{control_port}")
    _echo(f"data:      rfc2217://{host}:{data_port}")
    _echo(f"from VM:   {control_url()}")
    return 0


def uninstall(argv: list[str]) -> int:
    require_macos("uninstall")
    if not LAUNCHD_PLIST.exists():
        _echo("no plist installed")
        return 0
    subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)],
                   capture_output=True)
    LAUNCHD_PLIST.unlink()
    _echo(f"removed: {LAUNCHD_PLIST}")
    return 0


def _probe(timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(
            f"{control_url()}/status", timeout=timeout
        ) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def status(argv: list[str]) -> int:
    probe = _probe()
    if probe is None:
        _echo(f"serialsvc: unreachable at {control_url()}")
        if LAUNCHD_PLIST.exists():
            result = subprocess.run(["launchctl", "list", LAUNCHD_LABEL],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                _echo(f"plist installed but not loaded ({LAUNCHD_PLIST})")
            else:
                _echo(result.stdout.rstrip())
            _echo(f"log: {LOG_FILE}")
        else:
            _echo("not installed — run: orch serial install")
        return 1

    _echo(f"serialsvc: up at {control_url()}")
    _echo(f"device:    {probe.get('device') or '(none attached)'}")
    if probe.get("device") and not probe.get("port_live"):
        _echo(f"           ! {probe.get('port_detail')}")
        _echo("           try: orch serial restart")
    _echo(f"resets:    {probe.get('resets')} "
          f"(cooldown {probe.get('cooldown_remaining_s')}s "
          f"of {probe.get('reset_cooldown_s')}s)")
    if probe.get("last_flash"):
        last = probe["last_flash"]
        _echo(f"last flash: {last.get('when')} "
              f"{'ok' if last.get('ok') else 'FAILED'} "
              f"({last.get('elapsed_s')}s)")
    _echo(f"monitor:   {data_url()}")
    return 0


def restart(argv: list[str]) -> int:
    """Kickstart the launchd agent so it re-execs and re-imports fresh.

    This -- not ``install --upgrade`` -- is what ships a merged source fix:
    ``launchctl kickstart -k`` kills and respawns the process, which
    re-imports ``orch.serialsvc`` from disk. That only picks up a fix if the
    host's checkout of this repo already has it (``git pull`` on the host
    first); ``--upgrade`` governs the service's own pyserial/esptool venv,
    a separate thing entirely.
    """
    require_macos("restart")
    if not LAUNCHD_PLIST.exists():
        return install(argv)
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        subprocess.run(["launchctl", "load", str(LAUNCHD_PLIST)],
                       capture_output=True)
    _echo("serialsvc: restarted")
    return 0


def stop(argv: list[str]) -> int:
    require_macos("stop")
    if not LAUNCHD_PLIST.exists():
        _echo("no plist installed")
        return 0
    subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)],
                   capture_output=True)
    _echo("serialsvc: stopped")
    return 0


def logs(argv: list[str]) -> int:
    require_macos("logs")
    if not LOG_FILE.exists():
        _echo(f"no log at {LOG_FILE}")
        return 1
    follow = "-f" in argv or "--follow" in argv
    cmd = ["tail"] + (["-f"] if follow else ["-n", "200"]) + [str(LOG_FILE)]
    return subprocess.run(cmd).returncode


def doctor(argv: list[str]) -> int:
    """Check every link in the chain and say which one is broken."""
    problems = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal problems
        mark = "ok  " if ok else "FAIL"
        print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
        if not ok:
            problems += 1

    from . import is_macos

    print("\nserialsvc doctor\n")

    if is_macos():
        check("venv interpreter", service_python().exists(),
              str(service_python()))
        if service_python().exists():
            out = subprocess.run(
                [str(service_python()), "-c",
                 "import serial, esptool, esp_pylib.serial_reset as r; "
                 "print(serial.__version__, esptool.__version__)"],
                capture_output=True, text=True,
            )
            check("pyserial + esptool importable", out.returncode == 0,
                  out.stdout.strip() or out.stderr.strip()[-120:])
        check("launchd plist", LAUNCHD_PLIST.exists(), str(LAUNCHD_PLIST))
        if LAUNCHD_PLIST.exists():
            loaded = subprocess.run(["launchctl", "list", LAUNCHD_LABEL],
                                    capture_output=True, text=True)
            check("agent loaded", loaded.returncode == 0)
    else:
        _echo("running inside the VM — host-side checks skipped\n")

    probe = _probe()
    check("control plane reachable", probe is not None, control_url())

    if probe is not None:
        check("device attached", bool(probe.get("attached")),
              probe.get("device") or "nothing recognised on USB")
        if probe.get("attached"):
            # A VID:PID match at a path only means *something* recognised
            # is plugged in -- it is exactly what stayed green the whole
            # time a swapped device left every real operation failing with
            # errno 6. This is the actual open/close check that catches it.
            check("port opens (not a stale handle from a device swap)",
                  bool(probe.get("port_live")), probe.get("port_detail") or "")
        check("accepting monitor clients",
              bool(probe.get("data_client_accepted")),
              "busy with a flash" if not probe.get("data_client_accepted") else "")

    roots = ", ".join(str(r) for r in shared_roots())
    check("shared image roots exist",
          all(r.exists() for r in shared_roots()), roots)

    print()
    if problems:
        if (problems == 1 and probe is not None and probe.get("attached")
                and not probe.get("port_live")):
            # The daemon reopens on its own the next time a real operation
            # runs (monitor/reset/flash) -- this check caught the stale
            # handle, it doesn't need a restart to clear it.
            print("  1 problem(s): stale handle on a device that is "
                  "otherwise attached.\n"
                  "  the next monitor/reset/flash should reopen it "
                  "automatically; if it doesn't, try: orch serial restart\n")
        else:
            print(f"  {problems} problem(s). "
                  f"On the host try: orch serial install && orch serial restart\n")
        return 1
    print(f"  all good. reset cooldown is {reset_cooldown():.0f}s.\n")
    return 0
