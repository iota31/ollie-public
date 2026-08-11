#!/usr/bin/env python3
"""Ollie dream promoter — cure for the permanent amnesia.

The OpenClaw memory-core "dreaming" sweep stages memory candidates nightly
(light/YYYY-MM-DD.md) but its DEEP-tier promotion gate is hardcoded in the
minified bundle (minScore 0.8, minRecallCount 3, minUniqueQueries 3) and the
2026.5.28 config schema REJECTS any override under
plugins.entries.memory-core.config.dreaming (adding "deep"/"minScore" makes the
gateway exit 78). Result: across two nights it ranked 0 and promoted 0 of ~80
candidates (all confidence 0.58, recalls 0) into durable MEMORY.md. Ollie keeps
forgetting.

This is OUR OWN deterministic promoter. It bypasses the binary's gate entirely:
it reads the staged candidate files the sweep already wrote, applies a
CONSERVATIVE promotion policy, and APPENDS at most a handful of high-signal
lines to MEMORY.md per night — never deleting or rewriting a single existing
line. It keeps its own state (logs/dream-promoter-state.json) so re-runs never
re-promote, and it touches NONE of memory-core's own state.

Fired by a systemd timer at 03:30 box time, AFTER the 03:00 dreaming sweep.

Safety contract (this file must NEVER corrupt MEMORY.md):
  - If MEMORY.md is unreadable, ABORT without writing anything.
  - All writes go via a temp file + os.replace (atomic).
  - Append-only: existing content is copied verbatim, new lines added under a
    clearly marked, promoter-owned section.
  - Any unexpected exception is logged to ~/.openclaw/logs/dream-promoter.log
    and the process exits 0 (a crashing memory-fixer must never become the new
    way Ollie loses memory).

stdlib only. Mirrors sibling jobs (ollie_heartbeat.py) for house style; the
OLLIE_HOME / _paths() indirection makes every path overridable in tests.
"""
import datetime
import glob
import json
import os
import re
import sys
import tempfile
import time

# House style: HOME hardcoded to the box account (matches ollie_heartbeat.py),
# but every derived path is resolved lazily through _paths() so the off-box
# unittest suite can repoint the whole tree at a tempdir via OLLIE_HOME.
HOME = "/home/openclaw"

# --- promotion policy knobs (conservative on purpose) ----------------------
MAX_PROMOTIONS_PER_NIGHT = 3   # hard ceiling; we'd rather under- than over-promote
MIN_RECURRENCE_DAYS = 2        # appears on >=N distinct days -> durable signal
PERCENTILE = 90                # OR in the top 10% of THIS night's confidences
LOOKBACK_DAYS = 14             # how many recent light/ files to scan for recurrence
MIN_CANDIDATE_CHARS = 24       # ignore trivially short fragments
MAX_PROMOTED_CHARS = 280       # truncate a promoted line so MEMORY.md stays tidy

# Section header the promoter owns. We only ever append under THIS header; we
# never touch any line above it. Mirrors MEMORY.md's "## Heading" + "- bullet".
PROMOTER_SECTION = "## Dreamt (auto-promoted memories)"
PROMOTER_NOTE = (
    "_Appended by ollie_dream_promoter.py from the nightly dreaming sweep. "
    "Conservative: recurring or top-percentile candidates only. Never edits "
    "lines above this header._"
)

# Noise filter: the corpus is full of heartbeat/runner plumbing that is NOT a
# durable memory. We refuse to promote anything that looks like machinery.
NOISE_PATTERNS = [
    re.compile(r, re.IGNORECASE) for r in (
        r"^\s*SILENCE\b",
        r"\bSILENCE\b.*\bquiet hours\b",
        r"assistant turn failed before producing content",
        r"^\s*HEARTBEAT\s*[—-]",
        r"box-local time \(IST",
        r"Output protocol \(STRICT",
        r"=== HEARTBEAT INSTRUCTIONS ===",
        r"Recurring themes:",
        r"\bRanked \d+ candidate",
        r"Write a dream diary entry",
        r"BACKGROUND JOB \d{8}-\d{6}",
    )
]

# --- doctrine-boilerplate filter -------------------------------------------
# Candidates whose text is a fragment of these injected doctrine files are
# synthetic prompt text, NOT durable learnings. Never promote them.
#
# Filenames are relative to the workspace dir (_paths()["ws"]). The list is
# module-level so tests can substitute their own fake doctrine files (same
# pattern as OLLIE_HOME / _paths()).
DOCTRINE_FILENAMES = [
    "HEARTBEAT.md",
    "OPEN_LOOPS.md",
    "PROJECT_DOCTRINE.md",
    "AGENTS.md",
    "SOUL.md",
    "IDENTITY.md",
]

