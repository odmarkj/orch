# Test drive: upload a .bin to the ESP32 from inside the VM

Paste the brief below into a Claude session running **inside the orch VM**.

Everything it needs is reachable from the guest; nothing has to happen on the
macOS host. The target address and the "do not touch" list below were derived
by reading the live device, not from the project's `.csv` — see *Why these
addresses* at the bottom.

---

## Brief (copy from here)

You are in the orch Lima VM. A USB-serial ESP32 is attached to the Mac. You
**cannot** see it as a `/dev/tty*` device here and no driver will help: the VM
is `vmType: vz`, which has no USB passthrough. Do not try to install a CH340
driver, and do not build a `socat` PTY bridge — a PTY has no modem-control
lines, so it can monitor but can never flash.

Use `orch serial`, which talks to a host-side service. Run `orch serial --help`
first.

### The device

- ESP32-D0WD rev v1.1, 4 MB flash, MAC `d4:8c:49:21:9d:e8`, CH340 bridge.
- It runs an air-quality firmware (PlatformIO project `~/Apps/iaq_pio`,
  Arduino framework) driving a Quectel BG95-M5 cellular modem.
- **Application console is 9600 baud.** Boot/IDF logs come out at 115200 for
  roughly the first 19 seconds after a reset, then the app reconfigures UART0
  down to 9600. Reading at the wrong rate looks like binary garbage, not like
  a broken link.
- The cellular antenna is disconnected, so modem attach always fails and the
  serial output is sparse and bursty. Long silences are normal and are not a
  fault you need to fix.

### Your task

Prove the firmware-upload path works end to end, without disturbing the
running firmware.

Write this 10 KB payload to the **inactive OTA slot** and verify it landed
byte-exact:

- payload: `/Users/joshuaodmark/Apps/iaq_pio/.pio/libdeps/release/TinyGSM/extras/test_10k.bin`
  (10240 bytes, sha256 `50bdbdae2982afafcaf6c3391dfc63a8541190d2adeffb7ff9408f2c90337625`)
- target: **`0x190000`** — partition `app1` / `ota_1`, 1.5 MB, currently inactive

Suggested sequence:

1. `orch serial status` and `orch serial doctor` — confirm the service is up
   and the device is attached.
2. Back up the target region first, so whatever is in the inactive slot can be
   restored: `orch serial backup /Users/joshuaodmark/Apps/iaq_pio/app1-before.bin --addr 0x190000 --size 0x2800`
   Check whether it is all `0xFF` (slot empty, nothing to lose) or holds a real
   image (an older firmware kept for rollback).
3. Flash the payload: `orch serial flash <payload> --addr 0x190000 --baud 230400`
4. Read it back and compare: `orch serial backup /Users/joshuaodmark/Apps/iaq_pio/app1-after.bin --addr 0x190000 --size 0x2800`
   then `sha256sum` it against the payload. They must match exactly.
5. Confirm the running firmware is unharmed:
   `orch serial monitor --baud 9600 --seconds 40`
   You should eventually see `### TinyGSM Version:` / `### Modem: Quectel
   BG95-M5` lines. Give it time — see the antenna note above.
6. Clean up the two `.bin` files you created when you are done, and report
   what you observed.

### Rules you must follow

- **Never write to these.** They hold the running system:
  - `0x10000` `app0` — the firmware currently executing
  - `0xe000` `otadata` — decides which slot boots; changing it could make the
    board boot your 10 KB test blob and brick it until reflashed
  - `0x310000`–`0x400000` `spiffs` — WiFi credentials and device config. Note
    the project writes a config image at `0x3F0000` that runs to `0x400000`,
    so the apparent "unclaimed tail" past `0x3FB500` is **not** free space.
  - `0x1000` `bootloader`
- **Max baud is 230400.** 460800 fails on this adapter — verified failing on
  the host too, so it is the CH340/board ceiling, not the bridge. The project's
  old `iaq_partitions_commands.txt` says 460800, but that was a different
  adapter; ignore it.
- **Resets are rate-limited to 60 s and that is deliberate.** Each reset
  restarts the modem's attach sequence, and repeated failures put it into
  multi-minute 3GPP backoff. `flash` and `backup` each spend one reset. A `429`
  with `retry_after_s` is the service protecting the hardware: wait it out, do
  **not** retry in a loop, and do **not** reach for `--force`. The sequence
  above needs three resets, so expect to wait ~2 minutes in total.
- **Firmware paths must be absolute and under `/Users/joshuaodmark/Apps`.**
  That tree is mounted at the same absolute path on the host, which is why no
  file copying is needed. Paths outside it are refused.
- If `orch serial` cannot reach the service, stop and say so. The fix is
  `orch serial install` on the macOS **host**, which you cannot do from here.

### Done looks like

The read-back sha256 equals the payload sha256, and the device still prints
TinyGSM/modem lines at 9600. Report the measured throughput from the `flash`
output, and anything that behaved differently from this brief.

## (copy to here)

---

## Why these addresses

Read off the live device rather than assumed from `iaq_app.csv`:

| label | type | offset | size | end |
|---|---|---|---|---|
| nvs | data/nvs | 0x9000 | 20 KB | 0xe000 |
| otadata | data/ota | 0xe000 | 8 KB | 0x10000 |
| app0 | app/ota_0 | 0x10000 | 1.5 MB | 0x190000 |
| app1 | app/ota_1 | 0x190000 | 1.5 MB | 0x310000 |
| spiffs | data/spiffs | 0x310000 | 941 KB | 0x3fb500 |

`otadata` entry 0 is valid with `ota_seq = 1`, so the active slot is
`(1 - 1) % 2 = 0` → **app0 runs; app1 is free**. The CRC in otadata is
`crc32(seq_bytes, 0xFFFFFFFF)` — *not* plain zlib `crc32`, which reports every
entry as corrupt and leads you to the right answer for the wrong reason.

Writing `app1` is safe precisely because `otadata` is left alone: the
bootloader keeps selecting app0, and never validates or executes the test
blob. It is also a realistic rehearsal, since a real OTA writes the same slot.

The only flash region no partition claims is `0x3fb500`–`0x400000` (18 KB),
and it is **not** usable as scratch: `iaq_partitions_commands.txt` writes a
SPIFFS config image at `0x3F0000` sized `0x10000`, which runs to `0x400000`
and covers that tail.
