"""Keep Windows from going to sleep while the app runs.

Running around the clock means the computer has to stay awake: a sleeping PC drops the
IB Gateway connection, stops the automatic exits and misses the pre-market scan. The
request is Windows' own (``SetThreadExecutionState``) - it lasts only as long as the app
runs, lets the screen turn off, and changes no power settings. Other systems are left alone.
"""

from __future__ import annotations

import sys

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake() -> bool:
    """Ask Windows not to sleep until the app exits. Call it from a thread that lives as long
    as the app (the main thread). Returns whether the request was taken."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED))
    except Exception:  # noqa: BLE001
        return False
