"""Optional: keep a Windows laptop from sleeping while the bot runs.

Uses SetThreadExecutionState, the same request media players make. It changes no power
settings and ends by itself when the server stops. The screen may still turn off; closing
the lid can still sleep the laptop, depending on the Windows lid setting.
"""

from __future__ import annotations

import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def supported() -> bool:
    return sys.platform == "win32"


def set_keep_awake(on: bool) -> bool:
    """Ask Windows to stay awake (or release the request). Must run on the event-loop thread,
    because Windows ties the request to the thread that made it."""
    if not supported():
        return False
    import ctypes

    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
    return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))  # type: ignore[attr-defined]
