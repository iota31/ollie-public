"""T0 read-only observation: screenshot + window list + UIA snapshot.

One `observe()` call = full situational awareness (plan §MCP tool surface).
Windows-only at runtime; imports are guarded so the package loads anywhere
(dev happens on macOS, execution on the box).

Grounding notes (plan §Windows reality):
- All coordinates are VIRTUAL-DESKTOP space (the union of all monitors,
  origin may be negative). Per-monitor DPI is reported so later phases can
  map UIA/pixel coords correctly on mixed-DPI setups.
- This module never actuates. No SendInput, no clicks, no keys.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import sys
import time

WINDOWS = sys.platform == "win32"

if WINDOWS:
    import mss  # type: ignore
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    try:
        import uiautomation as uia  # type: ignore
    except ImportError:  # UIA snapshot degrades gracefully
        uia = None


def _require_windows() -> None:
    if not WINDOWS:
        raise RuntimeError("ollie-hands observe() only runs on the Windows host")


# ---------------------------------------------------------------- windows ---

def window_list() -> list[dict]:
    """Visible top-level windows: hwnd, title, pid, process, rect, state."""
    _require_windows()
    results: list[dict] = []
    foreground = user32.GetForegroundWindow()

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def enum_cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        placement_minimized = bool(user32.IsIconic(hwnd))
        results.append({
            "hwnd": hwnd,
            "title": buf.value,
            "pid": pid.value,
            "process": _process_name(pid.value),
            "rect": [rect.left, rect.top, rect.right, rect.bottom],
            "minimized": placement_minimized,
            "foreground": hwnd == foreground,
        })
        return True

    user32.EnumWindows(enum_cb, 0)
    return results


def _process_name(pid: int) -> str:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wt.DWORD(260)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        kernel32.CloseHandle(h)


# --------------------------------------------------------------- monitors ---

def monitor_info() -> list[dict]:
    """Monitors in virtual-desktop coords + per-monitor DPI."""
    _require_windows()
    monitors: list[dict] = []

    MONITORINFOF_PRIMARY = 1

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                    ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
    def enum_cb(hmon, _hdc, _lprect, _lparam):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        dpi_x, dpi_y = wt.UINT(96), wt.UINT(96)
        try:  # Win 8.1+ shcore; falls back to 96 if unavailable
            ctypes.windll.shcore.GetDpiForMonitor(
                hmon, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        except (OSError, AttributeError):
            pass
        r = mi.rcMonitor
        monitors.append({
            "rect": [r.left, r.top, r.right, r.bottom],
            "primary": bool(mi.dwFlags & MONITORINFOF_PRIMARY),
            "dpi": dpi_x.value,
            "scale": round(dpi_x.value / 96.0, 2),
        })
        return True

    user32.EnumDisplayMonitors(None, None, enum_cb, 0)
    return monitors


def last_input_tick() -> int:
    """Tick (ms since boot) of the last system-wide keyboard/mouse input.

    Increases only when input occurs. In Phase 1/2 the engine actuates via
    UIA patterns + shell (which do NOT synthesise input events), so a change
    in this value means a HUMAN touched the box — the basis for collision
    auto-pause. (When L3 SendInput lands, the engine must record its own
    injected-input ticks to avoid self-collision.)
    """
    _require_windows()

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    user32.GetLastInputInfo(ctypes.byref(info))
    return int(info.dwTime)


def session_state() -> dict:
    """Is the interactive session usable (not locked / not black)?"""
    _require_windows()
    # OpenInputDesktop fails when the secure/lock desktop is active.
    DESKTOP_SWITCHDESKTOP = 0x0100
    h = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    locked = not bool(h)
    if h:
        user32.CloseDesktop(h)
    return {"locked": locked}


# -------------------------------------------------------------- screenshot ---

def screenshot_png(save_path: str | None = None) -> tuple[bytes, dict]:
    """Full virtual-desktop screenshot as PNG bytes (+ geometry).

    mss (BitBlt/DXGI-backed) is the Phase-0 capture; per-monitor capture and
    region diffs come with the executor phases.
    """
    _require_windows()
    with mss.mss() as sct:
        mon = sct.monitors[0]  # 0 = full virtual desktop
        raw = sct.grab(mon)
        png = mss.tools.to_png(raw.rgb, raw.size)
        if save_path:
            with open(save_path, "wb") as f:
                f.write(png)
        geometry = {"left": mon["left"], "top": mon["top"],
                    "width": mon["width"], "height": mon["height"]}
    return png, geometry


# ------------------------------------------------------------ UIA snapshot ---

def uia_snapshot(max_windows: int, max_depth: int, max_children: int) -> list[dict]:
    """Shallow UIA tree of visible top-level windows.

    Bounded on purpose: enough for the brain to ground "which window / which
    control", small enough to stay fast. Deeper queries come in Phase 1
    (`uia_find` etc.).
    """
    _require_windows()
    if uia is None:
        return [{"error": "uiautomation package not installed"}]

    def node(ctrl, depth: int) -> dict:
        info = {
            "type": ctrl.ControlTypeName,
            "name": (ctrl.Name or "")[:120],
            "automation_id": ctrl.AutomationId or "",
            "rect": [ctrl.BoundingRectangle.left, ctrl.BoundingRectangle.top,
                     ctrl.BoundingRectangle.right, ctrl.BoundingRectangle.bottom],
        }
        if depth < max_depth:
            children = []
            for child in ctrl.GetChildren()[:max_children]:
                try:
                    children.append(node(child, depth + 1))
                except Exception:  # stale element mid-walk: skip, don't die
                    continue
            if children:
                info["children"] = children
        return info

    snapshot = []
    root = uia.GetRootControl()
    for win in root.GetChildren()[:max_windows]:
        try:
            if win.ControlTypeName in ("WindowControl", "PaneControl") and win.Name:
                snapshot.append(node(win, 1))
        except Exception:
            continue
    return snapshot


# ----------------------------------------------------------------- observe ---

def observe(cfg, audit) -> dict:
    """The one-call situational-awareness read (T0).

    A screenshot needs an attached, rendering desktop; the window list + UIA
    snapshot do NOT. So a screen-capture failure (engine session disconnected
    because RDP took/left the console, or the lock/secure desktop is up) must
    NOT blind the engine — we still return windows + UIA, with a clear
    `screenshot_status` explaining why pixels are missing.
    """
    _require_windows()
    t0 = time.monotonic()
    shot_path: str | None = str(audit.shot_path())
    screenshot_b64: str | None = None
    geometry = None
    screenshot_status = "ok"
    try:
        png, geometry = screenshot_png(save_path=shot_path)
        screenshot_b64 = base64.b64encode(png).decode("ascii")
    except Exception as e:
        if session_state().get("locked"):
            reason = "screen locked / secure desktop active"
        else:
            reason = ("screen unavailable: engine session not attached to a "
                      "rendering desktop (RDP active or session disconnected)")
        screenshot_status = f"{reason} [{type(e).__name__}: {str(e)[:120]}]"
        shot_path = None

    result = {
        "session": session_state(),
        "monitors": monitor_info(),
        "virtual_desktop": geometry,
        "windows": window_list(),
        "uia": uia_snapshot(cfg.uia_max_windows, cfg.uia_max_depth,
                            cfg.uia_max_children),
        "screenshot_b64": screenshot_b64,
        "screenshot_status": screenshot_status,
        "screenshot_path": shot_path,
    }
    audit.event("observe", status="ok" if screenshot_b64 else "degraded",
                duration_ms=int((time.monotonic() - t0) * 1000),
                screenshot=shot_path,
                detail="" if screenshot_b64 else screenshot_status[:200])
    return result
