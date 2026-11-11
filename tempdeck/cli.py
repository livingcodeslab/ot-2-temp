"""Command line interface: ``python3 -m tempdeck <command>``.

Commands
  info              print model / serial / firmware version
  temp              print current (and target) temperature, one line
  set <C>           set the target temperature
  off               stop heating / turn the module off
  wait <C>          block until the plate reaches <C> (default tolerance 1 C)
  monitor [C]       print readings until Ctrl-C
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from .driver import MAX_TEMP, MIN_TEMP, TempDeck, TempDeckError, find_port


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

    return parser


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
