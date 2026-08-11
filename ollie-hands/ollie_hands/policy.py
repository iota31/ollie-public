"""Scope-tier policy engine — the HARD gate (plan §Policy).

In-code, LLM-uneditable. The brain cannot widen scope: it only submits
actions; this module decides the tier + consent class. Config may *narrow*
(future T2 app list) but never override the deny/confirm sets below.

Tiers (this box is a dedicated spare → local actions are Ollie-scope):
  T0 read     reads/observe/get_text/clipboard_read            -> auto
  T1/T2 local local mutations (shell writes, UIA acts, windows) -> notify
  T3 acts-as-Tushar / high-blast-radius local destructive      -> confirm
  T4 forbidden security tamper, audit/policy/vault tamper       -> blocked

Consent classes:
  auto    proceed silently (still audited)
  notify  proceed + narrate to owner (Telegram, one-way)
  confirm block until owner approves (deny on timeout)
  blocked refuse outright
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import vault as Vault

AUTO = "auto"
NOTIFY = "notify"
CONFIRM = "confirm"
BLOCKED = "blocked"

# Effect categories for scoped authorization (used by grants.py)
EFFECT_CATEGORIES = frozenset({
    "observe",
    "navigation",
    "session_preference",
    "draft",
    "progress",
    "external_commit",
    "identity_commit",
    "destructive",
})

# Effects that consume a single-use commit grant
COMMIT_EFFECTS = frozenset({
    "external_commit",
    "identity_commit",
    "destructive",
})


# Runtime-settable (derived from cfg at startup) so shell classification can
# block relative reads when cwd is under the audit dir (supports audit-dir
# relocation without hardcoded strings).
_AUDIT_BLOCK_DIR: str | None = None


def set_blocked_dirs(*, audit_dir: str | None = None) -> None:
    """Configure directories that shell commands must not target (even relatively).
    Call once at engine init with cfg.audit_dir (vault dir is taken from vault.VAULT_DIR)."""
    global _AUDIT_BLOCK_DIR
    if audit_dir:
        _AUDIT_BLOCK_DIR = str(audit_dir).lower().rstrip("\\/")


def _norm_path(p: str) -> str:
    """OS-independent path canonicalization for prefix comparison: unify
    separators, lowercase, and resolve '.'/'..' segments. Deliberately NOT
    os.path (which is platform-specific) so the same logic holds on the Windows
    host and in cross-platform tests. Closes the `/` vs `\\` and `..` bypasses."""
    parts: list[str] = []
    for seg in str(p).replace("/", "\\").lower().split("\\"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "\\".join(parts)


def _is_under_blocked_dir(p: str | None) -> bool:
    if not p:
        return False
    s = _norm_path(p)
    blocked = [_norm_path(str(Vault.VAULT_DIR))]
    if _AUDIT_BLOCK_DIR:
        blocked.append(_norm_path(_AUDIT_BLOCK_DIR))
    for b in blocked:
        if not b:
            continue
        if s == b or s.startswith(b + "\\"):
            return True
    return False


@dataclass
class Decision:
    tier: str
    consent: str
    reason: str

    def to_dict(self) -> dict:
        return {"tier": self.tier, "consent": self.consent, "reason": self.reason}


# --- T4 forbidden: blocked outright, no consent path (security/integrity) ---
_BLOCKED = [
    (r"set-mppreference|add-mppreference|mpcmdrun|disable.{0,12}defender",
     "tampering with Microsoft Defender"),
    (r"stop-service.{0,40}(windefend|mpssvc|sense|securityhealthservice)",
     "stopping a security service"),
    (r"set-netfirewallprofile.{0,40}-enabled\s+(\$?false|0)|"
     r"netsh\s+advfirewall\s+set\s+\w+\s+state\s+off|disable-netfirewallrule",
     "disabling the firewall"),
    (r"disable-bitlocker|manage-bde.{0,40}(-off|-disable)",
     "disabling BitLocker"),
    (r"ollie-hands[\\/]+audit|[\\/]audit[\\/].{0,40}\.jsonl",
     "touching the audit trail"),
    (r"programdata[\\/]+ollie-hands[\\/]+(config\.json|bearer\.token|policy)",
     "editing the engine config/policy/token"),
    (r"ollie-hands[\\/]+vault|credential\s*manager|dpapi|protecteddata|unprotect|cryptunprotectdata",
     "accessing the secret vault or DPAPI"),
    # P0-C: the approval token is the other half of the authority split. If the
    # shell principal can read it, an action can authorize itself.
    (r"programdata[\\/]+ollie-hands[\\/]+approval\.token",
     "reading the approval token (authority-split credential)"),
]

# --- P0-B: commands that LOOK like reads but accept code-executing arguments.
# These verbs are in _READ_TOKENS for their pure-read forms, so this list must
# be consulted BEFORE the read-token check or the whole command reads as T0.
_DANGEROUS_READ_ARGS = [
    (r"\bwmic\b.{0,40}\bprocess\b.{0,20}\bcall\b.{0,20}\bcreate\b",
     "wmic process call create (launches process)"),
    (r"\becho\b.{0,20}\$\(", "echo with subexpression (command execution)"),
    (r"\bwhere\.exe\b.{0,20}/r\b",
     "where.exe /R (arbitrary path search + execute)"),
]

# --- T3 confirm: high blast radius / destructive — owner must approve ---
_CONFIRM = [
    # disk format only — NOT Format-List/Format-Table/Format-* read cmdlets
    (r"format-volume\b|\bformat\s+[a-z]:", "formatting a volume"),
    (r"remove-item.{0,80}-recurse.{0,40}-force|"
     r"remove-item.{0,40}-force.{0,80}-recurse",
     "recursive force delete"),
    (r"\b(rd|rmdir)\b.{0,40}/s|\bdel\b.{0,40}/s", "recursive directory delete"),
    (r"shutdown|restart-computer|stop-computer", "shutting down / restarting"),
    (r"reg\s+delete\s+(hklm|hkey_local_machine)", "deleting an HKLM registry key"),
    (r"diskpart|bcdedit|cipher\s+/w", "low-level disk/boot operation"),
    (r"stop-process\b(?!.{0,40}-id\s)", "broad process kill"),
]

# --- T0 auto: pure-read shells (must be the WHOLE command, no chaining) ---
_READ_TOKENS = (
    r"get-[\w]+|gci|gc|gp|gm|gsv|gps|"
    r"dir|ls|type|cat|whoami|hostname|systeminfo|tasklist|query|"
    r"test-path|select-string|findstr|measure-[\w]+|"
    r"where\.exe|wmic|echo|write-host|write-output|more|"
    r"resolve-path|split-path|join-path|out-string|format-[\w]+|"
    r"select-object|sort-object|where-object|"
    r"convertto-json|convertfrom-json|"
    r"get-volume|get-psdrive|get-disk|get-computerinfo"
)
# operators that can turn a "read" into a write/side-effect
_WRITE_OPS = re.compile(r">|>>|\bset-|\bnew-|\bremove-|\brename-|\bmove-|"
                        r"\bcopy-|\bstart-|\bstop-|\binvoke-|\bclear-|"
                        r"\bout-file|\badd-content|\bset-content|\b&\b", re.I)

# Shell operations that write to a service or publish local state outside the
# box.  This is deliberately only an escalation list: matching can never make
# an action less restrictive.  Unknown shell mutations are handled fail-closed
# below unless the caller supplies an explicit local effect envelope.
_EXTERNAL_SHELL_COMMIT = re.compile(
    r"\b(send-mailmessage|git\s+push|gh\s+(pr|issue|release)\s+create|"
    r"gh\s+(pr|issue)\s+comment|curl(?:\.exe)?\b.{0,160}(--request|-x)\s*"
    r"(post|put|patch|delete)|invoke-(restmethod|webrequest)\b.{0,160}"
    r"-method\s+(post|put|patch|delete))\b", re.I)


def _effect(effect: dict | None) -> tuple[str | None, bool, bool]:
    """Return (scope_or_category, commit, valid) for the typed consequence envelope.

    Supports two envelope formats (narrow fail-closed gate):
      Legacy: {"scope": "local" | "external" | "identity", "commit": bool}
      Current: {"category": "<EFFECT_CATEGORIES member>"}

    Mixing legacy and current keys, or providing unknown keys, fails closed
    (returns valid=False so callers classify as CONFIRM).  Missing metadata
    is distinct from a low-risk declaration.  Category effects are mapped so
    that COMMIT_EFFECTS categories return commit=True for the caller.
    """
    if effect is None:
        return None, False, True
    if not isinstance(effect, dict):
        return None, False, False
    # Current category-based envelope (grants/actscript)
    category = effect.get("category")
    if category is not None:
        # Reject mixed/unknown envelope: if any other known keys are present
        # alongside category, fail closed (caller classifies as CONFIRM).
        other_known = {k for k in effect if k != "category"}
        if other_known:
            return None, False, False
        if category not in EFFECT_CATEGORIES:
            return None, False, False
        return category, category in COMMIT_EFFECTS, True
    # Legacy scope/commit envelope
    scope = effect.get("scope")
    commit = effect.get("commit", False)
    # Reject mixed/unknown envelope: only "scope" and "commit" are permitted.
    other_known = {k for k in effect if k not in {"scope", "commit"}}
    if other_known:
        return None, False, False
    if scope not in {"local", "external", "identity"} or not isinstance(commit, bool):
        return None, False, False
    return scope, commit, True


def _norm(cmd: str) -> str:
    return re.sub(r"\s+", " ", cmd.strip().lower())


def classify_shell(command: str, *, cwd: str | None = None,
                   effect: dict | None = None,
                   commit: bool = False) -> Decision:
    c = _norm(command)
    for pat, why in _BLOCKED:
        if re.search(pat, c, re.I):
            return Decision("T4", BLOCKED, why)
    for pat, why in _CONFIRM:
        if re.search(pat, c, re.I):
            return Decision("T3", CONFIRM, why)
    scope, effect_commit, valid_effect = _effect(effect)
    if not valid_effect:
        return Decision("T3", CONFIRM, "invalid effect envelope")
    if commit or effect_commit or scope in {"external", "identity"}:
        return Decision("T3", CONFIRM, "shell external/identity commit")
    if _EXTERNAL_SHELL_COMMIT.search(c):
        return Decision("T3", CONFIRM, "shell writes to an external service")

    # cwd-aware block: relative reads under vault or (relocatable) audit dir are T4.
    # Absolute patterns above already catch explicit vault/audit paths; this catches
    # the case of cwd set to the protected dir and then a bare filename read.
    if _is_under_blocked_dir(cwd):
        # any shell command whose cwd is under a blocked dir is refused outright
        return Decision("T4", BLOCKED, "shell cwd under protected dir (vault/audit)")

    # P0-B hardening: block dangerous argument patterns in read-listed commands
    # BEFORE the general read-token check.  These commands are in _READ_TOKENS
    # for their pure-read forms but accept arguments that execute code, so an
    # otherwise-read-looking command would otherwise be classified T0 auto.
    # Matched case-insensitively: _norm() lowercases today, but the flag keeps
    # a literal like "/R" matching if that ever changes.
    for pat, why in _DANGEROUS_READ_ARGS:
        if re.search(pat, c, re.I):
            return Decision("T3", CONFIRM, why)

    # pure read: every segment (split on ; | ) is a read verb, no write ops
    if not _WRITE_OPS.search(c):
        segments = re.split(r"[;|]", c)
        read_re = re.compile(rf"^\(*\s*({_READ_TOKENS})\b", re.I)
        if segments and all(read_re.match(s.strip()) for s in segments if s.strip()):
            return Decision("T0", AUTO, "read-only shell")
    # A shell mutation's consequence cannot be inferred in the general case.
    # Caller-supplied effect metadata may escalate, but must never downgrade
    # arbitrary shell to an unattended tier.
    return Decision("T3", CONFIRM, "shell mutation with undeclared consequence")


# UIA / window / clipboard ops that only read state
_READ_OPS = {"get_text", "find", "exists", "read", "list", "snapshot",
             "locate", "locate_vision"}

# --- L2 browser (Camoufox) verbs --------------------------------------------
_BROWSER_READ = {"goto", "extract", "links", "screenshot", "get_attr",
                 "property_matches",
                 "status", "element_text"}
_BROWSER_INTERACT = {"click", "fill", "type_text", "press", "select"}
# text on a button/link that means "this acts as Tushar in the world" -> confirm
_COMMIT_WORDS = re.compile(
    r"\b(send|post|publish|tweet|repl(y|ies)|buy|order|checkout|pay|purchase|"
    r"place\s+order|connect|apply|submit|confirm|delete|remove|transfer|"
    r"book|reserve|subscribe|donate|bid|"
    r"sign\s*up|signup|sign\s+in|log\s*in|register|create\s+account|join)\b",
    re.I)


def classify_browser(op: str, *, commit: bool = False,
                     target_text: str = "", effect: dict | None = None,
                     key: str = "") -> Decision:
    """L2 consent. Reads = notify (browsing his logged-in session, narrated).
    Interactions = notify, UNLESS they commit (act as Tushar) -> confirm.
    `commit` is the planner's explicit flag; `target_text` is the resolved
    button/link text the engine checks regardless of the planner (defense in
    depth — a hijacked planner can't dodge confirm by omitting the flag)."""
    op = (op or "").lower()
    eff, effect_commit, valid_effect = _effect(effect)
    if not valid_effect:
        return Decision("T3", CONFIRM, "invalid effect envelope")
    if commit or effect_commit or eff in {"external", "identity"}:
        return Decision("T3", CONFIRM, "browser external/identity commit")
    if op in _BROWSER_READ:
        return Decision("T2", NOTIFY, f"browser read: {op} (narrated)")
    if op in _BROWSER_INTERACT:
        if op == "press" and (key or "").lower() in {"enter", "return", "numpadenter"}:
            return Decision("T3", CONFIRM, "browser Enter/Return may commit")
        if _COMMIT_WORDS.search(target_text or ""):
            return Decision("T3", CONFIRM,
                            f"browser commit (acts as Tushar): {op}")
        # Typing/filling cannot itself submit.  Click/select/press/fill/type_text
        # can accumulate consequential state (e.g., form data for signup), so an
        # omitted consequence declaration fails closed for all of them.  This is
        # the narrow engine-owned gate: progressive form filling via bare act()
        # calls is rejected; only plan_submit with explicit effects can proceed.
        if op in {"click", "select", "press", "fill", "type_text"}:
            if eff is None:
                return Decision("T3", CONFIRM,
                                f"browser {op} with undeclared consequence")
            # Allowed categories per op (narrow, engine-owned allowlist).
            # Only these categories permit the op at NOTIFY tier.
            allowed = {
                "fill": {"draft"},
                "type_text": {"draft"},
                "click": {"navigation", "session_preference", "progress"},
                "select": {"session_preference", "draft", "progress"},
                "press": {"navigation", "session_preference", "draft", "progress"},
            }
            # Strings are the canonical category values; no named constants.
            if eff not in allowed[op]:
                return Decision("T3", CONFIRM,
                                f"browser {op} incompatible with effect {eff}")
            return Decision("T2", NOTIFY,
                            f"browser {eff}: {op} (narrated)")
        return Decision("T2", NOTIFY, f"browser interact: {op} (narrated)")
    if op == "submit":
        return Decision("T3", CONFIRM, "browser submit (acts as Tushar)")
    return Decision("T3", CONFIRM, f"unclassified browser op '{op}'")


def classify_action(kind: str, op: str = "", *, command: str = "",
                    external: bool = False, cwd: str | None = None,
                    commit: bool = False, effect: dict | None = None,
                    key: str = "") -> Decision:
    """Top-level dispatcher used by the executor for every step."""
    kind = (kind or "").lower()
    op = (op or "").lower()

    scope, effect_commit, valid_effect = _effect(effect)
    if not valid_effect:
        return Decision("T3", CONFIRM, "invalid effect envelope")
    if external or commit or effect_commit or scope in {"external", "identity"}:
        return Decision("T3", CONFIRM, "acts as Tushar (external/identity)")

    if kind == "shell":
        return classify_shell(command, cwd=cwd, effect=effect, commit=commit)

    if kind in ("observe", "clipboard_read"):
        return Decision("T0", AUTO, "read-only")

    if kind in ("uia", "window", "clipboard", "clipboard_write"):
        if op in _READ_OPS:
            return Decision("T0", AUTO, "read-only UI query")
        return Decision("T3", CONFIRM,
                        f"{kind} mutation with undeclared consequence")

    if kind == "pixels":  # L3 raw mouse/keyboard injection — local, narrated
        if op in ("cursor_pos", "read"):
            return Decision("T0", AUTO, "read-only cursor query")
        if op == "move":
            return Decision("T2", NOTIFY, "local cursor move (narrated)")
        if op == "key" and (key or "").lower() in {"enter", "return", "numpadenter"}:
            return Decision("T3", CONFIRM, "pixel Enter/Return may commit")
        return Decision("T3", CONFIRM,
                        f"pixels {op or '?'} with undeclared consequence")

    if kind == "captcha":
        # External solve service. Narrated by default (credits, ToS surface).
        # If the planner marks commit=true, force confirm (rare, for high-stakes solves).
        if commit:
            return Decision("T3", CONFIRM, "captcha solve for commit action (acts as Tushar)")
        return Decision("T2", NOTIFY, "captcha solve (external service, narrated)")

    # unknown kind: safest is to require confirmation
    return Decision("T3", CONFIRM, f"unclassified action kind '{kind}'")
