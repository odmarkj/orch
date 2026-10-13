"""The serialsvc daemon: RFC 2217 data plane + HTTP control plane.

Runs under launchd on the macOS host. Requires the service venv (pyserial,
esptool); see ``orch serial install``.

Exclusivity is the central concern. One tty, three kinds of consumer:

  * a long-lived RFC 2217 session (monitoring from the VM),
  * atomic DTR/RTS pulse trains (bootloader entry, hard reset),
  * an esptool subprocess (flash, backup, chip id).

The subprocess needs the port to itself, so ``DeviceOwner.exclusive()``
drops any live data session, closes the port, runs the operation, and
reopens. A monitor client therefore sees its connection drop during a flash,
which is both unavoidable and the honest signal.

Staleness is the other central concern. The macOS node name
(``/dev/cu.usbserial-XXXXX``) is keyed to the USB location ID, so a device
swapped behind the same physical port -- same adapter, different board --
keeps the same path. ``is_open`` on a cached ``Serial`` object only tracks
whether *this process* called ``close()``, not whether the fd still belongs
to a live device, so a naive cache stays "open" and reports healthy forever
while every ioctl on it fails with ``OSError(6, 'Device not configured')``.
``DeviceOwner._open()`` screens for this on every call (``_handle_is_live()``)
and reopens fresh rather than trusting the cache; ``live_check()`` exposes
the same screen, read-only, for ``doctor``/``status`` to report it instead of
staying green.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import serial
import serial.rfc2217
import serial.tools.list_ports

# Espressif's own pulse trains, shared by esptool and esp-idf-monitor.
# Imported rather than reimplemented: the DTR/RTS convention here is
# active-low (asserted drives the pin LOW, so PIN_LOW is True), and getting
# one step backwards silently boots the application instead of the ROM
# loader. That bug is invisible except as "No serial data received".
from esp_pylib.serial_reset import hard_reset as esp_hard_reset
from esp_pylib.serial_reset import unix_tight_bootloader_reset

from . import (
    DEFAULT_BAUD,
    KNOWN_ADAPTERS,
    LOG_FILE,
    NATIVE_USB_VENDORS,
    STATE_DIR,
    bind,
    reset_cooldown,
    service_python,
    shared_roots,
    uf2,
)

log = logging.getLogger("serialsvc")

# esptool is given a generous ceiling: a 4MB read at 230400 runs several
# minutes, and killing it mid-erase is worse than waiting.
FLASH_TIMEOUT = 1800.0

# macOS's signature for "the fd is attached to a device that is no longer
# there": an ENXIO from the kernel, surfaced by pyserial as either a bare
# OSError(6, 'Device not configured') or a SerialException wrapping it (e.g.
# "Could not configure port: (6, 'Device not configured')"). This is exactly
# what a USB device swap behind the same OS path produces -- the macOS node
# name is keyed to the USB location ID, so the new device gets the *same*
# /dev/cu.* path, but any fd opened against the old device is permanently
# dead. It does not clear on retry or on a physical reconnect; only a fresh
# open() against the (new) path works.
_STALE_DEVICE_ERRNO = 6


def _is_stale_device_error(exc: BaseException) -> bool:
    """True for the specific errno-6 'device swapped under us' signature."""
    if getattr(exc, "errno", None) == _STALE_DEVICE_ERRNO:
        return True
    return "Device not configured" in str(exc)


def _text_has_stale_device_error(*texts: str) -> bool:
    """Same check against subprocess output (esptool has no exception to
    inspect -- the daemon only sees its stdout/stderr)."""
    return any("Device not configured" in t for t in texts if t)


# ── device discovery ───────────────────────────────────────────────────────


def list_devices() -> list[dict]:
    """Serial devices present, recognised adapters first.

    On macOS both /dev/tty.* and /dev/cu.* exist for one device; cu ("call
    out") is the right one for a host-initiated session because it does not
    block waiting for carrier detect.
    """
    found = []
    for p in serial.tools.list_ports.comports():
        if p.device.startswith("/dev/tty.") and os.path.exists(
            p.device.replace("/dev/tty.", "/dev/cu.")
        ):
            continue
        vid, pid = p.vid, p.pid
        known = KNOWN_ADAPTERS.get((vid, pid)) if vid is not None else None
        native = known is None and vid in NATIVE_USB_VENDORS
        if native:
            known = f"{NATIVE_USB_VENDORS[vid]} native USB ({p.description})"
        found.append({
            "device": p.device,
            "vid": f"{vid:04x}" if vid is not None else None,
            "pid": f"{pid:04x}" if pid is not None else None,
            "adapter": known,
            "native_usb": native,
            "serial_number": p.serial_number,
            "location": p.location,
            "description": p.description,
            "recognised": known is not None,
        })
    found.sort(key=lambda d: (not d["recognised"], d["device"]))
    return found


def resolve_device(preferred: str | None = None) -> str | None:
    """Pick the tty to own.

    Identity beats path: ORCH_SERIAL_DEVICE may name a literal node, but the
    default is the first recognised USB-serial bridge, because the macOS node
    name embeds the USB location ID and changes when the cable moves ports.
    """
    explicit = preferred or os.environ.get("ORCH_SERIAL_DEVICE", "").strip()
    if explicit:
        return explicit if os.path.exists(explicit) else None

    want = os.environ.get("ORCH_SERIAL_VIDPID", "").strip().lower()
    for dev in list_devices():
        if want:
            if f"{dev['vid']}:{dev['pid']}" == want:
                return dev["device"]
            continue
        if dev["recognised"]:
            return dev["device"]
    return None


def is_native_usb(path: str | None) -> bool:
    return any(d["device"] == path and d["native_usb"] for d in list_devices())


def native_usb_device() -> str | None:
    """The board a 1200-baud touch may be aimed at.

    Never ``resolve_device()``: with an ESP32 on a CH340 also attached, that
    can name the bridge, and opening a bridge toggles DTR -- a reset of the
    wrong board, and on this rig a cellular re-attach.
    """
    for dev in list_devices():
        if dev["native_usb"]:
            return dev["device"]
    return None


def touch_1200(path: str) -> None:
    """Ask a running CircuitPython/Arduino/TinyUSB firmware to reboot into
    its UF2 bootloader.

    The convention is the line coding, not the bytes: the firmware watches
    for the host setting 1200 baud and then dropping DTR. The device
    disconnects as it reboots, so errors on the way out are expected.
    """
    ser = serial.Serial()
    ser.port = path
    ser.baudrate = 1200
    ser.open()
    try:
        ser.dtr = False
    except (OSError, serial.SerialException):
        pass
    try:
        ser.close()
    except (OSError, serial.SerialException):
        pass
    log.info("1200-baud touch sent to %s", path)


# ── the one owner of the tty ───────────────────────────────────────────────


class DeviceOwner:
    def __init__(self, baud: int = DEFAULT_BAUD):
        self.baud = baud
        self._lock = threading.RLock()
        self._serial: serial.Serial | None = None
        self._session: "Redirector | None" = None
        self._blocked = False
        self.resets = 0
        self.last_reset: float | None = None
        self.last_flash: dict | None = None

    # -- open/close ---------------------------------------------------------

    def device_path(self) -> str | None:
        return resolve_device()

    def _handle_is_live(self) -> bool:
        """Does the cached handle's fd still belong to a live device?

        ``pyserial``'s ``is_open`` only tracks whether *this object's*
        ``close()`` has been called -- it says nothing about whether the
        underlying fd still points at a real device. After a USB swap behind
        the same path, the fd is permanently dead and every ioctl on it
        raises; ``in_waiting`` is a cheap one to use as the probe.
        """
        try:
            self._serial.in_waiting
            return True
        except (OSError, serial.SerialException):
            return False

    def _open(self) -> serial.Serial:
        path = self.device_path()
        if path is None:
            self._close()
            raise FileNotFoundError("no recognised serial device attached")
        if (self._serial is not None and self._serial.is_open
                and self._serial.port == path and self._handle_is_live()):
            return self._serial
        if self._serial is not None:
            # Either a device swap left the old fd dead, or the resolved
            # path moved out from under it -- either way the cached handle
            # is no good and the only fix is to open a fresh one.
            log.warning("stale handle on %s; closing and reopening", path)
            self._close()
        ser = serial.serial_for_url(path, do_not_open=True)
        ser.baudrate = self.baud
        ser.timeout = 0.5
        # Deasserted on open so merely starting the daemon, or reconnecting
        # after a flash, does not pulse EN and restart the application.
        # A native-USB board is the opposite case: DTR reaches no reset pin,
        # and TinyUSB firmware (CircuitPython, Arduino-Pico, Pico SDK stdio)
        # treats it as "a terminal is listening" and discards console output
        # until it is asserted -- a monitor would connect and see nothing.
        ser.dtr = is_native_usb(path)
        ser.rts = False
        ser.open()
        self._serial = ser
        log.info("opened %s @ %d", path, ser.baudrate)
        return ser

    def _close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    @contextmanager
    def port(self):
        """Borrow the open port under the lock."""
        with self._lock:
            yield self._open()

    def set_baud(self, baud: int) -> None:
        """Retune the line rate. Costs no reset.

        Needed because an application may reconfigure UART0 after boot (ours
        drops from the 115200 IDF console to 9600), so the right monitor rate
        differs from the right flashing rate.

        This is also where the original device-swap bug showed up: a
        ``monitor`` call sets the baud before connecting, which touched
        ``self._serial.baudrate`` directly on a cached handle. If that
        handle was stale (device swapped behind the same path), the ioctl
        failed with ``OSError(6, 'Device not configured')`` *before* the
        liveness check in ``_open()`` ever ran, and the error reached the
        client as a bare ``SerialException``. Route the stale case through
        the same close-and-reopen the rest of the owner uses, instead of
        reconfiguring a handle that may already be dead.
        """
        with self._lock:
            self.baud = baud
            if self._serial is not None and self._serial.is_open:
                if not self._handle_is_live():
                    log.warning("stale handle while setting baud; reopening")
                    self._close()
                else:
                    try:
                        self._serial.baudrate = baud
                        # Whatever is already buffered was framed at the old
                        # rate and would surface as a burst of mojibake in
                        # the monitor.
                        self._serial.reset_input_buffer()
                    except (OSError, serial.SerialException) as e:
                        if not _is_stale_device_error(e):
                            raise
                        log.warning(
                            "device went stale mid-reconfigure (%s); reopening", e)
                        self._close()
            if self._serial is None:
                # Reopen now (at the new baud) rather than leaving it to the
                # next operation, so a monitor connection right after this
                # call gets a live handle on the first try.
                try:
                    self._open()
                except FileNotFoundError:
                    pass
            log.info("baud set to %d", baud)

    @contextmanager
    def exclusive(self, require_device: bool = True):
        """Hand the raw device path to a subprocess, port fully released.

        A UF2 flash passes ``require_device=False``: a board already sitting
        in its bootloader has no serial port at all, only a drive.
        """
        with self._lock:
            path = self.device_path()
            if path is None and require_device:
                raise FileNotFoundError("no recognised serial device attached")
            self._blocked = True
            session, self._session = self._session, None
            if session is not None:
                session.stop()
            self._close()
            try:
                yield path
            finally:
                self._blocked = False

    def accepting_data_clients(self) -> bool:
        return not self._blocked

    def register_session(self, session: "Redirector | None") -> None:
        self._session = session

    def live_check(self) -> tuple[bool, str]:
        """Does the tracked device actually answer right now?

        This is the check ``doctor``/``status`` were missing: both only
        ever asked "is a recognised VID:PID present at this path", which
        stays true across a device swap because the OS node name is keyed
        to the USB location, not to the device behind it. Only an actual
        open (or a touch of an already-open handle) can tell a live device
        from a dead fd wearing the same path.

        Deliberately read-only: it reports a stale handle without clearing
        it, so ``doctor``/``status`` stay side-effect-free. The actual
        recovery happens lazily in ``_open()`` the next time a real
        operation runs.
        """
        with self._lock:
            path = self.device_path()
            if path is None:
                return False, "no recognised serial device attached"
            if self._blocked:
                # A flash/backup/chip call owns the fd right now; opening a
                # second handle to probe would race it.
                return True, f"{path} (skipped: exclusive operation in progress)"
            if (self._serial is not None and self._serial.is_open
                    and self._serial.port == path):
                if self._handle_is_live():
                    return True, path
                return False, (
                    f"{path}: stale handle (errno 6, 'Device not "
                    f"configured') -- the device behind this path changed "
                    f"since it was opened"
                )
            try:
                probe = serial.serial_for_url(path, do_not_open=True)
                probe.timeout = 0.2
                probe.open()
                probe.close()
                return True, path
            except (OSError, serial.SerialException) as e:
                return False, f"{path}: cannot open ({e})"

    # -- reset budget -------------------------------------------------------

    def cooldown_remaining(self) -> float:
        if self.last_reset is None:
            return 0.0
        return max(0.0, reset_cooldown() - (time.monotonic() - self.last_reset))

    def spend_reset(self, force: bool = False) -> float:
        """Consume one reset from the budget, or report the wait.

        Returns 0.0 when the caller may proceed, otherwise the seconds
        remaining. Deliberately a check-and-commit so a retry loop cannot
        drain the hardware: on this rig a reset costs a cellular re-attach.
        """
        with self._lock:
            remaining = self.cooldown_remaining()
            if remaining > 0 and not force:
                return remaining
            self.resets += 1
            self.last_reset = time.monotonic()
            return 0.0

    # -- operations ---------------------------------------------------------

    def _run_with_reopen_retry(self, fn) -> None:
        """Run ``fn(ser)`` against the port; on a stale-handle error, drop
        the cached handle and retry exactly once against a freshly opened
        one.

        ``_open()`` already screens for staleness on entry, so this mostly
        covers the race where the device goes away *between* that check and
        the ioctl pulse train itself. If the retry also hits the same
        error, the port is not just stale -- something is still wrong after
        a reopen -- so that's surfaced with a concrete next step rather than
        a bare errno.
        """
        with self.port() as ser:
            try:
                fn(ser)
                return
            except (OSError, serial.SerialException) as e:
                if not _is_stale_device_error(e):
                    raise
                log.warning("stale handle mid-operation (%s); reopening and "
                            "retrying once", e)
                self._close()
        try:
            with self.port() as ser:
                fn(ser)
        except (OSError, serial.SerialException) as e:
            if not _is_stale_device_error(e):
                raise
            raise RuntimeError(
                f"{e} -- reopening the port did not clear it; try: "
                f"orch serial restart (on the macOS host)"
            ) from e

    def enter_download_mode(self) -> str:
        """Put the owned board in its bootloader; returns the state reached.

        A native-USB board has no EN/IO0 wiring for the ESP pulse train to
        drive -- it would be a silent no-op reported as success -- so it gets
        the 1200-baud touch, and its "download mode" is a UF2 drive.
        """
        if is_native_usb(self.device_path()):
            with self.exclusive() as path:
                touch_1200(path)
            volume = uf2.wait_for_volume(None)
            if volume is None:
                raise RuntimeError(
                    "1200-baud touch sent but no UF2 drive mounted; the "
                    "running firmware does not honour it (bare Pico SDK "
                    "builds don't). Hold BOOT while plugging in instead.")
            log.info("UF2 bootloader entered (%s)", volume["name"])
            return f"uf2 bootloader ({volume['name']})"
        self._run_with_reopen_retry(unix_tight_bootloader_reset)
        log.info("download mode entered (ROM loader waiting)")
        return "download"

    def hard_reset(self) -> None:
        if is_native_usb(self.device_path()):
            # Same trap as above: the pulse train would "succeed" and the
            # board would keep running untouched.
            raise ValueError(
                "native-USB boards have no reset line on the serial port; "
                "press RESET, or flash a UF2 (the board reboots after)")
        self._run_with_reopen_retry(esp_hard_reset)
        log.info("hard reset issued (application firmware running)")

    def flash_uf2(self, image: Path, info: dict, volume_name: str | None,
                  force: bool) -> tuple[int, dict]:
        """Copy a UF2 image onto the board's bootloader drive.

        If no drive is mounted, a running native-USB board is touched into
        its bootloader first -- the only part of this that costs a reset,
        since a board already in the bootloader is running nothing.
        """
        with self.exclusive(require_device=False):
            touched = None
            volume = uf2.pick_volume(uf2.find_volumes(), volume_name)
            if volume is None:
                touched = native_usb_device()
                if touched is None:
                    return 503, {
                        "error": "no UF2 bootloader drive mounted and no "
                                 "native-USB board attached to reboot into one",
                        "hint": "hold the button labelled BOOT (BOOTSEL) "
                                "while plugging the board in -- not RESET",
                    }
                wait = self.spend_reset(force)
                if wait:
                    return 429, {
                        "error": "reset cooldown active",
                        "retry_after_s": round(wait, 1),
                        "hint": "rebooting into the bootloader is a reset; "
                                "pass force=true to override",
                    }
                touch_1200(touched)
                volume = uf2.wait_for_volume(volume_name)
                if volume is None:
                    return 504, {
                        "error": f"1200-baud touch sent to {touched} but no "
                                 f"UF2 drive mounted",
                        "hint": "the running firmware does not honour the "
                                "touch; hold BOOT while plugging in instead",
                    }

            mismatch = uf2.check_compatible(info, volume)
            if mismatch:
                return 400, {"error": mismatch, "volume": volume}

            log.info("uf2: %s -> %s", image, volume["volume"])
            result = uf2.copy_to_volume(image, volume)

        result.update({
            "image": str(image),
            "volume": volume["name"],
            "board_id": volume.get("board_id"),
            "families": [uf2.family_name(f) for f in info["families"]],
            "touched": touched,
        })
        if not result["ok"]:
            log.error("uf2 flash failed: %s", result["error"])
            return 500, result
        if result.get("write_error"):
            log.info("uf2: write ended with %s (normal: the drive "
                     "unmounts under the final flush)", result["write_error"])
        # Report the serial port the new firmware brings up, if it brings
        # one up -- that is what a caller's next step (monitor) needs.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result["serial"] = native_usb_device()
            if result["serial"]:
                break
            time.sleep(0.5)
        return 200, result

    def run_esptool(self, args: list[str], baud: int, bootloader: bool,
                    after: str = "hard-reset") -> dict:
        """Run one esptool command against the device.

        ``--before no-reset`` always: bootloader entry is performed here, by
        the atomic path, because esptool's own strategy would retry a
        sequential reset on every connect attempt.
        """
        with self.exclusive() as path:
            if bootloader:
                ser = serial.serial_for_url(path, do_not_open=True)
                ser.baudrate = baud
                ser.timeout = 0.5
                ser.dtr = False
                ser.rts = False
                try:
                    ser.open()
                    unix_tight_bootloader_reset(ser)
                except (OSError, serial.SerialException) as e:
                    # This open is already against the raw path, not a
                    # cached handle -- staleness here means the host-side
                    # driver for the tty is wedged, not just our own fd.
                    if _is_stale_device_error(e):
                        raise RuntimeError(
                            f"{e} -- fresh open of {path} still fails; try: "
                            f"orch serial restart (on the macOS host)"
                        ) from e
                    raise
                finally:
                    # ``close()`` on a handle whose ``open()`` itself failed
                    # is a no-op in pyserial, but don't bet the operation's
                    # own error on that.
                    try:
                        ser.close()
                    except Exception:
                        pass

            cmd = [
                str(service_python()), "-m", "esptool",
                "--port", path,
                "--baud", str(baud),
                "--before", "no-reset",
                "--after", after,
                *args,
            ]
            log.info("esptool: %s", " ".join(cmd[3:]))
            started = time.time()
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=FLASH_TIMEOUT,
            )
            elapsed = time.time() - started

        result = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "elapsed_s": round(elapsed, 1),
            # Tails only: a 4MB write produces thousands of progress lines and
            # the caller wants the verdict, not the transcript. Full output is
            # in the service log.
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-2000:],
        }
        if proc.returncode != 0:
            log.error("esptool failed (%s): %s", proc.returncode, proc.stderr[-500:])
            # esptool runs as its own subprocess against the raw path, so
            # unlike monitor/reset there is no cached handle of ours to be
            # stale here -- but the underlying tty can still be wedged
            # (e.g. the host-side driver itself needs a kick after a swap),
            # and esptool's own error text is the only place that shows.
            if _text_has_stale_device_error(proc.stdout, proc.stderr):
                result["hint"] = (
                    "esptool could not configure the port (errno 6, "
                    "'Device not configured') -- the signature of a device "
                    "swapped behind the same OS path. If this recurs, try: "
                    "orch serial restart (on the macOS host)"
                )
        return result


# ── data plane ─────────────────────────────────────────────────────────────


class Redirector:
    """Bridges one TCP client to the tty, speaking RFC 2217.

    RFC 2217 is what makes an in-VM esptool possible at all: it tunnels baud
    changes, data-bit/parity/stop settings, buffer purges and modem status,
    none of which survive a plain TCP relay -- and none of which a Linux PTY
    can represent, which is why ``socat PTY`` can monitor but never flash.
    """

    def __init__(self, owner: DeviceOwner, serial_instance, sock):
        self.owner = owner
        self.serial = serial_instance
        self.socket = sock
        self._write_lock = threading.Lock()
        self.rfc2217 = serial.rfc2217.PortManager(
            self.serial, self, logger=logging.getLogger("rfc2217")
        )
        self.alive = False

    def run(self) -> None:
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._poll_modem, daemon=True).start()
        self._writer()

    def stop(self) -> None:
        self.alive = False
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass

    def _poll_modem(self) -> None:
        while self.alive:
            time.sleep(1)
            try:
                self.rfc2217.check_modem_lines()
            except Exception:
                break

    def _reader(self) -> None:
        while self.alive:
            try:
                data = self.serial.read(1)
                if not data:
                    continue
                # Block for the first byte, then sweep up whatever else has
                # landed. PortManager.escape() yields a byte at a time, so
                # chunking here is what keeps bulk transfers moving.
                extra = self.serial.in_waiting
                if extra:
                    data += self.serial.read(extra)
                # Doubling IAC (0xFF) is the whole of escape(); bytes.replace
                # is the same transformation at C speed, and 0xFF is dense in
                # flash images.
                self._send(data.replace(b"\xff", b"\xff\xff"))
            except Exception:
                break
        self.alive = False

    def _send(self, data: bytes) -> None:
        with self._write_lock:
            self.socket.sendall(data)

    # PortManager calls back into ``write`` for its own subnegotiations.
    write = _send

    def _writer(self) -> None:
        while self.alive:
            try:
                chunk = self.socket.recv(4096)
                if not chunk:
                    break
                payload = b"".join(self.rfc2217.filter(chunk))
                if payload:
                    self.serial.write(payload)
            except Exception:
                break
        self.alive = False


def serve_data(owner: DeviceOwner, host: str, port: int) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(1)
    log.info("data:    rfc2217://%s:%d", host, port)
    while True:
        conn, addr = srv.accept()
        # The flashing protocol is tight request/response; coalescing hurts.
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if not owner.accepting_data_clients():
            log.info("refusing %s: device busy", addr)
            try:
                conn.sendall(b"device busy\r\n")
                conn.close()
            except Exception:
                pass
            continue
        log.info("client connected: %s", addr)
        session = None
        try:
            with owner.port() as ser:
                session = Redirector(owner, ser, conn)
                owner.register_session(session)
            session.run()
        except Exception as e:
            log.error("session error: %s", e)
        finally:
            owner.register_session(None)
            try:
                conn.close()
            except Exception:
                pass
            log.info("client disconnected: %s", addr)


# ── control plane ──────────────────────────────────────────────────────────


def _validate_image(raw: str) -> Path:
    """Resolve a firmware path, refusing anything outside the shared roots."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"image path must be absolute: {raw}")
    resolved = path.resolve()
    roots = shared_roots()
    if not any(resolved.is_relative_to(r.resolve()) for r in roots):
        raise ValueError(
            f"{resolved} is outside the shared roots "
            f"({', '.join(str(r) for r in roots)}); the host and the VM would "
            f"not agree on what that path means"
        )
    if not resolved.is_file():
        raise ValueError(f"no such file: {resolved}")
    return resolved


def _volume_names() -> list[str] | str:
    """For status: a missing privacy grant must not take status down too."""
    try:
        return [v["name"] for v in uf2.find_volumes()]
    except PermissionError as e:
        return f"unreadable: {e}"


def make_control_handler(owner: DeviceOwner):
    class Control(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "orch-serialsvc"

        def _reply(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, indent=2).encode() + b"\n"
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as e:
                raise ValueError(f"invalid JSON body: {e}")

        def _budget(self, body: dict) -> float:
            return owner.spend_reset(force=bool(body.get("force")))

        # -- routes ---------------------------------------------------------

        def _status(self) -> dict:
            path = owner.device_path()
            # A path existing is not the same question as the port actually
            # opening -- that's exactly the check that let a device swap
            # hide behind a permanently-green doctor/status. live_check()
            # answers the real question with a trivial open/close (or a
            # touch of the handle already open), without disturbing an
            # in-progress flash/backup/chip.
            live_ok, live_detail = owner.live_check()
            return {
                "ok": True,
                "device": path,
                "attached": path is not None,
                "port_live": live_ok,
                "port_detail": live_detail,
                "baud": owner.baud,
                "resets": owner.resets,
                "reset_cooldown_s": reset_cooldown(),
                "cooldown_remaining_s": round(owner.cooldown_remaining(), 1),
                "data_client_accepted": owner.accepting_data_clients(),
                "last_flash": owner.last_flash,
                "uf2_volumes": _volume_names(),
                "log": str(LOG_FILE),
            }

        def _flash(self, body: dict) -> tuple[int, dict]:
            images = body.get("images")
            if not images:
                if not body.get("image"):
                    return 400, {"error": "need 'images': [{addr, path}] or 'image'"}
                images = [{"addr": body.get("addr", "0x10000"),
                           "path": body["image"]}]
            if any(str(e.get("path", "")).lower().endswith(".uf2") for e in images):
                # esptool would write the UF2 container bytes verbatim to
                # flash -- a bricked app partition, not an error.
                return 400, {"error": "UF2 images go to POST /uf2, not esptool"}
            args: list[str] = []
            if body.get("erase"):
                args.append("--erase-all")
            for entry in images:
                try:
                    resolved = _validate_image(entry["path"])
                except (KeyError, ValueError) as e:
                    return 400, {"error": str(e)}
                args.extend([str(entry.get("addr", "0x10000")), str(resolved)])

            wait = self._budget(body)
            if wait:
                return 429, {
                    "error": "reset cooldown active",
                    "retry_after_s": round(wait, 1),
                    "hint": "each flash resets the board; pass force=true to override",
                }

            result = owner.run_esptool(
                ["write-flash", *args],
                baud=int(body.get("baud", 230400)),
                bootloader=True,
                after=body.get("after", "hard-reset"),
            )
            owner.last_flash = {
                "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "images": [{"addr": e.get("addr", "0x10000"), "path": e["path"]}
                           for e in images],
                "ok": result["ok"],
                "elapsed_s": result["elapsed_s"],
            }
            return (200 if result["ok"] else 500), result

        def _backup(self, body: dict) -> tuple[int, dict]:
            dest = body.get("path")
            if not dest:
                return 400, {"error": "need 'path' to write the image to"}
            dest_path = Path(dest).expanduser()
            try:
                roots = shared_roots()
                if not any(dest_path.resolve().parent.is_relative_to(r.resolve())
                           for r in roots):
                    return 400, {"error": f"{dest_path} is outside the shared roots"}
            except OSError as e:
                return 400, {"error": str(e)}
            dest_path.parent.mkdir(parents=True, exist_ok=True)

            wait = self._budget(body)
            if wait:
                return 429, {"error": "reset cooldown active",
                             "retry_after_s": round(wait, 1)}
            result = owner.run_esptool(
                ["read-flash", str(body.get("addr", "0x0")),
                 str(body.get("size", "0x400000")), str(dest_path)],
                baud=int(body.get("baud", 230400)),
                bootloader=True,
                after=body.get("after", "hard-reset"),
            )
            result["path"] = str(dest_path)
            return (200 if result["ok"] else 500), result

        def _uf2(self, body: dict) -> tuple[int, dict]:
            if not body.get("image"):
                return 400, {"error": "need 'image': an absolute path to a .uf2"}
            try:
                image = _validate_image(body["image"])
                info = uf2.parse_image(image)
            except (ValueError, OSError) as e:
                return 400, {"error": str(e)}
            code, result = owner.flash_uf2(
                image, info, body.get("volume"), bool(body.get("force")))
            if code in (200, 500):
                owner.last_flash = {
                    "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "images": [{"uf2": result.get("volume"), "path": str(image)}],
                    "ok": result["ok"],
                    "elapsed_s": result["elapsed_s"],
                }
            return code, result

        def _chip(self, body: dict) -> tuple[int, dict]:
            wait = self._budget(body)
            if wait:
                return 429, {"error": "reset cooldown active",
                             "retry_after_s": round(wait, 1)}
            result = owner.run_esptool(
                ["flash-id"], baud=int(body.get("baud", 115200)),
                bootloader=True, after=body.get("after", "hard-reset"),
            )
            return (200 if result["ok"] else 500), result

        def _dispatch(self) -> None:
            route = self.path.split("?")[0].rstrip("/") or "/"
            try:
                body = self._body() if self.command == "POST" else {}
            except ValueError as e:
                return self._reply(400, {"error": str(e)})

            try:
                if route in ("/", "/status", "/healthz"):
                    return self._reply(200, self._status())
                if route == "/devices":
                    return self._reply(200, {
                        "ok": True,
                        "devices": list_devices(),
                        # A board in its UF2 bootloader has no serial port;
                        # this is the only place it shows up.
                        "uf2_volumes": uf2.find_volumes(),
                    })
                if route == "/data-url":
                    from . import data_url
                    return self._reply(200, {"ok": True, "url": data_url()})

                if route == "/baud":
                    # A client that cannot negotiate RFC 2217 (the stdlib
                    # monitor) still needs to choose a rate, and changing baud
                    # costs nothing -- no reset, no budget.
                    try:
                        want = int(body.get("baud", owner.baud))
                    except (TypeError, ValueError):
                        return self._reply(400, {"error": "baud must be an integer"})
                    owner.set_baud(want)
                    return self._reply(200, {"ok": True, "baud": owner.baud})

                if route == "/download-mode":
                    wait = self._budget(body)
                    if wait:
                        return self._reply(429, {
                            "error": "reset cooldown active",
                            "retry_after_s": round(wait, 1),
                        })
                    state = owner.enter_download_mode()
                    return self._reply(200, {"ok": True, "state": state})

                if route == "/hard-reset":
                    if is_native_usb(owner.device_path()):
                        # Refused before the budget: there is no reset to
                        # spend it on.
                        return self._reply(400, {
                            "error": "native-USB boards have no reset line on "
                                     "the serial port; press RESET, or flash "
                                     "a UF2 (the board reboots after)",
                        })
                    wait = self._budget(body)
                    if wait:
                        return self._reply(429, {
                            "error": "reset cooldown active",
                            "retry_after_s": round(wait, 1),
                        })
                    owner.hard_reset()
                    return self._reply(200, {"ok": True, "state": "running"})

                if route == "/flash":
                    return self._reply(*self._flash(body))
                if route == "/backup":
                    return self._reply(*self._backup(body))
                if route == "/chip":
                    return self._reply(*self._chip(body))
                if route == "/uf2":
                    return self._reply(*self._uf2(body))

            except FileNotFoundError as e:
                return self._reply(503, {"error": str(e)})
            except PermissionError as e:
                # Reading /Volumes from a launchd agent needs macOS's
                # Removable Volumes grant; without it this is all you get.
                return self._reply(403, {
                    "error": str(e),
                    "hint": "on the macOS host, allow the serialsvc python "
                            f"({service_python()}) under System Settings > "
                            "Privacy & Security > Files and Folders > "
                            "Removable Volumes, then: orch serial restart",
                })
            except subprocess.TimeoutExpired:
                return self._reply(504, {"error": "esptool timed out"})
            except Exception as e:
                log.exception("control error")
                return self._reply(500, {"error": repr(e)})

            self._reply(404, {
                "error": "unknown route",
                "routes": ["/status", "/devices", "/data-url", "/download-mode",
                           "/hard-reset", "/flash", "/backup", "/chip", "/uf2"],
            })

        do_GET = _dispatch
        do_POST = _dispatch

        def log_message(self, fmt, *a):  # noqa: A003 - BaseHTTPRequestHandler API
            log.info("control %s", fmt % a)

    return Control


# ── entry point ────────────────────────────────────────────────────────────


def run() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host, data_port, control_port = bind()
    baud = DEFAULT_BAUD
    raw = os.environ.get("ORCH_SERIAL_BAUD", "").strip()
    if raw:
        try:
            baud = int(raw)
        except ValueError:
            pass

    owner = DeviceOwner(baud=baud)
    path = owner.device_path()
    if path is None:
        # Not fatal: the device may be unplugged now and attached later, and
        # launchd KeepAlive restarting in a tight loop would be worse.
        log.warning("no recognised serial device attached yet; control plane up")
    else:
        log.info("device:  %s", path)

    control = ThreadingHTTPServer((host, control_port), make_control_handler(owner))
    threading.Thread(target=control.serve_forever, daemon=True).start()
    log.info("control: http://%s:%d", host, control_port)

    serve_data(owner, host, data_port)
    return 0
