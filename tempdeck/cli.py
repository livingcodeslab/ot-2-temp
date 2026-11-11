"""Command line interface: ``python3 -m tempdeck <command>``.

Commands
  info              print model / serial / firmware version
  temp              print current (and target) temperature, one line
  set <C>           set the target temperature
  off               stop heating / turn the module off
  wait <C>          block until the plate reaches <C> (default tolerance 1 C)
  monitor [C]       print readings until Ctrl-C
  plan <profile>    dry run: show the timeline of a protocol, no hardware use
  run <profile>     execute a multi-step temperature protocol
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from .driver import MAX_TEMP, MIN_TEMP, TempDeck, TempDeckError, find_port
from .profile import Profile, human_duration, run_profile


def _raise_interrupt(signum, frame):  # pragma: no cover - signal plumbing
    raise KeyboardInterrupt


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tempdeck",
        description="Control an Opentrons Temperature Module over USB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python3 -m tempdeck info\n"
            "  python3 -m tempdeck set 30\n"
            "  python3 -m tempdeck temp\n"
            "  python3 -m tempdeck wait 30 --timeout 300\n"
            "  python3 -m tempdeck off\n"
        ),
    )
    parser.add_argument(
        "-p",
        "--port",
        default=None,
        help="serial port (default: auto-detect the module, e.g. /dev/ttyACM0)",
    )
    parser.add_argument(
        "-t", "--timeout", type=float, default=1.0, help="serial timeout in seconds"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="print model, serial number and firmware version")
    sub.add_parser("temp", help="print the current temperature")
    sub.add_parser("off", help="stop heating and turn the module off")

    p_set = sub.add_parser("set", help="set the target temperature")
    p_set.add_argument("celsius", type=float, help=f"{MIN_TEMP:.0f}-{MAX_TEMP:.0f} C")
    p_set.add_argument(
        "--no-wait", action="store_true", help="do not wait for the plate to arrive"
    )
    p_set.add_argument(
        "--tolerance", type=float, default=1.0, help="wait tolerance in C (default 1)"
    )
    p_set.add_argument(
        "--wait-timeout", type=float, default=600.0, help="max seconds to wait"
    )

    p_wait = sub.add_parser("wait", help="wait until the target temperature is reached")
    p_wait.add_argument("celsius", type=float)
    p_wait.add_argument("--tolerance", type=float, default=1.0)
    p_wait.add_argument("--wait-timeout", type=float, default=600.0)

    p_mon = sub.add_parser("monitor", help="poll and print readings until Ctrl-C")
    p_mon.add_argument("--interval", type=float, default=2.0)

    for name, help_text in (
        ("plan", "dry run: print a protocol's timeline (no hardware access)"),
        ("run", "execute a multi-step temperature protocol from a JSON file"),
    ):
        p_prof = sub.add_parser(name, help=help_text)
        p_prof.add_argument("profile", help="path to a profile JSON file")
        p_prof.add_argument(
            "--start-temp",
            type=float,
            default=None,
            help="assumed/starting plate temperature (default: read the module)",
        )
        if name == "run":
            p_prof.add_argument("--poll", type=float, default=2.0, help="poll seconds")
            p_prof.add_argument(
                "--tolerance", type=float, default=0.5, help="arrival tolerance in C"
            )
            p_prof.add_argument(
                "--max-lead",
                type=float,
                default=None,
                help=(
                    "optional safety valve: never put the setpoint more than this "
                    "many C past the plate. Off by default - enabling it on a ramp "
                    "the module cannot sustain silently caps the rate."
                ),
            )
            p_prof.add_argument(
                "--lag-warn", type=float, default=3.0, help="warn if this far behind"
            )
            p_prof.add_argument("--log", default=None, help="write a CSV log here")
            p_prof.add_argument(
                "--off-on-exit", action="store_true", help="turn the heater off at the end"
            )
            p_prof.add_argument(
                "--yes", action="store_true", help="skip the confirmation prompt"
            )

    return parser


def _plan(profile: Profile, start_temp: float) -> int:
    print(f"Profile: {profile.name}")
    print(f"Start:   {start_temp:.1f} C")
    total = 0.0
    current = start_temp
    for index, step in enumerate(profile.steps, 1):
        estimate = step.estimated_seconds(current)
        total += estimate
        print(
            f"  {index}. {step.label:<42s} ~{human_duration(estimate):>8s}"
            f"   (cumulative ~{human_duration(total)})"
        )
        current = step.target
    print(f"\nEstimated total: ~{human_duration(total)} (unloaded plate; see README)")
    return 0


def _print_temperature(deck: TempDeck) -> None:
    current, target = deck.get_temperature()
    target_s = "off" if target is None else f"{target:.0f} C"
    print(f"{current:.1f} C  (target: {target_s})")


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # `plan` is a dry run: it must never need the hardware when a start
    # temperature is supplied, so handle it before port discovery.
    if args.command == "plan" and args.start_temp is not None:
        try:
            profile = Profile.load(args.profile)
        except (OSError, ValueError) as exc:
            print(f"error: cannot load profile: {exc}", file=sys.stderr)
            return 2
        return _plan(profile, args.start_temp)

    try:
        port = args.port or find_port()
    except TempDeckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        with TempDeck(port=port, timeout=args.timeout) as deck:
            if args.verbose:
                print(f"# using {port}", file=sys.stderr)

            if args.command == "info":
                info = deck.get_device_info()
                print(f"port:    {port}")
                print(f"model:   {info.get('model', '?')}")
                print(f"serial:  {info.get('serial', '?')}")
                print(f"version: {info.get('version', '?')}")

            elif args.command == "temp":
                _print_temperature(deck)

            elif args.command == "off":
                deck.deactivate()
                print("Heater off.")

            elif args.command == "set":
                if not MIN_TEMP <= args.celsius <= MAX_TEMP:
                    print(
                        f"error: {args.celsius} C is outside "
                        f"{MIN_TEMP:.0f}-{MAX_TEMP:.0f} C",
                        file=sys.stderr,
                    )
                    return 2
                deck.set_temperature(args.celsius)
                print(f"Target set to {args.celsius:.0f} C.")
                if not args.no_wait:
                    _print_temperature(deck)
                    print("Waiting for the plate to reach target...", flush=True)
                    reached = deck.wait_for_temperature(
                        args.celsius,
                        tolerance=args.tolerance,
                        timeout=args.wait_timeout,
                        on_update=lambda t: print(f"  {t:.1f} C", flush=True),
                    )
                    print(f"Reached {reached:.1f} C (target {args.celsius:.0f} C).")

            elif args.command == "wait":
                reached = deck.wait_for_temperature(
                    args.celsius,
                    tolerance=args.tolerance,
                    timeout=args.wait_timeout,
                    on_update=lambda t: print(f"  {t:.1f} C", flush=True),
                )
                print(f"Reached {reached:.1f} C (target {args.celsius:.0f} C).")

            elif args.command == "plan":
                return _plan(profile, deck.get_temperature()[0])

            elif args.command == "run":
                try:
                    profile = Profile.load(args.profile)
                except (OSError, ValueError) as exc:
                    print(f"error: cannot load profile: {exc}", file=sys.stderr)
                    return 2
                if not args.yes:
                    print(f"Profile: {profile.name}")
                    for index, step in enumerate(profile.steps, 1):
                        print(f"  {index}. {step.label}")
                    print(f"\nRun this protocol now on {port}? [y/N] ", end="", flush=True)
                    if input().strip().lower() not in ("y", "yes"):
                        print("Aborted.")
                        return 1
                # Turn SIGTERM into KeyboardInterrupt so the run reports the
                # state it left behind (instead of dying silently mid-ramp).
                previous = signal.signal(signal.SIGTERM, _raise_interrupt)
                try:
                    summary = run_profile(
                        deck,
                        profile,
                        poll=args.poll,
                        tolerance=args.tolerance,
                        max_lead=args.max_lead,
                        lag_warn=args.lag_warn,
                        log_path=args.log,
                    )
                finally:
                    signal.signal(signal.SIGTERM, previous)
                if args.off_on_exit:
                    deck.deactivate()
                    print("Heater off.")
                print(f"Summary: {summary['profile']}, "
                      f"{human_duration(summary['elapsed_s'])}")

            elif args.command == "monitor":
                print("Reading temperatures; Ctrl-C to stop.", flush=True)
                while True:
                    _print_temperature(deck)
                    time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except TempDeckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
