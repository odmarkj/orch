"""Provisioning and supervision for the fetch service — orch's side of it.

Everything the service needs to exist is created here and nowhere else: the
systemd user unit, the one credential file, the Postgres role/database, the
schema. Projects do not install units for themselves; that is how :9876 ended
up owned by an untracked snapshot with no way to tell which tree was serving.

Credentials come from a single orch-owned file, ``~/.config/orch/fetch.env``,
loaded by the unit with ``EnvironmentFile=``. The service inherits no ambient
environment and reads no project ``.env``, so an empty ``ZYTE_API_KEY`` in
whatever tree happens to be checked out can no longer silently disable half
the tier ladder. ``sync_credentials`` refuses to write an empty value at all —
an unset key is reported, not persisted as ``""``.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import (
    CONFIG_DIR,
    CREDENTIAL_SOURCES,
    ENV_FILE,
    LEGACY_UNITS,
    STATE_DIR,
    SYSTEMD_USER_DIR,
    UNIT_FILE,
    UNIT_NAME,
    base_url,
    bind,
    orch_pythonpath,
    service_python,
    sites_root,
    web_fetch_dir,
)


DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/2"
PG_ROLE = "web_fetch"
PG_DATABASE = "web_fetch"

# Keys orch computes and owns in ENV_FILE. Anything else in that file is a
# human addition and is preserved verbatim across rewrites.
MANAGED_KEYS = ("WEB_FETCH_DATABASE_URL", "WEB_FETCH_REDIS_URL") + tuple(
    var for _, var in CREDENTIAL_SOURCES
)


def _echo(message: str) -> None:
    print(f"  {message}")


# ── systemd ─────────────────────────────────────────────────────────────────

def _systemd_env() -> dict[str, str]:
    """systemctl --user needs a session bus. A non-login context (a bridge
    worker, a provisioning script) often has neither variable set even though
    the user manager is running, and the failure is an opaque
    "Failed to connect to bus: No medium found"."""
    env = dict(os.environ)
    uid = os.getuid()
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    env["XDG_RUNTIME_DIR"] = runtime
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    return env


def systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        env=_systemd_env(),
        capture_output=True,
        text=True,
        check=check,
    )


def unit_text() -> str:
    """The unit, generated so the resolved paths are visible in the file.

    ExecStart runs web-fetch's own interpreter with orch on PYTHONPATH: the
    fetcher's dependencies come from its lockfile, orch contributes stdlib-only
    serving code, and neither project has to vendor the other.
    """
    host, port = bind()
    python = service_python()
    workdir = web_fetch_dir()
    if not python.is_file():
        # No venv yet — fall back to uv, which resolves (and creates) it.
        uv = shutil.which("uv") or f"{Path.home()}/.local/bin/uv"
        exec_start = (
            f"{uv} run --project {workdir} python -m orch.fetchsvc serve"
        )
    else:
        exec_start = f"{python} -m orch.fetchsvc serve"

    return f"""[Unit]
# Managed by orch — regenerate with `orch fetch install`. Do not hand-edit.
Description=orch local web fetch service ({host}:{port}, backed by web-fetch)
Documentation=file://{sites_root() / 'orch'}/README.md
After=network-online.target postgresql.service redis-server.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={workdir}
# The ONLY source of credentials and connection strings. The service reads no
# project .env, so a stale or empty key in a checkout cannot reach it.
EnvironmentFile={ENV_FILE}
Environment=PYTHONPATH={orch_pythonpath()}
Environment=ORCH_WEB_FETCH_DIR={workdir}
Environment=ORCH_FETCH_HOST={host}
Environment=ORCH_FETCH_PORT={port}
ExecStart={exec_start}
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


# ── credentials ─────────────────────────────────────────────────────────────

def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def write_env_file(values: dict[str, str], path: Path = ENV_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# orch fetch service configuration.",
        "# Managed keys are rewritten by `orch fetch sync-credentials`;",
        "# anything else here is preserved. Mode 600 — it holds secrets.",
    ]
    for key in sorted(values):
        lines.append(f"{key}={values[key]}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)


def sync_credentials() -> dict[str, Any]:
    """Mirror orch's per-provider secret files into the one env file.

    An empty or missing secret file yields *no* entry. Writing ``KEY=`` is
    what made a missing credential indistinguishable from a configured one:
    the variable existed, so nothing complained, and the tier was skipped in
    silence.
    """
    values = read_env_file()
    found: list[str] = []
    missing: list[str] = []

    for relative, var in CREDENTIAL_SOURCES:
        source = Path.home() / ".config" / relative
        secret = ""
        if source.is_file():
            try:
                secret = source.read_text().strip()
            except OSError:
                secret = ""
        if secret:
            values[var] = secret
            found.append(var)
        else:
            values.pop(var, None)
            missing.append(f"{var} (expected in ~/.config/{relative})")

    values.setdefault("WEB_FETCH_REDIS_URL", DEFAULT_REDIS_URL)
    write_env_file(values)
    return {"found": found, "missing": missing, "path": str(ENV_FILE)}


# ── database ────────────────────────────────────────────────────────────────

def _psql_as_postgres(sql: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sudo", "-n", "-u", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-tAc", sql],
        capture_output=True,
        text=True,
    )


