#!/usr/bin/env python3
"""Ollie Curiosity Engine — source/interest REGISTRY (atomic, guarded).

Owns the two UI-editable config files of the Curiosity Engine:

  - sources.json   : the curiosity feed list (what to poll, how fresh, weight)
  - interests.json : the "what onllm cares about" profile (seeds the gate)

Pure stdlib. Every read returns a sane, schema-normalized value even when the
file is absent or corrupt — a registry hiccup must never break a poll cycle.
Every write is atomic (temp + os.replace) so a crash mid-write can never leave
a half-written config. All paths resolve through _paths() so tests can repoint
HOME without touching the deployed box.

Shared contracts (other Curiosity Engine components depend on these EXACTLY):

  SOURCE = {
    id:           str,
    type:         "rss"|"reddit"|"blog"|"discovery"|"instagram"|"x",
    target:       str,          # feed url / subreddit / search query
    domain_tags:  [str],
    weight:       float = 1.0,
    enabled:      bool = True,
    recency_days: int  = 14,
    added_at:     ISO8601 str,
  }
  INTERESTS = {
    domains:        [str],
    keywords_boost: [str],
    anti_interests: [str],
    updated_at:     ISO8601 str,
  }
"""
import hashlib
import json
import os
import tempfile
import time

# --- runtime paths (computed at import; tests reassign these module globals) --
HOME = os.environ.get("OLLIE_HOME", "/home/openclaw")
WORKSPACE = f"{HOME}/.openclaw/workspace"
LOGS = f"{HOME}/.openclaw/logs"

SOURCE_TYPES = {"rss", "reddit", "blog", "discovery", "instagram", "x"}
DEFAULT_WEIGHT = 1.0
DEFAULT_RECENCY_DAYS = 14


def _paths():
    """Resolve runtime paths fresh each call so tests can repoint HOME.

    Module constants WORKSPACE/LOGS are computed at import; tests reassign the
    module globals and we read those reassigned globals here (functions read
    globals at call time)."""
    research = f"{WORKSPACE}/research"
    return {
        "dir": research,
        "sources": f"{research}/sources.json",
        "interests": f"{research}/interests.json",
        "log": f"{LOGS}/research-registry.log",
    }


def _log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        path = _paths()["log"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def fingerprint(url, title):
    """Shared candidate fingerprint: sha256 hex of normalized url + '|' + title.

    Pollers (components A/B) and the queue (component D) MUST use this exact
    helper so dedup is consistent across the engine."""
    norm_url = (url or "").strip().lower().rstrip("/")
    norm_title = " ".join((title or "").strip().lower().split())
    raw = f"{norm_url}|{norm_title}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()


def _atomic_write_json(path, obj):
    """Write obj as pretty JSON atomically (temp in same dir + os.replace)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        _log(f"corrupt/unreadable {path}: {e} -> defaults")
        return None


def _as_str_list(v):
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        if isinstance(x, str):
            s = x.strip()
            if s:
                out.append(s)
    return out


# ---------------------------------------------------------------- sources -----
def _normalize_source(raw):
    """Validate + fill defaults for one source dict. Returns None to skip
    structurally-invalid rows (no id or unknown type)."""
    if not isinstance(raw, dict):
        return None
    sid = raw.get("id")
    if not isinstance(sid, str) or not sid.strip():
        return None
    stype = raw.get("type")
    if stype not in SOURCE_TYPES:
        _log(f"source {sid!r}: bad type {stype!r} -> skipped")
        return None

    # weight
    try:
        weight = float(raw.get("weight", DEFAULT_WEIGHT))
    except (TypeError, ValueError):
        weight = DEFAULT_WEIGHT
    if weight <= 0:
        weight = DEFAULT_WEIGHT

    # recency_days
    try:
        recency = int(raw.get("recency_days", DEFAULT_RECENCY_DAYS))
    except (TypeError, ValueError):
        recency = DEFAULT_RECENCY_DAYS
    if recency <= 0:
        recency = DEFAULT_RECENCY_DAYS

    enabled = raw.get("enabled", True)
    enabled = bool(enabled) if isinstance(enabled, (bool, int)) else True

    return {
        "id": sid.strip(),
        "type": stype,
        "target": str(raw.get("target", "")).strip(),
        "domain_tags": _as_str_list(raw.get("domain_tags")),
        "weight": weight,
        "enabled": enabled,
        "recency_days": recency,
        "added_at": str(raw.get("added_at") or _now_iso()),
    }


def load_sources():
    """Load + normalize sources.json. Missing/corrupt -> []. Drops invalid rows
    and dedups by id (first wins)."""
    raw = _read_json(_paths()["sources"])
    if not isinstance(raw, list):
        if raw is not None:
            _log("sources.json not a list -> []")
        return []
    out = []
    seen = set()
    for row in raw:
        norm = _normalize_source(row)
        if norm is None:
            continue
        if norm["id"] in seen:
            _log(f"duplicate source id {norm['id']!r} -> kept first")
            continue
        seen.add(norm["id"])
        out.append(norm)
    return out


def save_sources(sources):
    """Normalize then atomically persist the source list. Returns the list as
    written. Never raises on bad input rows (they are dropped + logged)."""
    if not isinstance(sources, list):
        raise TypeError("save_sources expects a list of source dicts")
    norm = []
    seen = set()
    for row in sources:
        n = _normalize_source(row)
        if n is None:
            continue
        if n["id"] in seen:
            continue
        seen.add(n["id"])
        norm.append(n)
    _atomic_write_json(_paths()["sources"], norm)
    _log(f"saved {len(norm)} sources")
    return norm


# -------------------------------------------------------------- interests -----
def _default_interests():
    return {
        "domains": [],
        "keywords_boost": [],
        "anti_interests": [],
        "updated_at": _now_iso(),
    }


def _normalize_interests(raw):
    if not isinstance(raw, dict):
        return _default_interests()
    return {
        "domains": _as_str_list(raw.get("domains")),
        "keywords_boost": _as_str_list(raw.get("keywords_boost")),
        "anti_interests": _as_str_list(raw.get("anti_interests")),
        "updated_at": str(raw.get("updated_at") or _now_iso()),
    }


def load_interests():
    """Load + normalize interests.json. Missing/corrupt -> sane empty profile."""
    raw = _read_json(_paths()["interests"])
    if raw is None:
        return _default_interests()
    return _normalize_interests(raw)


def save_interests(interests):
    """Normalize, stamp updated_at, then atomically persist. Returns written dict."""
    norm = _normalize_interests(interests)
    norm["updated_at"] = _now_iso()
    _atomic_write_json(_paths()["interests"], norm)
    _log("saved interests")
    return norm


if __name__ == "__main__":  # pragma: no cover — tiny operational smoke
    p = _paths()
    print("research dir:", p["dir"])
    print("sources:", len(load_sources()))
    print("interests domains:", len(load_interests()["domains"]))
