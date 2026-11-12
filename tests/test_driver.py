"""Protocol tests using an in-memory fake of the TempDeck firmware.

Run with:  python3 -m unittest discover -s tests -v
(no hardware and no extra packages required)
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tempdeck import driver  # noqa: E402


class FakeSerial:
    """Pretends to be the module: consumes G-code, answers like the firmware."""

    is_open = True

    def __init__(self, *args, **kwargs):
        self.written = b""
        self._pending = b""
        self.temperature = 22.0
        self.target = None
        self.acks = b"ok\r\nok\r\n"
        self.stray_ack = b""  # emitted before the real reply, like a late duplicate

    # --- pyserial surface -------------------------------------------------
    @property
    def in_waiting(self):
        return len(self._pending)

    def write(self, data):
        self.written += data
        command = data.decode("ascii").strip()
        self._pending += self._handle(command)
        return len(data)

    def read(self, size=1):
        out, self._pending = self._pending[:size], self._pending[size:]
        return out

    def flush(self):
        pass

    def reset_input_buffer(self):
        self._pending = b""

    def close(self):
        self.is_open = False

    # --- fake firmware ----------------------------------------------------
    def _handle(self, command):
        reply = self._reply(command)
        return self.stray_ack + reply

    def _reply(self, command):
        if command.startswith("M104"):
            self.target = float(command.split("S")[1])
            self.temperature = self.target
            return self.acks
        if command == "M105":
            target = "none" if self.target is None else f"{self.target:.0f}"
            return f"T:{target} C:{self.temperature:.0f}\r\n".encode() + self.acks
        if command == "M115":
            return b"serial:TDV21P20230001 model:temp_deck_v20 version:temp_deck_v1.1\r\n" + self.acks
        if command == "M18":
            self.target = None
            return self.acks
        return b"Unknown command\r\n" + self.acks


class TempDeckTests(unittest.TestCase):
    def setUp(self):
        self._real_serial = driver.serial.Serial
        driver.serial.Serial = FakeSerial
        self.deck = driver.TempDeck(port="/dev/fake0")
        self.deck.open()
        self.fake = self.deck._serial

    def tearDown(self):
        self.deck.close()
        driver.serial.Serial = self._real_serial

    def test_command_framing(self):
        self.deck.set_temperature(30)
        self.assertEqual(self.fake.written, b"M104 S30\r\n\r\n")

    def test_temperature_is_rounded_to_integers(self):
        self.deck.set_temperature(30.7)
        self.assertEqual(self.fake.written, b"M104 S31\r\n\r\n")

    def test_set_and_read_back(self):
        self.deck.set_temperature(30)
        current, target = self.deck.get_temperature()
        self.assertEqual(current, 30.0)
        self.assertEqual(target, 30.0)

    def test_parses_none_target(self):
        self.fake.target = None
        current, target = self.deck.get_temperature()
        self.assertEqual(current, 22.0)
        self.assertIsNone(target)

    def test_device_info(self):
        info = self.deck.get_device_info()
        self.assertEqual(info["model"], "temp_deck_v20")
        self.assertEqual(info["serial"], "TDV21P20230001")
        self.assertEqual(info["version"], "temp_deck_v1.1")

    def test_deactivate(self):
        self.deck.set_temperature(30)
        self.deck.deactivate()
        self.assertIsNone(self.fake.target)

    def test_single_ok_ack_is_tolerated(self):
        self.fake.acks = b"ok\r\n"
        self.deck.set_temperature(30)
        self.assertEqual(self.deck.get_temperature()[1], 30.0)

    def test_leading_duplicate_ack_is_skipped_for_queries(self):
        """An M104-style ack left in the buffer must not be read as an answer.

        Real failure seen on hardware: `set 37` printed "Target set to 37 C."
        then `error: Unexpected M105 response: ''`, because the duplicate
        `ok\r\nok\r\n` from the M104 was still in the buffer and the next M105
        read returned an empty payload.
        """
        self.fake.stray_ack = b"ok\r\nok\r\n"
        current, target = self.deck.get_temperature()
        self.assertEqual(current, 22.0)
        self.assertIsNone(target)

        self.deck.set_temperature(37)
        self.fake.stray_ack = b"ok\r\nok\r\n"
        current, target = self.deck.get_temperature()
        self.assertEqual(target, 37.0)

    def test_device_info_skips_leading_duplicate_ack(self):
        self.fake.stray_ack = b"ok\r\nok\r\n"
        self.assertEqual(self.deck.get_device_info()["model"], "temp_deck_v20")

    def test_deactivate_is_not_confused_by_missing_payload(self):
        # M18 expects no payload, so a bare ack is a success, not an error.
        self.deck.set_temperature(30)
        self.deck.deactivate()
        self.assertIsNone(self.fake.target)

    def test_out_of_range_rejected(self):
        with self.assertRaises(driver.TempDeckError):
            self.deck.set_temperature(150)
        with self.assertRaises(driver.TempDeckError):
            self.deck.set_temperature(1)
        self.assertEqual(self.fake.written, b"")

    def test_wait_for_temperature(self):
        self.deck.set_temperature(30)
        self.assertEqual(self.deck.wait_for_temperature(30, tolerance=1), 30.0)

    def test_wait_for_temperature_timeout(self):
        with self.assertRaises(driver.TempDeckError):
            self.deck.wait_for_temperature(60, tolerance=1, timeout=0.3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
