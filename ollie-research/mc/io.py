"""
Generic, stdlib-only filesystem + time helpers for Mission Control.

These are deliberately path-based and stateless (no module config): callers
pass the path. Atomic JSON write semantics match the original dashboard
(write to `<path>.tmp`, then `os.replace`). `research_dashboard` re-exports
`_load`/`_save` so the on-disk format stays shared with `research_registry`.
"""
import json
import os
import time


def _load(path):
    """Load JSON from `path`; return None if the file does not exist."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


def _save(path, data):
    """Atomically write `data` as indented JSON to `path`."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _tail(path, n):
    """Return the last `n` lines of `path` (rstrip'd), or [] if missing.

    Defined for the expanding Mission Control panels (log views); the budget
    handler still inlines its own 20-line tail for byte-identical behavior.
    """
    try:
        with open(path) as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return []
    return [ln.rstrip() for ln in lines[-n:]]


def _utcnow() -> str:
    """ISO-8601 UTC timestamp, e.g. 2026-06-15T12:34:56Z."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
