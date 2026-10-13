"""UF2 drag-and-drop flashing, done on the host. Stdlib only.

RP2040 (and Adafruit's SAMD / ESP32-S2/S3 TinyUF2) boards do not take
firmware over a serial protocol at all. Their bootloader enumerates as a USB
mass-storage drive (``RPI-RP2``), and copying a ``.uf2`` file onto it *is*
the flash: the bootloader writes each 512-byte block as it arrives and
reboots into the new firmware after the last one, unmounting the drive.

That drive only ever appears under the host's ``/Volumes``, which is not
shared into the VM (and should not be -- a removable volume coming and going
under a virtiofs mount is a wedge waiting to happen). So the copy happens
here, on the host, and the VM names an image under the shared root exactly
as it does for an esptool flash.

Two silent failures this module exists to turn into loud ones:

*Wrong family.* The RP2040 boot ROM ignores every block whose family ID is
not RP2040's. Copying an RP2350 or SAMD image "succeeds" -- the write
completes, no error -- and the board just sits in the bootloader. The image's
families are checked against the drive before anything is written.

*No verdict from the copy.* The bootloader reboots as the last block lands,
so the final flush/close often fails with an I/O error even on a perfect
flash, and a truncated copy can close cleanly. Neither return code means
anything. The verdict is whether the drive goes away: it unmounts only once
the bootloader has accepted a complete image.
"""

from __future__ import annotations

import os
import struct
import time
from pathlib import Path

VOLUMES_ROOT = Path("/Volumes")

BLOCK_SIZE = 512
_MAGIC_START0 = 0x0A324655  # "UF2\n"
_MAGIC_START1 = 0x9E5D5157
_MAGIC_END = 0x0AB16F30
_FLAG_NOT_MAIN_FLASH = 0x00000001
_FLAG_FAMILY_ID = 0x00002000

# Family IDs from Microsoft's uf2 families.json, limited to boards this
# service has a reason to see.
FAMILIES: dict[int, str] = {
    0xE48BFF56: "RP2040",
    0xE48BFF57: "RP2XXX absolute",
    0xE48BFF58: "RP2XXX data",
    0xE48BFF59: "RP2350 ARM-S",
    0xE48BFF5A: "RP2350 RISC-V",
    0xE48BFF5B: "RP2350 ARM-NS",
    0x68ED2B88: "SAMD21",
    0x55114460: "SAMD51",
    0xBFDD4EEE: "ESP32-S2",
    0xC47E5767: "ESP32-S3",
}

# What each boot-ROM drive will actually write, keyed by the Board-ID line of
# its INFO_UF2.TXT. Drives not listed here (TinyUF2 boards name themselves per
# board) are not checked -- refusing an image we cannot judge would be worse
# than letting the drive-unmount verdict catch a mismatch.
ACCEPTED_FAMILIES: dict[str, frozenset[int]] = {
    "RPI-RP2": frozenset({0xE48BFF56}),
    "RP2350": frozenset({0xE48BFF57, 0xE48BFF58, 0xE48BFF59,
                         0xE48BFF5A, 0xE48BFF5B}),
}

# A 1200-baud touch reboots the board; macOS then needs a few seconds to
# enumerate the mass-storage device and mount it.
MOUNT_TIMEOUT = 20.0
# Writing ~1 MB to the RP2040 boot ROM takes a few seconds; the unmount
# follows the last block.
UNMOUNT_TIMEOUT = 30.0


def family_name(family: int) -> str:
    return FAMILIES.get(family, f"0x{family:08x}")


def parse_image(path: Path) -> dict:
    """Validate a UF2 file and summarise it. Raises ValueError if it isn't one.

    Every block is checked, not just the first: a ``.bin`` renamed to
    ``.uf2`` would otherwise be copied happily and ignored by the bootloader.
    """
    data = path.read_bytes()
    if not data or len(data) % BLOCK_SIZE:
        raise ValueError(
            f"{path} is not a UF2 image ({len(data)} bytes is not a whole "
            f"number of {BLOCK_SIZE}-byte blocks)")
    families: set[int] = set()
    payload = 0
    for offset in range(0, len(data), BLOCK_SIZE):
        block = data[offset:offset + BLOCK_SIZE]
        start0, start1, flags, _addr, size, _seq, _total, family = (
            struct.unpack_from("<8I", block))
        (end,) = struct.unpack_from("<I", block, BLOCK_SIZE - 4)
        if (start0, start1, end) != (_MAGIC_START0, _MAGIC_START1, _MAGIC_END):
            raise ValueError(
                f"{path}: block {offset // BLOCK_SIZE} has no UF2 magic -- "
                f"not a UF2 image (a raw .bin needs converting, e.g. picotool "
                f"uf2 convert)")
        if flags & _FLAG_NOT_MAIN_FLASH:
            continue
        payload += size
        if flags & _FLAG_FAMILY_ID:
            families.add(family)
    return {
        "blocks": len(data) // BLOCK_SIZE,
        "bytes": len(data),
        "payload_bytes": payload,
        "families": sorted(families),
    }


