"""Minimal serial driver for the Opentrons Temperature Module ("TempDeck").

This is a dependency-light reimplementation of the subset of Opentrons' own
temp-deck driver that we need: set a target temperature, read it back, and turn
the heater off.  It does not require the (large) ``opentrons`` package, only
``pyserial``.

Protocol summary -- full details in docs/PROTOCOL.md:

  * USB CDC-ACM serial device (``04d8:ee93``), 115200 8N1.
  * Every command is an ASCII G-code line terminated by ``"\\r\\n\\r\\n"``.
  * The firmware answers with an optional payload followed by the ack
    ``"ok\\r\\nok\\r\\n"`` (some firmware builds ack with a single ``ok\\r\\n``).

    M115          -> "serial:... model:... version:..."
    M105          -> "T:none C:23"   (T = target, C = current; "none" = off)
    M104 S<temp>  -> set target temperature, integer degrees Celsius
    M18           -> disengage (stop heating / turn off)
    M114          -> reset reason (diagnostic, may be unsupported)
"""

from __future__ import annotations

import glob
import logging
import os
import re
import time
from typing import Dict, Optional, Tuple

import serial

log = logging.getLogger(__name__)

# --- protocol constants (mirrors opentrons/drivers/temp_deck/driver.py) ---
BAUDRATE = 115200
COMMAND_TERMINATOR = "\r\n\r\n"
ACK = b"ok\r\nok\r\n"

# Opentrons Temperature Module USB IDs
USB_VID = 0x04D8  # Microchip Technology, Inc.
USB_PID = 0xEE93  # Opentrons TempDeck

# Hardware limits (degrees Celsius).  The Temperature Module is a thermoelectric
# (Peltier) device: it actively HEATS and COOLS over 4..95 C, so it can hold
# temperatures below ambient.  Driving it below ambient causes condensation.
MIN_TEMP = 4.0
MAX_TEMP = 95.0

DEFAULT_TIMEOUT = 1.0
DEFAULT_RETRIES = 3

# "T:none C:23"  /  "T:30 C:23"
_TEMP_RE = re.compile(r"T:(?P<target>none|[-0-9.]+)\s+C:(?P<current>[-0-9.]+)", re.I)
_KEY_VALUE_RE = re.compile(r"(?P<key>\S+):(?P<value>\S+)")


class TempDeckError(Exception):
    """Raised when the module cannot be reached or returns something odd."""


# --------------------------------------------------------------------------
# port discovery
# --------------------------------------------------------------------------
def _usb_ids_for_tty(tty: str) -> Optional[Tuple[int, int]]:
    """Return (idVendor, idProduct) for a tty by walking its sysfs parents."""
    node = os.path.realpath(f"/sys/class/tty/{os.path.basename(tty)}/device")
    for _ in range(6):
        if not node or node == "/":
            break
        vid_file = os.path.join(node, "idVendor")
        pid_file = os.path.join(node, "idProduct")
        if os.path.exists(vid_file) and os.path.exists(pid_file):
            try:
                with open(vid_file) as f:
                    vid = int(f.read().strip(), 16)
                with open(pid_file) as f:
                    pid = int(f.read().strip(), 16)
                return vid, pid
            except (OSError, ValueError):
                return None
        node = os.path.dirname(node)
    return None


