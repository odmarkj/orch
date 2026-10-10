"""Serial/USB device service — orch's side of the host-owned tty.

Why this exists at all: the Lima VM runs ``vmType: vz``, and
Virtualization.framework exposes no generic USB passthrough (its USB API
covers mass storage only). Lima has no USB stanza either. So a USB-serial
adapter plugged into the Mac can never appear as a device *inside* the VM,
and every Claude session that needs one lives in the VM.

The shape that follows from that: macOS owns the tty, and this service hands
the VM everything it needs over loopback.

    rfc2217://127.0.0.1:4700   data, baud, flush   (monitor, in-VM esptool)
    http://127.0.0.1:4701      control plane       (reset, flash, status)

Both bind loopback only. The guest reaches them as ``host.lima.internal``
because Lima's user-mode network forwards the gateway to the host's loopback
stack, so nothing is exposed to the LAN.

Two hard-won details are encoded in the control plane rather than left to
callers:

*Bootloader entry cannot be done from the VM.* ESP32 auto-reset circuits are
interlocked -- asserting both DTR and RTS pulls neither EN nor IO0 low -- so
entering download mode requires changing both lines in a single
``ioctl(TIOCMSET)``. RFC 2217 carries one control line per subnegotiation,
making the invalid intermediate state ~50ms wide, which releases reset with
IO0 still high and boots the application instead. Hence ``POST
/download-mode``: the host performs the atomic pulse train, and the guest
runs esptool with ``--before no-reset``.

*Resets are not free.* On a board driving a cellular modem, every reset
restarts the modem's attach sequence; repeated failed attaches walk it into
multi-minute 3GPP backoff. esptool's default strategy re-runs its reset on
every connect attempt, so one failed flash can mean seven modem restarts.
Every reset here is deliberate, counted, and rate-limited (see
``reset_cooldown``), so an agent retrying in a loop cannot quietly thrash the
hardware.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

SERVICE_NAME = "orch-serialsvc"
LAUNCHD_LABEL = "com.orch.serialsvc"

# ── Filesystem layout ───────────────────────────────────────────────────────
# Same reasoning as fetchsvc: inside the VM the host's ~/Apps is virtiofs
# mounted at the *host's* path, not under $HOME, so there is no portable way
# to derive it. Prefer an explicit setting; fall back to the known path.
_FALLBACK_SITES_ROOT = Path("/Users/joshuaodmark/Apps")

CONFIG_DIR = Path.home() / ".config" / "orch"
STATE_DIR = Path(
    os.environ.get("ORCH_SERIAL_STATE_DIR") or (Path.home() / ".orch" / "serial")
).expanduser()
LOG_FILE = STATE_DIR / "serialsvc.log"

# The service's own virtualenv. pyserial and esptool live here rather than in
# orch's dependency set: orch stays importable with nothing but the stdlib,
# which is what lets the VM-side client run on the guest's system python.
VENV_DIR = STATE_DIR / "venv"
VENV_PACKAGES = ("pyserial>=3.5", "esptool>=5.0")

LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_DATA_PORT = 4700
DEFAULT_CONTROL_PORT = 4701

# Idle/monitor baud. 115200 is the ESP-IDF console and ROM-loader default;
# an application that reconfigures UART0 (ours drops to 9600 after boot)
# needs --baud on the monitor call.
DEFAULT_BAUD = 115200

# Seconds between resets the service will honour. Tuned for hardware where a
# reset costs a cellular re-attach, not for how fast the chip can reboot.
DEFAULT_RESET_COOLDOWN = 60.0

# Seconds a fresh monitor session gives a handle to produce its first byte
# before treating it as suspect. Not a general idle timeout -- a monitor
# legitimately sits quiet for minutes waiting on a sentinel log line. This
# only fires in the opening window right after connecting, which is exactly
# where a handle can look open (``is_open`` true, ``in_waiting`` raising
# nothing) while actually belonging to a device that reset and moved on: a
# real boot prints its first lines within a couple of seconds, so silence
# past this grace period is the signal, not the data itself. See
# ``Redirector._try_heal`` in server.py.
DEFAULT_STALE_READ_GRACE = 5.0

# USB-serial bridges worth claiming, by (vid, pid). Matching on identity
# rather than on a /dev path matters: the macOS node name embeds the USB
# location ID (/dev/cu.usbserial-11240), so it changes the moment the cable
# moves to a different hub port.
KNOWN_ADAPTERS: dict[tuple[int, int], str] = {
    (0x1A86, 0x7523): "QinHeng CH340/CH341",
    (0x1A86, 0x55D4): "QinHeng CH9102",
    (0x10C4, 0xEA60): "Silicon Labs CP210x",
    (0x0403, 0x6001): "FTDI FT232R",
    (0x0403, 0x6015): "FTDI FT231X",
    (0x303A, 0x1001): "Espressif USB-Serial-JTAG",
}


def is_macos() -> bool:
    return platform.system() == "Darwin"


def sites_root() -> Path:
    """Root holding the project checkouts (``~/Apps``, mounted at the same
    absolute path inside the VM)."""
    env = os.environ.get("ORCH_SITES_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    return _FALLBACK_SITES_ROOT


def shared_roots() -> tuple[Path, ...]:
    """Directories a firmware image may be read from.

    A flash request names a path, never a payload, because ~/Apps is
    virtiofs-mounted at an identical absolute path on both sides -- a VM build
    is already readable by the host with no copying. Restricting to the shared
    roots keeps that property honest: a path that resolves outside them does
    not mean the same thing in both places, so it is refused rather than
    silently read from the host's filesystem.
    """
    extra = os.environ.get("ORCH_SERIAL_IMAGE_ROOTS", "").strip()
    roots = [sites_root()]
    if extra:
        roots.extend(Path(p).expanduser() for p in extra.split(os.pathsep) if p)
    return tuple(roots)


def service_python() -> Path:
    """Interpreter with pyserial + esptool available."""
    return VENV_DIR / "bin" / "python"


def orch_path_entries() -> tuple[Path, ...]:
    """Repo root(s) that ``import orch`` should resolve from."""
    return (Path(__file__).resolve().parents[2],)


def orch_pythonpath() -> str:
    return os.pathsep.join(str(p) for p in orch_path_entries())


def bind() -> tuple[str, int, int]:
    """(host, data_port, control_port) for the daemon's two listeners."""
    host = os.environ.get("ORCH_SERIAL_HOST", "").strip() or DEFAULT_HOST

    def _port(var: str, default: int) -> int:
        raw = os.environ.get(var, "").strip()
        try:
            return int(raw) if raw else default
        except ValueError:
            return default

    return (
        host,
        _port("ORCH_SERIAL_DATA_PORT", DEFAULT_DATA_PORT),
        _port("ORCH_SERIAL_CONTROL_PORT", DEFAULT_CONTROL_PORT),
    )


