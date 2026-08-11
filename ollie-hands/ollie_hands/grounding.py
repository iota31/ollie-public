"""Tiered grounding: target description -> click-ready coordinates (plan D5).

Tier 1 (UIA, `uia_actions.locate`) is deterministic, free, exact and covers
~90% of native UI. Tier 2 (here) is the VISION fallback for elementless targets
(canvas / custom-drawn / game UIs): send the screenshot to a vision model
(mimo-v2.5 by default) and ask where the target is.

We ask the model for FRACTIONAL coordinates (0..1) of the target center, then
map to virtual-desktop pixels — fractions are robust to any image downscaling
the API does. The engine resolves coordinates only; the brain still issues the
click via the pixels (L3) verbs.
"""

from __future__ import annotations

import base64
import json
import re
import struct
import urllib.error
import urllib.request
import zlib

from . import config as C
from . import uia_actions as L1

# We send near-full resolution: detail matters for precise grounding (downscaling
# to ~1024 measurably hurt hit-rate, and a full 1080p shot is only ~14s for
# mimo-v2.5). We only downscale genuinely huge virtual desktops. Fractional
# coordinates are resolution-independent and map back to full pixels either way.
_VISION_MAX_DIM = 4096

_PROMPT = (
    "You are a precise GUI grounding tool. Locate this target in the "
    "screenshot: {target}\n\n"
    "Reply with ONLY one JSON object, no prose: "
    '{{"found": true, "x": <0..1>, "y": <0..1>}} where x and y are the '
    "FRACTIONAL position of the CENTER of the target (x=0 left edge, x=1 right "
    "edge, y=0 top, y=1 bottom). If the target is not visible, reply "
    '{{"found": false}}.'
)


_CROP_FRAC = 0.35  # second-pass crop size as a fraction of each screen dimension


def _chunk(typ: bytes, data: bytes) -> bytes:
    c = typ + data
    return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)


def _rgb_to_png(rgb: bytes, w: int, h: int) -> bytes:
    """Encode raw row-major RGB bytes to a PNG (no Pillow)."""
    stride = w * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw))
            + _chunk(b"IEND", b""))


def _capture_raw():
    """Full virtual-desktop screenshot as (rgb_bytes, w, h, geom)."""
    import mss
    with mss.mss() as sct:
        mon = sct.monitors[0]
        shot = sct.grab(mon)
        rgb, (w, h) = bytes(shot.rgb), shot.size
        geom = {"left": mon["left"], "top": mon["top"], "width": w, "height": h}
    return rgb, w, h, geom


def _crop_rgb(rgb: bytes, W: int, x0: int, y0: int, x1: int, y1: int):
    stride = W * 3
    mv = memoryview(rgb)
    out = b"".join(mv[y * stride + x0 * 3:y * stride + x1 * 3] for y in range(y0, y1))
    return bytes(out), (x1 - x0), (y1 - y0)


def _downscale_rgb(rgb: bytes, w: int, h: int, max_dim: int):
    scale = min(1.0, max_dim / float(max(w, h)))
    if scale >= 1.0:
        return rgb, w, h
    sw, sh = max(1, int(w * scale)), max(1, int(h * scale))
    stride = w * 3
    xs = [int(x / scale) * 3 for x in range(sw)]
    mv = memoryview(rgb)
    out = bytearray()
    for y in range(sh):
        base = int(y / scale) * stride
        for xo in xs:
            out += mv[base + xo:base + xo + 3]
    return bytes(out), sw, sh


def _extract_json(text: str) -> dict | None:
    """Pull the last {...} object out of a (possibly reasoning) reply."""
    for cand in reversed(re.findall(r"\{[^{}]*\}", text or "", re.S)):
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def _mimo_locate(png: bytes, target: str, cfg) -> dict | None:
    """One vision call -> {fx, fy} fractional within the SENT image, or None."""
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    body = {
        "model": cfg.mimo_model, "max_tokens": 800, "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": _PROMPT.format(target=target)},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}],
    }
    req = urllib.request.Request(
        cfg.mimo_base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg.mimo_api_key},
        method="POST")
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
    except Exception:
        return None
    content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
    parsed = _extract_json(content)
    if not parsed or not parsed.get("found"):
        return None
    return {"fx": float(parsed.get("x", 0)), "fy": float(parsed.get("y", 0))}


def locate_vision(target: str, *, cfg=None) -> dict:
    """Tier 2 (crop-and-zoom): coarse-locate on the full screenshot, then crop
    that region and re-locate so the target fills more of the frame — much more
    precise than a single full-frame pass for LLM vision."""
    cfg = cfg or C.load()
    if not cfg.mimo_api_key:
        return {"found": False, "method": "vision",
                "error": "no mimo_api_key configured"}
    rgb, w, h, geom = _capture_raw()

    # pass 1 — coarse region on the (optionally downscaled) full image
    d_rgb, dw, dh = _downscale_rgb(rgb, w, h, _VISION_MAX_DIM)
    p1 = _mimo_locate(_rgb_to_png(d_rgb, dw, dh), target, cfg)
    if not p1:
        return {"found": False, "method": "vision", "raw": "coarse pass found nothing"}
    cx, cy = p1["fx"] * w, p1["fy"] * h

    # pass 2 — crop a window around the coarse point, re-locate at native res
    cw, ch = int(w * _CROP_FRAC), int(h * _CROP_FRAC)
    x0 = max(0, min(w - cw, int(cx - cw / 2)))
    y0 = max(0, min(h - ch, int(cy - ch / 2)))
    crop_rgb, cwx, chy = _crop_rgb(rgb, w, x0, y0, x0 + cw, y0 + ch)
    p2 = _mimo_locate(_rgb_to_png(crop_rgb, cwx, chy), target, cfg)

    if p2:
        fx_px, fy_px, refined = x0 + p2["fx"] * cwx, y0 + p2["fy"] * chy, True
    else:
        fx_px, fy_px, refined = cx, cy, False
    return {"found": True, "method": "vision-cz",
            "x": int(round(geom["left"] + fx_px)),
            "y": int(round(geom["top"] + fy_px)),
            "coarse": [round(p1["fx"], 3), round(p1["fy"], 3)],
            "refined": refined, "target": target}


def locate(*, name: str = "", query: str = "", control_type: str = "",
           window_title: str = "", allow_vision: bool = True, cfg=None) -> dict:
    """Tiered grounding: UIA first (free, exact), vision fallback."""
    target = query or name
    uia_res = L1.locate(name=name, query=query, control_type=control_type,
                        window_title=window_title)
    if uia_res.get("found"):
        return uia_res
    if allow_vision and target:
        return locate_vision(target, cfg=cfg)
    return uia_res
