"""VM-side client for serialsvc. Stdlib only.

These verbs are the whole interface a Claude session in the VM needs: it
never touches the tty, never performs a reset sequence, and never needs
anything installed on the host beyond the service itself. ``monitor`` is the
one verb that wants pyserial (for the RFC 2217 data plane) and degrades to
printing the URL when it is absent.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

from . import control_url, data_url


def _call(route: str, payload: dict | None = None, timeout: float = 1900.0) -> tuple[int, dict]:
    url = f"{control_url()}{route}"
    data = json.dumps(payload or {}).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {"error": e.reason}
    except (urllib.error.URLError, OSError) as e:
        return 0, {
            "error": f"cannot reach serialsvc at {control_url()}: {e}",
            "hint": "on the macOS host run: orch serial install",
        }


# The signature of a device swapped behind the same OS path: the macOS node
# name is keyed to the USB location, not the device, so doctor/devices stay
# green while every real open fails with this errno. Checked against
# whatever text the server handed back (error/hint/stdout/stderr) so the
# guidance shows up regardless of which verb or code path produced it.
_STALE_DEVICE_MARKERS = ("Device not configured", "Errno 6", "OSError(6")


def _looks_like_stale_device(body: dict) -> bool:
    text = " ".join(str(body.get(k, "")) for k in
                     ("error", "hint", "stdout", "stderr"))
    return any(marker in text for marker in _STALE_DEVICE_MARKERS)


def _stale_device_hint() -> None:
    print("  this is the signature of a device swapped behind the same OS "
          "path (same adapter, new board) -- serialsvc reopens the port on "
          "the next attempt automatically; if it keeps happening, try: "
          "orch serial restart (on the macOS host)", file=sys.stderr)


def _print(status: int, body: dict) -> int:
    if status == 0:
        print(f"  {body['error']}", file=sys.stderr)
        if body.get("hint"):
            print(f"  {body['hint']}", file=sys.stderr)
        return 1
    if status == 429:
        print(f"  refused: {body.get('error')}", file=sys.stderr)
        print(f"  retry in {body.get('retry_after_s')}s "
              f"— every reset restarts the board (and its modem attach).",
              file=sys.stderr)
        print("  override with --force if you are sure.", file=sys.stderr)
        return 2
    if not body.get("ok", status < 400):
        print(f"  failed ({status}): {body.get('error') or ''}", file=sys.stderr)
        for stream in ("stdout", "stderr"):
            if body.get(stream):
                print(body[stream].rstrip(), file=sys.stderr)
        if body.get("hint"):
            print(f"  {body['hint']}", file=sys.stderr)
        elif _looks_like_stale_device(body):
            _stale_device_hint()
        return 1
    return 0


def _flag(argv: list[str], name: str) -> bool:
    return name in argv


def _opt(argv: list[str], name: str, default: str | None = None) -> str | None:
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def cmd_devices(argv: list[str]) -> int:
    status, body = _call("/devices", timeout=10)
    rc = _print(status, body)
    if rc:
        return rc
    devices = body.get("devices") or []
    volumes = body.get("uf2_volumes") or []
    if not devices and not volumes:
        print("  no serial devices attached")
        return 1
    for d in devices:
        tag = d.get("adapter") or d.get("description") or "unknown"
        mark = "*" if d.get("recognised") else " "
        print(f"  {mark} {d['device']}  {d.get('vid')}:{d.get('pid')}  {tag}")
    for v in volumes:
        print(f"  ^ {v['volume']}  UF2 bootloader  "
              f"{v.get('board_id') or v.get('model') or ''}")
    print("\n  * = recognised device (the first one is what serialsvc claims)")
    if volumes:
        print("  ^ = board in its UF2 bootloader: orch serial flash <image.uf2>")
    return 0


def cmd_status(argv: list[str]) -> int:
    status, body = _call("/status", timeout=10)
    rc = _print(status, body)
    if rc:
        return rc
    print(json.dumps(body, indent=2))
    return 0


def cmd_reset(argv: list[str]) -> int:
    bootloader = _flag(argv, "--bootloader") or _flag(argv, "-b")
    route = "/download-mode" if bootloader else "/hard-reset"
    status, body = _call(route, {"force": _flag(argv, "--force")}, timeout=60)
    rc = _print(status, body)
    if rc:
        return rc
    print(f"  device state: {body.get('state')}")
    return 0


def cmd_flash(argv: list[str]) -> int:
    """orch serial flash <image> [--addr 0x10000] [--baud N] [--erase]

    Repeat ``<addr>:<image>`` to write several images in one pass (one reset
    for the batch rather than one per image).
    """
    positional = [a for a in argv if not a.startswith("-")]
    # Drop values consumed by flags so they are not mistaken for images.
    for name in ("--addr", "--baud", "--after", "--volume"):
        value = _opt(argv, name)
        if value in positional:
            positional.remove(value)
    if not positional:
        print("  usage: orch serial flash <image.bin> [--addr 0x10000] "
              "[--baud 230400] [--erase]\n"
              "         orch serial flash 0x1000:boot.bin 0x10000:app.bin",
              file=sys.stderr)
        return 2

    uf2_images = [p for p in positional if p.lower().endswith(".uf2")]
    if uf2_images:
        if len(positional) != 1:
            print("  a .uf2 is flashed on its own (it carries its own "
                  "addresses)", file=sys.stderr)
            return 2
        return _flash_uf2(argv, uf2_images[0])

    images = []
    for item in positional:
        if ":" in item and not item.startswith("/"):
            addr, _, path = item.partition(":")
            images.append({"addr": addr, "path": path})
        else:
            images.append({"addr": _opt(argv, "--addr", "0x10000"), "path": item})

    payload = {
        "images": images,
        "baud": int(_opt(argv, "--baud", "230400")),
        "erase": _flag(argv, "--erase"),
        "force": _flag(argv, "--force"),
        "after": _opt(argv, "--after", "hard-reset"),
    }
    for entry in images:
        print(f"  writing {entry['path']} @ {entry['addr']}")
    print("  (host-side esptool; one reset for the batch)")
    started = time.time()
    status, body = _call("/flash", payload)
    rc = _print(status, body)
    if rc:
        return rc
    print(f"  flashed in {body.get('elapsed_s')}s "
          f"(wall {time.time() - started:.1f}s)")
    return 0


def _flash_uf2(argv: list[str], image: str) -> int:
    """UF2 boards (RP2040 etc.): the host copies the image to the bootloader
    drive, touching a running board into its bootloader first if needed."""
    payload = {
        "image": image,
        "volume": _opt(argv, "--volume"),
        "force": _flag(argv, "--force"),
    }
    print(f"  writing {image} to the UF2 bootloader drive (host-side copy)")
    started = time.time()
    status, body = _call("/uf2", payload, timeout=180)
    rc = _print(status, body)
    if rc:
        return rc
    how = (f" after rebooting {body['touched']} into it"
           if body.get("touched") else "")
    print(f"  flashed {body.get('volume')} ({body.get('board_id')}){how} "
          f"in {body.get('elapsed_s')}s (wall {time.time() - started:.1f}s)")
    if body.get("serial"):
        print(f"  firmware is up on {body['serial']}: orch serial monitor")
    else:
        print("  no serial port came up in 10s (fine if the firmware has no "
              "USB console)")
    return 0


def cmd_backup(argv: list[str]) -> int:
    positional = [a for a in argv if not a.startswith("-")]
    for name in ("--addr", "--size", "--baud"):
        value = _opt(argv, name)
        if value in positional:
            positional.remove(value)
    if not positional:
        print("  usage: orch serial backup <dest.bin> [--addr 0x0] "
              "[--size 0x400000] [--baud 230400]", file=sys.stderr)
        return 2
    payload = {
        "path": positional[0],
        "addr": _opt(argv, "--addr", "0x0"),
        "size": _opt(argv, "--size", "0x400000"),
        "baud": int(_opt(argv, "--baud", "230400")),
        "force": _flag(argv, "--force"),
    }
    print(f"  reading {payload['size']} from {payload['addr']} "
          f"-> {payload['path']}")
    status, body = _call("/backup", payload)
    rc = _print(status, body)
    if rc:
        return rc
    print(f"  saved {body.get('path')} in {body.get('elapsed_s')}s")
    return 0


def cmd_chip(argv: list[str]) -> int:
    status, body = _call("/chip", {"force": _flag(argv, "--force")}, timeout=180)
    rc = _print(status, body)
    if rc:
        return rc
    print(body.get("stdout", "").rstrip())
    return 0


class _TelnetFilter:
    """Strip telnet control sequences from an RFC 2217 stream.

    Monitoring is read-only, so a full RFC 2217 client is overkill -- but the
    bytes on the wire are still telnet-framed, and ignoring that would print
    the server's option negotiation as mojibake and mangle every literal 0xFF
    in the payload. The server opens with four commands (WILL ECHO, WILL
    SGA, DO BINARY, WILL COM-PORT-OPTION) and thereafter sends data plus the
    occasional modem-state notification.

    Keeping this in the stdlib is deliberate: the guest's interpreter then
    needs nothing installed for a Claude session to read the device.
    """

    IAC = 0xFF
    SB, SE = 0xFA, 0xF0
    WILL, WONT, DO, DONT = 0xFB, 0xFC, 0xFD, 0xFE

    def __init__(self) -> None:
        self._pending = b""
        self._in_subneg = False

    def feed(self, data: bytes) -> bytes:
        buf = self._pending + data
        self._pending = b""
        out = bytearray()
        i = 0
        while i < len(buf):
            byte = buf[i]
            if self._in_subneg:
                # Skip to IAC SE, keeping a trailing lone IAC for next feed.
                if byte == self.IAC:
                    if i + 1 >= len(buf):
                        self._pending = buf[i:]
                        return bytes(out)
                    if buf[i + 1] == self.SE:
                        self._in_subneg = False
                        i += 2
                        continue
                    i += 2
                    continue
                i += 1
                continue

            if byte != self.IAC:
                out.append(byte)
                i += 1
                continue

            if i + 1 >= len(buf):
                self._pending = buf[i:]
                return bytes(out)

            cmd = buf[i + 1]
            if cmd == self.IAC:
                out.append(self.IAC)  # escaped literal 0xFF
                i += 2
            elif cmd in (self.WILL, self.WONT, self.DO, self.DONT):
                if i + 2 >= len(buf):
                    self._pending = buf[i:]
                    return bytes(out)
                i += 3
            elif cmd == self.SB:
                self._in_subneg = True
                i += 2
            else:
                i += 2
        return bytes(out)


def cmd_monitor(argv: list[str]) -> int:
    """Stream the device's serial output over the RFC 2217 data plane."""
    import socket

    baud = int(_opt(argv, "--baud", "115200"))
    seconds = _opt(argv, "--seconds")

    status, body = _call("/baud", {"baud": baud}, timeout=15)
    rc = _print(status, body)
    if rc:
        return rc

    # data_url() carries a pyserial query (?ign_set_control) that is
    # meaningless to a raw socket; keep only host:port.
    url = data_url()
    host = url.split("//", 1)[1].split("/")[0].split("?")[0]
    hostname, _, port = host.partition(":")
    print(f"  {hostname}:{port} @ {baud}  (ctrl-c to stop)")

    try:
        sock = socket.create_connection((hostname, int(port)), timeout=10)
    except OSError as e:
        print(f"  cannot open data plane: {e}", file=sys.stderr)
        print("  is a flash in progress? check: orch serial status",
              file=sys.stderr)
        return 1

    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(1.0)
    telnet = _TelnetFilter()
    deadline = time.time() + float(seconds) if seconds else None
    try:
        while deadline is None or time.time() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            text = telnet.feed(chunk)
            if text:
                sys.stdout.write(text.decode("utf-8", "replace"))
                sys.stdout.flush()
    except KeyboardInterrupt:
        print()
    finally:
        sock.close()
    return 0
