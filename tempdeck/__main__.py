"""Allow ``python3 -m tempdeck`` to run the CLI."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
