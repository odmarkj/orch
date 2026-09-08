"""orch's local web-fetch service — paths, settings, and tier diagnostics.

orch owns *the service*: the systemd user unit, the port, the credentials
and the HTTP contract at ``127.0.0.1:9876``. The *implementation* is the
``web-fetch`` library (``~/Apps/web-fetch``), imported in-process. Nothing
here imports ``web_fetch`` at module level — this module stays stdlib-only
so ``python3 -m orch fetch …`` works from the plain system interpreter,
and only ``serve`` needs the library's virtualenv.

Why the HTTP layer lives in orch rather than in web-fetch: the shape at
:9876 is not a property of the fetcher, it is the platform contract orch
publishes to every Claude session in the VM (it is documented in the global
CLAUDE.md that orch installs). web-fetch stays a library with a rich
in-process API that other consumers embed directly; orch owns the narrow,
agent-facing projection of it, plus the process that serves it.
"""

from __future__ import annotations

import os
from pathlib import Path


# ── Contract ────────────────────────────────────────────────────────────────
# Host and port are the published contract. Every project's CLAUDE.md tells
# Claude to POST here, so these are not casually configurable: the env
# overrides exist for tests and for a second instance, not for relocation.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9876

SERVICE_NAME = "orch-fetch"
UNIT_NAME = f"{SERVICE_NAME}.service"

# The units that used to own :9876. `orch fetch adopt` stops and disables
# these so two implementations can never both claim the port.
LEGACY_UNITS = ("asha-daemon.service", "asha-worker.service")


# ── Filesystem layout ───────────────────────────────────────────────────────
# Last-resort default for the projects root. orch already hardcodes this path
# in lima/orch.yaml and lima/provision-services.sh — inside the VM the host's
# ~/Apps is virtiofs-mounted at the *host's* path, not under $HOME, so there
# is no portable way to derive it. Every lookup below prefers an explicit
# setting and only falls back here.
_FALLBACK_SITES_ROOT = Path("/Users/joshuaodmark/Apps")

CONFIG_DIR = Path.home() / ".config" / "orch"

# The single orch-owned credential/config file. The systemd unit loads it with
# EnvironmentFile= and inherits nothing else, so a stray `.env` in whichever
# tree happens to be checked out can never reach the service.
ENV_FILE = CONFIG_DIR / "fetch.env"

# Overridable so a second instance (a test, a staging port) gets its own batch
# queue instead of racing the live one for the same rows.
STATE_DIR = Path(
    os.environ.get("ORCH_FETCH_STATE_DIR") or (Path.home() / ".orch" / "fetch")
).expanduser()
BATCH_DB = STATE_DIR / "batches.db"

SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
UNIT_FILE = SYSTEMD_USER_DIR / UNIT_NAME

# Provider credentials orch mirrors into ENV_FILE. Each entry maps an
# orch-owned secret file to the environment variable web_fetch reads.
# ~/.config/zyte/api_key is already provisioned by lima/orch.yaml; the rest
# are here so adding a provider is a one-line change rather than a redesign.
CREDENTIAL_SOURCES: tuple[tuple[str, str], ...] = (
    ("zyte/api_key", "ZYTE_API_KEY"),
    ("brightdata/wu_api_key", "BRIGHTDATA_WU_API_KEY"),
    ("brightdata/res_user", "BRIGHTDATA_RES_USER"),
    ("brightdata/res_pass", "BRIGHTDATA_RES_PASS"),
    ("brightdata/sb_customer", "BRIGHTDATA_SB_CUSTOMER"),
    ("lightpanda/url", "LIGHTPANDA_URL"),
)


