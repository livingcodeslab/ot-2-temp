# ot-2-temp — control an Opentrons Temperature Module from the command line

A small, dependency-light Python driver + CLI for the **Opentrons Temperature
Module** (a.k.a. "TempDeck", GEN1/GEN2) connected directly to a Linux machine
over USB — no robot required.

It speaks the module's native G-code-over-serial protocol directly, so it does
**not** need the large `opentrons` Python package. The only dependency is
`pyserial`.

---

## TL;DR — what to type

```bash
cd ~/ot-2-temp

./set-temp 30          # set 30 °C and wait until the plate gets there
./set-temp temp        # read current temperature
./set-temp off         # turn the heater off
```

**Verified working:** module `TDV21P20220707D12` (TempDeck v2.1, firmware
v2.1.0). The heater is currently **OFF** and the plate is cooling passively
(no active cooling) back toward ambient; it read 46.4 °C and falling at the end
of the last session.

Repository: <https://github.com/livingcodeslab/ot-2-temp> (private).

---

## One-time setup (permissions)

The module appears as `/dev/ttyACM0` owned by `root:dialout`. If you are not in
the `dialout` group, opening the port fails with `Permission denied`.

Install the bundled udev rule (recommended — no logout needed):

```bash
sudo ./scripts/install-udev-rule.sh
```

That installs `/etc/udev/rules.d/99-opentrons-tempdeck.rules`, which:

* grants the logged-in user access via the systemd-logind `uaccess` ACL, and
* creates the stable symlink **`/dev/opentrons_tempdeck`** (use it with `-p`).

If it does not take effect immediately, unplug/replug the module or log out and
back in.

Alternative (classic, requires a fresh login session):

```bash
sudo usermod -aG dialout "$USER"
```

## Requirements

* Linux with a USB port (tested on Ubuntu/Debian, Python 3.14).
* `pyserial` — already present system-wide as `python3-serial`. To install it
  manually if missing: `sudo apt install python3-serial` (or `pip install
  -r requirements.txt` inside a venv).

No virtualenv is used in this repo; the scripts run against the system Python.

---

## Usage

```
python3 -m tempdeck [-p PORT] [-t TIMEOUT] [-v] <command> [args]
```

`./set-temp ...` is a thin wrapper for the same thing, and a bare number is
short for `set`, so `./set-temp 30` == `python3 -m tempdeck set 30`.

| Command | What it does |
|---|---|
| `info` | print model, serial number, firmware version |
| `temp` | print `current C (target: ...)` |
| `set <C>` | set target temperature, then wait for it to arrive |
| `wait <C>` | block until the plate reaches `<C>` |
| `off` | stop heating (`M18`) |
| `monitor` | print readings every `--interval` seconds until Ctrl-C |

Useful flags:

* `-p/--port` — override auto-detection (e.g. `-p /dev/opentrons_tempdeck`).
* `-v/--verbose` — show which port was chosen and debug driver logging.
* `set --no-wait` — set the target and return immediately.
* `set --tolerance 0.5` — how close counts as "arrived" (default 1 °C).
* `set --wait-timeout 600` — give up waiting after N seconds (heater keeps
  running; use `off` to stop it).

### Examples

```console
$ ./set-temp set 30                  # real captured output
Target set to 30 C.
22.1 C  (target: 30 C)
Waiting for the plate to reach target...
  22.1 C
  22.0 C
  ...
  28.8 C
  29.1 C
  29.4 C
  29.6 C
Reached 29.6 C (target 30 C).

$ ./set-temp temp                    # ~20 s later
30.0 C  (target: 30 C)

$ ./set-temp off                     # turn it off when done
Heater off.
```

### Using it as a library

```python
from tempdeck import TempDeck

with TempDeck() as deck:          # auto-detects the port
    print(deck.get_device_info())
    deck.set_temperature(30)
    current, target = deck.get_temperature()   # (30.0, 30.0)
    deck.wait_for_temperature(30, tolerance=0.5, timeout=300)
```

---

## Safety / notes

* The module can only **heat**, and only in the range **4–95 °C**; below
  ambient it will simply stall. Requests outside that range are rejected
  client-side.
