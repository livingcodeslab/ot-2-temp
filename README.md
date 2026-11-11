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
| `plan <profile>` | dry run: print a protocol's timeline (works unplugged) |
| `run <profile>` | execute a multi-step protocol from a JSON file |

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

* The module is a **thermoelectric (Peltier) device**: it both heats and cools,
  actively holding 4–95 °C, including temperatures *below* ambient (it reached
  19.7 °C and falling from a 50 °C start while ambient was ~22 °C).
  Requests outside 4–95 °C are rejected client-side.
* Because it can go below ambient, the plate can form **condensation** when held
  cold. Keep it at or above ambient if that matters for your samples.
* Cooling and heating happen at full power toward whatever target you set — the
  firmware has no rate control. For a controlled ramp rate you must move the
  target along a trajectory yourself; see "Temperature protocols" below.
* The heater keeps running after the script exits. Always finish with
  `./set-temp off` (or `deactivate()`), and do not leave the module unattended
  at high temperature.
* `set` sends whole degrees (`M104 S30`), matching Opentrons' own driver, whose
  temp-deck rounding precision is 0 decimals.
* Only one process can hold the serial port at a time. If you get
  `Permission denied`/`Device busy`, check for a leftover `monitor` process or
  a running Opentrons app.

## Temperature protocols

Multi-step protocols (heat, hold, rate-limited ramps) live in JSON profiles and
are executed by `run`. The important constraint:

> **The firmware has no rate control.** `M104 S<x>` means "go to x" and the
> onboard PID drives the Peltier at full power to get there. It also accepts
> **whole degrees only**. So a "1 °C/min" ramp has to be produced by the host,
> by walking the target along a time trajectory:
>
> ```
> setpoint(t) = start + rate * (t - t0)      (clamped to the target)
> ```

That is what `tempdeck/profile.py` does: every poll it evaluates the trajectory,
rounds to an integer, and sends `M104` only when that integer changes. It is
time-based rather than "wait for the step, then dwell" on purpose — a loaded
plate may never reach a step, and a wait-based loop stalls silently.

### Profiles

```json
{
  "name": "heat-90-then-1cpm-cool-to-37-hold",
  "steps": [
    {"ramp": 90},
    {"hold": 90, "duration": "5m"},
    {"ramp": 37, "rate": 1.0},
    {"hold": 37, "duration": "10m"}
  ]
}
```

* `{"ramp": X}` — go to X °C as fast as the module can.
* `{"ramp": X, "rate": R}` — reach X °C at R °C/min. Optional per-step
  `"tolerance"` (default 0.5 °C).
* `{"hold": X, "duration": "10m"}` — sit at X °C; durations accept `30s`,
  `5m`, `1h30m` or a plain number of seconds.

The example above is a real file (`profiles/90-to-37-1cpm.json`). Note the
time budget: **90 → 37 °C at 1 °C/min is 53 minutes**, most of which is the ramp.

### Running

```bash
./set-temp plan profiles/90-to-37-1cpm.json --start-temp 22   # dry run, no hardware
./set-temp run  profiles/90-to-37-1cpm.json --log logs/run.csv --off-on-exit
```

`plan` prints the timeline without touching the module (it appears in the
estimate column: heating ≈20 °C/min, cooling ≈6 °C/min, measured — see below).
`run` asks for confirmation (`--yes` skips it), logs every sample to CSV
(`elapsed_s,phase,actual_c,setpoint_c`), and prints the achieved rate plus a
revised ETA as it goes. If the plate falls behind the requested rate by
`--lag-warn` (default 3 °C) it says so **once**, quoting the rate it is actually
achieving — an unachievable rate is loud, not silent.

`--max-lead <C>` is an optional safety valve that caps how far the setpoint may
sit beyond the plate. **It is off by default**, deliberately: enabling a fixed
2 °C clamp once turned a requested 10 °C/min ramp into ~2 °C/min, because on a
ramp the module cannot sustain the setpoint gets pinned just ahead of the plate
and the schedule is thrown away. Leave it off for rate work.

### Interrupting

Ctrl-C (or `SIGTERM`) is caught and reports the setpoint the module was left
regulating to. **The heater keeps running** after a profile ends or is aborted —
`run` leaves it at the final target on purpose (so a protocol can hand off to
microscopy or whatever comes next). Use `--off-on-exit`, or `./set-temp off`.

## Measured ramp rates

Measured on this unloaded TempDeck v2.1 at ~22 °C ambient. The module heats and
cools actively; `off` leaves it to drift passively:

| From → to | Time | Rate |
|---|---|---|
| 22 → 30 °C (heating) | ~20 s | ~22 °C/min |
| 30 → 50 °C (heating) | ~55 s | ~22 °C/min |
| 28 → 6.5 °C (active cooling) | ~4 min | ~5.5 °C/min avg |
| 50 → 46 °C (heater off, passive) | ~45 s | — |

Overshoot is small (~0.5 °C); the module settles within ~1 °C of target. Cooling
slows as it approaches the target (and the further below ambient, the harder it
is), so treat the cooling figure as a best case. After `off` it drifts back to
ambient slowly — set an explicit lower target instead if you want it to hurry.

## Tests

No hardware needed; the tests drive an in-memory fake of the firmware:

```bash
python3 -m unittest discover -s tests -v     # 30 tests
```

`tests/test_driver.py` covers command framing (`M104 S30\r\n\r\n`), ack parsing
(both `ok\r\n` variants), `T:none` handling, integer rounding, device-info
parsing, range rejection and the wait/timeout logic. `tests/test_profile.py`
covers duration parsing, the ramp trajectory, profile validation, the
`max_lead` regression (the clamp bug above), achieved-rate/ETA maths, and a
full run + CSV log against a scripted fake deck.

## Layout

```
tempdeck/driver.py      serial driver: framing, ack parsing, port discovery
tempdeck/profile.py     multi-step protocols: trajectory ramps, holds, CSV logs
tempdeck/cli.py         argparse CLI (info/temp/set/wait/off/monitor/plan/run)
tempdeck/__main__.py    enables `python3 -m tempdeck`
set-temp                convenience wrapper (`./set-temp 30`)
profiles/*.json         example protocols (90→37 at 1 °C/min, cool to 4 °C, ...)
logs/*.csv              run logs written by `run --log`
tests/test_driver.py    protocol tests against a fake serial port
tests/test_profile.py   protocol/ramp tests (incl. the max_lead regression)
scripts/99-opentrons-tempdeck.rules   udev rule (uaccess ACL + stable symlink)
scripts/install-udev-rule.sh          installs rules (needs sudo)
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
* **Correction — it is a Peltier, it *does* cool.** Earlier notes in this repo
  claimed the module was "heating only". That was wrong: Opentrons document it
  as a "fully automated thermoelectric module", 4–95 °C, and it visibly cooled
  from 50 °C down through ambient. Docs and code comments corrected.
* **Target 4 °C** — cooling from 44 → 24 °C in 2 min, then to 13 °C, then a
  restarted run reached **6.5 °C in 4 min** from 28.4 °C before being
  deliberately aborted. It never reached 4 °C simply because the target kept
  being overwritten by other experiments, not for any hardware reason.
* **Two bugs found from that session's CSV log** (`logs/smoke.csv`), both fixed:
  1. `max_lead` defaulted to 2 °C, so a ramp the module could not sustain got
     its setpoint pinned just above the plate and a requested **10 °C/min ran at
     ~2 °C/min** — silently. Now off by default, with the achieved rate and a
     revised ETA reported instead.
  2. Killing a run leaves the module regulating to the last *intermediate*
     setpoint (e.g. 24 °C, not the 30 °C it was ramping to), which looks like
     "it stopped". Aborts now say exactly what setpoint was left behind.
  Verified meanwhile that opening the port does **not** reset the module: the
  4 °C target survived ~15 separate `./set-temp temp` invocations.
* **Protocol runner added** — `tempdeck/profile.py` + `plan`/`run` CLI commands,
  JSON profiles in `profiles/`, CSV run logs in `logs/`. 30 tests pass.
* **Module unplugged** at the end of the session, heater off (`T:none`).

## Picking this up again

1. `cd ~/ot-2-temp`
2. `lsusb | grep -i 04d8:ee93` — confirm the module is plugged in.
3. `./set-temp info` — confirm you can talk to it.
4. `./set-temp 30` — do the thing.
5. `./set-temp off` — when finished.

Multi-step work: `./set-temp plan profiles/<x>.json --start-temp 22` to see the
timeline (works with the module unplugged), then
`./set-temp run profiles/<x>.json --log logs/run.csv` to execute it.

If step 3 fails with `Permission denied`, re-run
`sudo ./scripts/install-udev-rule.sh` and replug the module. Verify the ACL with
`getfacl -p /dev/ttyACM0` — you should see a `user:<you>:rw-` line.

Remember the heater is left at whatever `set` last requested; `./set-temp temp`
shows `(target: ...)` so you can tell whether it is currently heating.
