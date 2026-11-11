"""Multi-step temperature protocols (ramps, holds) for the Temperature Module.

Why this exists
---------------
The module's firmware has no notion of a ramp *rate*.  ``M104 S<x>`` says "go to
x" and the onboard PID drives the Peltier at full power until it gets there
(and ``M104`` only accepts whole degrees).  So a "1 °C/min" ramp can only be
produced by the host, by walking the setpoint along a time trajectory:

    setpoint(t) = start + rate * (t - t0)      (clamped to the target)

Every poll we round that to an integer and send ``M104`` only when the integer
changes (i.e. once per degree).  This is deliberately *time*-based rather than
"wait until the step is reached, then dwell", because a loaded plate may never
reach a step and a wait-based loop would stall silently.  Instead we keep the
clock honest and report how far the plate lags the trajectory.

Two extra guards matter in practice:

* ``max_lead`` (optional, **off by default**) -- clamps the setpoint to at most
  this many degrees beyond the plate.  It is a safety valve, not a control: if
  you enable it on a ramp the module cannot sustain, the setpoint gets pinned
  just ahead of the plate and the ramp silently degrades into "hold N degrees
  above current".  That exact bug once turned a requested 10 C/min into
  ~2 C/min, so the default is now ``None`` (follow the trajectory exactly) and
  a lagging plate is reported instead, with the measured rate.
* ``lag_warn`` -- if the plate falls this far behind the trajectory, say so once,
  quoting the achieved rate and a revised ETA, so an unachievable rate is
  obvious rather than silent.

Example profile (JSON, see ``profiles/``)::

    {"name": "heat-90-then-1cpm-to-37",
     "steps": [
       {"ramp": 90},                                  # as fast as possible
       {"hold": 90, "duration": "5m"},
       {"ramp": 37, "rate": 1.0},                     # 1 C per minute
       {"hold": 37, "duration": "10m"}
     ]}
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .driver import MAX_TEMP, MIN_TEMP, TempDeck, TempDeckError

DEFAULT_POLL = 2.0
DEFAULT_TOLERANCE = 0.5
DEFAULT_MAX_LEAD: Optional[float] = None  # opt-in safety valve; see module docstring
DEFAULT_LAG_WARN = 3.0

# Measured slew rates on an unloaded TempDeck v2.1 (see README), used only for
# the dry-run estimate in `plan`.  Cooling is slower than heating and tails off
# as it approaches ambient-minus targets, hence the conservative value.
HEAT_RATE_ESTIMATE = 20.0  # C per minute
COOL_RATE_ESTIMATE = 6.0  # C per minute


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def parse_duration(value) -> float:
    """Accept seconds (int/float) or a string like '90s', '5m', '1h30m'."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"Unsupported duration: {value!r}")
    text = value.strip().lower()
    if re.fullmatch(r"[0-9.]+", text):
        return float(text)
    total, matches = 0.0, re.findall(r"([0-9.]+)\s*(h|m|s|min|ms)", text)
    if not matches:
        raise ValueError(f"Unsupported duration: {value!r}")
    for amount, unit in matches:
        total += float(amount) * {"h": 3600.0, "m": 60.0, "min": 60.0, "s": 1.0, "ms": 0.001}[unit]
    return total


def scheduled_temperature(
    start: float, target: float, rate_c_per_min: float, elapsed_s: float
) -> float:
    """Ideal (unrounded) plate temperature ``elapsed_s`` into a rate ramp."""
    if rate_c_per_min <= 0:
        raise ValueError("rate must be > 0")
    direction = 1.0 if target >= start else -1.0
    span = abs(target - start)
    travelled = min(span, rate_c_per_min * max(elapsed_s, 0.0) / 60.0)
    return start + direction * travelled


def limited_setpoint(
    ideal: float, actual: float, direction: float, max_lead: Optional[float]
) -> float:
    """Apply the optional lead clamp to a trajectory setpoint.

    ``max_lead=None`` (the default) returns ``ideal`` untouched, so the setpoint
    follows the time trajectory exactly.  Passing a number caps the setpoint to
    that many degrees beyond the plate -- see the module docstring for why that
    is *off* by default: on an unachievable ramp it pins the setpoint just ahead
    of the plate and the ramp silently runs far slower than requested.
    """
    if max_lead is None:
        return ideal
    if abs(ideal - actual) > max_lead:
        return actual + direction * max_lead
    return ideal


def rate_status(
    actual: float, start_temp: float, elapsed_s: float, target: float
) -> tuple:
    """Return ``(achieved_c_per_min, eta_seconds)`` from observed progress."""
    if elapsed_s <= 0:
        return 0.0, float("inf")
    achieved = (actual - start_temp) / (elapsed_s / 60.0)
    remaining = abs(target - actual)
    if achieved == 0:
        return 0.0, float("inf")
    return achieved, remaining / abs(achieved) * 60.0


def human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s" if secs else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