def ensure_database() -> str:
    """Create the service's Postgres role + database if absent and return its
    URL. Idempotent: an existing URL in the env file is kept, so the cache
    survives re-provisioning."""
    values = read_env_file()
    existing = values.get("WEB_FETCH_DATABASE_URL", "").strip()
    if existing:
        return existing

    password = secrets.token_urlsafe(24)
    role_exists = _psql_as_postgres(
        f"SELECT 1 FROM pg_roles WHERE rolname = '{PG_ROLE}'"
    )
    if role_exists.returncode != 0:
        _echo(
            "could not reach postgres as the postgres superuser "
            f"({role_exists.stderr.strip() or 'sudo unavailable'}); "
            "set WEB_FETCH_DATABASE_URL in "
            f"{ENV_FILE} by hand to enable the cache"
        )
        return ""

    if role_exists.stdout.strip() == "1":
        # Rotate to a password we know — the point is that orch, not a
        # forgotten .env, holds the only copy.
        _psql_as_postgres(
            f"ALTER ROLE {PG_ROLE} WITH LOGIN PASSWORD '{password}'"
        )
    else:
        _psql_as_postgres(
            f"CREATE ROLE {PG_ROLE} WITH LOGIN PASSWORD '{password}'"
        )

    db_exists = _psql_as_postgres(
        f"SELECT 1 FROM pg_database WHERE datname = '{PG_DATABASE}'"
    )
    if db_exists.stdout.strip() != "1":
        created = _psql_as_postgres(
            f"CREATE DATABASE {PG_DATABASE} OWNER {PG_ROLE}"
        )
        if created.returncode != 0:
            _echo(f"could not create database: {created.stderr.strip()}")
            return ""

    url = f"postgresql://{PG_ROLE}:{password}@127.0.0.1:5432/{PG_DATABASE}"
    values["WEB_FETCH_DATABASE_URL"] = url
    write_env_file(values)
    return url