# Per-project state files. The project-tick injects projects/<slug>/{PROJECT.md,
# PLAN.md, JOURNAL.md, inbox.md} verbatim into its session prompts, so that
# content recurs in the dreaming corpus and trips the recurrence gate exactly
# like HEARTBEAT/OPEN_LOOPS did — a FALSE signal. We glob every project's copy
# of these and fold them into the same doctrine blob. The glob pattern is
# relative to <ws>/projects/*/<name>; the projects dir may not exist on a fresh
# box, in which case the glob yields nothing and we skip gracefully.
PROJECT_FILENAMES = [
    "PROJECT.md",
    "PLAN.md",
    "JOURNAL.md",
    "inbox.md",
]
PROJECTS_SUBDIR = "projects"

# Structural backup patterns: markdown fragments that are structurally
# characteristic of doctrine/state files and cannot be a durable personal fact.
# These fire even when doctrine files are unavailable (unreadable / not yet
# deployed to a new box).
_DOCTRINE_STRUCTURAL_PATTERNS = [
    re.compile(r, re.IGNORECASE) for r in (
        # Open-loops ledger lines: "- [ ] YYYY-MM-DD | X → Y | ..."
        r"-\s*\[\s*[x ]\s*\]\s*\d{4}-\d{2}-\d{2}\s*\|",
        # "OLLIE → TUSHAR" / "OLLIE -> TUSHAR" arrow notation
        r"\bOLLIE\s*[→\-]+\s*(TUSHAR|SELF)\b",
        # Instruction-list markers seen in HEARTBEAT.md / PROJECT_DOCTRINE.md
        r"## The bar for messaging",
        r"\bCuriosity slot\b.*\bquiet hours\b",
        r"## Output protocol",
        r"## Session contract",
        r"## Hard rules",
        r"## Journal entry format",
        # PROJECT_DOCTRINE session-protocol terminals
        r"\b(CONTINUE|MILESTONE|BLOCKED|DONE|FAILED):\s",
        # HEARTBEAT / OPEN_LOOPS file-path markers injected in the prompt
        r"/home/openclaw/\.openclaw/workspace/OPEN_LOOPS\.md",
        r"/home/openclaw/\.openclaw/workspace/HEARTBEAT\.md",
    )
]

# Sliding-window overlap threshold: if this fraction of 60-char windows from
# the normalized candidate text are found inside the normalized doctrine blob,
# the candidate is classified as doctrine boilerplate.
_DOCTRINE_WINDOW_SIZE = 60
_DOCTRINE_OVERLAP_THRESHOLD = 0.50  # 50% of windows must match

# Per-workspace-path cache: normalized_doctrine_blob (str) or None if all
# files were unreadable. Cleared between test runs via _doctrine_cache.clear().
_doctrine_cache: dict = {}


def _doctrine_paths(ws_path):
    """All concrete file paths whose content counts as injected boilerplate.

    = the 6 fixed workspace doctrine files
    + every per-project state file under projects/*/{PROJECT,PLAN,JOURNAL,inbox}.

    The projects glob is fully guarded: a missing projects dir simply yields no
    matches.  Never raises.
    """
    paths = [os.path.join(ws_path, name) for name in DOCTRINE_FILENAMES]
    try:
        for name in PROJECT_FILENAMES:
            # projects/<any-slug>/<name>
            paths.extend(
                glob.glob(os.path.join(ws_path, PROJECTS_SUBDIR, "*", name))
            )
    except OSError:
        pass  # glob shouldn't raise, but stay defensive — never crash
    return paths


def _load_doctrine_blob(ws_path):
    """Return a single normalized string of all readable boilerplate files.

    Includes the 6 fixed doctrine files AND every per-project state file
    (PROJECT.md/PLAN.md/JOURNAL.md/inbox.md under projects/*/). Cached per
    workspace path so I/O is paid at most once per run.  Returns None if NONE
    of them are readable; callers fall back to structural patterns and must
    never crash on None.
    """
    if ws_path in _doctrine_cache:
        return _doctrine_cache[ws_path]
    parts = []
    for path in _doctrine_paths(ws_path):
        try:
            with open(path, encoding="utf-8") as f:
                parts.append(f.read())
        except OSError:
            pass  # missing/unreadable — skip gracefully
    blob = re.sub(r"\s+", " ", " ".join(parts)).strip().lower() if parts else None
    _doctrine_cache[ws_path] = blob
    return blob