# --------------------------------------------------------------------------
# profile model
# --------------------------------------------------------------------------
@dataclass
class Step:
    """One protocol step: either a ramp to a temperature or a hold."""

    kind: str  # "ramp" | "hold"
    target: float
    rate: Optional[float] = None  # ramp only; C per minute, None = max speed
    duration: Optional[float] = None  # hold only, seconds
    tolerance: float = DEFAULT_TOLERANCE

    @property
    def label(self) -> str:
        if self.kind == "hold":
            return f"hold {self.target:.0f} C for {human_duration(self.duration or 0)}"
        if self.rate is None:
            return f"ramp to {self.target:.0f} C (max rate)"
        return f"ramp to {self.target:.0f} C at {self.rate:g} C/min"

    def estimated_seconds(self, start: float) -> float:
        """Rough duration, using measured slew rates (dry-run only)."""
        ramp = abs(self.target - start)
        if ramp == 0:
            travel = 0.0
        elif self.rate is not None:
            travel = ramp / self.rate * 60.0
        elif self.target > start:
            travel = ramp / HEAT_RATE_ESTIMATE * 60.0
        else:
            travel = ramp / COOL_RATE_ESTIMATE * 60.0
        if self.kind == "hold":
            return travel + (self.duration or 0.0)
        return travel

    @classmethod
    def from_dict(cls, data: dict) -> "Step":
        if not isinstance(data, dict) or len(data) == 0:
            raise ValueError(f"Invalid step: {data!r}")
        tolerance = float(data.get("tolerance", DEFAULT_TOLERANCE))
        if "hold" in data:
            step = cls(
                kind="hold",
                target=float(data["hold"]),
                duration=parse_duration(data.get("duration", 0)),
                tolerance=tolerance,
            )
        elif "ramp" in data:
            rate = data.get("rate")
            step = cls(
                kind="ramp",
                target=float(data["ramp"]),
                rate=None if rate is None else float(rate),
                tolerance=tolerance,
            )
            if step.rate is not None and step.rate <= 0:
                raise ValueError("ramp rate must be > 0")
        elif "set" in data:  # convenience: hold forever is not allowed, so treat as ramp
            step = cls(kind="ramp", target=float(data["set"]), tolerance=tolerance)
        else:
            raise ValueError(f"Step needs 'ramp', 'hold' or 'set': {data!r}")
        if not MIN_TEMP <= step.target <= MAX_TEMP:
            raise ValueError(
                f"Step target {step.target} C is outside {MIN_TEMP:.0f}-{MAX_TEMP:.0f} C"
            )
        return step


