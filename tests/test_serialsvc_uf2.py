"""UF2 image validation and bootloader-drive discovery (orch.serialsvc.uf2).

Stdlib-only module, so these run without the service venv. The drive is
faked with a directory carrying INFO_UF2.TXT, which is exactly how the
module identifies one.
"""

import struct
import threading
import time

import pytest

from orch.serialsvc import uf2

RP2040 = 0xE48BFF56
RP2350_ARM = 0xE48BFF59


def _block(seq: int, total: int, family: int | None, flags: int = 0) -> bytes:
    if family is not None:
        flags |= 0x2000
    head = struct.pack("<8I", 0x0A324655, 0x9E5D5157, flags,
                       0x10000000 + seq * 256, 256, seq, total, family or 0)
    return head + b"\x00" * (512 - len(head) - 4) + struct.pack("<I", 0x0AB16F30)


def _image(tmp_path, family=RP2040, blocks=4, name="fw.uf2"):
    path = tmp_path / name
    path.write_bytes(b"".join(_block(i, blocks, family) for i in range(blocks)))
    return path


def _drive(root, name="RPI-RP2", board_id="RPI-RP2"):
    vol = root / name
    vol.mkdir(parents=True)
    (vol / "INFO_UF2.TXT").write_text(
        f"UF2 Bootloader v3.0\nModel: Raspberry Pi RP2\nBoard-ID: {board_id}\n")
    return vol


def test_parse_reports_families_and_payload(tmp_path):
    info = uf2.parse_image(_image(tmp_path))
    assert info["blocks"] == 4
    assert info["payload_bytes"] == 4 * 256
    assert info["families"] == [RP2040]


def test_parse_rejects_raw_binary_renamed_to_uf2(tmp_path):
    path = tmp_path / "app.uf2"
    path.write_bytes(b"\x00" * 1024)
    with pytest.raises(ValueError, match="no UF2 magic"):
        uf2.parse_image(path)


def test_parse_rejects_partial_block(tmp_path):
    path = _image(tmp_path)
    path.write_bytes(path.read_bytes()[:-10])
    with pytest.raises(ValueError, match="whole number"):
        uf2.parse_image(path)


def test_find_volumes_ignores_ordinary_drives(tmp_path):
    _drive(tmp_path)
    (tmp_path / "CIRCUITPY").mkdir()
    (tmp_path / "Macintosh HD").mkdir()
    vols = uf2.find_volumes(tmp_path)
    assert [v["name"] for v in vols] == ["RPI-RP2"]
    assert vols[0]["board_id"] == "RPI-RP2"
    assert vols[0]["bootloader"] == "UF2 Bootloader v3.0"


def test_pick_volume_refuses_to_guess_between_two(tmp_path):
    _drive(tmp_path, "RPI-RP2")
    _drive(tmp_path, "RP2350", board_id="RP2350")
    vols = uf2.find_volumes(tmp_path)
    with pytest.raises(ValueError, match="--volume"):
        uf2.pick_volume(vols)
    assert uf2.pick_volume(vols, "RP2350")["board_id"] == "RP2350"


def test_wrong_family_is_caught_before_writing(tmp_path):
    image = uf2.parse_image(_image(tmp_path, family=RP2350_ARM))
    drive = uf2.find_volumes(_drive(tmp_path / "v").parent)[0]
    assert "RP2350 ARM-S" in uf2.check_compatible(image, drive)


def test_matching_and_unknown_boards_pass(tmp_path):
    image = uf2.parse_image(_image(tmp_path))
    rp2 = {"name": "RPI-RP2", "board_id": "RPI-RP2"}
    tinyuf2 = {"name": "FTHRS3BOOT", "board_id": "ESP32S3-Feather-revC"}
    assert uf2.check_compatible(image, rp2) is None
    assert uf2.check_compatible(image, tinyuf2) is None


def test_copy_succeeds_when_the_drive_unmounts(tmp_path):
    image = _image(tmp_path)
    vol = _drive(tmp_path / "Volumes")
    drive = uf2.find_volumes(vol.parent)[0]

    def bootloader_reboots():
        while not (vol / image.name).exists():
            time.sleep(0.01)
        (vol / "INFO_UF2.TXT").unlink()

    threading.Thread(target=bootloader_reboots, daemon=True).start()
    result = uf2.copy_to_volume(image, drive, timeout=5)
    assert result["ok"]
    assert (vol / image.name).read_bytes() == image.read_bytes()


def test_copy_fails_when_the_drive_stays(tmp_path):
    image = _image(tmp_path)
    drive = uf2.find_volumes(_drive(tmp_path / "Volumes").parent)[0]
    result = uf2.copy_to_volume(image, drive, timeout=0.5)
    assert not result["ok"]
    assert "still mounted" in result["error"]
