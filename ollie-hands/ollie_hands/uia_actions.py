"""L1 — UI Automation actions + window management + clipboard.

The native-app rung: find a control by its accessibility properties and act on
it deterministically (no pixels). This is how ~90% of native UI is reached
without a vision model (plan §D5).

All functions raise RuntimeError off-Windows or when the target can't be
found, so the executor can map that to an on_fail decision.
"""

from __future__ import annotations

import concurrent.futures
import functools
import sys
import threading
import time

WINDOWS = sys.platform == "win32"

if WINDOWS:
    import uiautomation as uia  # type: ignore


# UIA is COM; comtypes needs CoInitialize per thread. Because the engine runs
# actions in a thread pool, we marshal every UIA tree call onto ONE dedicated
# COM-initialized worker thread (also serializes UIA access, which the API
# prefers). Win32 clipboard calls don't need this.
_executor: concurrent.futures.ThreadPoolExecutor | None = None
_elock = threading.Lock()


def _com_init():
    import comtypes  # bundled with uiautomation
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass


def _uia_pool() -> concurrent.futures.ThreadPoolExecutor:
    global _executor
    with _elock:
        if _executor is None:
            _executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="uia", initializer=_com_init)
        return _executor


def _on_uia_thread(fn):
    """Run a public UIA verb on the dedicated COM thread (do NOT decorate
    internal helpers it calls, or the single worker deadlocks on itself)."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        return _uia_pool().submit(fn, *a, **k).result()
    return wrapper


def _require():
    if not WINDOWS:
        raise RuntimeError("UIA actions only run on the Windows host")
    if uia is None:
        raise RuntimeError("uiautomation package not installed")


# ------------------------------------------------------------------ finding ---

def _matches(ctrl, name, control_type, automation_id) -> bool:
    try:
        if name and name.lower() not in (ctrl.Name or "").lower():
            return False
        if control_type and ctrl.ControlTypeName != control_type:
            return False
        if automation_id and (ctrl.AutomationId or "") != automation_id:
            return False
        return True
    except Exception:
        return False


def find(*, name: str = "", control_type: str = "", automation_id: str = "",
         window_title: str = "", timeout: float = 5.0):
    """Breadth-first search for the first matching control.

    Optionally scope to a top-level window by title (recommended — faster and
    avoids cross-window ambiguity). Waits up to ``timeout`` for it to appear.
    """
    _require()
    deadline = time.monotonic() + timeout
    while True:
        root = uia.GetRootControl()
        roots = []
        for win in root.GetChildren():
            try:
                if window_title and window_title.lower() not in (win.Name or "").lower():
                    continue
                roots.append(win)
            except Exception:
                continue
        # BFS each candidate root
        for start in (roots or [root]):
            queue = [(start, 0)]
            while queue:
                ctrl, depth = queue.pop(0)
                if _matches(ctrl, name, control_type, automation_id):
                    return ctrl
                if depth < 12:
                    try:
                        for child in ctrl.GetChildren():
                            queue.append((child, depth + 1))
                    except Exception:
                        continue
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"UIA element not found (name={name!r} type={control_type!r} "
                f"id={automation_id!r} window={window_title!r})")
        time.sleep(0.3)


def _describe(ctrl) -> dict:
    r = ctrl.BoundingRectangle
    return {
        "type": ctrl.ControlTypeName,
        "name": ctrl.Name or "",
        "automation_id": ctrl.AutomationId or "",
        "rect": [r.left, r.top, r.right, r.bottom],
    }


# ------------------------------------------------------------- grounding ---

# control types a human would click — preferred when ranking locate matches
_CLICKABLE = {
    "ButtonControl", "MenuItemControl", "HyperlinkControl", "TabItemControl",
    "ListItemControl", "CheckBoxControl", "RadioButtonControl",
    "SplitButtonControl", "TreeItemControl", "ComboBoxControl", "EditControl",
}


def _rank(name: str, ctype: str, rect, query: str) -> float:
    """Score a candidate for `locate`: exact/prefix/substring name + clickable
    type + on-screen size. Higher = better."""
    nm, q = (name or "").lower(), (query or "").lower()
    s = 0.0
    if q:
        if nm == q:
            s += 100
        elif nm.startswith(q):
            s += 50
        elif q in nm:
            s += 25
    if ctype in _CLICKABLE:
        s += 20
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    if w > 0 and h > 0:
        s += 5
    return s


@_on_uia_thread
def locate(*, name: str = "", query: str = "", control_type: str = "",
           window_title: str = "", max_candidates: int = 5) -> dict:
    """UIA-tier grounding: resolve a target to click-ready coordinates.

    Full-tree search (deeper than observe's snapshot), ranks matches, returns
    the best element's CENTER plus alternates. This is the deterministic, free
    ~90% path. When it finds nothing, returns found=False so the caller falls
    back to vision grounding (screenshot -> coords) via observe + pixels.
    """
    _require()
    q = query or name
    if not q and not control_type:
        return {"found": False, "method": "uia",
                "error": "give a query/name or control_type to locate"}

    scored: list = []
    root = uia.GetRootControl()
    starts = []
    for win in root.GetChildren():
        try:
            if window_title and window_title.lower() not in (win.Name or "").lower():
                continue
            starts.append(win)
        except Exception:
            continue

    for start in (starts or [root]):
        queue = [(start, 0)]
        while queue:
            ctrl, depth = queue.pop(0)
            try:
                nm, ct = ctrl.Name or "", ctrl.ControlTypeName
                hit = True
                if q and q.lower() not in nm.lower():
                    hit = False
                if control_type and ct != control_type:
                    hit = False
                if hit:
                    d = _describe(ctrl)
                    if (d["rect"][2] - d["rect"][0]) > 0 and (d["rect"][3] - d["rect"][1]) > 0:
                        scored.append((_rank(nm, ct, d["rect"], q), d))
                if depth < 12:
                    for child in ctrl.GetChildren():
                        queue.append((child, depth + 1))
            except Exception:
                continue

    scored.sort(key=lambda m: m[0], reverse=True)
    cands = []
    for _, d in scored[:max_candidates]:
        r = d["rect"]
        d["center"] = [(r[0] + r[2]) // 2, (r[1] + r[3]) // 2]
        cands.append(d)
    if not cands:
        return {"found": False, "method": "uia", "query": q,
                "hint": "no UIA element matched; fall back to vision grounding "
                        "(observe screenshot -> coords) then pixels"}
    best = cands[0]
    return {"found": True, "method": "uia", "query": q,
            "x": best["center"][0], "y": best["center"][1],
            "best": best, "candidates": cands}


# --------------------------------------------------------------- read verbs ---

@_on_uia_thread
def get_text(**find_kw) -> dict:
    ctrl = find(**find_kw)
    text = ""
    try:
        vp = ctrl.GetValuePattern()
        text = vp.Value
    except Exception:
        text = ctrl.Name or ""
    return {"text": text, "element": _describe(ctrl)}


# -------------------------------------------------------------- write verbs ---

@_on_uia_thread
def invoke(**find_kw) -> dict:
    """Click/activate a control via the most appropriate UIA pattern."""
    ctrl = find(**find_kw)
    ctrl.SetFocus()
    for getter in ("GetInvokePattern", "GetTogglePattern", "GetExpandCollapsePattern"):
        try:
            pat = getattr(ctrl, getter)()
            if getter == "GetInvokePattern":
                pat.Invoke()
            elif getter == "GetTogglePattern":
                pat.Toggle()
            else:
                pat.Expand()
            return {"invoked": True, "via": getter, "element": _describe(ctrl)}
        except Exception:
            continue
    # fallback: UIA-driven click at the element center (still not raw pixels)
    ctrl.Click(simulateMove=False)
    return {"invoked": True, "via": "Click", "element": _describe(ctrl)}


@_on_uia_thread
def set_value(value: str, **find_kw) -> dict:
    """Set a control's value (edit boxes, combo boxes) via the Value pattern."""
    ctrl = find(**find_kw)
    ctrl.SetFocus()
    try:
        ctrl.GetValuePattern().SetValue(value)
    except Exception:
        # fallback for controls without ValuePattern: select-all + type
        ctrl.SendKeys("{Ctrl}a", waitTime=0.05)
        ctrl.SendKeys(value, waitTime=0.0)
    return {"set": True, "element": _describe(ctrl)}


# ------------------------------------------------------------ window mgmt ----

def _window(title: str):
    _require()
    win = uia.WindowControl(searchDepth=1, SubName=title)
    if not win.Exists(maxSearchSeconds=5):
        # pane-hosted windows (some apps) fall back to a broader search
        win = uia.PaneControl(searchDepth=1, SubName=title)
        if not win.Exists(maxSearchSeconds=2):
            raise RuntimeError(f"window not found: {title!r}")
    return win


@_on_uia_thread
def type_text(value: str, *, _secret: bool = False, **find_kw) -> dict:
    """Enter text using three strategies in order of cleanliness:
    1) ValuePattern.SetValue — instant, exact (most edit boxes)
    2) clipboard paste (Ctrl+V) — robust for large/unicode/RichEdit
    3) SendKeys char-by-char — last resort

    When _secret=True (originates from a secret_ref), NEVER use the clipboard
    path (strategy 2). Type via ValuePattern.SetValue or per-char SendKeys.
    If neither works, fail (do not fall back to clipboard). The caller is
    responsible for ensuring 'value' is the resolved secret and for masking
    audit/return values.
    """
    ctrl = find(**find_kw)
    ctrl.SetFocus()
    # strategy 1 — preferred
    try:
        ctrl.GetValuePattern().SetValue(value)
        return {"typed": True, "via": "ValuePattern", "element": _describe(ctrl)}
    except Exception:
        pass

    if _secret:
        # CRITICAL-3: secret must never be left on the clipboard.
        # If ValuePattern fails, try direct SendKeys char-by-char; if that
        # also fails, raise so the engine can surface a refusal instead of
        # leaking via a clipboard fallback.
        try:
            ctrl.SendKeys(value, waitTime=0.0)
            return {"typed": True, "via": "SendKeys", "element": _describe(ctrl)}
        except Exception as e:
            raise RuntimeError(f"secret type failed without clipboard fallback: {e}") from e

    # Non-secret path: allow the clipboard paste fallback.
    try:
        prev = uia.GetClipboardText()
        uia.SetClipboardText(value)
        ctrl.SendKeys("{Ctrl}v", waitTime=0.05)
        if prev:
            uia.SetClipboardText(prev)  # restore the user's clipboard
        return {"typed": True, "via": "clipboard", "element": _describe(ctrl)}
    except Exception:
        pass
    # final fallback
    ctrl.SendKeys(value, waitTime=0.0)
    return {"typed": True, "via": "SendKeys", "element": _describe(ctrl)}


@_on_uia_thread
def window_op(op: str, title: str, *, x: int = 0, y: int = 0,
              width: int = 0, height: int = 0) -> dict:
    win = _window(title)
    op = op.lower()
    if op == "focus":
        win.SetActive(); win.SetFocus()
    elif op == "minimize":
        win.Minimize()
    elif op == "maximize":
        win.Maximize()
    elif op == "restore":
        win.Restore()
    elif op == "close":
        win.GetWindowPattern().Close()
    elif op == "move":
        win.MoveWindow(x, y, win.BoundingRectangle.width(),
                       win.BoundingRectangle.height())
    elif op == "resize":
        r = win.BoundingRectangle
        win.MoveWindow(r.left, r.top, width or r.width(), height or r.height())
    else:
        raise RuntimeError(f"unknown window op: {op}")
    return {"op": op, "window": win.Name}


# --------------------------------------------------------------- clipboard ---

def clipboard_read() -> dict:
    _require()
    return {"text": uia.GetClipboardText() or ""}


def clipboard_write(text: str) -> dict:
    _require()
    uia.SetClipboardText(text)
    return {"written": len(text)}
