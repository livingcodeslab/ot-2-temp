"""Control an Opentrons Temperature Module ("TempDeck") over USB."""

from .driver import (  # noqa: F401
    MAX_TEMP,
    MIN_TEMP,
    TempDeck,
    TempDeckError,
    find_port,
)

__all__ = ["TempDeck", "TempDeckError", "find_port", "MIN_TEMP", "MAX_TEMP"]
__version__ = "0.1.0"