def _is_doctrine_boilerplate(text, ws_path):
    """Return True if *text* is a fragment of Ollie's injected doctrine files.

    Two-layer check:
      (a) Primary — sliding-window substring overlap against the live doctrine
          blob.  Robust even for truncated or mid-sentence fragments because
          the heartbeat injects whole-file content that the dreaming sweep
          slices at corpus-line boundaries.
      (b) Backup — structural regex markers that are definitionally
          doctrine-specific (open-loop ledger lines, instruction headers,
          arrow notation, etc.).  Always runs, no I/O required.

    Never raises; if doctrine files are completely unreadable (b) alone guards.
    """
    # (b) Structural backup — always evaluated, no I/O.
    if any(p.search(text) for p in _DOCTRINE_STRUCTURAL_PATTERNS):
        return True

    # (a) Doctrine-substring overlap.
    blob = _load_doctrine_blob(ws_path)
    if blob is None:
        return False  # couldn't load any doctrine file; structural layer is sole guard

    norm = re.sub(r"\s+", " ", text).strip().lower()
    if len(norm) < _DOCTRINE_WINDOW_SIZE:
        # Short candidate: direct substring check suffices.
        return norm in blob

    # Stride = half the window for good overlap sensitivity.
    stride = _DOCTRINE_WINDOW_SIZE // 2
    windows = [
        norm[i: i + _DOCTRINE_WINDOW_SIZE]
        for i in range(0, len(norm) - _DOCTRINE_WINDOW_SIZE + 1, stride)
    ]
    if not windows:
        return False
    hits = sum(1 for w in windows if w in blob)
    return (hits / len(windows)) >= _DOCTRINE_OVERLAP_THRESHOLD


def _paths():
    """All filesystem paths, resolved from OLLIE_HOME (defaults to HOME).

    Centralising here keeps the script honest about hardcoded HOME in
    production while letting tests repoint the entire tree at a tempdir.
    """
    home = os.environ.get("OLLIE_HOME", HOME)
    ws = f"{home}/.openclaw/workspace"
    return {
        "home": home,
        "ws": ws,
        "memory_md": f"{ws}/MEMORY.md",
        "light_dir": f"{ws}/memory/dreaming/light",
        "state": f"{home}/.openclaw/logs/dream-promoter-state.json",
        "log": f"{home}/.openclaw/logs/dream-promoter.log",
    }


