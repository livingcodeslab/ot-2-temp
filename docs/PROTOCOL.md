# TempDeck wire protocol (as implemented here)

Reference notes gathered from Opentrons' own driver
(`api/src/opentrons/drivers/temp_deck/driver.py` and `drivers/utils.py` at the
`edge` branch), then **verified on the real device** (TempDeck v2.1, firmware
v2.1.0, serial `TDV21P20220707D12`) on 2025-11-11.

## Transport

| Property | Value |
|---|---|
| USB IDs | `04d8:ee93` (Microchip / Opentrons TempDeck) |
| Linux node | `/dev/ttyACM0` (CDC-ACM), by-id `usb-Microchip_Technology_Inc_Opentrons_TempDeck-if00` |
| Baud rate | 115200, 8N1 (CDC-ACM, so baud is nominal) |
| Command terminator | `\r\n\r\n` |
| Ack | `ok\r\nok\r\n` (some builds send a single `ok\r\n`) |

Text is plain ASCII G-code. A response is `[payload] + ack`; the driver reads
until the ack arrives and returns everything before it.

## Commands used

| G-code | Meaning | Observed response (real device) |
|---|---|---|
| `M115` | device info | `serial:TDV21P20220707D12 model:temp_deck_v21 version:v2.1.0\r\nok\r\nok\r\n` |
| `M105` | read temperatures | `T:30.000 C:30.309\r\nok\r\nok\r\n`; `T:none C:22.1...` when idle |
| `M104 S<int>` | set target, integer °C | `ok\r\nok\r\n` (no payload) |
| `M18` | disengage / stop heating | `ok\r\nok\r\n` |
| `M114` | reset reason (diagnostic) | `ok\r\nok\r\n` — no payload on v2.1.0; harmless, ignore |

Notes:

* `M105` reports `T:none` when the heater is idle. Some firmware reports
  `T:0`, which this driver also treats as "off" (`target=None`).
* The **target** is echoed back with 3 decimals (`T:30.000`) and the current
  reading with 3 (`C:30.309`), even though upstream rounds both to 0 decimals
  before handing them to users (`TEMPDECK_GCODE_ROUNDING_PRECISION = 0`).
  This driver returns the raw floats; the CLI formats them.
* `M104` must be given an integer (`S30`, not `S30.5`), matching upstream's
  0-decimal precision.
* A freshly opened port may need a moment to settle; this driver sleeps 200 ms
  and clears the input buffer before the first command.
* Physical range is 4–95 °C, heating only. There is no cooling element.
* The module does not spontaneously report data; it is strictly
  request/response, so polling `M105` is how you track a ramp. Measured ramp
  rate from ambient: ~22 → 29.6 °C in about 20 s.

## Differences from the upstream driver

* Synchronous `pyserial` instead of `asyncio`/`SerialConnection`.
* Ack parsing tolerates both `ok\r\nok\r\n` and `ok\r\n`.
* Simple retry loop (3 attempts) instead of upstream's command queue.
* Port discovery by `/dev/serial/by-id/*tempdeck*` then USB VID/PID via sysfs.
* No firmware flashing (`dfu`), no reset-reason handling.
