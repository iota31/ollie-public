"""Append-only, TAMPER-EVIDENT JSONL audit (plan Track D2).

Every tool call is recorded BEFORE its response is returned. Screenshots are
saved as PNGs alongside, referenced by path.

Tamper-evidence: each record carries `prev` (the hash of the previous record)
and `hash = sha256(canonical(record-without-hash))`. Because `prev` is part of
the hashed body, the records form a chain — editing, reordering, or deleting any
record breaks it, and the break is localisable to the exact record. The chain
continues across daily files and across engine restarts (`prev` is reloaded from
the most recent file at boot). Verification is deliberately reproducible OFF the
box (see `scripts/audit-verify.py`), so a host compromise can be detected from a
machine the attacker doesn't control.

Note: the chain proves INTEGRITY (nothing was altered/removed unnoticed). It is
not a signature — anyone who can append can extend the chain. Off-box sync of a
read-only copy + off-box verify is what makes deletion/rewrite detectable; the
policy gate (T4) is what stops the brain editing the trail in the first place.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

GENESIS = "genesis"


def _canon(d: dict) -> str:
    """Deterministic serialization used for hashing (writer and verifier MUST
    agree). Sorted keys + compact separators.

    JSON-native only: no custom default=str. The writer must ensure every value
    is one of: dict, list, str, int, float, bool, or None. Using default=str here
    would let non-JSON-native objects (e.g., Path, custom classes) stringify
    differently on the writer vs. an off-box verifier, causing hash divergence.
    """
    return json.dumps(d, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _hash_record(core: dict) -> str:
    """Hash of a record body (everything EXCEPT its own `hash` field; `prev` is
    included, which is what links the chain)."""
    return hashlib.sha256(_canon(core).encode("utf-8")).hexdigest()


class Audit:
    def __init__(self, audit_dir: str) -> None:
        self.dir = Path(audit_dir)
        self.shots = self.dir / "shots"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.shots.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_hash = self._load_last_hash()

    def _file(self) -> Path:
        return self.dir / f"audit-{time.strftime('%Y%m%d')}.jsonl"

    def _load_last_hash(self) -> str:
        """Resume the chain: return the `hash` of the last chained record in the
        most recent audit file, or GENESIS if there is none (fresh, or only
        legacy pre-chain records exist)."""
        files = sorted(self.dir.glob("audit-*.jsonl"))
        for fp in reversed(files):
            try:
                lines = fp.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("hash"):
                    return rec["hash"]
            # most recent non-empty file had no chained record -> genesis
        return GENESIS

    def event(self, tool: str, *, args: dict | None = None,
              status: str = "ok", detail: str = "",
              duration_ms: int | None = None,
              screenshot: str | None = None) -> str:
        eid = uuid.uuid4().hex[:12]
        with self._lock:
            core = {
                "id": eid,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "tool": tool,
                "args": args or {},
                "status": status,
                "detail": detail,
                "duration_ms": duration_ms,
                "screenshot": screenshot,
                "prev": self._last_hash,
            }
            h = _hash_record(core)
            rec = {**core, "hash": h}
            line = json.dumps(rec, ensure_ascii=False, default=str)
            with self._file().open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._last_hash = h
        return eid

    def shot_path(self) -> Path:
        return self.shots / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.png"


def verify_chain(audit_dir: str) -> dict:
    """Walk every audit-*.jsonl in chronological order and verify the hash
    chain. Reproducible off-box. Returns:

      {"ok": bool, "chained": int, "legacy": int, "files": [...],
       "break": {file, line, id, reason} | None}

    `legacy` counts pre-chain records (no `hash`) — expected only at the
    transition where chaining was introduced.
    """
    d = Path(audit_dir)
    files = sorted(d.glob("audit-*.jsonl"))
    chained = legacy = 0
    started = False           # have we seen the first chained record yet?
    prev_hash = GENESIS
    for fp in files:
        try:
            lines = fp.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            return {"ok": False, "chained": chained, "legacy": legacy,
                    "files": [f.name for f in files],
                    "break": {"file": fp.name, "line": 0, "id": None,
                              "reason": f"unreadable: {e}"}}
        for i, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return {"ok": False, "chained": chained, "legacy": legacy,
                        "files": [f.name for f in files],
                        "break": {"file": fp.name, "line": i, "id": None,
                                  "reason": "invalid JSON"}}
            stored = rec.get("hash")
            if not stored:
                # pre-chain legacy record; only tolerated before the chain starts
                if started:
                    return {"ok": False, "chained": chained, "legacy": legacy,
                            "files": [f.name for f in files],
                            "break": {"file": fp.name, "line": i,
                                      "id": rec.get("id"),
                                      "reason": "unchained record after chain start "
                                                "(possible deletion of hash)"}}
                legacy += 1
                continue
            core = {k: v for k, v in rec.items() if k != "hash"}
            recomputed = _hash_record(core)
            if recomputed != stored:
                return {"ok": False, "chained": chained, "legacy": legacy,
                        "files": [f.name for f in files],
                        "break": {"file": fp.name, "line": i, "id": rec.get("id"),
                                  "reason": "record body altered (hash mismatch)"}}
            if rec.get("prev") != prev_hash:
                return {"ok": False, "chained": chained, "legacy": legacy,
                        "files": [f.name for f in files],
                        "break": {"file": fp.name, "line": i, "id": rec.get("id"),
                                  "reason": f"broken link: prev={rec.get('prev')} "
                                            f"expected={prev_hash} "
                                            "(reorder/deletion)"}}
            prev_hash = stored
            started = True
            chained += 1
    return {"ok": True, "chained": chained, "legacy": legacy,
            "files": [f.name for f in files], "break": None}