def migrate(url: str) -> bool:
    """Apply web-fetch's bundled schema. All DDL is IF NOT EXISTS."""
    if not url:
        return False
    python = service_python()
    if not python.is_file():
        _echo(f"no interpreter at {python}; run `uv sync` in {web_fetch_dir()}")
        return False
    env = dict(os.environ)
    env["WEB_FETCH_DATABASE_URL"] = url
    proc = subprocess.run(
        [str(python), "-m", "web_fetch.migrate"],
        cwd=str(web_fetch_dir()),
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        _echo(f"migration failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return False
    return True


# ── lifecycle ───────────────────────────────────────────────────────────────

def install(*, start: bool = True) -> int:
    """Provision everything and (re)start the unit. Safe to re-run."""
    workdir = web_fetch_dir()
    if not (workdir / "src" / "web_fetch").is_dir():
        _echo(f"web-fetch not found at {workdir}")
        _echo("clone it there, or set ORCH_WEB_FETCH_DIR / [fetch] dir in "
              "~/.orch/config.toml")
        return 1

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    creds = sync_credentials()
    _echo(f"credentials: {', '.join(creds['found']) or 'none found'}")
    for item in creds["missing"]:
        _echo(f"  not configured: {item}")

    url = ensure_database()
    if url:
        _echo(f"database: {PG_DATABASE} (role {PG_ROLE})")
        if migrate(url):
            _echo("schema: up to date")

    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    UNIT_FILE.write_text(unit_text())
    _echo(f"unit: {UNIT_FILE}")

    systemctl("daemon-reload")
    if start:
        result = systemctl("enable", "--now", UNIT_NAME)
        if result.returncode != 0:
            _echo(f"enable failed: {result.stderr.strip()}")
            return 1
        systemctl("restart", UNIT_NAME)
        _echo(f"started {UNIT_NAME} on {base_url()}")
    return 0


def uninstall() -> int:
    systemctl("disable", "--now", UNIT_NAME)
    if UNIT_FILE.exists():
        UNIT_FILE.unlink()
    systemctl("daemon-reload")
    _echo(f"removed {UNIT_NAME}")
    return 0


def retire_legacy() -> list[str]:
    """Stop and disable the units that used to own the port.

    Two implementations must never both be able to claim :9876 — whichever
    won the race would decide which code was actually serving, which is how
    the previous state went unnoticed for four months.
    """
    retired = []
    for unit in LEGACY_UNITS:
        shown = systemctl("show", unit, "--property=LoadState", "--value")
        if shown.stdout.strip() in ("not-found", ""):
            continue
        systemctl("disable", "--now", unit)
        # These units exit 143 on SIGTERM, so systemd parks them in `failed`
        # even though the stop was clean. Clearing it keeps `orch fetch
        # doctor` from reporting a scary state for a unit we deliberately
        # retired — and makes a *real* future failure visible.
        systemctl("reset-failed", unit)
        retired.append(unit)
    return retired


def adopt() -> int:
    """Take ownership of :9876 from the legacy units, then install."""
    retired = retire_legacy()
    if retired:
        _echo(f"stopped and disabled: {', '.join(retired)}")
    else:
        _echo("no legacy units to retire")
    return install()


def simple_action(action: str) -> int:
    result = systemctl(action, UNIT_NAME)
    if result.returncode != 0:
        _echo(result.stderr.strip() or f"{action} failed")
        return result.returncode
    _echo(f"{UNIT_NAME}: {action} ok")
    return 0


# ── health ──────────────────────────────────────────────────────────────────

def _http_get(path: str, timeout: float = 5.0) -> Any:
    request = urllib.request.Request(f"{base_url()}{path}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def health() -> tuple[bool, str]:
    try:
        payload = _http_get("/health")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, str(exc)
    ok = payload.get("status") == "ok"
    return ok, json.dumps(payload)


def doctor() -> int:
    """Answer "why is the fetch service not working" without reading code."""
    print(f"  service:        {UNIT_NAME}")
    print(f"  implementation: {web_fetch_dir()}")
    print(f"  listen:         {base_url()}")

    state = systemctl("is-active", UNIT_NAME).stdout.strip() or "unknown"
    enabled = systemctl("is-enabled", UNIT_NAME).stdout.strip() or "unknown"
    print(f"  unit:           {state} / {enabled}")

    for unit in LEGACY_UNITS:
        load = systemctl("show", unit, "--property=LoadState", "--value").stdout.strip()
        if load and load != "not-found":
            active = systemctl("is-active", unit).stdout.strip()
            enabled = systemctl("is-enabled", unit).stdout.strip()
            marker = ("  <-- must be stopped; it will fight for the port"
                      if active == "active" else "")
            print(f"  legacy {unit}: {active} / {enabled}{marker}")

    print(f"  env file:       {ENV_FILE}"
          f"{'' if ENV_FILE.is_file() else '  (MISSING)'}")
    values = read_env_file()
    for key in MANAGED_KEYS:
        if key in values and values[key]:
            print(f"    {key}: set")
        else:
            print(f"    {key}: NOT SET")

    ok, detail = health()
    print(f"  /health:        {'ok' if ok else 'FAILED — ' + detail}")
    if not ok:
        print("  journal:        journalctl --user -u " + UNIT_NAME + " -n 50")
        return 1

    try:
        status = _http_get("/status")
    except Exception as exc:  # noqa: BLE001
        print(f"  /status:        unavailable ({exc})")
        return 1

    live = status.get("tiers_live") or []
    print(f"  tiers live:     {', '.join(live) or 'NONE'}")
    for entry in status.get("tiers_skipped") or []:
        want = list(entry.get("missing_credentials") or [])
        want += [f"module:{m}" for m in (entry.get("missing_modules") or [])]
        print(f"    skipped {entry['tier']}: {entry['reason']}"
              f"{' — needs ' + ', '.join(want) if want else ''}")
    db = status.get("database_reachable")
    print(f"  postgres:       "
          f"{'reachable' if db else ('unreachable' if db is False else 'not used')}")
    print(f"  batch queued:   {status.get('batch_pending')}")

    if not live:
        print("  VERDICT: no tier is configured — fetches will return "
              "error_class=not_configured, which is a config gap, not a block")
        return 1
    return 0


def logs(argv: list[str]) -> int:
    return subprocess.run(
        ["journalctl", "--user", "-u", UNIT_NAME, *(argv or ["-n", "50"])],
        env=_systemd_env(),
    ).returncode


def show_status() -> int:
    proc = systemctl("status", UNIT_NAME, "--no-pager")
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode
