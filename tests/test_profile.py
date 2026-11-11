"""Tests for protocol planning/running. No hardware required."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tempdeck import profile as prof  # noqa: E402


class DurationTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(prof.parse_duration(90), 90.0)
        self.assertEqual(prof.parse_duration("90"), 90.0)
        self.assertEqual(prof.parse_duration("45s"), 45.0)
        self.assertEqual(prof.parse_duration("5m"), 300.0)
        self.assertEqual(prof.parse_duration("1h30m"), 5400.0)
        self.assertEqual(prof.parse_duration("2 min"), 120.0)

    def test_bad(self):
        with self.assertRaises(ValueError):
            prof.parse_duration("soon")


class TrajectoryTests(unittest.TestCase):
    def test_cooling_one_degree_per_minute(self):
        # 90 -> 37 at 1 C/min
        self.assertEqual(prof.scheduled_temperature(90, 37, 1.0, 0), 90.0)
        self.assertEqual(prof.scheduled_temperature(90, 37, 1.0, 60), 89.0)
        self.assertEqual(prof.scheduled_temperature(90, 37, 1.0, 600), 80.0)
        self.assertEqual(prof.scheduled_temperature(90, 37, 1.0, 3180), 37.0)

    def test_clamped_at_target(self):
        self.assertEqual(prof.scheduled_temperature(22, 30, 60.0, 600), 30.0)

    def test_heating_direction(self):
        self.assertEqual(prof.scheduled_temperature(22, 90, 30.0, 60), 52.0)

    def test_zero_rate_rejected(self):
        with self.assertRaises(ValueError):
            prof.scheduled_temperature(22, 30, 0, 60)


class ProfileParsingTests(unittest.TestCase):
    def test_example_profile_loads(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "profiles",
            "90-to-37-1cpm.json",
        )
        p = prof.Profile.load(path)
        self.assertEqual(len(p.steps), 4)
        self.assertEqual(p.steps[0].kind, "ramp")
        self.assertIsNone(p.steps[0].rate)  # max rate
        self.assertEqual(p.steps[2].rate, 1.0)
        self.assertEqual(p.steps[3].kind, "hold")
        self.assertEqual(p.steps[3].duration, 600.0)

    def test_out_of_range_target_rejected(self):
        with self.assertRaises(ValueError):
            prof.Profile.from_dict({"steps": [{"ramp": 200}]})

    def test_empty_profile_rejected(self):
        with self.assertRaises(ValueError):
            prof.Profile.from_dict({"steps": []})

    def test_bad_step_rejected(self):
        with self.assertRaises(ValueError):
            prof.Profile.from_dict({"steps": [{"nonsense": 5}]})


class FakeDeck:
    """Instantly arrives at whatever target is set, recording the commands."""

    def __init__(self, start=22.0):
        self.temperature = start
        self.target = None
        self.commands = []

    def get_temperature(self):
        return self.temperature, self.target

    def set_temperature(self, celsius):
        self.target = float(round(celsius))
        self.commands.append(self.target)
        self.temperature = self.target

    def deactivate(self):
        self.target = None


class LeadClampTests(unittest.TestCase):
    """Regression tests for the max_lead bug.

    A fixed 2 C clamp used to be on by default, which silently throttled a
    requested 10 C/min ramp to ~2 C/min: the setpoint got pinned 2 C above the
    plate, so the schedule was thrown away.
    """

    def test_default_follows_trajectory_exactly(self):
        # Plate stuck at 13 C, schedule says 30 C -> must still command 30.
        self.assertEqual(prof.limited_setpoint(30.0, 13.0, 1.0, None), 30.0)

    def test_explicit_clamp_is_applied(self):
        self.assertEqual(prof.limited_setpoint(30.0, 13.0, 1.0, 2.0), 15.0)
        self.assertEqual(prof.limited_setpoint(4.0, 20.0, -1.0, 2.0), 18.0)

    def test_clamp_is_inactive_when_within_lead(self):
        self.assertEqual(prof.limited_setpoint(14.5, 13.0, 1.0, 2.0), 14.5)

    def test_default_max_lead_is_none(self):
        self.assertIsNone(prof.DEFAULT_MAX_LEAD)

    def test_slow_ramp_not_throttled_by_default(self):
        # The exact smoke-test scenario: 13 -> 30 at 10 C/min from a cold plate.
        # schedule at t=30 s should be 18 C, and with no clamp that is what we
        # send, rather than "plate + 2".
        sched = prof.scheduled_temperature(13.0, 30.0, 10.0, 30)
        self.assertAlmostEqual(sched, 18.0)
        self.assertEqual(prof.limited_setpoint(sched, 14.0, 1.0, None), 18.0)


class RateStatusTests(unittest.TestCase):
    def test_achieved_rate_and_eta(self):
        achieved, eta = prof.rate_status(actual=20.0, start_temp=10.0,
                                         elapsed_s=60.0, target=30.0)
        self.assertAlmostEqual(achieved, 10.0)  # 10 C in one minute
        self.assertAlmostEqual(eta, 60.0)  # 10 C left at 10 C/min

    def test_no_movement_gives_unknown_eta(self):
        achieved, eta = prof.rate_status(20.0, 20.0, 60.0, 30.0)
        self.assertEqual(achieved, 0.0)
        self.assertEqual(eta, float("inf"))

    def test_zero_elapsed_is_safe(self):
        achieved, eta = prof.rate_status(20.0, 20.0, 0.0, 30.0)
        self.assertEqual(achieved, 0.0)


class RunTests(unittest.TestCase):
    def test_run_ramp_and_hold(self):
        deck = FakeDeck(22.0)
        p = prof.Profile.from_dict(
            {
                "name": "t",
                "steps": [
                    {"ramp": 30},
                    {"hold": 30, "duration": "1s"},
                    {"ramp": 25, "rate": 600.0},
                ],
            }
        )
        out = []
        summary = prof.run_profile(deck, p, poll=0.001, say=out.append, interactive=False)
        self.assertEqual(summary["final_target"], 25)
        self.assertAlmostEqual(deck.temperature, 25.0)
        # integer-only setpoints, as the firmware requires
        self.assertTrue(all(float(c).is_integer() for c in deck.commands))
        self.assertTrue(any("1 C/min" in line or "600 C/min" in line for line in out))

    def test_log_file_written(self):
        import tempfile

        deck = FakeDeck(22.0)
        p = prof.Profile.from_dict({"steps": [{"ramp": 25}]})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "run.csv")
            prof.run_profile(deck, p, poll=0.001, log_path=path, say=lambda _l: None)
            with open(path) as fh:
                lines = fh.read().strip().splitlines()
        self.assertEqual(lines[0], "elapsed_s,phase,actual_c,setpoint_c")
        self.assertGreater(len(lines), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