def find_port() -> str:
    """Locate the temp module's serial port.

    Tries ``/dev/serial/by-id`` first (stable names that survive replugging),
    then falls back to matching USB vendor/product IDs on ``/dev/ttyACM*``.
    """
    for path in sorted(glob.glob("/dev/serial/by-id/*")):
        if "tempdeck" in os.path.basename(path).lower():
            return os.path.realpath(path)

    for tty in sorted(glob.glob("/dev/ttyACM*")):
        if _usb_ids_for_tty(tty) == (USB_VID, USB_PID):
            return tty

    raise TempDeckError(
        "No Opentrons temperature module found. Is it plugged in with a "
        "USB *data* cable? Check with: lsusb | grep -i 04d8:ee93"
    )


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
class TempDeck:
    """Blocking serial driver for the Opentrons Temperature Module."""

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = BAUDRATE,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.port = port or find_port()
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial: Optional[serial.Serial] = None

    # -- lifecycle --------------------------------------------------------
    def open(self) -> "TempDeck":
        if self._serial and self._serial.is_open:
            return self
        try:
            self._serial = serial.Serial(
                self.port,
                self.baudrate,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
        except serial.SerialException as exc:
            raise TempDeckError(
                f"Cannot open {self.port}: {exc}. "
                "Permission problem? You must be in the 'dialout' group -- "
                "see scripts/install-udev-rule.sh in the README."
            ) from exc
        # Let the CDC endpoint settle, then drop any stale bytes.
        time.sleep(0.2)
        self._serial.reset_input_buffer()
        return self

    def close(self) -> None:
        if self._serial and self._serial.is_open:
            self._serial.close()
        self._serial = None

    def __enter__(self) -> "TempDeck":
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return bool(self._serial and self._serial.is_open)

    # -- low level --------------------------------------------------------
    def _read_response(self) -> str:
        """Read until the firmware ack arrives, return the payload before it."""
        assert self._serial is not None
        buf = b""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            chunk = self._serial.read(self._serial.in_waiting or 1)
            if chunk:
                buf += chunk
                if ACK in buf:
                    payload, _, _ = buf.partition(ACK)
                    return payload.decode("ascii", "replace").strip()
                # Tolerate firmware that acks with one "ok\r\n".
                if buf.rstrip().endswith(b"ok"):
                    payload = buf.rstrip()
                    for ack in (b"ok", b"ok\r\n", b"ok\r\nok"):
                        if payload.endswith(ack):
                            payload = payload[: -len(ack)]
                            break
                    return payload.decode("ascii", "replace").strip()
        raise TempDeckError(
            f"No response from module at {self.port} (got {buf!r})."
        )

    def _send_command(self, gcode: str, retries: int = DEFAULT_RETRIES) -> str:
        """Send one G-code line and return its payload."""
        if not self.is_open:
            self.open()
        assert self._serial is not None
        command = (gcode + COMMAND_TERMINATOR).encode("ascii")
        last_error: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                self._serial.reset_input_buffer()
                self._serial.write(command)
                self._serial.flush()
                return self._read_response()
            except (TempDeckError, serial.SerialException) as exc:
                last_error = exc
                log.debug("attempt %d/%d for %r failed: %s", attempt, retries, gcode, exc)
                time.sleep(0.2)
        raise TempDeckError(f"Command {gcode!r} failed: {last_error}")

    # -- public API -------------------------------------------------------
    def get_device_info(self) -> Dict[str, str]:
        payload = self._send_command("M115")
        info = {m.group("key"): m.group("value") for m in _KEY_VALUE_RE.finditer(payload)}
        if not info:
            raise TempDeckError(f"Unexpected M115 response: {payload!r}")
        return info

    def get_temperature(self) -> Tuple[float, Optional[float]]:
        """Return ``(current_celsius, target_celsius_or_None)``."""
        payload = self._send_command("M105")
        match = _TEMP_RE.search(payload)
        if not match:
            raise TempDeckError(f"Unexpected M105 response: {payload!r}")
        current = float(match.group("current"))
        raw_target = match.group("target").lower()
        target = None if raw_target == "none" else float(raw_target)
        if target == 0.0:  # firmware reports an idle heater as T:0
            target = None
        return current, target

    def set_temperature(self, celsius: float) -> None:
        """Set the target plate temperature in degrees Celsius."""
        celsius = float(celsius)
        if not MIN_TEMP <= celsius <= MAX_TEMP:
            raise TempDeckError(
                f"{celsius} C is outside the supported range "
                f"({MIN_TEMP:.0f}-{MAX_TEMP:.0f} C)."
            )
        self._send_command(f"M104 S{celsius:.0f}")

    def deactivate(self) -> None:
        """Stop heating and turn the module off."""
        self._send_command("M18")

    def wait_for_temperature(
        self,
        celsius: float,
        tolerance: float = 1.0,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
        on_update=None,
    ) -> float:
        """Block until the plate reaches ``celsius`` +/- ``tolerance``.

        Returns the last measured temperature.  Raises ``TempDeckError`` on
        timeout (the heater is *left running*; call ``deactivate()`` if you
        want it off).
        """
        deadline = time.monotonic() + timeout
        last = float("nan")
        while time.monotonic() < deadline:
            last, _target = self.get_temperature()
            if on_update:
                on_update(last)
            if abs(last - celsius) <= tolerance:
                return last
            time.sleep(poll_interval)
        raise TempDeckError(
            f"Timed out after {timeout:.0f}s waiting for {celsius:.0f} C "
            f"(last reading {last:.1f} C)."
        )
