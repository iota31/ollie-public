"""Pre/postcondition checkers (plan §executor semantics).

This is the structural cure for the POC's worst bug — "acted on ambient
state" (typed into a stale Notepad). Every step asserts the world matches its
expectation BEFORE acting (preconditions), and every write asserts the world
changed as intended AFTER acting (postcondition). No condition can be
satisfied by injected screen text — they query the OS/UIA tree, not pixels or
model output.

Each checker returns (ok: bool, detail: str).

Precondition types:
  foreground     {process?, title?}   foreground window matches
  window_exists  {title}              a top-level window title contains <title>
  window_absent  {title}              no such window
  uia_exists     {name?,control_type?,automation_id?,window_title?}
  uia_absent     {...same...}
  file_exists    {path}
  file_absent    {path}

Postcondition types (superset — also usable as preconditions):
  uia_text       {..locator.., equals? | contains?}
  uia_exists / window_exists / file_exists
  shell_exit_zero                    the step's own shell exit code == 0
"""

from __future__ import annotations

import os
import sys

WINDOWS = sys.platform == "win32"


def _foreground(spec: dict) -> tuple[bool, str]:
    from . import observe as obs
    wins = obs.window_list()
    fg = next((w for w in wins if w.get("foreground")), None)
    if not fg:
        return False, "no foreground window"
    proc = (spec.get("process") or "").lower()
    title = (spec.get("title") or "").lower()
    if proc and proc not in (fg.get("process", "").lower()):
        return False, f"foreground is {fg.get('process')}, expected {proc}"
    if title and title not in (fg.get("title", "").lower()):
        return False, f"foreground title {fg.get('title')!r} lacks {title!r}"
    return True, f"foreground={fg.get('process')}:{fg.get('title')[:40]}"


def _window_present(title: str) -> bool:
    from . import observe as obs
    t = title.lower()
    return any(t in (w.get("title", "").lower()) for w in obs.window_list())


def _uia_present(spec: dict) -> bool:
    from . import uia_actions as L1
    find_kw = {k: spec[k] for k in ("name", "control_type", "automation_id",
                                    "window_title") if spec.get(k)}
    try:
        L1.find(timeout=spec.get("timeout", 2.0), **find_kw)
        return True
    except Exception:
        return False


def _uia_text(spec: dict) -> tuple[bool, str]:
    from . import uia_actions as L1
    find_kw = {k: spec[k] for k in ("name", "control_type", "automation_id",
                                    "window_title") if spec.get(k)}
    try:
        res = L1.get_text(**find_kw)
    except Exception as e:
        return False, f"element not found: {e}"
    text = res.get("text", "")
    if "equals" in spec:
        return (text == spec["equals"],
                f"text={text!r} equals expected={spec['equals']!r}")
    if "contains" in spec:
        return (spec["contains"] in text,
                f"text={text!r} contains {spec['contains']!r}")
    return True, f"text={text!r} (existence only)"


def check(cond: dict, *, last_shell_exit: int | None = None) -> tuple[bool, str]:
    """Evaluate one condition dict. Used for both pre- and postconditions."""
    if not WINDOWS:
        raise RuntimeError("conditions evaluate only on the Windows host")
    ctype = (cond.get("type") or "").lower()

    if ctype == "foreground":
        return _foreground(cond)
    if ctype == "window_exists":
        ok = _window_present(cond.get("title", ""))
        return ok, f"window_exists({cond.get('title')!r})={ok}"
    if ctype == "window_absent":
        ok = not _window_present(cond.get("title", ""))
        return ok, f"window_absent({cond.get('title')!r})={ok}"
    if ctype == "uia_exists":
        ok = _uia_present(cond)
        return ok, f"uia_exists={ok}"
    if ctype == "uia_absent":
        ok = not _uia_present(cond)
        return ok, f"uia_absent={ok}"
    if ctype == "uia_text":
        return _uia_text(cond)
    if ctype == "file_exists":
        ok = os.path.exists(os.path.expandvars(cond.get("path", "")))
        return ok, f"file_exists({cond.get('path')!r})={ok}"
    if ctype == "file_absent":
        ok = not os.path.exists(os.path.expandvars(cond.get("path", "")))
        return ok, f"file_absent({cond.get('path')!r})={ok}"
    if ctype == "shell_exit_zero":
        ok = last_shell_exit == 0
        return ok, f"shell_exit={last_shell_exit}"
    if ctype == "web_url":
        from . import browser as L2
        url = (L2.status().get("url") or "")
        want = cond.get("contains", "")
        return (want in url, f"url={url!r} contains {want!r}")
    if ctype == "web_text":
        from . import browser as L2
        text = L2.extract(cond.get("selector", "")).get("text", "")
        if "equals" in cond:
            return (text.strip() == cond["equals"], f"web_text equals check")
        want = cond.get("contains", "")
        return (want in text, f"web_text contains {want!r}")
    if ctype == "web_property":
        from . import browser as L2
        result = L2.property_matches(
            cond.get("selector", ""), cond.get("property", "value"),
            equals=cond.get("equals") if "equals" in cond else None,
            contains=cond.get("contains") if "contains" in cond else None,
            nonempty=bool(cond.get("nonempty")),
        )
        return result["matched"], (
            f"web_property({result['selector']!r}, {result['property']!r}) "
            f"matched={result['matched']}"
        )

    return False, f"unknown condition type {ctype!r}"


def check_all(conds: list, *, last_shell_exit: int | None = None) -> tuple[bool, str]:
    for cond in conds:
        ok, detail = check(cond, last_shell_exit=last_shell_exit)
        if not ok:
            return False, detail
    return True, "all preconditions met"