def reset_cooldown() -> float:
    raw = os.environ.get("ORCH_SERIAL_RESET_COOLDOWN", "").strip()
    try:
        return float(raw) if raw else DEFAULT_RESET_COOLDOWN
    except ValueError:
        return DEFAULT_RESET_COOLDOWN


def stale_read_grace() -> float:
    raw = os.environ.get("ORCH_SERIAL_STALE_READ_GRACE", "").strip()
    try:
        return float(raw) if raw else DEFAULT_STALE_READ_GRACE
    except ValueError:
        return DEFAULT_STALE_READ_GRACE


def control_host() -> str:
    """Where the control plane is reachable *from the caller*.

    The same ``orch serial`` verbs run on the host and in the VM; only the
    address of the host's loopback differs.
    """
    override = os.environ.get("ORCH_SERIAL_CONTROL_HOST", "").strip()
    if override:
        return override
    return DEFAULT_HOST if is_macos() else "host.lima.internal"


def control_url() -> str:
    _, _, port = bind()
    raw = os.environ.get("ORCH_SERIAL_CONTROL_PORT_REMOTE", "").strip()
    try:
        port = int(raw) if raw else port
    except ValueError:
        pass
    return f"http://{control_host()}:{port}"


def data_url() -> str:
    """pyserial URL for the data plane, as seen by the caller.

    ``ign_set_control`` is not optional: without it a client's control-line
    writes race the download mode the host just established, and the
    sequential DTR/RTS transitions would knock the chip back into the
    application. Bootloader entry belongs to ``POST /download-mode``.
    """
    _, port, _ = bind()
    raw = os.environ.get("ORCH_SERIAL_DATA_PORT_REMOTE", "").strip()
    try:
        port = int(raw) if raw else port
    except ValueError:
        pass
    return f"rfc2217://{control_host()}:{port}?ign_set_control"


def require_macos(command: str) -> None:
    """Fail fast for verbs that drive launchd or the tty itself."""
    if is_macos():
        return
    print(
        f"orch serial {command}: host-only — it manages the launchd agent and\n"
        f"the USB tty, neither of which exists inside the VM.\n\n"
        f"Run it in a terminal on the macOS host.\n\n"
        f"From the VM these work against {control_url()}:\n"
        f"  orch serial status | devices | monitor | flash | reset | backup",
        file=sys.stderr,
    )
    raise SystemExit(2)
