"""
orch global config — ~/.orch/config.toml readers used by the daemon.

Project-level overrides (`<project>/.orch/project.toml`) are read directly
on the Project model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

CONFIG_FILE = Path.home() / ".orch" / "config.toml"


def _load_toml() -> dict[str, dict]:
    if not CONFIG_FILE.exists():
        return {}
    try:
        import tomllib  # type: ignore[import-not-found]
        with CONFIG_FILE.open("rb") as f:
            return tomllib.load(f)  # type: ignore[no-any-return]
    except Exception:
        return _parse_toml_fallback()


def _parse_toml_fallback() -> dict[str, dict]:
    """Minimal section/key parser for environments without tomllib."""
    out: dict[str, dict] = {}
    section = ""
    try:
        for raw in CONFIG_FILE.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                out.setdefault(section, {})
                continue
            if "=" in line and section:
                k, _, v = line.partition("=")
                out[section][k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _section(name: str) -> dict[str, Any]:
    return _load_toml().get(name, {}) or {}


# ── Daemon ─────────────────────────────────────────────────────────────────

def daemon_port() -> int:
    raw = _section("daemon").get("port", 7777)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 7777


def daemon_worker_timeout_seconds() -> int:
    raw = _section("daemon").get("worker_timeout_seconds", 900)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 900


def daemon_record_retention_days() -> int:
    raw = _section("daemon").get("record_retention_days", 30)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 30


def daemon_main_sync_interval_seconds() -> int:
    """Cadence of the daemon's local-main fast-forward sweep. 0 disables."""
    raw = _section("daemon").get("main_sync_interval_seconds", 900)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 900


# ── Bridge defaults ────────────────────────────────────────────────────────

def bridge_max_concurrent_total() -> int:
    raw = _section("bridge").get("max_concurrent_total", 3)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 3


def bridge_worker_timeout_seconds() -> int:
    """Per-call timeout for the headless Claude subprocess that runs a bridge.

    Covers both the initial turn and any clarification follow-up. Bumping this
    lets bridges that legitimately need to do long work (e.g. multi-file
    refactors with lots of test runs) finish instead of being killed.
    """
    raw = _section("bridge").get("worker_timeout_seconds", 3600)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 3600


def bridge_max_retries() -> int:
    raw = _section("bridge").get("max_retries", 3)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 3


def bridge_retry_backoff_seconds() -> list[int]:
    raw = _section("bridge").get("retry_backoff_seconds")
    if isinstance(raw, list):
        try:
            return [int(x) for x in raw]
        except (TypeError, ValueError):
            pass
    if isinstance(raw, str):
        # Fallback parser: comma- or space-separated numbers.
        try:
            cleaned = raw.strip().lstrip("[").rstrip("]")
            parts = [p.strip() for p in cleaned.replace(",", " ").split() if p.strip()]
            if parts:
                return [int(p) for p in parts]
        except ValueError:
            pass
    return [30, 120, 480]