* The heater keeps running after the script exits. Always finish with
  `./set-temp off` (or `deactivate()`), and do not leave the module unattended
  at high temperature.
* `set` sends whole degrees (`M104 S30`), matching Opentrons' own driver, whose
  temp-deck rounding precision is 0 decimals.
* Only one process can hold the serial port at a time. If you get
  `Permission denied`/`Device busy`, check for a leftover `monitor` process or
  a running Opentrons app.

## Tests

Determined ramp behaviour, for planning runs (heating only, no cooling):

| From → to | Time |
|---|---|
| 22 → 30 °C | ~20 s |
| 30 → 50 °C | ~55 s |
| 50 → 46 °C (heater off) | ~45 s |

Overshoot is small (~0.5 °C); the module settles within ~1 °C of target. After
`off` it cools only passively, so it can take a long time to return to ambient.

No hardware needed for the test suite; the tests drive an in-memory fake of the
firmware:

```bash
python3 -m unittest discover -s tests -v     # 10 tests
```

They cover command framing (`M104 S30\r\n\r\n`), ack parsing (both `ok\r\n`
variants), `T:none` handling, integer rounding, device-info parsing, range
rejection and the wait/timeout logic.

## Layout

```
tempdeck/driver.py      serial driver: framing, ack parsing, port discovery
tempdeck/cli.py         argparse CLI (info/temp/set/wait/off/monitor)
tempdeck/__main__.py    enables `python3 -m tempdeck`
set-temp                convenience wrapper (`./set-temp 30`)
tests/test_driver.py    protocol tests against a fake serial port
scripts/99-opentrons-tempdeck.rules   udev rule (uaccess ACL + stable symlink)
scripts/install-udev-rule.sh          installs the rule (needs sudo)
docs/PROTOCOL.md        the wire protocol, for reference
```

## Session log

* **2025-11-11** — Module detected on USB (`04d8:ee93`, "Microchip Technology,
  Inc. Opentrons TempDeck") enumerating as `/dev/ttyACM0`
  (`/dev/serial/by-id/usb-Microchip_Technology_Inc_Opentrons_TempDeck-if00`).
  Driver + CLI + tests written; 10/10 protocol tests pass, port auto-detection
  resolves to `/dev/ttyACM0`.
* **Permissions** — host user was not in `dialout`. First udev rule relied on
  `TAG+="uaccess"`, which silently does nothing over SSH (no logind seat), so the
  symlink appeared but access was still denied. Fixed by adding an explicit
  `setfacl` rule for the user (`99-opentrons-tempdeck-acl.rules`) — see
  "One-time setup".
* **Device identified** — model `temp_deck_v21`, firmware `v2.1.0`, serial
  `TDV21P20220707D12`; idle at 22.1 °C with the heater off.
* **Target set to 30 °C** — `./set-temp set 30 --tolerance 0.5`. Ramped
  22.1 → 29.6 °C in ~20 s and settled at 30.0–30.2 °C. Raw reply confirmed:
  `T:30.000 C:30.309`.
* **Target raised to 50 °C** — `./set-temp set 50 --tolerance 0.5`. Ramped
  30 → 49.6 °C and settled at 50.4–50.5 °C.
* **Heater turned off** — `./set-temp off` (`M18`). Readback confirms
  `T:none C:50.419`, i.e. no target. Passively cooled 50.4 → 46.4 °C in ~45 s.
* **Published** — pushed to `livingcodeslab/ot-2-temp` (private) on GitHub;
  `origin` is `git@github.com:livingcodeslab/ot-2-temp.git`, branch `main`.

## Picking this up again

1. `cd ~/ot-2-temp`
2. `lsusb | grep -i 04d8:ee93` — confirm the module is plugged in.
3. `./set-temp info` — confirm you can talk to it.
4. `./set-temp 30` — do the thing.
5. `./set-temp off` — when finished.

If step 3 fails with `Permission denied`, re-run
`sudo ./scripts/install-udev-rule.sh` and replug the module. Verify the ACL with
`getfacl -p /dev/ttyACM0` — you should see a `user:<you>:rw-` line.

Remember the heater is left at whatever `set` last requested; `./set-temp temp`
shows `(target: ...)` so you can tell whether it is currently heating.