def _read_info(volume: Path) -> dict | None:
    """Parse INFO_UF2.TXT, the file every UF2 bootloader drive carries.

    Its presence is what identifies a bootloader drive, as opposed to any
    other volume -- including CircuitPython's ``CIRCUITPY``, which is a
    filesystem the *firmware* serves and must never be written a .uf2.
    """
    info_file = volume / "INFO_UF2.TXT"
    try:
        text = info_file.read_text(errors="replace")
    except FileNotFoundError:
        return None
    info = {"volume": str(volume), "name": volume.name}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            info[key.strip().lower().replace("-", "_")] = value.strip()
    first = text.splitlines()[0].strip() if text.strip() else ""
    info["bootloader"] = first
    return info


def find_volumes(root: Path = VOLUMES_ROOT) -> list[dict]:
    """Mounted UF2 bootloader drives.

    PermissionError is deliberately not swallowed: on macOS a launchd agent
    needs the Removable Volumes privacy grant to read these, and "no drive
    found" would send the caller hunting for a BOOT button that isn't the
    problem.
    """
    found = []
    try:
        entries = sorted(root.iterdir())
    except FileNotFoundError:
        return []
    for entry in entries:
        if not entry.is_dir():
            continue
        info = _read_info(entry)
        if info is not None:
            found.append(info)
    return found


def pick_volume(volumes: list[dict], name: str | None = None) -> dict | None:
    """The drive to flash, or None. Raises ValueError when it is ambiguous."""
    if name:
        for v in volumes:
            if v["name"] == name:
                return v
        return None
    if len(volumes) > 1:
        raise ValueError(
            "more than one UF2 bootloader drive is mounted ("
            + ", ".join(v["name"] for v in volumes)
            + "); name one with --volume")
    return volumes[0] if volumes else None


def wait_for_volume(name: str | None, timeout: float = MOUNT_TIMEOUT,
                    root: Path = VOLUMES_ROOT) -> dict | None:
    deadline = time.monotonic() + timeout
    while True:
        volume = pick_volume(find_volumes(root), name)
        if volume is not None or time.monotonic() >= deadline:
            return volume
        time.sleep(0.5)


def check_compatible(image: dict, volume: dict) -> str | None:
    """Why this drive would silently ignore this image, or None if it won't."""
    accepted = ACCEPTED_FAMILIES.get(volume.get("board_id", ""))
    if accepted is None or not image["families"]:
        return None
    if accepted.intersection(image["families"]):
        return None
    have = ", ".join(family_name(f) for f in image["families"])
    want = ", ".join(sorted(family_name(f) for f in accepted))
    return (f"image is built for {have}, but {volume['name']} only accepts "
            f"{want}; the boot ROM would ignore every block and the board "
            f"would stay in the bootloader")


def copy_to_volume(image: Path, volume: dict,
                   timeout: float = UNMOUNT_TIMEOUT) -> dict:
    """Write the image onto the drive and wait for the bootloader to take it.

    Plain read/write rather than shutil or cp: macOS's copyfile path carries
    extended attributes, and the boot ROM's FAT emulation has been known to
    fail those with EIO before any firmware block is written.
    """
    mount = Path(volume["volume"])
    dest = mount / image.name
    data = image.read_bytes()
    started = time.monotonic()
    write_error = None
    try:
        with open(dest, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except PermissionError:
        raise
    except OSError as e:
        # Expected on success: the drive can vanish under the final flush.
        write_error = f"{type(e).__name__}: {e}"

    deadline = started + timeout
    while mount.exists() and (mount / "INFO_UF2.TXT").exists():
        if time.monotonic() >= deadline:
            return {
                "ok": False,
                "elapsed_s": round(time.monotonic() - started, 1),
                "error": f"{volume['name']} is still mounted {timeout:.0f}s "
                         f"after the copy; the bootloader did not accept a "
                         f"complete image",
                "write_error": write_error,
            }
        time.sleep(0.25)
    return {
        "ok": True,
        "elapsed_s": round(time.monotonic() - started, 1),
        # Kept for the log: an error here alongside an unmount is the normal
        # shape of a successful flash, not a problem.
        "write_error": write_error,
    }