def sites_root() -> Path:
    """Root that holds the sibling project checkouts (``~/Apps`` on the host,
    virtiofs-mounted at the same path in the VM)."""
    env = os.environ.get("ORCH_SITES_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    configured = _config_toml_value("projects", "sites_root")
    if configured:
        return Path(configured).expanduser()
    return _FALLBACK_SITES_ROOT


def web_fetch_dir() -> Path:
    """Directory of the web-fetch checkout that backs the service.

    Resolution order: ``$ORCH_WEB_FETCH_DIR`` → ``[fetch] dir`` in
    ~/.orch/config.toml → ``<sites_root>/web-fetch`` → the first ancestor of
    this file with a ``web-fetch/src/web_fetch`` in it (this is what makes it
    work from a git worktree, where the file's own path is a few levels
    deeper than the real checkout).
    """
    env = os.environ.get("ORCH_WEB_FETCH_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    configured = _config_toml_value("fetch", "dir")
    if configured:
        return Path(configured).expanduser()

    candidate = sites_root() / "web-fetch"
    if (candidate / "src" / "web_fetch").is_dir():
        return candidate
    for ancestor in Path(__file__).resolve().parents:
        guess = ancestor / "web-fetch"
        if (guess / "src" / "web_fetch").is_dir():
            return guess
    return candidate


def orch_path_entries() -> list[Path]:
    """Directories to put on PYTHONPATH so ``import orch.fetchsvc`` resolves,
    most specific first.

    Normally one entry. It is a list because ``orch fetch install`` can be run
    from a git worktree whose code has not landed on the shared checkout yet:
    the worktree goes first so the unit runs the code that generated it, and
    the shared checkout follows so the unit keeps working after the worktree
    is cleaned up. ``orch`` is a regular package, so Python takes whichever
    entry it finds first rather than merging them — the order is the whole
    mechanism.
    """
    entries: list[Path] = []
    env = os.environ.get("ORCH_HOME", "").strip()
    if env:
        entries.append(Path(env).expanduser())
    entries.append(Path(__file__).resolve().parents[2])
    entries.append(sites_root() / "orch")

    out: list[Path] = []
    for candidate in entries:
        if candidate in out:
            continue
        if (candidate / "orch" / "__init__.py").is_file():
            out.append(candidate)
    return out


def orch_pythonpath() -> str:
    return os.pathsep.join(str(p) for p in orch_path_entries())


def orch_dir() -> Path:
    """Primary directory ``import orch`` will resolve from."""
    entries = orch_path_entries()
    return entries[0] if entries else Path(__file__).resolve().parents[2]


def service_python() -> Path:
    """Interpreter that can import both ``web_fetch`` and ``orch``.

    web-fetch's own virtualenv: its lockfile owns the fetcher's dependency
    set, and orch contributes only stdlib code on PYTHONPATH, so the service
    adds no dependency to orch itself.
    """
    return web_fetch_dir() / ".venv" / "bin" / "python"


def bind() -> tuple[str, int]:
    host = os.environ.get("ORCH_FETCH_HOST", "").strip() or DEFAULT_HOST
    raw = os.environ.get("ORCH_FETCH_PORT", "").strip()
    try:
        port = int(raw) if raw else DEFAULT_PORT
    except ValueError:
        port = DEFAULT_PORT
    return host, port


def base_url() -> str:
    host, port = bind()
    return f"http://{host}:{port}"


def batch_concurrency() -> int:
    raw = os.environ.get("ORCH_FETCH_BATCH_CONCURRENCY", "").strip()
    try:
        return max(1, int(raw)) if raw else 8
    except ValueError:
        return 8


def _config_toml_value(section: str, key: str) -> str:
    """Read one ``key`` from ``[section]`` of ~/.orch/config.toml.

    Deliberately a hand-rolled reader rather than tomllib: this module is
    imported by the 3.11 system interpreter and by web-fetch's 3.13 venv, and
    the file is a handful of flat key/value lines.
    """
    path = Path.home() / ".orch" / "config.toml"
    if not path.is_file():
        return ""
    current = None
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            continue
        if current == section and "=" in line:
            name, _, value = line.partition("=")
            if name.strip() == key:
                return value.strip().strip('"').strip("'")
    return ""
