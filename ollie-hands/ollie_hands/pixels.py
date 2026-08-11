"""L3 — pixel/coordinate actuation via SendInput (capability ladder, last rung).

Below UIA/DOM and used only when there is no element to target: raw mouse +
keyboard for canvas / custom-drawn / game-like controls. Coordinates are
VIRTUAL-DESKTOP pixels (the same space `observe()` reports), mapped to
SendInput's 0..65535 absolute range over the whole virtual desktop.

Self-collision: GetLastInputInfo (used by the executor to detect a human
touching the box) ALSO ticks when WE inject. So after every injection we record
that tick; `last_injected_tick()` lets the executor tell Ollie's own input
apart from a human's.

No COM, so unlike UIA/browser these calls are thread-agnostic (safe from the
act/executor worker thread).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time

WINDOWS = sys.platform == "win32"
if WINDOWS:
    user32 = ctypes.windll.user32

# --- virtual-desktop metrics -------------------------------------------------
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

# --- SendInput constants -----------------------------------------------------
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
WHEEL_DELTA = 120
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

ULONG_PTR = ctypes.POINTER(wt.ULONG)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTunion)]


def _require_windows() -> None:
    if not WINDOWS:
        raise RuntimeError("ollie-hands pixels (L3) only runs on the Windows host")


_last_injected_tick = 0


def last_injected_tick() -> int:
    """Tick of the engine's most recent SendInput (for collision detection)."""
    return _last_injected_tick


def _send(inputs: list) -> None:
    global _last_injected_tick
    _require_windows()
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        raise RuntimeError(f"SendInput injected {sent}/{n} events "
                           f"(blocked? secure desktop?)")
    from . import observe as obs
    _last_injected_tick = obs.last_input_tick()


# --- coordinate mapping ------------------------------------------------------

def _to_abs(x: int, y: int) -> tuple[int, int]:
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    nx = int(round((x - vx) * 65535 / max(vw - 1, 1)))
    ny = int(round((y - vy) * 65535 / max(vh - 1, 1)))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.u.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, None)
    return inp


def cursor_pos() -> dict:
    _require_windows()
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return {"x": pt.x, "y": pt.y}


# --- mouse verbs -------------------------------------------------------------

_MOVE_ABS = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
_BTN = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def move(x: int, y: int) -> dict:
    nx, ny = _to_abs(int(x), int(y))
    _send([_mouse(_MOVE_ABS, nx, ny)])
    return {"moved": [int(x), int(y)], "cursor": cursor_pos()}


def click(x: int | None = None, y: int | None = None, *,
          button: str = "left", double: bool = False) -> dict:
    if x is not None and y is not None:
        move(int(x), int(y))
        time.sleep(0.02)
    down, up = _BTN.get(button, _BTN["left"])
    seq = [_mouse(down), _mouse(up)]
    if double:
        seq += [_mouse(down), _mouse(up)]
    _send(seq)
    return {"clicked": button, "double": double,
            "at": [x, y] if x is not None else "current"}


def drag(x1: int, y1: int, x2: int, y2: int, *,
         button: str = "left", steps: int = 12) -> dict:
    """Press at (x1,y1), glide to (x2,y2) in steps, release — i.e. drag-select.

    Stepped movement (not a teleport) so apps register a real drag/selection.
    """
    down, up = _BTN.get(button, _BTN["left"])
    move(int(x1), int(y1))
    time.sleep(0.03)
    _send([_mouse(down)])
    time.sleep(0.03)
    steps = max(1, int(steps))
    for i in range(1, steps + 1):
        ix = int(round(x1 + (x2 - x1) * i / steps))
        iy = int(round(y1 + (y2 - y1) * i / steps))
        nx, ny = _to_abs(ix, iy)
        _send([_mouse(_MOVE_ABS, nx, ny)])
        time.sleep(0.012)
    _send([_mouse(up)])
    return {"dragged": [[int(x1), int(y1)], [int(x2), int(y2)]], "button": button}


def scroll(amount: int, *, horizontal: bool = False) -> dict:
    """Wheel scroll; amount is in notches (positive=up/right)."""
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    _send([_mouse(flag, data=int(amount) * WHEEL_DELTA)])
    return {"scrolled": int(amount), "horizontal": horizontal}


# --- keyboard verbs ----------------------------------------------------------

_VK = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
}


def _vk(name: str) -> int:
    n = name.strip().lower()
    if n in _VK:
        return _VK[n]
    if len(n) == 1:
        return ord(n.upper())
    raise ValueError(f"unknown key {name!r}")


def _key_vk(vk: int, up: bool = False) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)
    return inp


def _key_unicode(ch: str, up: bool = False) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    inp.u.ki = KEYBDINPUT(0, ord(ch), flags, 0, None)
    return inp


def type_text(text: str, *, _secret: bool = False) -> dict:
    """Type a literal string as Unicode keystrokes (layout-independent).

    When _secret=True (originates from a secret_ref), we MUST NOT use any
    clipboard path. We type via per-char SendInput only. The caller (engine)
    is responsible for ensuring 'text' is the resolved secret and for masking
    audit/return values; this function just ensures the input path is direct.
    """
    seq: list = []
    for ch in str(text):
        seq.append(_key_unicode(ch, up=False))
        seq.append(_key_unicode(ch, up=True))
    if seq:
        _send(seq)
    # For secrets we intentionally do not advertise a typed_len in the
    # higher-level result (engine masks the return path); keep it here for
    # internal use only (not surfaced for secret steps).
    return {"typed_len": len(str(text))}


def key(combo: str) -> dict:
    """Press a key chord like 'ctrl+a', 'alt+f4', 'enter'."""
    parts = [p for p in str(combo).split("+") if p.strip()]
    vks = [_vk(p) for p in parts]
    seq = [_key_vk(v, up=False) for v in vks]
    seq += [_key_vk(v, up=True) for v in reversed(vks)]
    _send(seq)
    return {"key": combo}
