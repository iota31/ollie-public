"""
Tiny stdlib TTL cache for Mission Control.

DEFINED, NOT YET WIRED IN: future panels will read expensive things
(subprocess output, large logs, remote stats). This gives them a thread-safe
time-bounded memo without pulling in a dependency. Phase 0 leaves all current
endpoints uncached so behavior is byte-identical.
"""
import threading
import time


class TTLCache:
    """Thread-safe dict cache where entries expire after `ttl` seconds."""

    def __init__(self, ttl: float = 30.0):
        self.ttl = ttl
        self._store: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        """Return the cached value, or None if absent/expired."""
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if now >= expires_at:
                self._store.pop(key, None)
                return None
            return value

    def set(self, key, value):
        """Store `value` under `key` with the cache's TTL."""
        with self._lock:
            self._store[key] = (time.monotonic() + self.ttl, value)

    def get_or_set(self, key, producer):
        """Return cached value for `key`, else compute via `producer()`, cache, return."""
        hit = self.get(key)
        if hit is not None:
            return hit
        value = producer()
        self.set(key, value)
        return value

    def clear(self):
        """Drop all entries."""
        with self._lock:
            self._store.clear()