def log(msg):
    p = _paths()
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(p["log"]), exist_ok=True)
        with open(p["log"], "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --- candidate parsing -----------------------------------------------------
# A staged block looks EXACTLY like (real sample, 2026-06-12.md):
#
#   - Candidate: User: HEARTBEAT — 2026-06-11 01:31 (Thursday) box-local ...
#     - confidence: 0.58
#     - evidence: memory/.dreams/session-corpus/2026-06-11.txt:114-114
#     - recalls: 0
#     - status: staged
#
# confidence/recalls/status are optional sub-bullets; we default them. The text
# after "Candidate:" may itself contain colons, so we split only on the first.
_CAND_RE = re.compile(r"^- Candidate:\s*(.*)$")
_SUB_RE = re.compile(r"^\s+- (\w+):\s*(.*)$")


def parse_candidates(text):
    """Parse one light/ markdown file into a list of candidate dicts.

    Returns dicts: {text, confidence(float), recalls(int), evidence(str),
    status(str)}. Robust to missing sub-fields and to lines wrapping. Never
    raises on malformed input — best-effort, skips junk.
    """
    cands = []
    cur = None
    for raw in text.splitlines():
        m = _CAND_RE.match(raw)
        if m:
            if cur is not None:
                cands.append(cur)
            cur = {
                "text": m.group(1).strip(),
                "confidence": 0.0,
                "recalls": 0,
                "evidence": "",
                "status": "",
            }
            continue
        if cur is None:
            continue
        sm = _SUB_RE.match(raw)
        if not sm:
            continue
        key, val = sm.group(1).lower(), sm.group(2).strip()
        if key == "confidence":
            try:
                cur["confidence"] = float(val)
            except ValueError:
                pass
        elif key == "recalls":
            try:
                cur["recalls"] = int(val)
            except ValueError:
                pass
        elif key in ("evidence", "status"):
            cur[key] = val
    if cur is not None:
        cands.append(cur)
    return cands


def is_noise(text, ws_path=None):
    """Return True if *text* should never be promoted.

    Checks (in order):
      1. Too short (< MIN_CANDIDATE_CHARS).
      2. Matches a static noise pattern (SILENCE, HEARTBEAT header, etc.).
      3. Is a fragment of an injected doctrine file (HEARTBEAT.md, OPEN_LOOPS.md,
         PROJECT_DOCTRINE.md, AGENTS.md, SOUL.md, IDENTITY.md).

    ws_path is the workspace directory used to load doctrine files.  When
    omitted (e.g. from callers that don't have it), the doctrine check still
    runs structural patterns but skips the file-content overlap check.
    """
    if len(text) < MIN_CANDIDATE_CHARS:
        return True
    if any(p.search(text) for p in NOISE_PATTERNS):
        return True
    if _is_doctrine_boilerplate(text, ws_path or ""):
        return True
    return False


def normalize(text):
    """Lowercase, collapse whitespace, strip a leading speaker tag.

    Used for dedup and cross-day recurrence so 'User: foo' and 'foo' match.
    """
    t = re.sub(r"^\s*(user|assistant)\s*:\s*", "", text, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def percentile_threshold(values, pct):
    """Nearest-rank percentile of a list of floats. Empty -> +inf (nothing
    qualifies). A flat batch (all equal, the real 0.58 case) yields that same
    value, so the percentile rule alone promotes nothing — recurrence carries
    the night, exactly as intended."""
    if not values:
        return float("inf")
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return s[k]


# --- file discovery --------------------------------------------------------
_DATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


def light_files(light_dir, lookback_days):
    """Recent light/ candidate files, newest first, within lookback window."""
    try:
        names = os.listdir(light_dir)
    except OSError:
        return []
    dated = []
    for n in names:
        m = _DATE_FILE_RE.match(n)
        if not m:
            continue
        try:
            d = datetime.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        dated.append((d, n))
    dated.sort(reverse=True)
    if lookback_days:
        dated = dated[:lookback_days]
    return [(d, os.path.join(light_dir, n)) for d, n in dated]


def read_file(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# --- core selection --------------------------------------------------------
def select_promotions(light_dir, existing_memory, promoted_keys, ws_path=None):
    """Decide which candidates to promote tonight.

    Rule (conservative): from the MOST RECENT night's batch, take a candidate
    if EITHER it recurs on >= MIN_RECURRENCE_DAYS distinct days across the
    lookback window, OR its confidence is >= the 90th percentile of tonight's
    batch. Then drop noise, drop anything already in MEMORY.md, drop anything we
    already promoted, and cap at MAX_PROMOTIONS_PER_NIGHT (recurrence wins ties,
    then confidence, then longer text). Returns a list of selection dicts.

    ws_path: workspace directory that contains the doctrine files.  When None,
    it is inferred as the grandparent of light_dir (light_dir is
    <ws>/memory/dreaming/light, so ws = light_dir/../../..).
    """
    # Infer ws_path from light_dir if not supplied.
    if ws_path is None:
        ws_path = os.path.normpath(os.path.join(light_dir, "..", "..", ".."))

    # Invalidate the doctrine cache for this workspace so test setUp / tearDown
    # cycles (which use fresh tempdirs) always see fresh doctrine content.
    _doctrine_cache.pop(ws_path, None)

    files = light_files(light_dir, LOOKBACK_DAYS)
    if not files:
        return []
    # Tonight = newest file.
    tonight_date, tonight_path = files[0]
    tonight = parse_candidates(read_file(tonight_path))

    # Recurrence: count distinct DAYS each normalized text appears on, across
    # the whole lookback window (incl. tonight).
    days_by_norm = {}
    for d, path in files:
        try:
            cands = parse_candidates(read_file(path))
        except OSError:
            continue
        seen_today = set()
        for c in cands:
            norm = normalize(c["text"])
            if not norm or norm in seen_today:
                continue
            seen_today.add(norm)
            days_by_norm.setdefault(norm, set()).add(d)

    confidences = [c["confidence"] for c in tonight]
    pct_thresh = percentile_threshold(confidences, PERCENTILE)
    existing_norm = normalize_blob(existing_memory)

    selected = []
    seen_norm = set()
    for c in tonight:
        text = c["text"].strip()
        if is_noise(text, ws_path):
            continue
        norm = normalize(text)
        if not norm or norm in seen_norm:
            continue
        key = promotion_key(norm)
        if key in promoted_keys:
            continue
        if norm in existing_norm:          # already a durable memory
            continue
        recurrence = len(days_by_norm.get(norm, ()))
        meets_recurrence = recurrence >= MIN_RECURRENCE_DAYS
        # Strict '>' so a flat batch (everyone == the percentile value)
        # qualifies NOBODY on confidence alone; recurrence must carry it.
        meets_percentile = (
            c["confidence"] > pct_thresh
            or (pct_thresh != float("inf")
                and c["confidence"] >= pct_thresh
                and len(set(confidences)) > 1)
        )
        if not (meets_recurrence or meets_percentile):
            continue
        seen_norm.add(norm)
        selected.append({
            "text": text,
            "norm": norm,
            "key": key,
            "confidence": c["confidence"],
            "recurrence": recurrence,
            "evidence": c["evidence"],
            "source_date": tonight_date.isoformat(),
        })

    selected.sort(
        key=lambda s: (s["recurrence"], s["confidence"], len(s["text"])),
        reverse=True,
    )
    return selected[:MAX_PROMOTIONS_PER_NIGHT]


def normalize_blob(text):
    """Normalized whole-file blob of MEMORY.md for substring dedup."""
    return re.sub(r"\s+", " ", text).strip().lower()


def promotion_key(norm):
    """Stable dedup key for the state file (first 200 normalized chars)."""
    return norm[:200]


# --- MEMORY.md append (atomic, append-only) --------------------------------
def format_line(sel):
    text = sel["text"]
    if len(text) > MAX_PROMOTED_CHARS:
        text = text[:MAX_PROMOTED_CHARS - 1].rstrip() + "…"
    return f"- {text}  _(dreamt {sel['source_date']})_"


def build_appended_memory(existing, lines):
    """Return new MEMORY.md content = existing verbatim + our additions.

    If the promoter section already exists, append the new bullets under it
    (after the existing bullets). Otherwise add the section at the very end.
    Existing bytes above are never altered.
    """
    body = existing.rstrip("\n")
    if PROMOTER_SECTION in existing:
        # Append after the last line of the file (the section is promoter-owned
        # and lives at the end by construction).
        return body + "\n" + "\n".join(lines) + "\n"
    block = [PROMOTER_SECTION, "", PROMOTER_NOTE, ""] + lines
    return body + "\n\n" + "\n".join(block) + "\n"


def atomic_write(path, content):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".memory-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- state -----------------------------------------------------------------
def load_state(path):
    try:
        with open(path) as f:
            s = json.load(f)
        if not isinstance(s, dict):
            return {"promoted": []}
        s.setdefault("promoted", [])
        return s
    except (OSError, ValueError):
        return {"promoted": []}


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write(path, json.dumps(state, indent=2, ensure_ascii=False))


# --- main ------------------------------------------------------------------
def run(dry_run=False):
    p = _paths()

    # Hard safety gate: if MEMORY.md cannot be read, we ABORT without writing.
    try:
        existing = read_file(p["memory_md"])
    except OSError as e:
        log(f"ABORT: MEMORY.md unreadable ({e}); refusing to write.")
        return 0

    state = load_state(p["state"])
    promoted_keys = set(e.get("key") for e in state.get("promoted", []) if isinstance(e, dict))

    selected = select_promotions(p["light_dir"], existing, promoted_keys)
    if not selected:
        log("no candidates met the promotion bar tonight (0 promoted)")
        return 0

    lines = [format_line(s) for s in selected]

    if dry_run:
        log(f"DRY-RUN: would promote {len(selected)} candidate(s):")
        for s, line in zip(selected, lines):
            log(f"  recurrence={s['recurrence']} conf={s['confidence']:.2f} {line}")
        return 0

    new_content = build_appended_memory(existing, lines)
    try:
        atomic_write(p["memory_md"], new_content)
    except OSError as e:
        log(f"ABORT: failed to write MEMORY.md atomically ({e}); original intact.")
        return 0

    now_iso = datetime.datetime.now().isoformat(timespec="seconds")
    for s in selected:
        state["promoted"].append({
            "key": s["key"],
            "source_date": s["source_date"],
            "promoted_at": now_iso,
            "confidence": s["confidence"],
            "recurrence": s["recurrence"],
        })
    try:
        save_state(p["state"], state)
    except OSError as e:
        # MEMORY.md is already updated; a state-save failure only risks a
        # future re-promote, which the in-MEMORY dedup will then catch anyway.
        log(f"WARN: promoted {len(selected)} but state save failed ({e})")

    log(f"PROMOTED {len(selected)} candidate(s) into MEMORY.md:")
    for line in lines:
        log(f"  {line}")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in argv
    try:
        return run(dry_run=dry_run)
    except Exception as e:  # noqa: BLE001  (never let a memory-fixer crash loudly)
        log(f"UNEXPECTED ERROR (exiting 0 to protect MEMORY.md): {e!r}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
