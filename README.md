# ot-2-temp — Opentrons Temperature Module control over USB

**Which product:** the **Opentrons Temperature Module** (Opentrons calls it the
"TempDeck"; GEN1 and GEN2) — the aluminium-plate module that actively heats and
cools 4–95 °C. USB ID `04d8:ee93` ("Microchip Technology, Inc. Opentrons
TempDeck"), which enumerates as `/dev/ttyACM0`.

**What this is:** a small Python driver + CLI that talks to that module
**directly over USB from a Linux host**. No OT-2 or Flex robot, no Opentrons app,
no Robot server, and no `opentrons` Python package — the only dependency is
`pyserial`. (Despite the repo name, **the robot is not involved at all**; the
module is plugged straight into the computer holding this repo.)

**What this is not:** not the Thermocycler, not the Heater-Shaker, and not the
OT-2's onboard temperature control. Those speak different protocols.

```
module ──USB──> /dev/ttyACM0 ──> tempdeck/driver.py ──> ./set-temp (CLI)
                                                        └─> from tempdeck import TempDeck
```

---

## Interface at a glance

Three ways in, in increasing order of complexity.

### 1. One-shot commands

```bash
./set-temp 30            # set 30 °C and wait for the plate to get there
./set-temp temp          # read: "30.1 C  (target: 30 C)"
./set-temp off           # stop heating/cooling (M18)
```

| Command | What it does |
|---|---|
| `info` | print model, serial number, firmware version |
| `temp` | print current temperature and target |
| `set <C>` | set target temperature, then wait until it arrives |
| `wait <C>` | block until the plate reaches `<C>` |
| `off` | disengage the element (`M18`) |
| `monitor` | print readings every `--interval` seconds until Ctrl-C |
| `plan <profile>` | dry run of a protocol timeline (**works unplugged**) |
| `run <profile>` | execute a multi-step protocol from a JSON file |

`./set-temp ...` is a thin wrapper around `python3 -m tempdeck ...`, and a bare
number is shorthand for `set` (`./set-temp 30` == `./set-temp set 30`).

Global flags: `-p/--port` (override auto-detect, e.g. `/dev/opentrons_tempdeck`),
`-t/--timeout`, `-v/--verbose`.
Per-command: `set --no-wait --tolerance 0.5 --wait-timeout 600`,
`run --poll --tolerance --log --off-on-exit --yes --max-lead --lag-warn`,
`monitor --interval`.

### 2. Multi-step protocols (heat / hold / rate-limited ramps)

```bash
./set-temp plan profiles/90-to-37-1cpm.json --start-temp 22    # dry run
./set-temp run  profiles/90-to-37-1cpm.json --log logs/run.csv
```

```json
{"name": "heat-90-then-1cpm-to-37",
 "steps": [
   {"ramp": 90},
   {"hold": 90, "duration": "5m"},
   {"ramp": 37, "rate": 1.0},
   {"hold": 37, "duration": "10m"}
 ]}
```

Full details in [Temperature protocols](#temperature-protocols).

### 3. Python API

```python
from tempdeck import TempDeck, find_port, TempDeckError

with TempDeck() as deck:                      # auto-detects the port
    deck.get_device_info()                    # {'model': 'temp_deck_v21', ...}
    deck.set_temperature(30)
    current, target = deck.get_temperature()  # (30.0, 30.0); target is None if off
    deck.wait_for_temperature(30, tolerance=0.5, timeout=300)
    deck.deactivate()

find_port()          # '/dev/ttyACM0' -- raises TempDeckError if absent
TempDeck(port='/dev/opentrons_tempdeck', timeout=1.0)
```

Module-level helpers: `TempDeck`, `TempDeckError`, `find_port`, `MIN_TEMP` (4),
`MAX_TEMP` (95). For protocols: `tempdeck.profile.Profile`, `run_profile`.

---

## Status (end of last session, 2025-11-11)

* Device: model `temp_deck_v21`, firmware `v2.1.0`, serial `TDV21P20220707D12`.
* Heater **off** (`T:none`, confirmed by query) and the **module was then
  unplugged** — nothing is running and nothing is holding the serial port.
* Everything below was verified on this unit: 30 °C, 50 °C, cooling to 6.5 °C,
  and 30 automated tests pass.
* Repo: <https://github.com/livingcodeslab/ot-2-temp> (public).

## Requirements

| Need | Detail |
|---|---|
| OS | Linux (developed on Ubuntu, kernel devtmpfs + udev + systemd). Untested on macOS/Windows. |
| Python | 3.8+ (developed and tested on **3.14.4**). No third-party packages beyond pyserial. |
| pyserial | **≥3.5.** On this machine it is already installed system-wide: `python3-serial 3.5-2build1`. |
| Permissions | Read/write on `/dev/ttyACM0` — see [Setup](#setup-permissions-one-time). |
| Cable | A **USB data** cable. Charge-only cables never appear in `lsusb`. |

Verify the dependency in one line:

```bash
python3 -c "import serial; print(serial.__version__)"   # expect 3.5
```

**There is no pip and no venv on this machine** (`python3 -m pip` → *No module
named pip*; `python3 -m venv` fails with *ensurepip is not available*). Do not
follow generic "make a venv" advice here. Use the distro package:

```bash
sudo apt install python3-serial      # already installed, listed for completeness
```

Only if you specifically want an isolated environment would you need to install
the tooling first: `sudo apt install python3-pip python3-venv`.

`requirements.txt` exists for portable setups and just pins `pyserial>=3.5`.

## Setup: permissions (one time)

**Plug the module in first**, then run the installer. It creates two udev rules,
reloads them, and applies the ACL to the device that is already present:

```bash
lsusb | grep -i 04d8:ee93            # confirm the module is attached
sudo ./scripts/install-udev-rule.sh
```

| Rule | Purpose |
|---|---|
| `99-opentrons-tempdeck.rules` | stable symlink `/dev/opentrons_tempdeck`, `dialout` group, `TAG+="uaccess"` |
| `99-opentrons-tempdeck-acl.rules` | explicit `setfacl` for **your** user — the one that actually grants access here |

**Why two:** `TAG+="uaccess"` only grants an ACL to users with an active *local
seat* session. Over SSH there is no seat, so `uaccess` silently does nothing —
the symlink appears but access stays denied. The `setfacl` rule is
session-agnostic and fixes that. The installer bakes in your username
(`$SUDO_USER`) and resolves the real `setfacl` path for the udev `RUN+=` line.

Requirements for the installer: root, the `acl` package (`setfacl`), and — for
the username — running it via `sudo` rather than from a root shell.

Confirm it worked:

```bash
getfacl -p /dev/ttyACM0        # want a line: user:<you>:rw-
./set-temp info                # want: model/serial/version, no Permission denied
```

No replug is needed *if* the module was plugged in during install; if it was not,
the ACL is applied on next plug-in (the installer tells you when this is the
case).

Classic alternative, if you would rather not use udev rules — requires a full
logout/login so the new group takes effect:

```bash
sudo usermod -aG dialout "$USER"
```

---

## Examples

```console
$ ./set-temp set 30                  # real captured output
Target set to 30 C.
22.1 C  (target: 30 C)
Waiting for the plate to reach target...
  22.1 C
  22.0 C
  ...
  29.4 C
  29.6 C
Reached 29.6 C (target 30 C).

$ ./set-temp temp                    # ~20 s later
30.0 C  (target: 30 C)

$ ./set-temp off                     # turn it off when done
Heater off.
```

## Safety / notes

* The module is a **thermoelectric (Peltier) device**: it both heats and cools,
  actively holding 4–95 °C, including temperatures *below* ambient (it was
  observed cooling from 50 °C down through ambient to 6.5 °C). Requests outside
  4–95 °C are rejected client-side.
* Because it can go below ambient, the plate can form **condensation** when held
  cold. Keep it at or above ambient if that matters for your samples.
* The **heater keeps running** after any script exits or is aborted. Finish with
  `./set-temp off` (or `deactivate()`), and do not leave the module unattended at
  high temperature.
* Heating and cooling happen at full power toward whatever target you set — the
  firmware has no rate control. See [Temperature protocols](#temperature-protocols).
* `set` sends whole degrees (`M104 S30`), matching Opentrons' own driver, whose
  temp-deck rounding precision is 0 decimals.
* Only one process can hold the serial port. If you get `Permission denied` or
  `Device busy`, look for a leftover `monitor` process or a running Opentrons app.

## Temperature protocols

Multi-step protocols (heat, hold, rate-limited ramps) live in JSON profiles and
are executed by `run`. The constraint that shapes everything:

> **The firmware has no rate control.** `M104 S<x>` means "go to x" and the
> onboard PID drives the Peltier at full power to get there. It also accepts
> **whole degrees only**. So a "1 °C/min" ramp has to be produced by the host, by
> walking the target along a time trajectory:
>
> ```
> setpoint(t) = start + rate * (t - t0)      (clamped to the target)
> ```

`tempdeck/profile.py` does exactly that: every poll it evaluates the trajectory,
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
* `{"hold": X, "duration": "10m"}` — sit at X °C; durations accept `30s`, `5m`,
  `1h30m`, or a plain number of seconds.

Real examples live in `profiles/`. Note the time budget: **90 → 37 °C at
1 °C/min is 53 minutes**, most of it the ramp.

### Running

```bash
./set-temp plan profiles/90-to-37-1cpm.json --start-temp 22   # dry run, no hardware
./set-temp run  profiles/90-to-37-1cpm.json --log logs/run.csv --off-on-exit
```

`plan` prints the timeline without touching the module, estimating with measured
slew rates (heating ≈20 °C/min, cooling ≈6 °C/min — see
[Measured ramp rates](#measured-ramp-rates)).

`run` asks for confirmation (`--yes` skips it), logs every sample to CSV
(`elapsed_s,phase,actual_c,setpoint_c`), and prints the achieved rate plus a
revised ETA as it goes. If the plate falls behind the requested rate by
`--lag-warn` (default 3 °C), it says so **once**, quoting the rate it is actually
achieving — an unachievable rate is loud, not silent.

`--max-lead <C>` is an optional safety valve capping how far the setpoint may sit
beyond the plate. **It is off by default**, deliberately: a fixed 2 °C clamp once
turned a requested 10 °C/min ramp into ~2 °C/min, because on a ramp the module
cannot sustain the setpoint gets pinned just ahead of the plate and the schedule
is thrown away. Leave it off for rate work.

### Interrupting

Ctrl-C (or `SIGTERM`) is caught and reports the setpoint the module was left
regulating to. **The heater keeps running** after a profile ends or is aborted —
`run` leaves it at the final target on purpose, so a protocol can hand off to
microscopy or whatever comes next. Use `--off-on-exit`, or `./set-temp off`.

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
ambient slowly — set an explicit lower target if you want it to hurry.

**Rate tracking accuracy** (measured, 37 → 21.8 °C at 1 °C/min,
`logs/37-to-ambient.csv`): 0.99 °C/min overall, 1.02 °C/min through the steady
part, mean deviation from the scheduled trajectory 0.39 °C (max 1.09 °C). The
first ~3 min are a little slow while the element reverses from heating to
cooling, which is normal.

## Tests

No hardware needed; the tests drive an in-memory fake of the firmware:

```bash
python3 -m unittest discover -s tests -v     # 30 tests
```

`tests/test_driver.py` covers command framing (`M104 S30\r\n\r\n`), ack parsing
(both `ok\r\n` variants), `T:none` handling, integer rounding, device-info
parsing, range rejection, and the wait/timeout logic. `tests/test_profile.py`
covers duration parsing, the ramp trajectory, profile validation, the `max_lead`
regression (the clamp bug above), achieved-rate/ETA maths, and a full run + CSV
log against a scripted fake deck.

## Layout

```
tempdeck/driver.py      serial driver: framing, ack parsing, port discovery
tempdeck/profile.py     multi-step protocols: trajectory ramps, holds, CSV logs
tempdeck/cli.py         argparse CLI (info/temp/set/wait/off/monitor/plan/run)
tempdeck/__main__.py    enables `python3 -m tempdeck`
set-temp                convenience wrapper (`./set-temp 30`)
profiles/*.json         example protocols (90→37 at 1 °C/min, cool to 4 °C, ...)
logs/*.csv              run logs written by `run --log` (two kept as evidence)
tests/test_driver.py    protocol tests against a fake serial port
tests/test_profile.py   protocol/ramp tests (incl. the max_lead regression)
scripts/99-opentrons-tempdeck.rules   udev rule (uaccess ACL + stable symlink)
scripts/99-opentrons-tempdeck-acl.rules  written by the installer for your user
scripts/install-udev-rule.sh          installs rules (needs sudo)
docs/PROTOCOL.md        the wire protocol, for reference
```

## Session log

* **2025-11-11** — Module detected on USB (`04d8:ee93`, "Microchip Technology,
  Inc. Opentrons TempDeck") enumerating as `/dev/ttyACM0`
  (`/dev/serial/by-id/usb-Microchip_Technology_Inc_Opentrons_TempDeck-if00`).
  Driver + CLI + tests written; port auto-detection resolves to `/dev/ttyACM0`.
* **Permissions** — host user was not in `dialout`. First udev rule relied on
  `TAG+="uaccess"`, which silently does nothing over SSH (no logind seat), so the
  symlink appeared but access was still denied. Fixed by adding an explicit
  `setfacl` rule for the user — see [Setup](#setup-permissions-one-time).
* **Device identified** — model `temp_deck_v21`, firmware `v2.1.0`, serial
  `TDV21P20220707D12`; idle at 22.1 °C with the heater off.
* **Target set to 30 °C** — `./set-temp set 30 --tolerance 0.5`. Ramped
  22.1 → 29.6 °C in ~20 s and settled at 30.0–30.2 °C. Raw reply confirmed:
  `T:30.000 C:30.309`.
* **Target raised to 50 °C** — `./set-temp set 50 --tolerance 0.5`. Ramped
  30 → 49.6 °C and settled at 50.4–50.5 °C.
* **Heater turned off** — `./set-temp off` (`M18`). Readback confirms
  `T:none C:50.419`, i.e. no target. Drifted 50.4 → 46.4 °C in ~45 s.
* **Published** — pushed to `livingcodeslab/ot-2-temp` on GitHub
  `origin` is `git@github.com:livingcodeslab/ot-2-temp.git`, branch `main`.
* **Correction — it is a Peltier, it *does* cool.** Earlier notes in this repo
  claimed the module was "heating only". That was wrong: Opentrons document it as
  a "fully automated thermoelectric module", 4–95 °C, and it visibly cooled from
  50 °C down through ambient. Docs and code comments corrected.
* **Target 4 °C** — cooled 44 → 24 °C in 2 min, then to 13 °C; a restarted run
  reached **6.5 °C in 4 min** from 28.4 °C before being deliberately aborted. It
  never reached 4 °C simply because the target kept being overwritten by other
  experiments — not for any hardware reason.
* **Two bugs found from that session's CSV log** (`logs/smoke.csv`), both fixed:
  1. `max_lead` defaulted to 2 °C, so a ramp the module could not sustain got its
     setpoint pinned just above the plate and a requested **10 °C/min ran at
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
* **Protocol run verified on hardware (37 → ambient at 1 °C/min).** Replugged and
  ran `profiles/37-to-ambient-1cpm.json`: 21.8 → 37 °C in ~35 s (max rate), then
  a 1 °C/min descent to ambient, 15m06s total (`logs/37-to-ambient.csv`).
  Measured from the CSV: **0.99 °C/min overall vs 1.00 requested**, 1.02 °C/min
  over the steady 11.5 min, mean deviation from schedule 0.39 °C, max 1.09 °C,
  17 integer setpoints (37→22). So the `max_lead` fix works: the rate is now
  actually delivered, not silently throttled.
* **Bug: intermittent `Unexpected M105 response: ''`.** `./set-temp set 37`
  printed "Target set to 37 C." and then errored, because a payload-less command
  (`M104`) acks `ok\r\nok\r\n` and a duplicate of that ack could still be in the
  kernel buffer when the next `M105` was written — `reset_input_buffer()` races
  with it, so the parser returned an empty payload as if it were an answer.
  Fixed by skipping a leading empty ack when a payload is expected
  (`expect_payload`), with regression tests; verified on hardware with 10
  back-to-back set-then-query cycles, 10/10 clean.
* **Holding at 37 °C** at the end of the session (`target: 37`), readback ~37.4 °C.

## Picking this up again

1. Plug the module in, then `lsusb | grep -i 04d8:ee93` to confirm it appeared.
2. `cd ~/ot-2-temp && ./set-temp info` — confirms comms and prints firmware.
3. `./set-temp temp` — see where the plate is and whether a target is set.
4. `./set-temp 30` — do the thing.
5. `./set-temp off` — when finished.

Multi-step work: `./set-temp plan profiles/<x>.json --start-temp 22` to see the
timeline (**works with the module unplugged**), then
`./set-temp run profiles/<x>.json --log logs/run.csv` to execute it.

If step 2 fails with `Permission denied`, re-run
`sudo ./scripts/install-udev-rule.sh` and replug. If it fails with "No Opentrons
temperature module found", the USB cable is missing/unplugged or is charge-only.

Remember the heater is left at whatever `set`/`run` last requested — `./set-temp
temp` shows `(target: ...)` so you can tell whether it is currently regulating.
