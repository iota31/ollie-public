r"""Grounding eval harness — the bake-off scaffold (plan D5 / Track C2).

Measures how well a grounder turns a human target label into the right pixel
coordinates. Runs a corpus of (app, target-label) cases against the live engine
and reports per-grounder hit-rate, where a "hit" = the produced (x,y) lands
inside the target element's UIA bounding rect (the ground truth).

Grounders are pluggable so cloud-vision / self-hosted UI-TARS / OmniParser can
be dropped in later and compared on the SAME corpus. Today only the UIA tier is
wired (it is also used to derive ground truth).

    python3 scripts/grounding-eval.py <bearer>            # all cases
    python3 scripts/grounding-eval.py <bearer> uia        # one grounder

Run from anywhere that can reach the engine (e.g. the WSL gateway):
    wsl ... python3 /path/grounding-eval.py $BEARER
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://<TAILSCALE_IP>:3200/mcp"

# Corpus: each case launches `app` (idempotent) then asks for `label`.
# Calculator is the safe, deterministic seed; add apps/labels over time.
CORPUS = [
    {"app": "calc", "launch": "Start-Process calc", "window": "Calculator",
     "labels": ["Seven", "Plus", "Three", "Equals", "Clear", "Percent",
                "Backspace", "Memory store"]},
]


class Engine:
    def __init__(self, token: str) -> None:
        self.hdr = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.sid = None
        self._tid = 1
        self.sid, _ = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "eval", "version": "1"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized",
                    "params": {}})

    def _post(self, body, timeout=40):
        h = dict(self.hdr)
        if self.sid:
            h["mcp-session-id"] = self.sid
        req = urllib.request.Request(BASE, data=json.dumps(body).encode(),
                                     headers=h, method="POST")
        r = urllib.request.urlopen(req, timeout=timeout)
        out = None
        for line in r.read().decode().splitlines():
            if line.startswith("data: "):
                out = json.loads(line[6:])
        return r.headers.get("mcp-session-id"), out

    def act(self, args: dict) -> dict:
        self._tid += 1
        _, res = self._post({"jsonrpc": "2.0", "id": self._tid,
                             "method": "tools/call",
                             "params": {"name": "act", "arguments": args}})
        c = res.get("result", {}).get("content", [])
        txt = next((x["text"] for x in c if x.get("type") == "text"), "{}")
        try:
            return json.loads(txt)
        except Exception:
            return {"raw": txt[:120]}


def _in_rect(x: int, y: int, rect: list) -> bool:
    return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


# --- grounders: label -> {x, y} (or None). Pluggable for the bake-off. ------

def ground_uia(eng: Engine, window: str, label: str):
    g = eng.act({"kind": "uia", "op": "locate", "name": label,
                 "window_title": window}).get("result", {})
    if g.get("found"):
        return {"x": g["x"], "y": g["y"]}, g["best"]["rect"]
    return None, None


def ground_vision(eng: Engine, window: str, label: str):
    # force the vision tier (mimo-v2.5). Slow (~10-15s/target) — it's a bench.
    g = eng.act({"kind": "uia", "op": "locate_vision",
                 "value": f"the {label} button in the {window} app"},
                ).get("result", {})
    if g.get("found"):
        return {"x": g["x"], "y": g["y"]}, None
    return None, None


GROUNDERS = {"uia": ground_uia, "vision": ground_vision}


def main() -> int:
    token = sys.argv[1]
    only = sys.argv[2] if len(sys.argv) > 2 else None
    eng = Engine(token)

    for case in CORPUS:
        eng.act({"kind": "shell", "command": case["launch"]})
        time.sleep(3)
        eng.act({"kind": "window", "op": "focus", "title": case["window"]})
        time.sleep(0.4)

        for gname, gfn in GROUNDERS.items():
            if only and gname != only:
                continue
            hits = total = 0
            print(f"\n== grounder '{gname}' on {case['app']} ==")
            for label in case["labels"]:
                total += 1
                # ground truth rect always from UIA
                _, truth_rect = ground_uia(eng, case["window"], label)
                coords, _ = gfn(eng, case["window"], label)
                if coords is None or truth_rect is None:
                    print(f"  MISS {label:14} (not located)")
                    continue
                hit = _in_rect(coords["x"], coords["y"], truth_rect)
                hits += hit
                print(f"  {'HIT ' if hit else 'OFF '} {label:14} "
                      f"({coords['x']},{coords['y']})")
            rate = (hits / total * 100) if total else 0
            print(f"  -> {gname}: {hits}/{total} = {rate:.0f}% single-shot")

        eng.act({"kind": "shell",
                 "command": "Get-Process *alculator* -ErrorAction "
                            "SilentlyContinue | Stop-Process -Force"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