@dataclass
class Profile:
    name: str
    steps: List[Step] = field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "Profile":
        with open(path) as fh:
            data = json.load(fh)
        return cls.from_dict(data, default_name=os.path.basename(path))

    @classmethod
    def from_dict(cls, data: dict, default_name: str = "profile") -> "Profile":
        steps = [Step.from_dict(s) for s in data.get("steps", [])]
        if not steps:
            raise ValueError("Profile has no steps")
        return cls(name=data.get("name", default_name), steps=steps)


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------
class RunLog:
    """CSV log of a protocol run: elapsed time, phase, plate temp, setpoint."""

    def __init__(self, path: Optional[str]) -> None:
        self.path = path
        self._fh = None
        self._writer = None
        self._t0 = time.monotonic()
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._fh = open(path, "w", newline="")
            self._writer = csv.writer(self._fh)
            self._writer.writerow(["elapsed_s", "phase", "actual_c", "setpoint_c"])

    def write(self, phase: str, actual: float, setpoint: Optional[float]) -> None:
        if self._writer:
            self._writer.writerow(
                [f"{time.monotonic() - self._t0:.1f}", phase, f"{actual:.3f}",
                 "" if setpoint is None else f"{setpoint:.3f}"]
            )
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def run_profile(
    deck: TempDeck,
    profile: Profile,
    poll: float = DEFAULT_POLL,
    tolerance: float = DEFAULT_TOLERANCE,
    max_lead: Optional[float] = DEFAULT_MAX_LEAD,    lag_warn: float = DEFAULT_LAG_WARN,
    log_path: Optional[str] = None,
    say: Callable[[str], None] = print,
    interactive: bool = True,
) -> dict:
    """Execute ``profile`` on ``deck``.  Returns a small summary dict.

    The heater is left at the final step's target on return; the caller decides
    whether to turn it off (see the ``--off-on-exit`` CLI flag).
    """
    log = RunLog(log_path)
    started = time.monotonic()
    sent_setpoint: Optional[float] = None
    say(f"Profile: {profile.name}")
    for _i, s in enumerate(profile.steps, 1):
        say(f"  {_i}. {s.label}")
    if log_path:
        say(f"  logging to {log_path}")

    def read() -> float:
        nonlocal sent_setpoint
        actual, target = deck.get_temperature()
        if target is not None and target != sent_setpoint:
            sent_setpoint = target
        return actual

    def command(celsius: float) -> None:
        nonlocal sent_setpoint
        celsius = float(round(celsius))
        if celsius != sent_setpoint:
            deck.set_temperature(celsius)
            sent_setpoint = celsius

    try:
        for index, step in enumerate(profile.steps, 1):
            phase = f"{index}:{step.kind}"
            say(f"\n[{_clock(started)}] step {index}/{len(profile.steps)}: {step.label}")
            tol = step.tolerance if step.tolerance != DEFAULT_TOLERANCE else tolerance

            if step.kind == "hold":
                command(step.target)
                actual = _settle(deck, step.target, tol, poll, say, log, phase)
                say(f"  holding {step.target:.0f} C for {human_duration(step.duration or 0)}...")
                end = time.monotonic() + (step.duration or 0.0)
                while time.monotonic() < end:
                    actual = read()
                    log.write(phase, actual, step.target)
                    say(f"  {actual:.1f} C  ({human_duration(end - time.monotonic())} left)")
                    time.sleep(min(poll, max(0.0, end - time.monotonic())))
                continue

            # ---- ramp ----
            start_temp = read()
            log.write(phase, start_temp, sent_setpoint)
            if step.rate is None:
                command(step.target)
                _settle(deck, step.target, tol, poll, say, log, phase)
                say(f"  reached {step.target:.0f} C")
                continue

            direction = 1.0 if step.target >= start_temp else -1.0
            span = abs(step.target - start_temp)
            expected = span / step.rate * 60.0
            say(
                f"  from {start_temp:.1f} C, {span:.0f} C at {step.rate:g} C/min "
                f"-> {human_duration(expected)}"
            )
            t0 = time.monotonic()
            warned = False
            # Give up only if wildly overdue; a slow load is reported, not killed.
            hard_deadline = expected * 3 + 300
            while True:
                actual = read()
                elapsed = time.monotonic() - t0
                sched = scheduled_temperature(
                    start_temp, step.target, step.rate, elapsed
                )
                ideal = limited_setpoint(sched, actual, direction, max_lead)
                command(ideal)
                log.write(phase, actual, sent_setpoint)

                achieved, eta = rate_status(actual, start_temp, elapsed, step.target)
                eta_text = human_duration(eta) if eta != float("inf") else "?"
                say(
                    f"  {actual:.1f} C  (sched {sched:.1f} C, "
                    f"{abs(achieved):.1f} C/min, ~{eta_text} left)"
                )
                lag = abs(actual - sched)
                if lag > lag_warn and not warned:
                    say(
                        f"  ! cannot sustain {step.rate:g} C/min: the plate is "
                        f"{lag:.1f} C behind schedule and moving at ~{abs(achieved):.1f} "
                        f"C/min. ETA revised to ~{eta_text}."
                    )
                    if max_lead is not None:
                        say(
                            f"  ! note: --max-lead {max_lead:g} is also capping the "
                            f"setpoint; raise or drop it for the full rate."
                        )
                    warned = True
                if abs(actual - step.target) <= tol:
                    break
                if elapsed > hard_deadline:
                    raise TempDeckError(
                        f"Ramp to {step.target:.0f} C stalled at {actual:.1f} C "
                        f"after {human_duration(elapsed)}"
                    )
                time.sleep(poll)
            _settle(deck, step.target, tol, poll, say, log, phase)
            say(f"  reached {step.target:.0f} C")
    except KeyboardInterrupt:
        say(
            f"\n! interrupted; the heater is still regulating to the last "
            f"setpoint sent ({_fmt_setpoint(sent_setpoint)}). "
            f"Run `./set-temp off` to disengage."
        )
        raise
    finally:
        log.close()

    summary = {
        "profile": profile.name,
        "elapsed_s": time.monotonic() - started,
        "final_target": profile.steps[-1].target,
        "log": log_path,
    }
    say(f"\nDone in {human_duration(summary['elapsed_s'])}; "
        f"holding {summary['final_target']:.0f} C (heater still on).")
    return summary


def _settle(
    deck: TempDeck,
    target: float,
    tolerance: float,
    poll: float,
    say: Callable[[str], None],
    log: RunLog,
    phase: str,
    timeout: float = 1200.0,
) -> float:
    """Wait until the plate is within ``tolerance`` of ``target``."""
    deadline = time.monotonic() + timeout
    while True:
        actual, _ = deck.get_temperature()
        log.write(phase, actual, target)
        say(f"  {actual:.1f} C  (settling to {target:.0f} C)")
        if abs(actual - target) <= tolerance:
            return actual
        if time.monotonic() > deadline:
            raise TempDeckError(
                f"Timed out settling to {target:.0f} C (last {actual:.1f} C)"
            )
        time.sleep(poll)


def _clock(started: float) -> str:
    return f"+{human_duration(time.monotonic() - started)}"


def _fmt_setpoint(setpoint: Optional[float]) -> str:
    return "unknown" if setpoint is None else f"{setpoint:.0f} C"
