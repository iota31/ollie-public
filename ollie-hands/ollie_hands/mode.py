"""Owner-controlled in-memory Hands confirmation mode."""

from __future__ import annotations

import threading

NORMAL = "normal"
BYPASS = "bypass"
_VALID = {NORMAL, BYPASS}


class Mode:
    def __init__(self) -> None:
        self._value = NORMAL
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            return self._value

    def set(self, value: str) -> str:
        if value not in _VALID:
            raise ValueError("mode must be 'normal' or 'bypass'")
        with self._lock:
            self._value = value
            return self._value

    def is_bypass(self) -> bool:
        return self.get() == BYPASS
